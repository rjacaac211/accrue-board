from datetime import UTC, datetime, timedelta

import pytest

from accrueboard.clock import FixedClock, SystemClock


def test_system_clock_is_timezone_aware() -> None:
    assert SystemClock().now().tzinfo is not None


def test_fixed_clock_only_moves_when_advanced() -> None:
    start = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    clock = FixedClock(start)
    assert clock.now() == start
    clock.advance(timedelta(days=3))
    assert clock.now() == start + timedelta(days=3)


def test_fixed_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 3, 1))  # noqa: DTZ001
