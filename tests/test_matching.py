from __future__ import annotations

from prediction_arb.matching.market_matcher import MarketMatcher, extract_fields, score_pair
from prediction_arb.models.market import Market


def _market(exchange: str, market_id: str, title: str, **kwargs) -> Market:
    return Market(
        exchange=exchange,
        exchange_market_id=market_id,
        ticker=market_id,
        title=title,
        status="open",
        **kwargs,
    )


def test_similar_titles_different_settlement_are_not_equivalent() -> None:
    electoral = _market(
        "kalshi",
        "k1",
        "Will Trump win the 2024 presidential election?",
        description="Resolves YES based on the Electoral College result certified by Congress.",
        resolution_source="Electoral College",
    )
    popular = _market(
        "polymarket",
        "p1",
        "Will Trump win the 2024 presidential election?",
        description="Resolves YES if Trump wins the national popular vote.",
        resolution_source="Popular vote",
    )
    decision = score_pair(extract_fields(electoral), extract_fields(popular))
    assert decision.match_type not in {"EXACT", "LIKELY_EQUIVALENT"}
    assert any("settlement" in item.lower() or "resolution" in item.lower() for item in decision.contradictions)


def test_threshold_conflict_is_not_equivalent() -> None:
    cut_25 = _market(
        "kalshi",
        "k2",
        "Will the Fed cut rates by 25 bps in September?",
        description="YES if the FOMC cuts 25 bps at the September meeting.",
    )
    cut_50 = _market(
        "polymarket",
        "p2",
        "Will the Fed cut rates by 50 bps in September?",
        description="YES if the FOMC cuts 50 bps at the September meeting.",
    )
    decision = score_pair(extract_fields(cut_25), extract_fields(cut_50))
    assert decision.match_type not in {"EXACT", "LIKELY_EQUIVALENT"}
    assert any("threshold" in item.lower() for item in decision.contradictions)


def test_high_overlap_fed_titles_are_candidates_but_unverified() -> None:
    kalshi = _market(
        "kalshi",
        "k3",
        "Will the Fed cut rates at the September 2026 meeting?",
        description="Resolves on the FOMC statement.",
        category="Economics",
    )
    poly = _market(
        "polymarket",
        "p3",
        "Will the Fed cut rates at the September 2026 meeting?",
        description="Resolves on the FOMC statement.",
        category="Economics",
    )
    matcher = MarketMatcher(candidate_min_score=0.4, high_confidence_min_score=0.82)
    matches = matcher.match([kalshi, poly])
    assert matches
    assert matches[0].verified is False
    assert matches[0].match_score >= 0.55
    assert "FOMC" in matches[0].reason or "event" in matches[0].reason.lower()
