from __future__ import annotations

from prediction_arb.exchanges.polymarket import normalize_clob_book, normalize_polymarket_market


POLYMARKET_MARKET = {
    "id": "12345",
    "slug": "will-the-fed-cut-rates-in-september",
    "question": "Will the Fed lower interest rates in September?",
    "description": "Resolves YES if the FOMC cuts the federal funds rate in September.",
    "closed": False,
    "active": True,
    "clobTokenIds": '["yes-token","no-token"]',
    "outcomes": '["Yes","No"]',
    "outcomePrices": '["0.48","0.52"]',
    "endDate": "2026-09-30T00:00:00Z",
    "volumeNum": 1234.9,
    "liquidityNum": 500.4,
    "category": "Economics",
}


def test_normalize_polymarket_market() -> None:
    market = normalize_polymarket_market(POLYMARKET_MARKET)
    assert market is not None
    assert market.exchange == "polymarket"
    assert market.exchange_market_id == "12345"
    assert market.status == "open"
    assert market.yes_token_id == "yes-token"
    assert market.no_token_id == "no-token"
    assert market.last_price == 48
    assert market.volume == 1234
    assert market.yes_ask is None  # last/mid prices are not executable asks
    assert "FOMC" in (market.description or "")


def test_normalize_clob_book_best_is_top_of_book() -> None:
    yes_book = {
        "bids": [{"price": "0.44", "size": "10"}, {"price": "0.46", "size": "30"}],
        "asks": [{"price": "0.49", "size": "5"}, {"price": "0.47", "size": "20"}],
    }
    no_book = {
        "bids": [{"price": "0.50", "size": "12"}],
        "asks": [{"price": "0.52", "size": "8"}],
    }
    book = normalize_clob_book("12345", yes_book, no_book)
    assert book.best_price("yes", "bid") == 46
    assert book.best_price("yes", "ask") == 47
    assert book.best_size("yes", "ask") == 20
    assert book.best_price("no", "ask") == 52
    assert book.best_size("no", "ask") == 8
