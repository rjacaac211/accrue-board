"""Due-date alerts and escalation for unpaid invoices waiting on a person.

An invoice held for review (or blocked, or failed) can still miss its due date. Alerts warn as
the date approaches. Once it is due, the task is escalated: reassigned to a senior reviewer by
the ``sla-monitor`` actor, audited like any other change, and only once.
"""

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from accrueboard.db.models import Document, Task, User
from accrueboard.domain.bottleneck import (
    DEFAULT_DUE_RULE,
    WAITING_ON_PEOPLE,
    DueAlert,
    DueItem,
    DueRule,
    document_date,
    due_alerts,
    needs_escalation,
)
from accrueboard.domain.documents import DocumentType
from accrueboard.domain.lifecycle import Actor
from accrueboard.services.tasks import record_event

ACTOR = "sla-monitor"
SENIOR = "senior"


def _due_date(document: Document) -> date | None:
    """The due date of an unpaid invoice (None for anything already paid or without one)."""
    extracted = document.extracted or {}
    if document.doc_type != DocumentType.INVOICE.value or extracted.get("payment_method"):
        return None
    raw = extracted.get("due_date")
    return date.fromisoformat(raw) if raw else None


def due_items(session: Session, now: datetime, client_id: str | None = None) -> list[DueItem]:
    query = (
        select(Task, Document)
        .join(Document, Document.id == Task.document_id)
        .where(Task.state.in_([s.value for s in WAITING_ON_PEOPLE]))
    )
    if client_id is not None:
        query = query.where(Task.client_id == client_id)
    items: list[DueItem] = []
    for task, document in session.execute(query).all():
        due = _due_date(document)
        if due is None:
            continue
        items.append(
            DueItem(
                task_id=task.id,
                state=task.state,
                due_date=due,
                as_of=document_date(document.received_at, task.created_at, now),
            )
        )
    return items


def alerts(
    session: Session, now: datetime, client_id: str | None = None, rule: DueRule = DEFAULT_DUE_RULE
) -> tuple[DueAlert, ...]:
    return due_alerts(due_items(session, now, client_id), rule)


def escalate_due(session: Session, now: datetime, rule: DueRule = DEFAULT_DUE_RULE) -> list[str]:
    """Reassign every task with a breached due date to a senior reviewer. Returns their ids."""
    senior = session.execute(
        select(User).where(User.role == SENIOR).order_by(User.id).limit(1)
    ).scalar_one_or_none()
    if senior is None:
        return []
    roles = dict(session.execute(select(User.id, User.role)).tuples().all())
    escalated: list[str] = []
    for alert in alerts(session, now, rule=rule):
        task = session.get(Task, alert.task_id, with_for_update=True)
        if task is None:
            continue
        is_senior = task.assignee_id is not None and roles.get(task.assignee_id) == SENIOR
        if not needs_escalation(alert, assignee_is_senior=is_senior):
            continue
        previous = task.assignee_id
        task.assignee_id = senior.id
        task.updated_at = now
        record_event(
            session,
            task,
            now=now,
            actor=ACTOR,
            actor_kind=Actor.MACHINE,
            action="escalated",
            details={
                "from": previous,
                "to": senior.id,
                "due_date": alert.due_date.isoformat(),
                "days_left": alert.days_left,
                "reason": alert.message,
            },
        )
        escalated.append(task.id)
    return escalated
