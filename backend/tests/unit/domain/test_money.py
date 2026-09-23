from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from accrueboard.domain.money import (
    CENT,
    Money,
    Quantity,
    UnitPrice,
    allocate,
    money,
    round_money,
)


class Holder(BaseModel):
    amount: Money
    unit_price: UnitPrice | None = None
    quantity: Quantity | None = None


# ------------------------------------------------------------------ construction


def test_money_parses_strings_and_ints() -> None:
    assert money("12.50") == Decimal("12.50")
    assert money(3) == Decimal("3.00")


def test_money_rejects_floats() -> None:
    with pytest.raises(TypeError, match="float"):
        money(0.1)  # type: ignore[arg-type]


def test_money_rejects_sub_cent_precision() -> None:
    with pytest.raises(ValueError, match="cent"):
        money("1.005")


def test_money_is_always_two_decimal_places() -> None:
    assert str(money("7")) == "7.00"
    assert str(money("7.5")) == "7.50"


def test_pydantic_money_field_rejects_float() -> None:
    with pytest.raises(ValidationError, match="float"):
        Holder(amount=1.5)  # type: ignore[arg-type]


def test_pydantic_money_field_accepts_string_and_quantizes() -> None:
    assert Holder(amount="10").amount == Decimal("10.00")  # type: ignore[arg-type]


def test_pydantic_money_field_rejects_sub_cent() -> None:
    with pytest.raises(ValidationError, match="cent"):
        Holder(amount="10.001")  # type: ignore[arg-type]


def test_unit_price_allows_four_decimal_places() -> None:
    assert Holder(amount="0", unit_price="0.1250").unit_price == Decimal("0.1250")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Holder(amount="0", unit_price="0.12345")  # type: ignore[arg-type]


def test_quantity_rejects_float() -> None:
    with pytest.raises(ValidationError, match="float"):
        Holder(amount="0", quantity=2.0)  # type: ignore[arg-type]


def test_money_serializes_as_string() -> None:
    assert Holder(amount="3.10").model_dump(mode="json")["amount"] == "3.10"  # type: ignore[arg-type]


# ------------------------------------------------------------------ rounding


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2.345", "2.35"), ("2.344", "2.34"), ("-2.345", "-2.35"), ("0.005", "0.01")],
)
def test_round_money_is_half_up(value: str, expected: str) -> None:
    assert round_money(Decimal(value)) == Decimal(expected)


# ------------------------------------------------------------------ allocation


def test_allocate_pro_rata() -> None:
    assert allocate(money("10.00"), [Decimal(1), Decimal(1)]) == [money("5.00"), money("5.00")]


def test_allocate_exact_split_has_no_remainder() -> None:
    shares = allocate(money("10.00"), [Decimal(1), Decimal(3), Decimal(1)])
    assert shares == [money("2.00"), money("6.00"), money("2.00")]


def test_allocate_leftover_cents_go_to_largest_remainders() -> None:
    # 10.00 / 3 = 3.333...: floors sum to 9.99; the leftover cent breaks the tie by index.
    assert allocate(money("10.00"), [Decimal(1)] * 3) == [
        money("3.34"),
        money("3.33"),
        money("3.33"),
    ]
    # 1.00 split 1:2 -> 0.333.. / 0.666..: the larger fractional remainder gets the cent.
    assert allocate(money("1.00"), [Decimal(1), Decimal(2)]) == [money("0.33"), money("0.67")]


def test_allocate_never_produces_negative_shares() -> None:
    # Naive round-then-fix would give -0.05 here.
    shares = allocate(money("0.06"), [Decimal(1)] * 12)
    assert all(s >= 0 for s in shares)
    assert sum(shares) == money("0.06")


def test_allocate_negative_total_rejected() -> None:
    with pytest.raises(ValueError, match="negative"):
        allocate(money("-1.00"), [Decimal(1)])


def test_allocate_zero_amount() -> None:
    assert allocate(money("0"), [Decimal(2), Decimal(5)]) == [money("0"), money("0")]


def test_allocate_all_zero_weights_rejected() -> None:
    with pytest.raises(ValueError, match="weight"):
        allocate(money("1.00"), [Decimal(0), Decimal(0)])


def test_allocate_negative_weight_rejected() -> None:
    with pytest.raises(ValueError, match="weight"):
        allocate(money("1.00"), [Decimal(1), Decimal(-1)])


cents = st.integers(min_value=0, max_value=10_000_000).map(lambda c: Decimal(c) * CENT)
weights = st.lists(
    st.integers(min_value=0, max_value=1_000_000).map(Decimal), min_size=1, max_size=12
).filter(lambda ws: any(w > 0 for w in ws))


@given(total=cents, ws=weights)
def test_allocate_always_sums_exactly(total: Decimal, ws: list[Decimal]) -> None:
    shares = allocate(total, ws)
    assert sum(shares, Decimal(0)) == total
    assert all(s == s.quantize(CENT) for s in shares)
    assert all(s == 0 for s, w in zip(shares, ws, strict=True) if w == 0)


@given(total=cents, ws=weights)
def test_allocate_shares_are_within_a_cent_of_exact(total: Decimal, ws: list[Decimal]) -> None:
    weight_sum = sum(ws, Decimal(0))
    for share, w in zip(allocate(total, ws), ws, strict=True):
        exact = total * w / weight_sum
        assert abs(share - exact) < CENT
