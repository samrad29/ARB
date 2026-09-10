from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher

from prediction_arb.models.market import Market
from prediction_arb.models.opportunity import MarketMatch

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "by", "at",
    "from", "with", "will", "be", "is", "are", "was", "were", "this", "that",
    "these", "those", "before", "after", "between", "vs", "versus", "over",
    "under", "above", "below", "into", "out", "up", "down", "as", "it", "its",
    "if", "not", "no", "yes", "more", "than", "less", "market", "contract",
    "win", "wins", "winner", "election", "whether",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

GEO_ALIASES = {
    "usa": "us", "u.s.": "us", "u.s": "us", "united states": "us", "america": "us",
    "uk": "uk", "u.k.": "uk", "united kingdom": "uk", "britain": "uk",
    "nyc": "new york", "new york city": "new york", "ny": "new york",
    "la": "los angeles", "sf": "san francisco",
}

NUMBER_RE = re.compile(
    r"(?P<num>[-+]?\d+(?:\.\d+)?)\s*(?P<unit>bps|bp|%|percent|celsius|fahrenheit|degrees|deg|°[cf]?|votes|pts|points)?",
    re.I,
)
YEAR_RE = re.compile(r"\b(20[2-3]\d)\b")
THRESHOLD_WORDS = ("cut", "hike", "raise", "lower", "above", "below", "at least", "at most")


@dataclass
class StructuredFields:
    tokens: set[str]
    significant: set[str]
    years: set[int]
    months: set[int]
    numbers: set[tuple[str, str]]
    geo: set[str]
    settlement_hints: set[str]
    category: str | None
    close_date: datetime | None
    title: str
    rules: str


@dataclass
class MatchDecision:
    score: float
    match_type: str
    reasons: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


class MarketMatcher:
    """Conservative cross-exchange candidate matcher.

    A high text-similarity score never overrides contradictory structured
    fields (dates, thresholds, geography, settlement rules). Matches are
    proposals only — never auto-verified.
    """

    def __init__(
        self,
        candidate_min_score: float = 0.55,
        high_confidence_min_score: float = 0.82,
        max_candidates_per_market: int = 8,
    ) -> None:
        self.candidate_min_score = candidate_min_score
        self.high_confidence_min_score = high_confidence_min_score
        self.max_candidates_per_market = max_candidates_per_market

    def match(self, markets: list[Market]) -> list[MarketMatch]:
        by_exchange: dict[str, list[Market]] = defaultdict(list)
        for market in markets:
            if market.is_active:
                by_exchange[market.exchange].append(market)
        exchanges = sorted(by_exchange)
        if len(exchanges) < 2:
            return []

        # V1: pairwise across the two primary exchanges. Additional venues
        # can be compared the same way later.
        results: list[MarketMatch] = []
        seen: set[tuple[str, str, str, str]] = set()
        for i, left_name in enumerate(exchanges):
            for right_name in exchanges[i + 1 :]:
                results.extend(
                    self._match_pair_exchanges(
                        by_exchange[left_name],
                        by_exchange[right_name],
                        seen,
                    )
                )
        results.sort(key=lambda item: item.match_score, reverse=True)
        return results

    def classify_pairs(self, pairs: list[tuple[Market, Market]]) -> list[MarketMatch]:
        """Score provided candidate pairs with the conservative matcher.

        This does not generate extra pairs and does not change thresholds.
        """
        results: list[MarketMatch] = []
        seen: set[tuple[str, str, str, str]] = set()
        for left, right in pairs:
            if left.exchange == right.exchange:
                continue
            key = (left.exchange, left.exchange_market_id, right.exchange, right.exchange_market_id)
            if key in seen:
                continue
            seen.add(key)
            decision = score_pair(extract_fields(left), extract_fields(right))
            results.append(
                MarketMatch(
                    market_a_exchange=left.exchange,
                    market_a_id=left.exchange_market_id,
                    market_b_exchange=right.exchange,
                    market_b_id=right.exchange_market_id,
                    match_type=decision.match_type,  # type: ignore[arg-type]
                    match_score=round(decision.score, 4),
                    verified=False,
                    reason=" ".join(decision.reasons),
                    details={
                        "contradictions": decision.contradictions,
                        **decision.details,
                        "left_title": left.title,
                        "right_title": right.title,
                    },
                )
            )
        results.sort(key=lambda item: item.match_score, reverse=True)
        return results

    def _match_pair_exchanges(
        self,
        left_markets: list[Market],
        right_markets: list[Market],
        seen: set[tuple[str, str, str, str]],
    ) -> list[MarketMatch]:
        left_fields = [(market, extract_fields(market)) for market in left_markets]
        right_fields = [(market, extract_fields(market)) for market in right_markets]
        index: dict[str, list[int]] = defaultdict(list)
        for idx, (_, fields) in enumerate(right_fields):
            for token in fields.significant:
                index[token].append(idx)

        matches: list[MarketMatch] = []
        for left, left_struct in left_fields:
            candidate_ids: dict[int, int] = defaultdict(int)
            for token in left_struct.significant:
                for idx in index.get(token, []):
                    candidate_ids[idx] += 1
            ranked_ids = sorted(candidate_ids.items(), key=lambda item: item[1], reverse=True)
            scored: list[tuple[float, Market, MatchDecision]] = []
            for idx, overlap in ranked_ids[:80]:
                if overlap < 1:
                    continue
                right, right_struct = right_fields[idx]
                decision = score_pair(left_struct, right_struct)
                if decision.score < self.candidate_min_score:
                    continue
                scored.append((decision.score, right, decision))
            scored.sort(key=lambda item: item[0], reverse=True)
            for score, right, decision in scored[: self.max_candidates_per_market]:
                key = (left.exchange, left.exchange_market_id, right.exchange, right.exchange_market_id)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    MarketMatch(
                        market_a_exchange=left.exchange,
                        market_a_id=left.exchange_market_id,
                        market_b_exchange=right.exchange,
                        market_b_id=right.exchange_market_id,
                        match_type=decision.match_type,  # type: ignore[arg-type]
                        match_score=round(score, 4),
                        verified=False,
                        reason=" ".join(decision.reasons),
                        details={
                            "contradictions": decision.contradictions,
                            **decision.details,
                            "left_title": left.title,
                            "right_title": right.title,
                        },
                    )
                )
        return matches


def extract_fields(market: Market) -> StructuredFields:
    title = market.title or ""
    rules = " ".join(part for part in (market.description, market.rules_text, market.resolution_source) if part)
    blob = f"{title} {rules}".lower()
    tokens = _tokenize(blob)
    significant = {token for token in tokens if token not in STOPWORDS and len(token) > 2}
    years = {int(match.group(1)) for match in YEAR_RE.finditer(blob)}
    months = {month for name, month in MONTHS.items() if re.search(rf"\b{name}\b", blob)}
    numbers: set[tuple[str, str]] = set()
    for match in NUMBER_RE.finditer(blob):
        unit = (match.group("unit") or "").lower()
        numbers.add((match.group("num"), unit))
    geo = set()
    for alias, canonical in GEO_ALIASES.items():
        if alias in blob:
            geo.add(canonical)
    settlement = set()
    for hint in (
        "electoral college", "popular vote", "fomc", "cme", "uma", "chainlink",
        "ap call", "decision desk", "kalshi", "polymarket", "associated press",
        "reuters", "bloomberg", "noaa", "weather.gov", "bea", "bls", "cdc",
    ):
        if hint in blob:
            settlement.add(hint)
    return StructuredFields(
        tokens=tokens,
        significant=significant,
        years=years,
        months=months,
        numbers=numbers,
        geo=geo,
        settlement_hints=settlement,
        category=(market.category or "").lower() or None,
        close_date=market.close_time or market.event_date or market.resolution_time,
        title=title.lower(),
        rules=rules.lower(),
    )


def score_pair(left: StructuredFields, right: StructuredFields) -> MatchDecision:
    jaccard = _jaccard(left.significant, right.significant)
    sequence = SequenceMatcher(None, left.title, right.title).ratio()
    token_set = _token_set_ratio(left.tokens, right.tokens)
    score = 0.40 * jaccard + 0.30 * sequence + 0.30 * token_set

    reasons: list[str] = []
    contradictions: list[str] = []
    details = {
        "jaccard": round(jaccard, 4),
        "sequence": round(sequence, 4),
        "token_set": round(token_set, 4),
    }

    if left.years and right.years:
        if left.years == right.years:
            score += 0.04
            reasons.append("Same year.")
        elif left.years.isdisjoint(right.years):
            contradictions.append("Different years.")
    if left.months and right.months:
        if left.months == right.months:
            score += 0.04
            reasons.append("Same month.")
        elif left.months.isdisjoint(right.months):
            contradictions.append("Different months.")
    if left.close_date and right.close_date:
        delta_days = abs((left.close_date.date() - right.close_date.date()).days)
        details["close_date_delta_days"] = delta_days
        if delta_days <= 2:
            score += 0.05
            reasons.append("Close/resolution dates align.")
        elif delta_days > 45:
            contradictions.append("Resolution dates differ by more than 45 days.")

    if left.geo and right.geo and left.geo.isdisjoint(right.geo):
        contradictions.append("Different geography.")
    elif left.geo and right.geo and left.geo & right.geo:
        reasons.append("Same geography.")
        score += 0.03

    if _numeric_conflict(left.numbers, right.numbers):
        contradictions.append("Different numeric thresholds.")
    elif left.numbers and right.numbers and left.numbers & right.numbers:
        reasons.append("Same threshold.")
        score += 0.04

    if left.settlement_hints and right.settlement_hints:
        if left.settlement_hints.isdisjoint(right.settlement_hints):
            contradictions.append("Different settlement sources/criteria.")
        else:
            reasons.append("Same settlement concept.")
            score += 0.03

    if left.category and right.category and _categories_compatible(left.category, right.category):
        score += 0.02
        reasons.append("Compatible category.")

    if _rules_conflict(left.rules, right.rules):
        contradictions.append("Resolution conditions appear to differ.")

    if contradictions:
        # Structured disagreement always wins over a high semantic score.
        score = min(score, 0.49)
        match_type = "POTENTIAL"
        reasons.append("Not treated as equivalent: " + " ".join(contradictions))
    elif score >= 0.90 and (left.years & right.years or not (left.years and right.years)):
        match_type = "EXACT"
        reasons.insert(0, "Same underlying event.")
        reasons.append("Same outcome framing.")
    elif score >= 0.82:
        match_type = "LIKELY_EQUIVALENT"
        reasons.insert(0, "Likely the same underlying event; not auto-verified.")
    else:
        match_type = "POTENTIAL"
        reasons.append("Candidate only; titles/metadata overlap but equivalence is unproven.")

    score = max(0.0, min(score, 1.0))
    if not reasons:
        reasons.append("Token overlap without a confirmed structured match.")
    return MatchDecision(
        score=score,
        match_type=match_type,
        reasons=reasons,
        contradictions=contradictions,
        details=details,
    )


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _token_set_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    shared = " ".join(sorted(a & b))
    left = " ".join(sorted(a))
    right = " ".join(sorted(b))
    if not shared:
        return SequenceMatcher(None, left, right).ratio()
    return max(
        SequenceMatcher(None, shared, left).ratio(),
        SequenceMatcher(None, shared, right).ratio(),
    )


def _numeric_conflict(left: set[tuple[str, str]], right: set[tuple[str, str]]) -> bool:
    if not left or not right:
        return False
    left_by_unit: dict[str, set[str]] = defaultdict(set)
    right_by_unit: dict[str, set[str]] = defaultdict(set)
    for number, unit in left:
        if unit:
            left_by_unit[unit].add(number)
    for number, unit in right:
        if unit:
            right_by_unit[unit].add(number)
    for unit, values in left_by_unit.items():
        other = right_by_unit.get(unit)
        if other and values.isdisjoint(other):
            return True
    return False


def _categories_compatible(left: str, right: str) -> bool:
    if left in right or right in left:
        return True
    aliases = {
        "politics": {"politics", "elections", "world", "geopolitics"},
        "economics": {"economics", "economy", "finance", "fed", "rates"},
        "sports": {"sports", "nba", "nfl", "mlb", "nhl", "soccer"},
        "crypto": {"crypto", "bitcoin", "ethereum"},
        "weather": {"weather", "climate"},
    }
    for group in aliases.values():
        if any(token in left for token in group) and any(token in right for token in group):
            return True
    return False


def _rules_conflict(left: str, right: str) -> bool:
    if not left or not right:
        return False
    pairs = [
        ("electoral college", "popular vote"),
        ("at the meeting", "by the end of"),
        ("regular season", "including playoffs"),
        ("in-person", "virtual"),
        ("confirmed", "announced"),
    ]
    for first, second in pairs:
        if (first in left and second in right) or (second in left and first in right):
            return True
    return False
