"""Due-date alerts and escalation on Postgres: warn, breach, escalate once, stop when resolved."""

from datetime import UTC, datetime, time, timedelta

import pytest

from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import Split
from accrueboard.db.models import Task
from accrueboard.domain.bottleneck import AlertLevel
from accrueboard.domain.documents import DocumentType
from accrueboard.services import queries, review, sla
from accrueboard.services.tasks import audit_trail

from .helpers import World, make_world

pytestmark = pytest.mark.integration


def held_unpaid_invoice(world: World) -> GroundTruth:
    """An unpaid invoice with a due date that a hard rule holds for review."""
    return next(
        r
        for r in sorted(world.records, key=lambda r: (r.received_at, r.doc_id))
        if r.split is Split.VALIDATION
        and r.document.doc_type is DocumentType.INVOICE
        and r.document.due_date is not None
        and r.document.payment_method is None
        and any(a.type == "tax_on_resale_inventory" for a in r.anomalies)
    )


def at(day: object, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=UTC)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def world() -> World:
    return make_world("sla")


def test_due_dates_warn_breach_and_escalate_once(world: World) -> None:
    record = held_unpaid_invoice(world)
    due = record.document.due_date
    assert due is not None
    task_id = world.process(record)

    with world.sessions() as session:
        assert session.get(Task, task_id).state == "needs_review"  # type: ignore[union-attr]
        early = {a.task_id for a in sla.alerts(session, at(due - timedelta(days=10)))}
        assert task_id not in early
        soon = {a.task_id: a for a in sla.alerts(session, at(due - timedelta(days=2)))}
        assert soon[task_id].level is AlertLevel.WARNING
        assert soon[task_id].message == "due in 2 days"
        # The board card and the bottleneck view carry the alert too.
        cards = {
            c.task_id: c for c in queries.board(session, world.spec.id, at(due - timedelta(days=2)))
        }
        card_due = cards[task_id].due
        assert card_due is not None
        assert card_due.level is AlertLevel.WARNING
        view = queries.bottlenecks(session, world.spec.id, at(due))
        assert task_id in {a.task_id for a in view.due}

    overdue = at(due + timedelta(days=1))
    with world.sessions() as session, session.begin():
        assert task_id in sla.escalate_due(session, overdue)
    with world.sessions() as session, session.begin():
        assert task_id not in sla.escalate_due(session, overdue)  # already with a senior
    with world.sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.assignee_id == "u_jordan"
        assert task.state == "needs_review"  # escalation reassigns; it never decides
        event = audit_trail(session, task_id)[-1]
        assert (event.actor, event.action) == ("sla-monitor", "escalated")
        assert event.details["to"] == "u_jordan"
        assert event.details["reason"] == "overdue by 1 day"

    # Once a person resolves it, it no longer counts as at risk.
    with world.sessions() as session, session.begin():
        review.block(session, task_id, reviewer_id="u_jordan", now=overdue, note="ask vendor")
        review.unblock(session, task_id, reviewer_id="u_jordan", now=overdue)
        review.reject(session, task_id, reviewer_id="u_jordan", now=overdue, note="duplicate")
    with world.sessions() as session:
        assert task_id not in {a.task_id for a in sla.alerts(session, overdue)}


def test_paid_invoices_and_receipts_are_never_at_risk(world: World) -> None:
    paid = next(
        r
        for r in world.records
        if r.split is Split.VALIDATION
        and r.document.doc_type is DocumentType.INVOICE
        and r.document.payment_method is not None
        and any(a.type == "amount_outlier" for a in r.anomalies)
    )
    task_id = world.process(paid)
    with world.sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        far_future = at(paid.received_at.date() + timedelta(days=400))
        assert task_id not in {a.task_id for a in sla.alerts(session, far_future)}
