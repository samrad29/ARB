"""Shared HTTP and parsing helpers."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
POLY = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"


def get_json(url: str, params: dict | None = None) -> dict | list:
    if params:
        url = url + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"User-Agent": "moneyline-scanner", "Accept": "application/json"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 5:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"failed {url}")


def parse_price(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_game_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _flag(value) -> int:
    return 1 if value in (1, True, "true", "True") else 0


def poly_game_state(event: dict | None) -> dict:
    """Polymarket sports events expose `live` / `ended` (pregame is both unset)."""
    event = event or {}
    return {"live": _flag(event.get("live")), "ended": _flag(event.get("ended"))}


def game_status(row: dict | None) -> str:
    """live, pregame, ended, or unknown from stored Polymarket flags."""
    if not row:
        return "unknown"
    if _flag(row.get("live")):
        return "live"
    if _flag(row.get("ended")):
        return "ended"
    if row.get("live") is None and row.get("ended") is None:
        return "unknown"
    return "pregame"
