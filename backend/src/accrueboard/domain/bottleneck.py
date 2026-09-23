"""Bottleneck detection: tasks aging in a stage, and review queues beyond capacity.

The useful signal is how long something has been waiting, not just how many things are
waiting: one invoice sitting in review for three days matters more than ten that arrived
this morning. The current time is always passed in, so the demo can fast-forward it and
tests can pin it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from pydantic import AwareDatetime, BaseModel, ConfigDict

from accrueboard.domain.lifecycle import TaskState


class AlertLevel(StrEnum):
    WARNING = "warning"
    BREACH = "breach"


@dataclass(frozen=True)
class StageLimit:
    warning: timedelta
    breach: timedelta | None = None


DEFAULT_STAGE_LIMITS: Mapping[TaskState, StageLimit] = MappingProxyType(
    {
        TaskState.QUEUED: StageLimit(warning=timedelta(minutes=15)),
        TaskState.PROCESSING: StageLimit(warning=timedelta(minutes=10)),
        TaskState.NEEDS_REVIEW: StageLimit(warning=timedelta(hours=24), breach=timedelta(hours=72)),
        TaskState.BLOCKED: StageLimit(warning=timedelta(days=3), breach=timedelta(days=5)),
    }
)


class TaskAge(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    state: TaskState
    entered_at: AwareDatetime


class AgeAlert(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    state: TaskState
    level: AlertLevel
    age: timedelta
    limit: timedelta
    message: str


class CongestionAlert(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: TaskState
    count: int
    capacity: int
    message: str


def format_age(age: timedelta) -> str:
    """Compact human duration, e.g. '3d 4h', '5h 10m', '12m'."""
    minutes = int(age.total_seconds() // 60)
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def age_alerts(
    tasks: Iterable[TaskAge],
    *,
    now: datetime,
    limits: Mapping[TaskState, StageLimit] = DEFAULT_STAGE_LIMITS,
) -> tuple[AgeAlert, ...]:
    """Alerts for tasks that have been in their current stage too long, oldest first."""
    alerts: list[AgeAlert] = []
    for task in tasks:
        limit = limits.get(task.state)
        if limit is None:
            continue
        age = now - task.entered_at
        if limit.breach is not None and age >= limit.breach:
            level, threshold = AlertLevel.BREACH, limit.breach
        elif age >= limit.warning:
            level, threshold = AlertLevel.WARNING, limit.warning
        else:
            continue
        stage = task.state.value.replace("_", " ")
        alerts.append(
            AgeAlert(
                task_id=task.task_id,
                state=task.state,
                level=level,
                age=age,
                limit=threshold,
                message=(
                    f"{stage} for {format_age(age)} ({level.value} limit {format_age(threshold)})"
                ),
            )
        )
    return tuple(sorted(alerts, key=lambda a: a.age, reverse=True))


def review_congestion(
    *, needs_review_count: int, reviewers: int, per_reviewer_daily_capacity: int
) -> CongestionAlert | None:
    """Flag a review queue bigger than the team can clear in a day."""
    capacity = max(reviewers, 0) * per_reviewer_daily_capacity
    if needs_review_count <= capacity:
        return None
    return CongestionAlert(
        state=TaskState.NEEDS_REVIEW,
        count=needs_review_count,
        capacity=capacity,
        message=(
            f"{needs_review_count} items need review but {reviewers} reviewer(s) can clear "
            f"about {capacity} per day"
        ),
    )
