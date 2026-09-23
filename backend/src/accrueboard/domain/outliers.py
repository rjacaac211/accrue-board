"""Amount-outlier detection with a robust z-score on log amounts.

    z = 0.6745 * (ln(x) - median(ln h)) / max(MAD(ln h), MAD_FLOOR)

- Logs make the score depend on *ratios* (10x the usual is as unusual as 1/10th), which suits
  right-skewed money amounts.
- Median and MAD (median absolute deviation) are not pulled around by the outliers themselves,
  unlike mean and standard deviation. 0.6745 scales MAD to a standard-deviation equivalent
  (Iglewicz and Hoaglin's modified z-score).
- The MAD floor stops identical histories (a fixed monthly subscription) from making any
  change infinitely unusual: with the floor at 0.05, a ~20% price change scores about 2.5
  (soft signal) while a 10x amount scores above 30.
- The vendor's own history is used when it has at least MIN_HISTORY amounts, otherwise the
  history of the account the document is coded to. With neither, no judgement is made; a new
  vendor is handled by its own rule.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from accrueboard.domain.money import Money, round_money


class OutlierBasis(StrEnum):
    VENDOR = "vendor"
    ACCOUNT = "account"
    NONE = "none"


class OutlierLevel(StrEnum):
    NONE = "none"
    SOFT = "soft"
    HARD = "hard"


@dataclass(frozen=True)
class OutlierConfig:
    min_history: int = 5
    mad_floor: float = 0.05
    soft_z: float = 2.5
    hard_z: float = 3.5


DEFAULT_OUTLIER = OutlierConfig()
_MAD_SCALE = 0.6745


class OutlierAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    basis: OutlierBasis
    level: OutlierLevel
    z: float | None
    history_size: int
    median: Money | None
    ratio_to_median: float | None
    explanation: str


def _positive_logs(values: Sequence[Decimal]) -> list[float]:
    return [math.log(float(v)) for v in values if v > 0]


def robust_z(
    value: Decimal, history: Sequence[Decimal], *, mad_floor: float = DEFAULT_OUTLIER.mad_floor
) -> float:
    """Modified z-score of ``value`` against ``history`` on the log scale."""
    logs = _positive_logs(history)
    if not logs or value <= 0:
        raise ValueError("robust_z needs a positive value and at least one positive history amount")
    center = statistics.median(logs)
    mad = statistics.median(abs(x - center) for x in logs)
    return _MAD_SCALE * (math.log(float(value)) - center) / max(mad, mad_floor)


def assess_amount(
    total: Decimal,
    vendor_history: Sequence[Decimal],
    account_history: Sequence[Decimal],
    config: OutlierConfig = DEFAULT_OUTLIER,
) -> OutlierAssessment:
    """Judge whether a document total is unusual for this vendor (or, failing that, account)."""
    vendor = [v for v in vendor_history if v > 0]
    account = [v for v in account_history if v > 0]
    if len(vendor) >= config.min_history:
        basis, history = OutlierBasis.VENDOR, vendor
    elif len(account) >= config.min_history:
        basis, history = OutlierBasis.ACCOUNT, account
    else:
        return OutlierAssessment(
            basis=OutlierBasis.NONE,
            level=OutlierLevel.NONE,
            z=None,
            history_size=max(len(vendor), len(account)),
            median=None,
            ratio_to_median=None,
            explanation=f"not enough history to judge (fewer than {config.min_history} amounts)",
        )

    if total <= 0:
        return OutlierAssessment(
            basis=basis,
            level=OutlierLevel.NONE,
            z=None,
            history_size=len(history),
            median=None,
            ratio_to_median=None,
            explanation="non-positive total is not assessed",
        )

    z = robust_z(total, history, mad_floor=config.mad_floor)
    median = round_money(Decimal(str(statistics.median(history))))
    ratio = float(total) / float(median)
    if abs(z) > config.hard_z:
        level = OutlierLevel.HARD
    elif abs(z) > config.soft_z:
        level = OutlierLevel.SOFT
    else:
        level = OutlierLevel.NONE
    return OutlierAssessment(
        basis=basis,
        level=level,
        z=z,
        history_size=len(history),
        median=median,
        ratio_to_median=ratio,
        explanation=(
            f"{total} is {ratio:.2f}x the {basis.value} median of {median} "
            f"over {len(history)} documents (robust z = {z:.1f})"
        ),
    )
