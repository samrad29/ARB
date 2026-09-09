"""Exchange-specific rate limiting from official published docs.

Kalshi (authenticated Basic tier, documented token bucket):
  https://docs.kalshi.com/getting_started/rate_limits
  Read budget 200 tokens/sec, default cost 10, Basic Read capacity = 2 seconds.

Polymarket (Cloudflare sliding 10s windows):
  https://docs.polymarket.com/api-reference/rate-limits
  General 15,000/10s; Gamma /markets 300/10s; CLOB /book 1,500/10s;
  CLOB /books 500/10s.

Unauthenticated Kalshi public traffic is not given a separate published table.
We use the documented Basic read bucket as the configured ceiling and still
honor HTTP 429 + Retry-After. We do not invent tighter unofficial limits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

from prediction_arb.logging_utils import log_event
from prediction_arb.metrics import MetricsRegistry

logger = logging.getLogger("prediction_arb.rate_limit")


@dataclass(frozen=True)
class SlidingWindowSpec:
    max_requests: int
    window_seconds: float


@dataclass(frozen=True)
class TokenBucketSpec:
    refill_per_second: float
    capacity: float
    default_cost: float


# Documented Kalshi Basic Predictions Read bucket.
KALSHI_READ = TokenBucketSpec(refill_per_second=200.0, capacity=400.0, default_cost=10.0)

# Documented Polymarket Cloudflare windows.
POLYMARKET_WINDOWS: dict[str, SlidingWindowSpec] = {
    "general": SlidingWindowSpec(15_000, 10.0),
    "gamma": SlidingWindowSpec(4_000, 10.0),
    "markets": SlidingWindowSpec(300, 10.0),
    "events": SlidingWindowSpec(500, 10.0),
    "clob": SlidingWindowSpec(9_000, 10.0),
    "book": SlidingWindowSpec(1_500, 10.0),
    "books": SlidingWindowSpec(500, 10.0),
}


class _TokenBucket:
    def __init__(self, spec: TokenBucketSpec) -> None:
        self.spec = spec
        self.tokens = spec.capacity
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated_at
        if elapsed > 0:
            self.tokens = min(self.spec.capacity, self.tokens + elapsed * self.spec.refill_per_second)
            self.updated_at = now

    async def acquire(self, cost: float | None = None) -> None:
        cost = self.spec.default_cost if cost is None else cost
        while True:
            async with self._lock:
                self._refill()
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                wait = (cost - self.tokens) / self.spec.refill_per_second if self.spec.refill_per_second else 0.05
            await asyncio.sleep(max(wait, 0.01))

    async def cooldown(self, seconds: float) -> None:
        async with self._lock:
            self.tokens = 0
            self.updated_at = time.monotonic()
            _ = seconds


class _SlidingWindow:
    def __init__(self, spec: SlidingWindowSpec) -> None:
        self.spec = spec
        self.events: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.cooldown_until = 0.0

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self.cooldown_until:
                    wait = self.cooldown_until - now
                else:
                    cutoff = now - self.spec.window_seconds
                    while self.events and self.events[0] <= cutoff:
                        self.events.popleft()
                    if len(self.events) < self.spec.max_requests:
                        self.events.append(now)
                        return
                    wait = self.events[0] + self.spec.window_seconds - now
            await asyncio.sleep(max(wait, 0.01))

    async def cooldown(self, seconds: float) -> None:
        async with self._lock:
            self.cooldown_until = max(self.cooldown_until, time.monotonic() + seconds)


class RateLimiter:
    """Per-exchange limiter. Acquire before each HTTP call."""

    def __init__(self, metrics: MetricsRegistry | None = None) -> None:
        self.metrics = metrics
        self._kalshi_read = _TokenBucket(KALSHI_READ)
        self._poly: dict[str, _SlidingWindow] = {
            name: _SlidingWindow(spec) for name, spec in POLYMARKET_WINDOWS.items()
        }
        self._429_streak: dict[str, int] = {}

    async def acquire(self, exchange: str, endpoint: str) -> None:
        exchange = (exchange or "unknown").lower()
        endpoint = (endpoint or "general").lower()
        if exchange == "kalshi":
            await self._kalshi_read.acquire()
            return
        if exchange == "polymarket":
            await self._poly["general"].acquire()
            specific = endpoint if endpoint in self._poly else _poly_bucket(endpoint)
            if specific != "general":
                await self._poly[specific].acquire()
            return
        # Unknown exchange: do not invent a limit.

    async def notify_429(self, exchange: str, endpoint: str, retry_after: float | None) -> None:
        key = f"{exchange}:{endpoint}"
        streak = self._429_streak.get(key, 0) + 1
        self._429_streak[key] = streak
        # Exponential pause to avoid retry storms. Honor Retry-After when present.
        backoff = min(2 ** min(streak, 5), 16)
        delay = retry_after if retry_after and retry_after > 0 else backoff
        log_event(
            logger,
            logging.WARNING,
            "rate_limiter_cooldown",
            exchange=exchange,
            endpoint=endpoint,
            sleep_seconds=round(delay, 3),
            streak=streak,
        )
        if exchange == "kalshi":
            await self._kalshi_read.cooldown(delay)
        elif exchange == "polymarket":
            await self._poly["general"].cooldown(delay)
            specific = endpoint if endpoint in self._poly else _poly_bucket(endpoint)
            if specific in self._poly:
                await self._poly[specific].cooldown(delay)
        await asyncio.sleep(delay)

    def clear_429(self, exchange: str, endpoint: str) -> None:
        self._429_streak.pop(f"{exchange}:{endpoint}", None)


def classify_request(url: str) -> tuple[str, str]:
    """Map a request URL onto (exchange, endpoint) using documented surfaces."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if "kalshi.com" in host or "kalshi.co" in host:
        if "/markets/orderbooks" in path:
            return "kalshi", "orderbooks"
        if path.endswith("/orderbook") or "/orderbook" in path:
            return "kalshi", "orderbook"
        if "/markets/trades" in path:
            return "kalshi", "trades"
        if "/events" in path:
            return "kalshi", "events"
        if "/markets" in path:
            return "kalshi", "markets"
        return "kalshi", "general"
    if "gamma-api.polymarket.com" in host:
        if "/events" in path:
            return "polymarket", "events"
        if "/markets" in path:
            return "polymarket", "markets"
        return "polymarket", "gamma"
    if "clob.polymarket.com" in host:
        trimmed = path.rstrip("/")
        if trimmed.endswith("/books"):
            return "polymarket", "books"
        if trimmed.endswith("/book"):
            return "polymarket", "book"
        return "polymarket", "clob"
    if "polymarket.com" in host:
        return "polymarket", "general"
    return "unknown", "general"


def _poly_bucket(endpoint: str) -> str:
    if endpoint in POLYMARKET_WINDOWS:
        return endpoint
    if endpoint in {"market", "keyset"}:
        return "markets"
    return "gamma"
