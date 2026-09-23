"""Injectable time source.

Every component that needs "now" takes a Clock, so tests can freeze time and the demo
can fast-forward it (e.g. to show a task aging past its review deadline).
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)  # noqa: TID251 - the one sanctioned call site


class FixedClock:
    """A clock that only moves when told to. Used in tests and demo fast-forward."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta
