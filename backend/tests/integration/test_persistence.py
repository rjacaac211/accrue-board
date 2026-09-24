"""Database-enforced integrity: lifecycle + audit chain, ledger, notifications, seeding."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from accrueboard.config import get_settings
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import ClientSpec, Split
from accrueboard.db.models import AuditEvent, Client, Document, Task
from accrueboard.db.session import get_sessionmaker
from accrueboard.domain.journal import JournalEntry, JournalLine
from accrueboard.domain.lifecycle import Actor, InvalidTransitionError, TaskState
from accrueboard.domain.money import money
from accrueboard.retrieval.embeddings import HashingEmbedder
from accrueboard.services import ledger
from accrueboard.services.seed import seed_client
from accrueboard.services.tasks import audit_trail, create_task, transition, verify_chain

from .conftest import unique_spec

pytestmark = pytest.mark.integration

NOW = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
EMBEDDER = HashingEmbedder(dimensions=384)


def new_task(session: Session, client_id: str, doc_id: str = "doc-1") -> Task:
    if session.get(Client, client_id) is None:
        session.add(Client(id=client_id, name="Test Co", business="testing", config={}))
        session.flush()
    document = Document(
        id=f"{client_id}-{doc_id}", client_id=client_id, filename="a.pdf", received_at=NOW
    )
    session.add(document)
    session.flush()
    return create_task(session, document, now=NOW)


# ------------------------------------------------------------------ lifecycle and audit chain


def test_transitions_are_validated_and_audited(session: Session, spec: ClientSpec) -> None:
    task = new_task(session, spec.id)
    transition(
        session,
        task,
        TaskState.PROCESSING,
        now=NOW,
        actor="worker",
        actor_kind=Actor.MACHINE,
        action="claimed",
    )
    transition(
        session,
        task,
        TaskState.NEEDS_REVIEW,
        now=NOW + timedelta(seconds=5),
        actor="pipeline",
        actor_kind=Actor.MACHINE,
        action="routed",
        details={"rules": ["first_time_vendor"], "score": 0.93},
    )
    with pytest.raises(InvalidTransitionError):
        transition(
            session,
            task,
            TaskState.APPROVED,
            now=NOW,
            actor="pipeline",
            actor_kind=Actor.MACHINE,
            action="approve",
        )
    events = audit_trail(session, task.id)
    assert [e.action for e in events] == ["received", "claimed", "routed"]
    assert [e.to_state for e in events] == ["queued", "processing", "needs_review"]
    assert events[2].details == {"rules": ["first_time_vendor"], "score": 0.93}
    assert task.state == "needs_review"
    assert verify_chain(session, task.id) == (True, None)


def test_audit_log_rejects_updates_and_deletes(session: Session, spec: ClientSpec) -> None:
    task = new_task(session, spec.id)
    event_id = audit_trail(session, task.id)[0].id
    for statement in (
        "UPDATE audit_log SET actor = 'someone-else' WHERE id = :id",
        "DELETE FROM audit_log WHERE id = :id",
    ):
        with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
            session.execute(text(statement), {"id": event_id})


def test_tampering_breaks_the_hash_chain(session: Session, spec: ClientSpec) -> None:
    task = new_task(session, spec.id)
    transition(
        session,
        task,
        TaskState.PROCESSING,
        now=NOW,
        actor="worker",
        actor_kind=Actor.MACHINE,
        action="claimed",
    )
    # Simulate someone with superuser rights bypassing the trigger.
    session.execute(text("SET LOCAL session_replication_role = replica"))
    session.execute(
        text("UPDATE audit_log SET actor = 'forged' WHERE task_id = :t AND seq = 2"),
        {"t": task.id},
    )
    session.execute(text("SET LOCAL session_replication_role = origin"))
    session.expire_all()
    assert verify_chain(session, task.id) == (False, 2)


# ------------------------------------------------------------------ ledger


def entry(debit: str, credit: str) -> JournalEntry:
    return JournalEntry.model_construct(
        entry_date=date(2026, 3, 1),
        memo="test",
        reverses=None,
        lines=(
            JournalLine.model_construct(
                account_code="6100", debit=money(debit), credit=money("0"), memo=""
            ),
            JournalLine.model_construct(
                account_code="2000", debit=money("0"), credit=money(credit), memo=""
            ),
        ),
    )


def test_balanced_entry_posts_and_reverses_to_zero(session: Session, spec: ClientSpec) -> None:
    task = new_task(session, spec.id)
    posted = ledger.post_entry(
        session, entry("100.00", "100.00"), client_id=spec.id, task_id=task.id, now=NOW
    )
    ledger.reverse(session, posted, now=NOW, entry_date=date(2026, 3, 5), task_id=task.id)
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    balances = ledger.trial_balance(session, spec.id)
    assert balances == {"2000": Decimal("0.00"), "6100": Decimal("0.00")}
    rows = ledger.entries_for_task(session, task.id)
    assert rows[1].reverses == rows[0].id


def test_database_rejects_an_unbalanced_entry(session: Session, spec: ClientSpec) -> None:
    task = new_task(session, spec.id)
    bad = entry("100.00", "99.99")
    savepoint = session.begin_nested()
    # Bypass the domain check to prove the database enforces balance on its own.
    session.add(
        ledger.EntryRow(
            id="je-bad",
            client_id=spec.id,
            task_id=task.id,
            entry_date=bad.entry_date,
            memo="bad",
            posted_at=NOW,
        )
    )
    session.flush()
    session.add_all(
        ledger.LineRow(
            entry_id="je-bad",
            line_no=i,
            account_code=line.account_code,
            debit=line.debit,
            credit=line.credit,
            memo="",
        )
        for i, line in enumerate(bad.lines, start=1)
    )
    session.flush()
    with pytest.raises(DBAPIError, match="does not balance"):
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    savepoint.rollback()


def test_posted_lines_cannot_be_edited(session: Session, spec: ClientSpec) -> None:
    task = new_task(session, spec.id)
    posted = ledger.post_entry(
        session, entry("50.00", "50.00"), client_id=spec.id, task_id=task.id, now=NOW
    )
    with pytest.raises(DBAPIError, match="append-only"), session.begin_nested():
        session.execute(
            text("UPDATE journal_lines SET debit = 60 WHERE entry_id = :e AND line_no = 1"),
            {"e": posted.id},
        )


def test_one_sided_lines_are_enforced(session: Session, spec: ClientSpec) -> None:
    task = new_task(session, spec.id)
    session.add(
        ledger.EntryRow(
            id="je-2",
            client_id=spec.id,
            task_id=task.id,
            entry_date=date(2026, 3, 1),
            memo="x",
            posted_at=NOW,
        )
    )
    session.flush()
    savepoint = session.begin_nested()
    session.add(
        ledger.LineRow(
            entry_id="je-2",
            line_no=1,
            account_code="6100",
            debit=Decimal("5.00"),
            credit=Decimal("5.00"),
            memo="",
        )
    )
    with pytest.raises(DBAPIError, match="one_side"):
        session.flush()
    savepoint.rollback()


def test_invalid_state_is_rejected(session: Session, spec: ClientSpec) -> None:
    task = new_task(session, spec.id)
    with pytest.raises(DBAPIError, match="tasks_state_valid"), session.begin_nested():
        session.execute(text("UPDATE tasks SET state = 'lost' WHERE id = :t"), {"t": task.id})


# ------------------------------------------------------------------ notifications


def raw_url() -> str:
    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")


def test_task_changes_notify_only_on_commit() -> None:
    client_id = unique_spec().id
    with psycopg.connect(raw_url(), autocommit=True) as listener:
        listener.execute("LISTEN task_events")
        maker = get_sessionmaker()
        with maker() as db, db.begin():
            new_task(db, client_id, "committed")
        with maker() as db:
            db.begin()
            new_task(db, client_id, "rolled-back")
            db.rollback()
        received = [json.loads(n.payload) for n in listener.notifies(timeout=2.0, stop_after=5)]
    mine = [p for p in received if p["client_id"] == client_id]
    assert [p["state"] for p in mine] == ["queued"]
    assert mine[0]["task_id"].startswith("task_")


# ------------------------------------------------------------------ seeding


def test_seed_imports_history_as_posted_books(
    session: Session, spec: ClientSpec, records: list[GroundTruth]
) -> None:
    summary = seed_client(session, spec, records, EMBEDDER, now=NOW)
    history = [r for r in records if r.split is Split.HISTORY]
    assert summary.created
    assert summary.documents == len(history)
    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    states = (
        session.execute(select(Task.state).where(Task.client_id == spec.id).distinct())
        .scalars()
        .all()
    )
    assert states == ["posted"]
    balances = ledger.trial_balance(session, spec.id)
    assert sum(balances.values()) == Decimal("0.00")
    assert balances["2000"] + balances["2100"] < 0  # credits to payables and card clearing
    events = (
        session.execute(select(AuditEvent.action).join(Task).where(Task.client_id == spec.id))
        .scalars()
        .all()
    )
    assert events.count("imported_posted") == len(history)
    assert seed_client(session, spec, records, EMBEDDER, now=NOW).created is False
