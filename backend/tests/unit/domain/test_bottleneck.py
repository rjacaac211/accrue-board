from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from accrueboard.domain.bottleneck import (
    AlertLevel,
    DueItem,
    DueRule,
    TaskAge,
    age_alerts,
    document_date,
    due_alerts,
    needs_escalation,
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


# ------------------------------------------------------------------ due dates


def due(task_id: str, days_left: int, state: TaskState = TaskState.NEEDS_REVIEW) -> DueItem:
    today = NOW.date()
    return DueItem(
        task_id=task_id, state=state, due_date=today + timedelta(days=days_left), as_of=today
    )


def test_due_soon_warns_and_due_today_or_overdue_breaches() -> None:
    items = [due("later", 5), due("soon", 3), due("today", 0), due("late", -4)]
    alerts = due_alerts(items)
    assert [(a.task_id, a.level, a.days_left) for a in alerts] == [
        ("late", AlertLevel.BREACH, -4),
        ("today", AlertLevel.BREACH, 0),
        ("soon", AlertLevel.WARNING, 3),
    ]
    assert alerts[0].message == "overdue by 4 days"
    assert alerts[1].message == "due today"
    assert alerts[2].message == "due in 3 days"


def test_due_rules_are_configurable_and_cover_waiting_states_only() -> None:
    rule = DueRule(warn_days=7, breach_days=2)
    items = [
        due("a", 6),
        due("b", 2),
        due("c", 1, TaskState.BLOCKED),
        due("d", -1, TaskState.POSTED),
    ]
    assert [(a.task_id, a.level) for a in due_alerts(items, rule)] == [
        ("c", AlertLevel.BREACH),
        ("b", AlertLevel.BREACH),
        ("a", AlertLevel.WARNING),
    ]
    with pytest.raises(ValueError, match="warn"):
        DueRule(warn_days=1, breach_days=2)


def test_document_clock_keeps_the_time_since_arrival() -> None:
    # Received on 1 March, queued (in a replay) on 15 June; 36 hours later it is 2 March
    # plus a bit on the document's clock, whatever today's calendar says.
    received = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    queued = NOW
    assert document_date(received, queued, NOW + timedelta(hours=36)).isoformat() == "2026-03-02"
    assert document_date(received, queued, queued) == received.date()
    # Normal operation: received and queued together, so it is simply today.
    assert document_date(NOW, NOW, NOW + timedelta(days=3)) == (NOW + timedelta(days=3)).date()


def test_escalation_goes_to_a_senior_once() -> None:
    breach = due_alerts([due("late", -1)])[0]
    warning = due_alerts([due("soon", 2)])[0]
    assert needs_escalation(breach, assignee_is_senior=False)
    assert not needs_escalation(breach, assignee_is_senior=True)
    assert not needs_escalation(warning, assignee_is_senior=False)
