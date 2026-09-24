"""Evaluation arithmetic: proportions with confidence intervals, and the automation trade-off.

Pure functions only, so the reported numbers can be checked by unit tests.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

Z_95 = 1.959964


@dataclass(frozen=True)
class Proportion:
    """``hits`` out of ``n``, with a Wilson 95% interval (sound for small n and extreme rates)."""

    hits: int
    n: int

    @property
    def rate(self) -> float | None:
        return self.hits / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson(self.hits, self.n)

    def __str__(self) -> str:
        if not self.n:
            return "— (0)"
        low, high = wilson(self.hits, self.n) or (0.0, 0.0)
        return f"{self.hits / self.n:.1%} ({self.hits}/{self.n}; 95% CI {low:.0%} to {high:.0%})"


def wilson(hits: int, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return None
    if not 0 <= hits <= n:
        raise ValueError(f"hits must be between 0 and n ({hits} of {n})")
    p = hits / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    low = 0.0 if hits == 0 else max(0.0, centre - half)
    high = 1.0 if hits == n else min(1.0, centre + half)
    return low, high


# ---------------------------------------------------------------------------- automation


@dataclass(frozen=True)
class Candidate:
    """One document as routing saw it."""

    held_by_rule: bool
    """A hard rule fired, so it goes to a person whatever the threshold."""
    score: float
    wrong_if_posted: bool
    """Posting it as the pipeline read and coded it would put an error in the ledger."""


@dataclass(frozen=True)
class OperatingPoint:
    threshold: float
    documents: int
    auto_posted: int
    escaped: int
    """Auto-posted documents that were wrong."""
    errors: int
    """Documents that would be wrong if posted (caught or not)."""

    @property
    def automation(self) -> Proportion:
        return Proportion(self.auto_posted, self.documents)

    @property
    def escape_rate(self) -> Proportion:
        """Share of auto-posted documents that were wrong."""
        return Proportion(self.escaped, self.auto_posted)

    @property
    def errors_caught(self) -> Proportion:
        return Proportion(self.errors - self.escaped, self.errors)


def operating_point(candidates: Sequence[Candidate], threshold: float) -> OperatingPoint:
    auto = [c for c in candidates if not c.held_by_rule and c.score >= threshold]
    return OperatingPoint(
        threshold=threshold,
        documents=len(candidates),
        auto_posted=len(auto),
        escaped=sum(c.wrong_if_posted for c in auto),
        errors=sum(c.wrong_if_posted for c in candidates),
    )


def thresholds(candidates: Iterable[Candidate]) -> list[float]:
    """Every threshold at which the set of auto-posted documents changes, plus 1.0."""
    scores = {round(c.score, 6) for c in candidates if not c.held_by_rule}
    return sorted(scores | {1.0})


def sweep(candidates: Sequence[Candidate]) -> list[OperatingPoint]:
    return [operating_point(candidates, t) for t in thresholds(candidates)]


def calibrate(candidates: Sequence[Candidate], max_escape_rate: float) -> OperatingPoint:
    """The lowest threshold (most automation) whose escape rate is within the target.

    Threshold 1.0 is always acceptable in practice: it auto-posts only perfect scores, and if
    even those exceed the target the returned point says so.
    """
    points = sweep(candidates)
    for point in points:
        rate = point.escape_rate.rate
        if rate is None or rate <= max_escape_rate:
            return point
    return points[-1]


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in [0, 100])."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]
