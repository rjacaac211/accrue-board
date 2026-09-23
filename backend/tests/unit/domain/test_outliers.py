import math
from decimal import Decimal

import pytest

from accrueboard.domain.money import money
from accrueboard.domain.outliers import (
    OutlierBasis,
    OutlierLevel,
    assess_amount,
    robust_z,
)


def amounts(*values: str) -> list[Decimal]:
    return [money(v) for v in values]


MONTHLY = amounts("480.00", "495.00", "510.00", "500.00", "520.00", "490.00")


def test_robust_z_is_zero_at_the_median() -> None:
    assert robust_z(money("500.00"), MONTHLY) == pytest.approx(0.0, abs=0.1)


def test_robust_z_uses_log_scale_and_is_symmetric_in_ratio() -> None:
    history = amounts("100.00", "90.00", "110.00", "100.00", "95.00", "105.00")
    up = robust_z(money("1000.00"), history)
    down = robust_z(money("10.00"), history)
    assert up == pytest.approx(-down, rel=1e-6)


def test_identical_history_uses_the_mad_floor() -> None:
    # A recurring 49.00 subscription: same amount again is not unusual.
    history = amounts(*["49.00"] * 6)
    assert robust_z(money("49.00"), history) == 0.0
    # A price change to 60.00 (log ratio ~0.20) scores 0.6745 * 0.2026 / 0.05 ~ 2.73.
    assert robust_z(money("60.00"), history) == pytest.approx(
        0.6745 * math.log(60 / 49) / 0.05, rel=1e-6
    )


def test_ten_times_the_usual_amount_is_a_hard_outlier() -> None:
    result = assess_amount(money("4850.00"), vendor_history=MONTHLY, account_history=[])
    assert result.level is OutlierLevel.HARD
    assert result.basis is OutlierBasis.VENDOR
    assert result.history_size == 6
    assert result.median == money("497.50")
    assert result.ratio_to_median == pytest.approx(9.75, rel=0.01)


def test_tiny_amount_is_also_an_outlier() -> None:
    assert assess_amount(money("5.00"), MONTHLY, []).level is OutlierLevel.HARD


def test_typical_amount_is_fine() -> None:
    result = assess_amount(money("505.00"), MONTHLY, [])
    assert result.level is OutlierLevel.NONE


def test_moderate_deviation_is_soft() -> None:
    history = amounts(*["49.00"] * 6)
    assert assess_amount(money("60.00"), history, []).level is OutlierLevel.SOFT


def test_falls_back_to_account_history_when_vendor_history_is_short() -> None:
    result = assess_amount(
        money("4850.00"), vendor_history=amounts("500.00", "510.00"), account_history=MONTHLY
    )
    assert result.basis is OutlierBasis.ACCOUNT
    assert result.level is OutlierLevel.HARD


def test_no_basis_without_enough_history() -> None:
    result = assess_amount(money("4850.00"), amounts("500.00"), amounts("1.00", "2.00"))
    assert result.basis is OutlierBasis.NONE
    assert result.level is OutlierLevel.NONE
    assert result.z is None


def test_non_positive_amounts_are_ignored() -> None:
    history = [*MONTHLY, money("0.00")]
    assert assess_amount(money("505.00"), history, []).history_size == 6
    assert assess_amount(money("0.00"), MONTHLY, []).level is OutlierLevel.NONE


def test_explanation_mentions_ratio() -> None:
    result = assess_amount(money("4850.00"), MONTHLY, [])
    assert "9.7" in result.explanation
