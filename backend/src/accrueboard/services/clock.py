"""A clock the demo can fast-forward, shared by the API and the workers through the database.

The offset is stored in ``app_settings`` so every process agrees on "now". It is read at most
once per second per process, which keeps the clock cheap without letting processes disagree for
long after an advance.
"""

import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from accrueboard.clock import Clock, SystemClock
from accrueboard.db.models import AppSetting

OFFSET_KEY = "clock_offset_seconds"
_CACHE_SECONDS = 1.0


def read_offset(session: Session) -> timedelta:
    row = session.get(AppSetting, OFFSET_KEY)
    return timedelta(seconds=float(row.value.get("seconds", 0))) if row else timedelta(0)


def advance(session: Session, by: timedelta) -> timedelta:
    """Move the shared clock forward and return the new total offset."""
    row = session.get(AppSetting, OFFSET_KEY, with_for_update=True)
    current = float(row.value.get("seconds", 0)) if row else 0.0
    total = current + by.total_seconds()
    if row is None:
        session.add(AppSetting(key=OFFSET_KEY, value={"seconds": total}))
    else:
        row.value = {"seconds": total}
    session.flush()
    return timedelta(seconds=total)


def reset(session: Session) -> None:
    row = session.get(AppSetting, OFFSET_KEY)
    if row is not None:
        row.value = {"seconds": 0}
        session.flush()


class SharedClock:
    """Real time plus the shared demo offset."""

    def __init__(self, sessions: sessionmaker[Session], base: Clock | None = None) -> None:
        self.sessions = sessions
        self.base = base or SystemClock()
        self._offset = timedelta(0)
        self._read_at = float("-inf")

    def offset(self) -> timedelta:
        if time.monotonic() - self._read_at > _CACHE_SECONDS:
            with self.sessions() as session:
                self._offset = read_offset(session)
            self._read_at = time.monotonic()
        return self._offset

    def invalidate(self) -> None:
        self._read_at = float("-inf")

    def now(self) -> datetime:
        return self.base.now() + self.offset()
