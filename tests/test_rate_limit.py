from __future__ import annotations

import time

from prediction_arb.rate_limit import (
    SlidingWindowSpec,
    TokenBucketSpec,
    _SlidingWindow,
    _TokenBucket,
    classify_request,
)


def test_classify_documented_endpoints() -> None:
    assert classify_request("https://external-api.kalshi.com/trade-api/v2/markets") == (
        "kalshi",
        "markets",
    )
    assert classify_request("https://external-api.kalshi.com/trade-api/v2/markets/ABC/orderbook") == (
        "kalshi",
        "orderbook",
    )
    assert classify_request("https://gamma-api.polymarket.com/markets/keyset") == (
        "polymarket",
        "markets",
    )
    assert classify_request("https://clob.polymarket.com/books") == ("polymarket", "books")
    assert classify_request("https://clob.polymarket.com/book") == ("polymarket", "book")


async def test_sliding_window_blocks_after_documented_count() -> None:
    window = _SlidingWindow(SlidingWindowSpec(max_requests=3, window_seconds=1.0))
    started = time.monotonic()
    await window.acquire()
    await window.acquire()
    await window.acquire()
    await window.acquire()
    elapsed = time.monotonic() - started
    assert elapsed >= 0.9


async def test_token_bucket_respects_cost() -> None:
    bucket = _TokenBucket(TokenBucketSpec(refill_per_second=100, capacity=20, default_cost=10))
    await bucket.acquire()
    await bucket.acquire()
    started = time.monotonic()
    await bucket.acquire()
    assert time.monotonic() - started >= 0.05


async def test_429_cooldown_pauses_window() -> None:
    window = _SlidingWindow(SlidingWindowSpec(max_requests=100, window_seconds=10))
    await window.cooldown(0.2)
    started = time.monotonic()
    await window.acquire()
    assert time.monotonic() - started >= 0.15
