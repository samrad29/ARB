from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher

from prediction_arb.matching.market_matcher import STOPWORDS, extract_fields
from prediction_arb.matching.taxonomy import (
    BROAD_TOPICS,
    NARROW_TOPICS,
    classify_market,
    entity_key,
    normalize_settlement_sources,
)
from prediction_arb.models.market import Market

SIGNAL_SAME_TOPIC = "same_topic"
SIGNAL_SHARED_ENTITY = "shared_entity"
SIGNAL_DATE_OVERLAP = "date_overlap"
SIGNAL_SETTLEMENT = "settlement_source"
SIGNAL_LEXICAL = "lexical"
SIGNAL_SERIES_EVENT = "same_series_event"
SIGNAL_CATEGORY = "category_similarity"

ALL_SIGNALS = (
    SIGNAL_SAME_TOPIC,
    SIGNAL_SHARED_ENTITY,
    SIGNAL_DATE_OVERLAP,
    SIGNAL_SETTLEMENT,
    SIGNAL_LEXICAL,
    SIGNAL_SERIES_EVENT,
    SIGNAL_CATEGORY,
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class CandidatePair:
    kalshi_market: Market
    polymarket_market: Market
    candidate_score: float
    reasons: list[str] = field(default_factory=list)
    signals: tuple[str, ...] = ()


@dataclass
class CandidateGenerationStats:
    generated: int = 0
    unique_pairs: int = 0
    by_signal: dict[str, int] = field(default_factory=dict)
    rejected_incompatible_dates: int = 0
    matcher_matches: int = 0
    high_confidence_matches: int = 0


@dataclass
class _IndexedMarket:
    market: Market
    topics: frozenset[str]
    entities: frozenset[tuple[str, str]]
    settlement: frozenset[str]
    tokens: frozenset[str]
    concept_tokens: frozenset[str]
    date: datetime | None
    category: str


class CandidateGenerator:
    """Permissive Kalshi ↔ Polymarket candidate generation.

    False positives are acceptable here. Equivalence is decided later by
    MarketMatcher, and executability by the arb detector.
    """

    def __init__(
        self,
        date_tolerance_days: int = 90,
        lexical_min_score: float = 0.32,
        strong_lexical_min_score: float = 0.45,
        min_candidate_score: float = 0.20,
        max_candidates_per_market: int = 40,
        max_index_postings: int = 800,
    ) -> None:
        self.date_tolerance_days = date_tolerance_days
        self.lexical_min_score = lexical_min_score
        self.strong_lexical_min_score = strong_lexical_min_score
        self.min_candidate_score = min_candidate_score
        self.max_candidates_per_market = max_candidates_per_market
        self.max_index_postings = max_index_postings

    def generate(self, markets: list[Market]) -> tuple[list[CandidatePair], CandidateGenerationStats]:
        kalshi: list[_IndexedMarket] = []
        polymarket: list[_IndexedMarket] = []
        for market in markets:
            if not market.is_active:
                continue
            indexed = self._index_market(market)
            if market.exchange == "kalshi":
                kalshi.append(indexed)
            elif market.exchange == "polymarket":
                polymarket.append(indexed)
        stats = CandidateGenerationStats(by_signal={name: 0 for name in ALL_SIGNALS})
        if not kalshi or not polymarket:
            return [], stats

        poly_by_entity: dict[tuple[str, str], list[int]] = defaultdict(list)
        poly_by_topic: dict[str, list[int]] = defaultdict(list)
        poly_by_settlement: dict[str, list[int]] = defaultdict(list)
        poly_by_token: dict[str, list[int]] = defaultdict(list)
        poly_by_concept: dict[str, list[int]] = defaultdict(list)
        for idx, item in enumerate(polymarket):
            for entity in item.entities:
                _append_capped(poly_by_entity[entity], idx, self.max_index_postings)
            for topic in item.topics:
                _append_capped(poly_by_topic[topic], idx, self.max_index_postings)
            for source in item.settlement:
                _append_capped(poly_by_settlement[source], idx, self.max_index_postings)
            for token in item.tokens:
                if len(token) > 3:
                    _append_capped(poly_by_token[token], idx, self.max_index_postings)
            for token in item.concept_tokens:
                _append_capped(poly_by_concept[token], idx, self.max_index_postings)

        proposed: dict[int, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
        for k_idx, left in enumerate(kalshi):
            for entity in left.entities:
                for p_idx in poly_by_entity.get(entity, ()):
                    proposed[k_idx][p_idx].add(SIGNAL_SHARED_ENTITY)
            for topic in left.topics:
                if topic not in NARROW_TOPICS:
                    continue
                for p_idx in poly_by_topic.get(topic, ()):
                    proposed[k_idx][p_idx].add(SIGNAL_SAME_TOPIC)
            for source in left.settlement:
                for p_idx in poly_by_settlement.get(source, ()):
                    proposed[k_idx][p_idx].add(SIGNAL_SETTLEMENT)
            token_hits: dict[int, int] = defaultdict(int)
            for token in left.tokens:
                if len(token) <= 3:
                    continue
                for p_idx in poly_by_token.get(token, ()):
                    token_hits[p_idx] += 1
            for p_idx, overlap in token_hits.items():
                if overlap >= 2 or (overlap == 1 and any(len(token) >= 6 for token in left.tokens)):
                    proposed[k_idx][p_idx].add(SIGNAL_LEXICAL)
            for token in left.concept_tokens:
                for p_idx in poly_by_concept.get(token, ()):
                    proposed[k_idx][p_idx].add(SIGNAL_SERIES_EVENT)

        pairs: list[CandidatePair] = []
        for k_idx, poly_hits in proposed.items():
            left = kalshi[k_idx]
            scored: list[CandidatePair] = []
            for p_idx, hint_signals in poly_hits.items():
                right = polymarket[p_idx]
                if not dates_compatible(left.date, right.date, self.date_tolerance_days):
                    stats.rejected_incompatible_dates += 1
                    continue
                pair = self._score_pair(left, right, hint_signals)
                if pair is None or not self._accept(pair):
                    continue
                scored.append(pair)
            scored.sort(key=lambda item: item.candidate_score, reverse=True)
            pairs.extend(scored[: self.max_candidates_per_market])

        pairs.sort(key=lambda item: item.candidate_score, reverse=True)
        stats.generated = len(pairs)
        stats.unique_pairs = len(pairs)
        for pair in pairs:
            for signal in pair.signals:
                stats.by_signal[signal] = stats.by_signal.get(signal, 0) + 1
        return pairs, stats

    def _index_market(self, market: Market) -> _IndexedMarket:
        classify_market(market)
        tokens = {
            token
            for token in _TOKEN_RE.findall(market.blob())
            if token not in STOPWORDS and len(token) > 2
        }
        concept = " ".join(
            part.lower()
            for part in (market.series_title, market.event_title, market.series_ticker)
            if part
        )
        concept_tokens = {
            token
            for token in _TOKEN_RE.findall(concept)
            if token not in STOPWORDS and len(token) > 3
        }
        settlement = set(normalize_settlement_sources(list(market.settlement_sources)))
        if market.resolution_source:
            settlement.update(normalize_settlement_sources([market.resolution_source]))
        return _IndexedMarket(
            market=market,
            topics=frozenset(market.canonical_topics),
            entities=frozenset(entity_key(entity) for entity in market.entities),
            settlement=frozenset(settlement),
            tokens=frozenset(tokens),
            concept_tokens=frozenset(concept_tokens),
            date=resolution_date(market),
            category=(market.category or market.series_title or "").lower(),
        )

    def _score_pair(
        self,
        left: _IndexedMarket,
        right: _IndexedMarket,
        hint_signals: set[str],
    ) -> CandidatePair | None:
        reasons: list[str] = []
        signals: set[str] = set()
        shared_topics = left.topics & right.topics
        shared_entities = left.entities & right.entities
        shared_settlement = left.settlement & right.settlement
        lexical = lexical_similarity(left.market, right.market)
        date_score = date_similarity(left.date, right.date, self.date_tolerance_days)
        category_score = 1.0 if left.category and right.category and (
            left.category in right.category or right.category in left.category
        ) else 0.0

        if shared_topics:
            signals.add(SIGNAL_SAME_TOPIC)
            reasons.append("same topic")
            reasons.extend(sorted(shared_topics)[:4])
        if shared_entities:
            signals.add(SIGNAL_SHARED_ENTITY)
            reasons.append("shared entity")
            reasons.extend(value.replace("_", " ") for _kind, value in sorted(shared_entities)[:4])
        if left.date and right.date and date_score > 0:
            signals.add(SIGNAL_DATE_OVERLAP)
            reasons.append("date overlap")
        if shared_settlement:
            signals.add(SIGNAL_SETTLEMENT)
            reasons.append("settlement source")
        if lexical >= self.lexical_min_score or SIGNAL_LEXICAL in hint_signals:
            signals.add(SIGNAL_LEXICAL)
            reasons.append("lexical similarity")
        shared_concept = left.concept_tokens & right.concept_tokens
        if shared_concept or SIGNAL_SERIES_EVENT in hint_signals:
            if shared_concept:
                signals.add(SIGNAL_SERIES_EVENT)
                reasons.append("same series/event concept")
        if category_score:
            signals.add(SIGNAL_CATEGORY)
            reasons.append("category similarity")

        topic_score = 1.0 if shared_topics else 0.0
        entity_score = 0.0
        if shared_entities:
            denom = max(1, min(len(left.entities) or 1, len(right.entities) or 1))
            entity_score = min(1.0, len(shared_entities) / denom)
        settlement_score = 1.0 if shared_settlement else 0.0
        series_score = 1.0 if SIGNAL_SERIES_EVENT in signals else 0.0
        candidate_score = (
            0.20 * topic_score
            + 0.24 * entity_score
            + 0.14 * date_score
            + 0.10 * settlement_score
            + 0.18 * lexical
            + 0.08 * category_score
            + 0.06 * series_score
        )
        if left.settlement and right.settlement and not shared_settlement:
            candidate_score -= 0.04
        candidate_score = max(0.0, min(1.0, candidate_score))
        if candidate_score < self.min_candidate_score and SIGNAL_SHARED_ENTITY not in signals:
            return None
        return CandidatePair(
            kalshi_market=left.market,
            polymarket_market=right.market,
            candidate_score=round(candidate_score, 4),
            reasons=_dedupe_reasons(reasons),
            signals=tuple(signal for signal in ALL_SIGNALS if signal in signals),
        )

    def _accept(self, pair: CandidatePair) -> bool:
        signals = set(pair.signals)
        shared_topics = set(pair.kalshi_market.canonical_topics) & set(
            pair.polymarket_market.canonical_topics
        )
        if SIGNAL_SHARED_ENTITY in signals:
            return True
        if (shared_topics & NARROW_TOPICS):
            return True
        if (shared_topics & BROAD_TOPICS) and signals & {
            SIGNAL_SHARED_ENTITY,
            SIGNAL_LEXICAL,
            SIGNAL_SERIES_EVENT,
            SIGNAL_SETTLEMENT,
        }:
            return True
        if SIGNAL_SETTLEMENT in signals and SIGNAL_LEXICAL in signals:
            return True
        if SIGNAL_LEXICAL in signals and pair.candidate_score >= self.strong_lexical_min_score:
            return True
        if SIGNAL_SERIES_EVENT in signals and signals & {
            SIGNAL_DATE_OVERLAP,
            SIGNAL_SHARED_ENTITY,
            SIGNAL_LEXICAL,
            SIGNAL_SAME_TOPIC,
        }:
            return True
        return False


def dates_compatible(left: datetime | None, right: datetime | None, tolerance_days: int) -> bool:
    if left is None or right is None:
        return True
    return abs((left.date() - right.date()).days) <= tolerance_days


def date_similarity(left: datetime | None, right: datetime | None, tolerance_days: int) -> float:
    if left is None or right is None:
        return 0.4
    delta = abs((left.date() - right.date()).days)
    if delta > tolerance_days:
        return 0.0
    return max(0.0, 1.0 - (delta / max(tolerance_days, 1)))


def resolution_date(market: Market) -> datetime | None:
    return market.close_time or market.resolution_time or market.event_date


def lexical_similarity(left: Market, right: Market) -> float:
    """Candidate-only lexical score. Includes series/event text; does not cap on contradictions."""
    left_tokens = _significant_blob(left)
    right_tokens = _significant_blob(right)
    if not left_tokens or not right_tokens:
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, left.title.lower(), right.title.lower()).ratio()
    shared = " ".join(sorted(left_tokens & right_tokens))
    token_set = max(
        SequenceMatcher(None, shared, " ".join(sorted(left_tokens))).ratio() if shared else 0.0,
        SequenceMatcher(None, shared, " ".join(sorted(right_tokens))).ratio() if shared else 0.0,
    )
    title_fields = extract_fields(left)
    other_fields = extract_fields(right)
    title_jaccard = (
        len(title_fields.significant & other_fields.significant)
        / len(title_fields.significant | other_fields.significant)
        if title_fields.significant and other_fields.significant
        else 0.0
    )
    return 0.35 * jaccard + 0.20 * sequence + 0.25 * token_set + 0.20 * title_jaccard


def _significant_blob(market: Market) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall(market.blob())
        if token not in STOPWORDS and len(token) > 2
    }


def _append_capped(bucket: list[int], value: int, cap: int) -> None:
    if len(bucket) < cap:
        bucket.append(value)


def _dedupe_reasons(reasons: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for reason in reasons:
        key = reason.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(reason)
    return out
