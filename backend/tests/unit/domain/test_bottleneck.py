from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from accrueboard.domain.bottleneck import (
    AlertLevel,
    TaskAge,
    age_alerts,
    review_congestion,
)
from accrueboard.domain.lifecycle import TaskState

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def aged(task_id: str, state: TaskState, age: timedelta) -> TaskAge:
    return TaskAge(task_id=task_id, state=state, entered_at=NOW - age)


def test_review_warning_after_a_day_and_breach_after_three() -> None:
    tasks = [
        aged("fresh", TaskState.NEEDS_REVIEW, timedelta(hours=23)),
        aged("warn", TaskState.NEEDS_REVIEW, timedelta(hours=30)),
        aged("breach", TaskState.NEEDS_REVIEW, timedelta(days=3, minutes=1)),
    ]
    alerts = age_alerts(tasks, now=NOW)
    assert [(a.task_id, a.level) for a in alerts] == [
        ("breach", AlertLevel.BREACH),
        ("warn", AlertLevel.WARNING),
    ]


def test_threshold_boundaries_are_inclusive() -> None:
    alerts = age_alerts([aged("t", TaskState.NEEDS_REVIEW, timedelta(hours=24))], now=NOW)
    assert [a.level for a in alerts] == [AlertLevel.WARNING]


def test_queued_and_processing_only_warn() -> None:
    tasks = [
        aged("q", TaskState.QUEUED, timedelta(hours=10)),
        aged("p", TaskState.PROCESSING, timedelta(minutes=11)),
    ]
    assert {(a.task_id, a.level) for a in age_alerts(tasks, now=NOW)} == {
        ("q", AlertLevel.WARNING),
        ("p", AlertLevel.WARNING),
    }


def test_blocked_thresholds() -> None:
    tasks = [
        aged("b1", TaskState.BLOCKED, timedelta(days=2)),
        aged("b2", TaskState.BLOCKED, timedelta(days=4)),
        aged("b3", TaskState.BLOCKED, timedelta(days=6)),
    ]
    assert [(a.task_id, a.level) for a in age_alerts(tasks, now=NOW)] == [
        ("b3", AlertLevel.BREACH),
        ("b2", AlertLevel.WARNING),
    ]


def test_settled_states_never_alert() -> None:
    tasks = [
        aged(state.value, state, timedelta(days=365))
        for state in (TaskState.POSTED, TaskState.REJECTED, TaskState.APPROVED)
    ]
    assert age_alerts(tasks, now=NOW) == ()


def test_alert_message_is_readable() -> None:
    (alert,) = age_alerts([aged("t", TaskState.NEEDS_REVIEW, timedelta(days=4))], now=NOW)
    assert alert.age == timedelta(days=4)
    assert "4d" in alert.message
    assert "needs review" in alert.message


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskAge(task_id="t", state=TaskState.QUEUED, entered_at=datetime(2026, 1, 1))  # noqa: DTZ001


def test_congestion_when_queue_exceeds_daily_capacity() -> None:
    alert = review_congestion(needs_review_count=41, reviewers=2, per_reviewer_daily_capacity=20)
    assert alert is not None
    assert alert.capacity == 40
    assert "41" in alert.message
    assert (
        review_congestion(needs_review_count=40, reviewers=2, per_reviewer_daily_capacity=20)
        is None
    )


def test_congestion_with_no_reviewers() -> None:
    alert = review_congestion(needs_review_count=1, reviewers=0, per_reviewer_daily_capacity=20)
    assert alert is not None
    assert alert.capacity == 0
