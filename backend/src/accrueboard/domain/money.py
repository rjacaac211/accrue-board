"""Money primitives.

Rules:
- Money is always ``Decimal`` with exactly two decimal places. Floats are rejected everywhere,
  because binary floating point cannot represent most cent values exactly.
- Rounding is ROUND_HALF_UP to the cent, applied only at defined points (line amounts, tax,
  allocations) via :func:`round_money`.
- Unit prices and quantities may carry up to four decimal places (e.g. 0.1250 per unit).
"""

from collections.abc import Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
_FINE = Decimal("0.0001")


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise TypeError("float is not allowed for monetary values; pass a str, int or Decimal")
    return value


def _to_decimal(value: object) -> Decimal:
    _reject_float(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(value)
    raise TypeError(f"cannot interpret {type(value).__name__} as a decimal amount")


def _with_places(value: object, quantum: Decimal, what: str) -> Decimal:
    number = _to_decimal(value)
    if not number.is_finite():
        raise ValueError(f"{what} must be finite")
    quantized = number.quantize(quantum)
    if quantized != number:
        raise ValueError(f"{what} has more precision than allowed ({quantum})")
    return quantized


def money(value: Decimal | int | str) -> Decimal:
    """Build a monetary amount with exactly two decimal places. Rejects sub-cent precision."""
    return _with_places(value, CENT, "amount (sub-cent precision; must be whole cents)")


def _field(value: object, quantum: Decimal, what: str) -> Decimal:
    # Pydantic only converts ValueError into a ValidationError.
    try:
        return _with_places(value, quantum, what)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc


def _money_validator(value: object) -> Decimal:
    return _field(value, CENT, "amount (sub-cent precision; must be whole cents)")


def _fine_validator(value: object) -> Decimal:
    return _field(value, _FINE, "value")


_as_str = PlainSerializer(str, return_type=str, when_used="json")

Money = Annotated[Decimal, BeforeValidator(_money_validator), _as_str]
"""A whole-cent Decimal amount. Floats and sub-cent values are rejected."""

UnitPrice = Annotated[Decimal, BeforeValidator(_fine_validator), _as_str]
"""A per-unit price with up to four decimal places."""

Quantity = Annotated[Decimal, BeforeValidator(_fine_validator), _as_str]
"""A quantity with up to four decimal places."""


def round_money(value: Decimal) -> Decimal:
    """Round to the cent, half away from zero (ROUND_HALF_UP)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def allocate(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """Split ``total`` across ``weights`` pro rata so the shares sum to ``total`` exactly.

    Uses the largest-remainder method: every share is first rounded down to the cent, then
    the leftover cents go one at a time to the shares with the largest fractional remainders
    (ties: larger weight first, then lower index). Every share is within one cent of its exact
    pro-rata value and never negative. Zero weights always receive zero.
    """
    if total < 0:
        raise ValueError("cannot allocate a negative total")
    if any(w < 0 for w in weights):
        raise ValueError("allocation weights must be non-negative")
    weight_sum = sum(weights, Decimal(0))
    if weight_sum == 0:
        raise ValueError("at least one allocation weight must be positive")

    exact = [total * w / weight_sum for w in weights]
    floors = [e.quantize(CENT, rounding=ROUND_DOWN) for e in exact]
    leftover_cents = int((total - sum(floors, Decimal(0))) / CENT)

    order = sorted(
        range(len(weights)),
        key=lambda i: (-(exact[i] - floors[i]), -weights[i], i),
    )
    shares = list(floors)
    for i in order[:leftover_cents]:
        shares[i] += CENT
    return [s.quantize(CENT) for s in shares]
