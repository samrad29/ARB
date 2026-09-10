from __future__ import annotations

import re
from dataclasses import dataclass

from prediction_arb.models.market import Market, MarketEntity

CANONICAL_TOPICS = (
    "US_POLITICS",
    "US_ELECTIONS",
    "FED",
    "INFLATION",
    "EMPLOYMENT",
    "GDP",
    "CRYPTO",
    "STOCKS",
    "SPORTS_NFL",
    "SPORTS_NBA",
    "SPORTS_MLB",
    "SPORTS_NHL",
    "SOCCER",
    "WEATHER",
    "TECH",
    "ENTERTAINMENT",
)

# Topic+date alone is too explosive for these. Require a second signal.
BROAD_TOPICS = {
    "CRYPTO",
    "STOCKS",
    "WEATHER",
    "SPORTS_NFL",
    "SPORTS_NBA",
    "SPORTS_MLB",
    "SPORTS_NHL",
    "SOCCER",
    "TECH",
    "ENTERTAINMENT",
}

NARROW_TOPICS = {
    "US_POLITICS",
    "US_ELECTIONS",
    "FED",
    "INFLATION",
    "EMPLOYMENT",
    "GDP",
}

ENTITY_PERSON = "person"
ENTITY_TEAM = "team"
ENTITY_COMPANY = "company"
ENTITY_CRYPTO = "cryptocurrency"
ENTITY_COUNTRY = "country"
ENTITY_CITY = "city"
ENTITY_ORG = "organization"
ENTITY_INDICATOR = "economic_indicator"

_PERSON_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PERSON_PREFIXES = {"will", "the", "for", "and", "did"}


@dataclass(frozen=True)
class Alias:
    canonical: str
    entity_type: str
    extra_topics: tuple[str, ...] = ()


def _alias(canonical: str, entity_type: str, *topics: str) -> Alias:
    return Alias(canonical=canonical, entity_type=entity_type, extra_topics=topics)


# Longer keys must be matched first.
_ALIAS_ENTRIES: list[tuple[tuple[str, ...], Alias]] = [
    (("federal reserve", "the fed", "fomc", "fed funds", "fed"), _alias("federal_reserve", ENTITY_ORG, "FED")),
    (("consumer price index", "cpi"), _alias("cpi", ENTITY_INDICATOR, "INFLATION")),
    (("producer price index", "ppi"), _alias("ppi", ENTITY_INDICATOR, "INFLATION")),
    (("nonfarm payrolls", "non-farm payrolls", "nfp", "payrolls"), _alias("nfp", ENTITY_INDICATOR, "EMPLOYMENT")),
    (("unemployment rate", "jobless claims", "initial claims"), _alias("unemployment", ENTITY_INDICATOR, "EMPLOYMENT")),
    (("gross domestic product", "gdp"), _alias("gdp", ENTITY_INDICATOR, "GDP")),
    (("electoral college",), _alias("electoral_college", ENTITY_ORG, "US_ELECTIONS")),
    (("popular vote",), _alias("popular_vote", ENTITY_ORG, "US_ELECTIONS")),
    (("white house",), _alias("white_house", ENTITY_ORG, "US_POLITICS")),
    (("congress", "us senate", "senate", "house of representatives"), _alias("us_congress", ENTITY_ORG, "US_POLITICS")),
    (("supreme court", "scotus"), _alias("supreme_court", ENTITY_ORG, "US_POLITICS")),
    (("bitcoin", "xbt"), _alias("bitcoin", ENTITY_CRYPTO, "CRYPTO")),
    (("ethereum",), _alias("ethereum", ENTITY_CRYPTO, "CRYPTO")),
    (("solana",), _alias("solana", ENTITY_CRYPTO, "CRYPTO")),
    (("dogecoin", "doge"), _alias("dogecoin", ENTITY_CRYPTO, "CRYPTO")),
    (("shiba inu", "shibainu", "shiba"), _alias("shiba_inu", ENTITY_CRYPTO, "CRYPTO")),
    (("xrp", "ripple"), _alias("xrp", ENTITY_CRYPTO, "CRYPTO")),
    (("s&p 500", "s&p500", "spx", "sp500"), _alias("sp500", ENTITY_COMPANY, "STOCKS")),
    (("nasdaq", "qqq"), _alias("nasdaq", ENTITY_COMPANY, "STOCKS")),
    (("dow jones", "djia"), _alias("dow", ENTITY_COMPANY, "STOCKS")),
    (("nvidia", "nvda"), _alias("nvidia", ENTITY_COMPANY, "STOCKS", "TECH")),
    (("tesla", "tsla"), _alias("tesla", ENTITY_COMPANY, "STOCKS")),
    (("apple", "aapl"), _alias("apple", ENTITY_COMPANY, "STOCKS", "TECH")),
    (("microsoft", "msft"), _alias("microsoft", ENTITY_COMPANY, "STOCKS", "TECH")),
    (("amazon", "amzn"), _alias("amazon", ENTITY_COMPANY, "STOCKS")),
    (("alphabet", "google", "googl"), _alias("google", ENTITY_COMPANY, "STOCKS", "TECH")),
    (("meta", "facebook"), _alias("meta", ENTITY_COMPANY, "STOCKS", "TECH")),
    (("openai", "chatgpt"), _alias("openai", ENTITY_COMPANY, "TECH")),
    (("united states", "u.s.", "u.s", "usa", "america"), _alias("united_states", ENTITY_COUNTRY, "US_POLITICS")),
    (("united kingdom", "u.k.", "britain", "england"), _alias("united_kingdom", ENTITY_COUNTRY)),
    (("new york city", "new york", "nyc"), _alias("new_york", ENTITY_CITY, "WEATHER")),
    (("los angeles",), _alias("los_angeles", ENTITY_CITY)),
    (("san francisco",), _alias("san_francisco", ENTITY_CITY)),
    (("iran",), _alias("iran", ENTITY_COUNTRY)),
    (("china", "chinese"), _alias("china", ENTITY_COUNTRY)),
    (("russia", "russian"), _alias("russia", ENTITY_COUNTRY)),
    (("ukraine",), _alias("ukraine", ENTITY_COUNTRY)),
    (("israel",), _alias("israel", ENTITY_COUNTRY)),
    (("greenland",), _alias("greenland", ENTITY_COUNTRY)),
    (("oscars", "academy awards", "oscar"), _alias("oscars", ENTITY_ORG, "ENTERTAINMENT")),
    (("grammys", "grammy"), _alias("grammys", ENTITY_ORG, "ENTERTAINMENT")),
    (("super bowl",), _alias("super_bowl", ENTITY_ORG, "SPORTS_NFL")),
    (("world cup", "fifa"), _alias("world_cup", ENTITY_ORG, "SOCCER")),
    (("premier league", "epl"), _alias("premier_league", ENTITY_ORG, "SOCCER")),
    (("champions league", "ucl"), _alias("champions_league", ENTITY_ORG, "SOCCER")),
    (("kansas city chiefs", "chiefs"), _alias("chiefs", ENTITY_TEAM, "SPORTS_NFL")),
    (("philadelphia eagles", "eagles"), _alias("eagles", ENTITY_TEAM, "SPORTS_NFL")),
    (("dallas cowboys", "cowboys"), _alias("cowboys", ENTITY_TEAM, "SPORTS_NFL")),
    (("los angeles lakers", "lakers"), _alias("lakers", ENTITY_TEAM, "SPORTS_NBA")),
    (("boston celtics", "celtics"), _alias("celtics", ENTITY_TEAM, "SPORTS_NBA")),
    (("new york yankees", "yankees"), _alias("yankees", ENTITY_TEAM, "SPORTS_MLB")),
    (("seattle seahawks", "seahawks"), _alias("seahawks", ENTITY_TEAM, "SPORTS_NFL")),
]

# Single-token aliases that would be too noisy as substrings ("fed" in "federal" is ok;
# "us" as a word must be bounded).
_WORD_ALIASES: dict[str, Alias] = {
    "btc": _alias("bitcoin", ENTITY_CRYPTO, "CRYPTO"),
    "eth": _alias("ethereum", ENTITY_CRYPTO, "CRYPTO"),
    "sol": _alias("solana", ENTITY_CRYPTO, "CRYPTO"),
    "doge": _alias("dogecoin", ENTITY_CRYPTO, "CRYPTO"),
    "shib": _alias("shiba_inu", ENTITY_CRYPTO, "CRYPTO"),
    "xrp": _alias("xrp", ENTITY_CRYPTO, "CRYPTO"),
    "fed": _alias("federal_reserve", ENTITY_ORG, "FED"),
    "fomc": _alias("federal_reserve", ENTITY_ORG, "FED"),
    "cpi": _alias("cpi", ENTITY_INDICATOR, "INFLATION"),
    "ppi": _alias("ppi", ENTITY_INDICATOR, "INFLATION"),
    "nfp": _alias("nfp", ENTITY_INDICATOR, "EMPLOYMENT"),
    "gdp": _alias("gdp", ENTITY_INDICATOR, "GDP"),
    "nfl": _alias("nfl", ENTITY_ORG, "SPORTS_NFL"),
    "nba": _alias("nba", ENTITY_ORG, "SPORTS_NBA"),
    "mlb": _alias("mlb", ENTITY_ORG, "SPORTS_MLB"),
    "nhl": _alias("nhl", ENTITY_ORG, "SPORTS_NHL"),
    "soccer": _alias("soccer", ENTITY_ORG, "SOCCER"),
    "football": _alias("soccer", ENTITY_ORG, "SOCCER"),
    "uk": _alias("united_kingdom", ENTITY_COUNTRY),
    "usa": _alias("united_states", ENTITY_COUNTRY, "US_POLITICS"),
}

_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "US_ELECTIONS": (
        "election", "electoral", "primary", "nominee", "nomination", "president",
        "presidential", "governor", "senate race", "house race", "midterm",
    ),
    "US_POLITICS": (
        "congress", "white house", "impeach", "executive order", "cabinet",
        "attorney general", "supreme court", "legislation", "bill passes",
    ),
    "FED": ("fed ", "federal reserve", "fomc", "rate cut", "rate hike", "interest rate", "fed funds"),
    "INFLATION": ("inflation", "cpi", "ppi", "pce", "consumer price"),
    "EMPLOYMENT": ("unemployment", "payroll", "nfp", "jobless", "jobs report", "nonfarm"),
    "GDP": ("gdp", "gross domestic", "recession"),
    "CRYPTO": (
        "crypto", "bitcoin", "ethereum", "solana", "dogecoin", "shiba", "blockchain",
        "btc", "eth", "memecoin",
    ),
    "STOCKS": ("stock", "s&p", "nasdaq", "dow jones", "equity", "earnings", "ipo"),
    "SPORTS_NFL": ("nfl", "super bowl", "touchdown", "seahawks", "chiefs", "eagles"),
    "SPORTS_NBA": ("nba", "lakers", "celtics", "playoff", "basketball"),
    "SPORTS_MLB": ("mlb", "world series", "yankees", "baseball"),
    "SPORTS_NHL": ("nhl", "stanley cup", "hockey"),
    "SOCCER": ("soccer", "premier league", "la liga", "bundesliga", "world cup", "fifa", "uefa", "fc "),
    "WEATHER": ("weather", "temperature", "hurricane", "rainfall", "snowfall", "celsius", "fahrenheit"),
    "TECH": ("openai", "chatgpt", "apple", "google", "nvidia", "ai model", "iphone"),
    "ENTERTAINMENT": ("oscar", "grammy", "emmy", "box office", "movie", "album", "netflix"),
}

_SETTLEMENT_ALIASES = {
    "fomc": "federal_reserve",
    "federal reserve": "federal_reserve",
    "fed": "federal_reserve",
    "bls": "bls",
    "bureau of labor statistics": "bls",
    "bea": "bea",
    "cme": "cme",
    "uma": "uma",
    "uma oracle": "uma",
    "chainlink": "chainlink",
    "associated press": "ap",
    "ap": "ap",
    "decision desk": "decision_desk",
    "electoral college": "electoral_college",
    "popular vote": "popular_vote",
    "noaa": "noaa",
    "weather.gov": "noaa",
    "cdc": "cdc",
    "reuters": "reuters",
    "bloomberg": "bloomberg",
}


def classify_market(market: Market) -> Market:
    """Fill canonical_topics and entities in place. A market may have several topics."""
    blob = market.blob().lower()
    topics: set[str] = set()
    entities: dict[tuple[str, str], MarketEntity] = {}

    for phrases, alias in _ALIAS_ENTRIES:
        if any(phrase in blob for phrase in phrases):
            topics.update(alias.extra_topics)
            entities[(alias.entity_type, alias.canonical)] = MarketEntity(
                type=alias.entity_type, value=alias.canonical
            )

    tokens = set(_TOKEN_RE.findall(blob))
    for token in tokens:
        alias = _WORD_ALIASES.get(token)
        if alias is None:
            continue
        topics.update(alias.extra_topics)
        entities[(alias.entity_type, alias.canonical)] = MarketEntity(
            type=alias.entity_type, value=alias.canonical
        )

    category_blob = " ".join(
        part.lower()
        for part in (market.category, market.subcategory, market.series_title, *market.tags)
        if part
    )
    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(keyword in blob or keyword in category_blob for keyword in keywords):
            topics.add(topic)

    for name in _PERSON_RE.findall(market.title or "") + _PERSON_RE.findall(market.event_title or ""):
        for canonical in _person_values(name):
            entities[(ENTITY_PERSON, canonical)] = MarketEntity(type=ENTITY_PERSON, value=canonical)

    if market.category:
        topics.update(_topics_from_category(market.category))
    if market.series_title:
        topics.update(_topics_from_category(market.series_title))
    for tag in market.tags:
        topics.update(_topics_from_category(tag))

    market.canonical_topics = sorted(topics)
    market.entities = list(entities.values())
    if not market.settlement_sources and market.resolution_source:
        market.settlement_sources = sorted(normalize_settlement_sources([market.resolution_source]))
    return market


def _topics_from_category(text: str) -> set[str]:
    lowered = text.lower()
    found: set[str] = set()
    mapping = (
        (("politic", "election", "world"), "US_POLITICS"),
        (("election",), "US_ELECTIONS"),
        (("econom", "finance", "fed", "rates"), "FED"),
        (("crypto", "bitcoin"), "CRYPTO"),
        (("stock", "equity", "finance"), "STOCKS"),
        (("nfl",), "SPORTS_NFL"),
        (("nba",), "SPORTS_NBA"),
        (("mlb",), "SPORTS_MLB"),
        (("nhl",), "SPORTS_NHL"),
        (("soccer", "football"), "SOCCER"),
        (("weather", "climate"), "WEATHER"),
        (("tech", "science"), "TECH"),
        (("entertainment", "culture", "awards"), "ENTERTAINMENT"),
        (("sports",), "SPORTS_NFL"),
    )
    for needles, topic in mapping:
        if any(needle in lowered for needle in needles):
            found.add(topic)
    if "sports" in lowered and not any(t.startswith("SPORTS_") or t == "SOCCER" for t in found):
        found.update({"SPORTS_NFL", "SPORTS_NBA"})
    return found


def normalize_settlement_sources(values: list[str]) -> set[str]:
    found: set[str] = set()
    for value in values:
        if not value:
            continue
        lowered = value.lower()
        matched = False
        for alias, canonical in _SETTLEMENT_ALIASES.items():
            if alias in lowered:
                found.add(canonical)
                matched = True
        if not matched:
            tokens = [token for token in _TOKEN_RE.findall(lowered) if len(token) > 3]
            if tokens:
                found.add(" ".join(tokens[:6]))
    return found


def entity_key(entity: MarketEntity) -> tuple[str, str]:
    return (entity.type, entity.value)


def _person_values(name: str) -> list[str]:
    parts = [part for part in name.split() if part]
    values: list[str] = []
    trimmed = [part for part in parts if part.lower() not in _PERSON_PREFIXES]
    for candidate in (parts, trimmed):
        if len(candidate) >= 2:
            values.append("_".join(part.lower() for part in candidate))
    return values
