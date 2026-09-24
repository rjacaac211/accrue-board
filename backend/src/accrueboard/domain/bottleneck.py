"""Bottleneck detection: tasks aging in a stage, review queues beyond capacity, and unpaid
invoices whose due date is close while they wait on a person.

The useful signal is how long something has been waiting, not just how many things are
waiting: one invoice sitting in review for three days matters more than ten that arrived
this morning, and an invoice due tomorrow matters more than either. The current time is always
passed in, so the demo can fast-forward it and tests can pin it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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


# ---------------------------------------------------------------------------- due dates

WAITING_ON_PEOPLE = frozenset({TaskState.NEEDS_REVIEW, TaskState.BLOCKED, TaskState.FAILED})
"""States in which an unpaid invoice can miss its due date because nobody has acted."""


@dataclass(frozen=True)
class DueRule:
    warn_days: int = 3
    """Warn when the due date is this many days away or fewer."""
    breach_days: int = 0
    """Breach (and escalate) when this many days or fewer are left: 0 means due today."""

    def __post_init__(self) -> None:
        if self.warn_days < self.breach_days:
            raise ValueError("warn_days must be at least breach_days")


DEFAULT_DUE_RULE = DueRule()


class DueItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    state: TaskState
    due_date: date
    as_of: date
    """Today on the document's clock (see ``document_date``)."""


class DueAlert(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    state: TaskState
    level: AlertLevel
    due_date: date
    days_left: int
    message: str


def document_date(received_at: datetime, queued_at: datetime, now: datetime) -> date:
    """Today as the document experiences it: its arrival plus the time since it was queued.

    Normally a document is queued when it arrives, so this is simply today. A replayed or demo
    document keeps its original arrival date, and its due date is judged by the time that has
    passed since it entered the queue, not by how old the paper is.
    """
    return (received_at + (now - queued_at)).date()


def _due_message(days_left: int) -> str:
    if days_left < 0:
        late = -days_left
        return f"overdue by {late} day{'s' if late != 1 else ''}"
    if days_left == 0:
        return "due today"
    return f"due in {days_left} day{'s' if days_left != 1 else ''}"


def due_alerts(items: Iterable[DueItem], rule: DueRule = DEFAULT_DUE_RULE) -> tuple[DueAlert, ...]:
    """Unpaid invoices waiting on a person with a due date close or past, most urgent first."""
    alerts: list[DueAlert] = []
    for item in items:
        if item.state not in WAITING_ON_PEOPLE:
            continue
        days_left = (item.due_date - item.as_of).days
        if days_left <= rule.breach_days:
            level = AlertLevel.BREACH
        elif days_left <= rule.warn_days:
            level = AlertLevel.WARNING
        else:
            continue
        alerts.append(
            DueAlert(
                task_id=item.task_id,
                state=item.state,
                level=level,
                due_date=item.due_date,
                days_left=days_left,
                message=_due_message(days_left),
            )
        )
    return tuple(sorted(alerts, key=lambda a: (a.days_left, a.task_id)))


def needs_escalation(alert: DueAlert, *, assignee_is_senior: bool) -> bool:
    """A breached due date goes to a senior reviewer, once."""
    return alert.level is AlertLevel.BREACH and not assignee_is_senior
