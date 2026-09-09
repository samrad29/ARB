"""Integer-cent money helpers.

Prices and cash amounts are stored as integer cents (47 == $0.47).
Quantities are stored as whole contracts, rounded down so liquidity
estimates stay conservative.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any

CENTS_PER_DOLLAR = Decimal("100")
CONTRACT_PAYOUT_CENTS = 100


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def dollars_to_cents(value: Any, *, rounding=ROUND_HALF_UP) -> int | None:
    """Convert a dollar amount to integer cents."""
    decimal_value = _as_decimal(value)
    if decimal_value is None:
        return None
    quantized = (decimal_value * CENTS_PER_DOLLAR).to_integral_value(rounding=rounding)
    return int(quantized)


def cents_to_dollars(cents: int | None) -> Decimal | None:
    if cents is None:
        return None
    return Decimal(cents) / CENTS_PER_DOLLAR


def format_cents(cents: int | None) -> str:
    if cents is None:
        return "n/a"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def format_quantity(quantity: int | None) -> str:
    if quantity is None:
        return "n/a"
    return f"{quantity:,}"


def floor_quantity(value: Any) -> int | None:
    """Whole contracts, rounded down so we never overstate executable size."""
    decimal_value = _as_decimal(value)
    if decimal_value is None:
        return None
    if decimal_value < 0:
        return 0
    return int(decimal_value.to_integral_value(rounding=ROUND_DOWN))


def dollars_to_cents_ceiling(value: Any) -> int:
    """Convert a fee in dollars to cents, rounding away from zero (conservative)."""
    decimal_value = _as_decimal(value) or Decimal("0")
    if decimal_value <= 0:
        return 0
    quantized = (decimal_value * CENTS_PER_DOLLAR).to_integral_value(rounding=ROUND_CEILING)
    return int(quantized)


def complement_cents(price_cents: int | None) -> int | None:
    """YES bid at X is equivalent to a NO ask at (100 - X)."""
    if price_cents is None:
        return None
    return CONTRACT_PAYOUT_CENTS - price_cents
