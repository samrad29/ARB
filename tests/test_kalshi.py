from __future__ import annotations

from prediction_arb.exchanges.kalshi import (
    compose_kalshi_title,
    match_series_ticker,
    normalize_kalshi_market,
    normalize_kalshi_orderbook,
)


KALSHI_MARKET = {
    "ticker": "KXRATECUT-26SEP-T25",
    "event_ticker": "KXRATECUT-26SEP",
    "market_type": "binary",
    "title": "Will the Fed cut rates at the September meeting?",
    "yes_sub_title": "25 bps cut",
    "no_sub_title": "No 25 bps cut",
    "created_time": "2026-01-01T00:00:00Z",
    "updated_time": "2026-09-01T00:00:00Z",
    "open_time": "2026-01-01T00:00:00Z",
    "close_time": "2026-09-17T18:00:00Z",
    "latest_expiration_time": "2026-09-17T20:00:00Z",
    "status": "active",
    "yes_bid_dollars": "0.4600",
    "yes_ask_dollars": "0.4700",
    "no_bid_dollars": "0.5300",
    "no_ask_dollars": "0.5400",
    "yes_bid_size_fp": "1200.00",
    "yes_ask_size_fp": "850.00",
    "last_price_dollars": "0.4800",
    "volume_fp": "10000.00",
    "open_interest_fp": "4000.00",
    "rules_primary": "Resolves YES if the FOMC announces a 25 bps cut at the September meeting.",
    "rules_secondary": "Source: FOMC statement.",
}


def test_normalize_kalshi_market_uses_cents_and_sizes() -> None:
    market = normalize_kalshi_market(KALSHI_MARKET, {"title": "Fed September", "category": "Economics"})
    assert market.exchange == "kalshi"
    assert market.exchange_market_id == "KXRATECUT-26SEP-T25"
    assert market.status == "open"
    assert market.yes_bid == 46
    assert market.yes_ask == 47
    assert market.no_bid == 53
    assert market.no_ask == 54
    assert market.yes_bid_size == 1200
    assert market.yes_ask_size == 850
    assert market.no_ask_size == 1200
    assert market.no_bid_size == 850
    assert market.last_price == 48
    assert market.volume == 10000
    assert "FOMC" in (market.resolution_source or "")
    assert market.raw_data["ticker"] == "KXRATECUT-26SEP-T25"


def test_normalize_kalshi_orderbook_complements_asks() -> None:
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", "10.00"], ["0.4600", "25.00"]],
            "no_dollars": [["0.5000", "8.00"], ["0.5300", "40.00"]],
        }
    }
    book = normalize_kalshi_orderbook("KXRATECUT-26SEP-T25", payload)
    assert book.best_price("yes", "bid") == 46
    assert book.best_size("yes", "bid") == 25
    assert book.best_price("no", "bid") == 53
    assert book.best_price("yes", "ask") == 47  # 100 - 53
    assert book.best_size("yes", "ask") == 40
    assert book.best_price("no", "ask") == 54  # 100 - 46
    assert book.best_size("no", "ask") == 25


def test_normalize_kalshi_attaches_series_and_event_metadata() -> None:
    market = normalize_kalshi_market(
        KALSHI_MARKET,
        {"title": "Fed September", "category": "Economics", "series_ticker": "KXRATECUT"},
        {
            "ticker": "KXRATECUT",
            "title": "Fed Funds Rate",
            "category": "Economics",
            "tags": ["Fed", "Rates"],
            "settlement_sources": [{"name": "FOMC"}],
        },
    )
    assert market.series_ticker == "KXRATECUT"
    assert market.series_title == "Fed Funds Rate"
    assert market.event_title == "Fed September"
    assert "Fed Funds Rate" in market.title
    assert "25 bps cut" in market.title
    assert "Fed" in market.tags


def test_compose_and_match_series_ticker() -> None:
    assert compose_kalshi_title("Oscars", "Best Director 2027", "James Ashcroft", "T") == (
        "Oscars: Best Director 2027: James Ashcroft"
    )
    catalog = {"KXRATECUT": {}, "KXRATECUTLONG": {}}
    assert match_series_ticker("KXRATECUT-26SEP", catalog) == "KXRATECUT"
    assert match_series_ticker("KXRATECUTLONG-26SEP", catalog) == "KXRATECUTLONG"
