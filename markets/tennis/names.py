"""Player-name and tour helpers for tennis match discovery."""

from __future__ import annotations

import re

SPORT = "tennis"
TOURS = ("atp", "wta", "itf", "challenger")
STOP = {"de", "da", "das", "dos", "del", "van", "von", "la", "le", "di", "du"}
SKIP_TITLE = re.compile(r"\b(junior|juniors|doubles)\b", re.I)

KALSHI_SERIES = (
    ("KXATPMATCH", "atp"),
    ("KXWTAMATCH", "wta"),
    ("KXITFMATCH", "itf"),
    ("KXATPCHALLENGERMATCH", "challenger"),
    ("KXWTACHALLENGERMATCH", "challenger"),
)
POLY_TAG = 864
POLY_SPORT = {"atp": "atp", "wta": "wta", "itf": "itf"}
GAME_SLUG = re.compile(r"^(atp|wta|itf)-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}$")
KALSHI_DATE = re.compile(r"^[A-Z0-9]+-(\d{2}[A-Z]{3}\d{2})")


def player_id(text: str | None) -> str | None:
    """Canonical player id: lowercase letters/digits/spaces, hyphens become spaces."""
    if not text:
        return None
    raw = re.sub(r"[^a-z0-9]+", " ", str(text).lower())
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw or None


def _tokens(name: str) -> list[str]:
    return [token for token in name.split() if token not in STOP]


def names_equal(left: str | None, right: str | None) -> bool:
    """True when two labels are the same player (full name, last name, or containment)."""
    a, b = player_id(left), player_id(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta[-1] == tb[-1] and len(ta[-1]) > 2:
        return ta[0][0] == tb[0][0]
    return False


def poly_tour(sport_slug: str | None, title: str | None) -> str | None:
    """Map a Polymarket tennis event to atp / wta / itf / challenger. Skip doubles and juniors."""
    slug = (sport_slug or "").lower()
    heading = title or ""
    if slug in {"atp-doubles", "wta-doubles"} or SKIP_TITLE.search(heading) or SKIP_TITLE.search(slug):
        return None
    blob = f"{slug} {heading}".lower()
    if "challenger" in blob or "wta 125" in blob or "125k" in blob:
        return "challenger"
    return POLY_SPORT.get(slug)
