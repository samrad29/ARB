from __future__ import annotations

from datetime import datetime, timezone

from prediction_arb.matching.candidates import CandidateGenerator
from prediction_arb.matching.market_matcher import MarketMatcher
from prediction_arb.matching.taxonomy import classify_market
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


SEP_2026 = datetime(2026, 9, 17, tzinfo=timezone.utc)


def test_short_kalshi_fed_title_is_a_candidate_despite_lexical_miss() -> None:
    kalshi = _market(
        "kalshi",
        "k-fed",
        "25 bps cut",
        series_ticker="KXRATECUT",
        series_title="Fed Funds Rate",
        event_title="Fed decision September 2026",
        category="Economics",
        tags=["Fed", "Interest Rates"],
        close_time=SEP_2026,
    )
    poly = _market(
        "polymarket",
        "p-fed",
        "Will the Fed lower interest rates in September 2026?",
        tags=["Finance", "Fed"],
        close_time=SEP_2026,
        description="Resolves on the FOMC statement.",
    )
    matcher = MarketMatcher(candidate_min_score=0.55, high_confidence_min_score=0.82)
    assert matcher.match([kalshi, poly]) == []

    pairs, stats = CandidateGenerator().generate([kalshi, poly])
    assert pairs, "obvious Fed equivalent should be surfaced as a candidate"
    assert stats.generated >= 1
    assert any(pair.kalshi_market.exchange_market_id == "k-fed" for pair in pairs)
    reasons = " ".join(pairs[0].reasons).lower()
    assert "fed" in reasons or "federal" in reasons or "same topic" in reasons
    assert "FED" in kalshi.canonical_topics
    assert "FED" in poly.canonical_topics


def test_oscar_person_entity_becomes_a_candidate() -> None:
    kalshi = _market(
        "kalshi",
        "k-oscar",
        "James Ashcroft",
        series_title="Oscars",
        event_title="2027 Oscar Best Director",
        tags=["Entertainment"],
        close_time=datetime(2027, 3, 15, tzinfo=timezone.utc),
    )
    poly = _market(
        "polymarket",
        "p-oscar",
        "Will James Ashcroft win Best Director at the 2027 Oscars?",
        tags=["Awards"],
        close_time=datetime(2027, 3, 15, tzinfo=timezone.utc),
    )
    matcher = MarketMatcher()
    lexical_matches = matcher.match([kalshi, poly])
    # Person-name overlap can still produce a weak lexical hit; the new
    # layer must keep the pair even when we rely on Oscars metadata.
    assert lexical_matches == [] or lexical_matches[0].match_score < 0.90

    pairs, _stats = CandidateGenerator().generate([kalshi, poly])
    assert pairs
    entity_values = {entity.value for entity in kalshi.entities} & {entity.value for entity in poly.entities}
    assert "james_ashcroft" in entity_values
    assert "shared_entity" in pairs[0].signals or "same_series_event" in pairs[0].signals


def test_shib_crypto_ladder_is_a_candidate_via_entity() -> None:
    kalshi = _market(
        "kalshi",
        "k-shib",
        "Above strike",
        series_title="Shiba Inu price",
        event_title="SHIB daily target",
        tags=["Crypto"],
        close_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    poly = _market(
        "polymarket",
        "p-shib",
        "Will SHIB print a new all-time high this month?",
        tags=["Crypto"],
        close_time=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert MarketMatcher().match([kalshi, poly]) == []
    pairs, _stats = CandidateGenerator().generate([kalshi, poly])
    assert pairs
    assert "CRYPTO" in kalshi.canonical_topics
    assert any(entity.value == "shiba_inu" for entity in kalshi.entities)
    assert any(entity.value == "shiba_inu" for entity in poly.entities)


def test_greenland_series_metadata_surfaces_a_lexical_miss() -> None:
    kalshi = _market(
        "kalshi",
        "k-greenland",
        "No Acquisition",
        series_title="Greenland",
        event_title="US Greenland purchase",
        tags=["Politics"],
        close_time=datetime(2029, 1, 21, tzinfo=timezone.utc),
    )
    poly = _market(
        "polymarket",
        "p-greenland",
        "Will the United States acquire Greenland by January 2029?",
        tags=["Geopolitics"],
        close_time=datetime(2029, 1, 21, tzinfo=timezone.utc),
    )
    assert MarketMatcher().match([kalshi, poly]) == []
    pairs, _stats = CandidateGenerator().generate([kalshi, poly])
    assert pairs
    assert any(entity.value == "greenland" for entity in kalshi.entities)
    assert any(entity.value == "greenland" for entity in poly.entities)


def test_incompatible_dates_are_not_candidates() -> None:
    kalshi = _market(
        "kalshi",
        "k-date",
        "Fed cut",
        series_title="Fed Funds Rate",
        close_time=datetime(2024, 9, 18, tzinfo=timezone.utc),
    )
    poly = _market(
        "polymarket",
        "p-date",
        "Will the Fed cut rates in September 2026?",
        close_time=datetime(2026, 9, 17, tzinfo=timezone.utc),
    )
    pairs, stats = CandidateGenerator(date_tolerance_days=90).generate([kalshi, poly])
    assert pairs == []
    assert stats.rejected_incompatible_dates >= 1


def test_unrelated_markets_are_not_candidates() -> None:
    kalshi = _market(
        "kalshi",
        "k-weather",
        "High temperature in NYC",
        series_title="NYC Weather",
        tags=["Weather"],
        close_time=SEP_2026,
    )
    poly = _market(
        "polymarket",
        "p-election",
        "Will the Seahawks visit the White House in 2026?",
        tags=["Sports"],
        close_time=SEP_2026,
    )
    pairs, _stats = CandidateGenerator().generate([kalshi, poly])
    assert pairs == []


def test_conservative_matcher_still_rejects_settlement_conflict() -> None:
    kalshi = _market(
        "kalshi",
        "k1",
        "Will Trump win the 2024 presidential election?",
        description="Resolves YES based on the Electoral College result certified by Congress.",
        resolution_source="Electoral College",
        series_title="US Presidential Election",
        close_time=datetime(2024, 11, 5, tzinfo=timezone.utc),
    )
    poly = _market(
        "polymarket",
        "p1",
        "Will Trump win the 2024 presidential election?",
        description="Resolves YES if Trump wins the national popular vote.",
        resolution_source="Popular vote",
        close_time=datetime(2024, 11, 5, tzinfo=timezone.utc),
    )
    pairs, _stats = CandidateGenerator().generate([kalshi, poly])
    assert pairs, "candidate generation may keep the pair"
    scored = MarketMatcher().classify_pairs(
        [(pair.kalshi_market, pair.polymarket_market) for pair in pairs]
    )
    assert scored
    assert scored[0].match_type not in {"EXACT", "LIKELY_EQUIVALENT"}


def test_classify_does_not_change_high_confidence_thresholds() -> None:
    kalshi = _market(
        "kalshi",
        "k3",
        "Will the Fed cut rates at the September 2026 meeting?",
        description="Resolves on the FOMC statement.",
        category="Economics",
        close_time=SEP_2026,
    )
    poly = _market(
        "polymarket",
        "p3",
        "Will the Fed cut rates at the September 2026 meeting?",
        description="Resolves on the FOMC statement.",
        category="Economics",
        close_time=SEP_2026,
    )
    matcher = MarketMatcher(candidate_min_score=0.4, high_confidence_min_score=0.82)
    direct = matcher.match([kalshi, poly])
    via_candidates = matcher.classify_pairs([(kalshi, poly)])
    assert direct
    assert via_candidates
    assert via_candidates[0].verified is False
    assert via_candidates[0].match_score >= 0.55
    assert abs(direct[0].match_score - via_candidates[0].match_score) < 1e-9


def test_btc_alias_classifies_as_bitcoin() -> None:
    market = _market("kalshi", "btc1", "BTC above 100k", tags=["Crypto"])
    classify_market(market)
    assert "CRYPTO" in market.canonical_topics
    assert any(entity.value == "bitcoin" for entity in market.entities)
