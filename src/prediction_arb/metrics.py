from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class ExchangeMetrics:
    requests: int = 0
    http_429: int = 0
    errors: int = 0
    latency_ms_sum: float = 0.0
    latency_count: int = 0
    by_endpoint: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    recent: deque[float] = field(default_factory=deque)

    def requests_per_minute(self, now: float | None = None) -> float:
        now = now or time.monotonic()
        cutoff = now - 60
        while self.recent and self.recent[0] < cutoff:
            self.recent.popleft()
        return float(len(self.recent))

    def avg_latency_ms(self) -> float:
        if not self.latency_count:
            return 0.0
        return self.latency_ms_sum / self.latency_count


class MetricsRegistry:
    """In-process collector metrics. Safe for the asyncio thread plus CLI snapshots."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.exchanges: dict[str, ExchangeMetrics] = defaultdict(ExchangeMetrics)
        self.websocket_connections = 0
        self.websocket_reconnects = 0
        self.markets_discovered = 0
        self.markets_monitored = 0
        self.watchlist_high = 0
        self.watchlist_candidate = 0
        self.started_at = time.time()

    def record_http(
        self,
        exchange: str,
        endpoint: str,
        latency_seconds: float,
        status_code: int,
    ) -> None:
        with self._lock:
            row = self.exchanges[exchange or "unknown"]
            row.requests += 1
            row.by_endpoint[endpoint or "general"] += 1
            row.recent.append(time.monotonic())
            if latency_seconds >= 0:
                row.latency_ms_sum += latency_seconds * 1000
                row.latency_count += 1
            if status_code == 429:
                row.http_429 += 1
            if status_code >= 500:
                row.errors += 1

    def set_watchlist(self, high: int, candidate: int) -> None:
        with self._lock:
            self.watchlist_high = high
            self.watchlist_candidate = candidate
            self.markets_monitored = high + candidate

    def set_discovered(self, count: int) -> None:
        with self._lock:
            self.markets_discovered = count

    def add_ws_connection(self, delta: int = 1) -> None:
        with self._lock:
            self.websocket_connections = max(0, self.websocket_connections + delta)

    def add_ws_reconnect(self) -> None:
        with self._lock:
            self.websocket_reconnects += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            exchanges = {}
            for name, row in self.exchanges.items():
                exchanges[name] = {
                    "requests": row.requests,
                    "requests_per_minute": round(row.requests_per_minute(now), 2),
                    "http_429": row.http_429,
                    "errors": row.errors,
                    "avg_latency_ms": round(row.avg_latency_ms(), 2),
                    "by_endpoint": dict(row.by_endpoint),
                }
            return {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "exchanges": exchanges,
                "markets_discovered": self.markets_discovered,
                "markets_monitored": self.markets_monitored,
                "watchlist_high": self.watchlist_high,
                "watchlist_candidate": self.watchlist_candidate,
                "websocket_connections": self.websocket_connections,
                "websocket_reconnects": self.websocket_reconnects,
            }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")


def format_stats(payload: dict[str, Any]) -> str:
    lines = [
        "API STATISTICS",
        "------------------------------------------------",
    ]
    exchanges = payload.get("exchanges") or {}
    for name in sorted(exchanges):
        row = exchanges[name]
        lines.extend(
            [
                name.capitalize(),
                f"  Requests/min:     {row.get('requests_per_minute', 0):>6}",
                f"  429 responses:    {row.get('http_429', 0):>6}",
                f"  Avg latency:      {row.get('avg_latency_ms', 0):>5.0f}ms",
                "",
            ]
        )
    if not exchanges:
        lines.append("(no HTTP traffic recorded yet)")
        lines.append("")
    lines.extend(
        [
            f"Active monitored markets: {payload.get('markets_monitored', 0)}",
            f"  High-interest:          {payload.get('watchlist_high', 0)}",
            f"  Candidate:              {payload.get('watchlist_candidate', 0)}",
            f"Markets discovered:       {payload.get('markets_discovered', 0)}",
            f"WebSocket connections:    {payload.get('websocket_connections', 0)}",
            f"WebSocket reconnects:     {payload.get('websocket_reconnects', 0)}",
        ]
    )
    if payload.get("recorded_at"):
        lines.append(f"As of:                    {payload['recorded_at']}")
    return "\n".join(lines)
