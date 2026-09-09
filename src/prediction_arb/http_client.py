from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from urllib.parse import urlparse

import httpx

from prediction_arb.logging_utils import log_event
from prediction_arb.metrics import MetricsRegistry
from prediction_arb.rate_limit import RateLimiter, classify_request

logger = logging.getLogger("prediction_arb.http")


class HttpClient:
    """Shared async HTTP client with documented rate limits, 429 handling, and metrics."""

    def __init__(
        self,
        timeout_seconds: float = 20.0,
        max_retries: int = 5,
        user_agent: str = "prediction-arb/0.1 (research scanner; no trading)",
        limiter: RateLimiter | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.limiter = limiter
        self.metrics = metrics
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        exchange: str | None = None,
        endpoint: str | None = None,
    ) -> httpx.Response:
        inferred_exchange, inferred_endpoint = classify_request(url)
        exchange = exchange or inferred_exchange
        endpoint = endpoint or inferred_endpoint
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            if self.limiter and exchange != "unknown":
                await self.limiter.acquire(exchange, endpoint)
            started = asyncio.get_running_loop().time()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                delay = _retry_delay(attempt, None)
                log_event(
                    logger,
                    logging.WARNING,
                    "http_transport_error",
                    url=_safe_url(url),
                    error=type(exc).__name__,
                    attempt=attempt,
                    sleep_seconds=round(delay, 3),
                    exchange=exchange,
                    endpoint=endpoint,
                )
                await asyncio.sleep(delay)
                continue

            latency = asyncio.get_running_loop().time() - started
            if self.metrics:
                self.metrics.record_http(exchange, endpoint, latency, response.status_code)

            if response.status_code == 429:
                retry_after = _retry_after_seconds(response)
                log_event(
                    logger,
                    logging.WARNING,
                    "rate_limited",
                    url=_safe_url(url),
                    status=429,
                    attempt=attempt,
                    retry_after=retry_after,
                    exchange=exchange,
                    endpoint=endpoint,
                )
                if self.limiter:
                    await self.limiter.notify_429(exchange, endpoint, retry_after)
                else:
                    await asyncio.sleep(_retry_delay(attempt, response))
                continue

            if self.limiter:
                self.limiter.clear_429(exchange, endpoint)

            if response.status_code >= 500:
                delay = _retry_delay(attempt, response)
                log_event(
                    logger,
                    logging.WARNING,
                    "server_error",
                    url=_safe_url(url),
                    status=response.status_code,
                    attempt=attempt,
                    sleep_seconds=round(delay, 3),
                    exchange=exchange,
                    endpoint=endpoint,
                )
                await asyncio.sleep(delay)
                continue
            return response
        if last_error:
            raise last_error
        raise httpx.HTTPError(f"request failed after {self.max_retries} retries: {url}")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()

    async def post_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("POST", url, **kwargs)
        response.raise_for_status()
        if not response.content:
            return None
        return response.json()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return min(float(retry_after), 30.0)
    except ValueError:
        return None


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    if response is not None:
        retry_after = _retry_after_seconds(response)
        if retry_after is not None:
            return retry_after
    base = min(2 ** (attempt - 1), 16)
    return base + random.random()


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
