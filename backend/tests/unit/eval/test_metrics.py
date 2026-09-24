import pytest
from hypothesis import given
from hypothesis import strategies as st

from accrueboard.eval.metrics import (
    Candidate,
    Proportion,
    calibrate,
    operating_point,
    percentile,
    sweep,
    thresholds,
    wilson,
)


def test_wilson_matches_known_values() -> None:
    low, high = wilson(8, 10) or (0, 0)
    assert low == pytest.approx(0.4902, abs=1e-4)
    assert high == pytest.approx(0.9433, abs=1e-4)
    zero = wilson(0, 20)
    assert zero is not None
    assert zero[0] == 0.0
    assert zero[1] == pytest.approx(0.1611, abs=1e-4)
    assert wilson(0, 0) is None
    with pytest.raises(ValueError, match="between 0 and n"):
        wilson(3, 2)


@given(
    st.integers(min_value=1, max_value=500).flatmap(
        lambda n: st.tuples(st.integers(0, n), st.just(n))
    )
)
def test_wilson_interval_contains_the_estimate(case: tuple[int, int]) -> None:
    hits, n = case
    low, high = wilson(hits, n) or (0, 0)
    assert 0.0 <= low <= hits / n <= high <= 1.0


def test_proportion_formatting() -> None:
    assert str(Proportion(8, 10)) == "80.0% (8/10; 95% CI 49% to 94%)"
    assert str(Proportion(0, 0)) == "— (0)"
    assert Proportion(0, 0).rate is None


CANDIDATES = [
    Candidate(held_by_rule=True, score=0.99, wrong_if_posted=True),  # a caught duplicate
    Candidate(held_by_rule=False, score=0.97, wrong_if_posted=False),
    Candidate(held_by_rule=False, score=0.97, wrong_if_posted=False),
    Candidate(held_by_rule=False, score=0.92, wrong_if_posted=True),  # a miscoded line
    Candidate(held_by_rule=False, score=0.80, wrong_if_posted=False),
    Candidate(held_by_rule=False, score=0.40, wrong_if_posted=True),
]


def test_operating_point_counts() -> None:
    point = operating_point(CANDIDATES, 0.9)
    assert (point.auto_posted, point.escaped, point.errors) == (3, 1, 3)
    assert point.automation.rate == 0.5
    assert point.escape_rate.rate == pytest.approx(1 / 3)
    assert point.errors_caught.hits == 2


def test_sweep_steps_at_each_score() -> None:
    assert thresholds(CANDIDATES) == [0.4, 0.8, 0.92, 0.97, 1.0]
    automation = [p.auto_posted for p in sweep(CANDIDATES)]
    assert automation == sorted(automation, reverse=True)
    assert automation[-1] == 0


def test_calibrate_takes_the_most_automation_within_the_target() -> None:
    assert calibrate(CANDIDATES, max_escape_rate=0.0).threshold == 0.97
    assert calibrate(CANDIDATES, max_escape_rate=0.2).threshold == 0.97
    assert calibrate(CANDIDATES, max_escape_rate=0.34).threshold == 0.8  # 1 of 4 wrong
    assert calibrate(CANDIDATES, max_escape_rate=1.0).threshold == 0.4
    everything_wrong = [Candidate(held_by_rule=False, score=1.0, wrong_if_posted=True)]
    assert calibrate(everything_wrong, max_escape_rate=0.01).threshold == 1.0


def test_percentile() -> None:
    assert percentile([], 50) is None
    assert percentile([5.0, 1.0, 3.0], 50) == 3.0
    assert percentile([float(i) for i in range(1, 101)], 95) == 95.0
