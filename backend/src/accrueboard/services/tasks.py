"""Task lifecycle persistence: every state change is validated, audited and announced.

A transition runs inside the caller's transaction and does three things together:
1. checks the move against the lifecycle table (``domain.lifecycle``),
2. updates the task,
3. appends an audit event that is hash-chained to the task's previous event.
A database trigger then emits ``NOTIFY task_events``; Postgres delivers it only if the
transaction commits, so observers never see a change that was rolled back.
"""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from accrueboard.db.models import AuditEvent, Document, Task
from accrueboard.domain.lifecycle import Actor, TaskState, check_transition

GENESIS_HASH = "0" * 64


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _event_hash(prev_hash: str, fields: dict[str, Any]) -> str:
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


def _hash_fields(event: AuditEvent) -> dict[str, Any]:
    return {
        "task_id": event.task_id,
        "seq": event.seq,
        "occurred_at": event.occurred_at.isoformat(),
        "actor": event.actor,
        "actor_kind": event.actor_kind,
        "action": event.action,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "details": event.details,
    }


def record_event(
    session: Session,
    task: Task,
    *,
    now: datetime,
    actor: str,
    actor_kind: Actor,
    action: str,
    details: dict[str, Any] | None = None,
    from_state: TaskState | None = None,
    to_state: TaskState | None = None,
) -> AuditEvent:
    """Append an audit event to the task's hash chain (no state change by itself)."""
    last = session.execute(
        select(AuditEvent)
        .where(AuditEvent.task_id == task.id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    event = AuditEvent(
        task_id=task.id,
        seq=(last.seq + 1) if last else 1,
        occurred_at=now,
        actor=actor,
        actor_kind=actor_kind.value,
        action=action,
        from_state=from_state.value if from_state else None,
        to_state=to_state.value if to_state else None,
        # Round-trip through JSON so the stored details hash identically when re-read.
        details=json.loads(json.dumps(details or {}, sort_keys=True, default=str)),
        prev_hash=last.hash if last else GENESIS_HASH,
        hash="",
    )
    event.hash = _event_hash(event.prev_hash, _hash_fields(event))
    session.add(event)
    session.flush()
    return event


def create_task(
    session: Session,
    document: Document,
    *,
    now: datetime,
    actor: str = "intake",
    task_id: str | None = None,
    state: TaskState = TaskState.QUEUED,
) -> Task:
    task = Task(
        id=task_id or new_id("task"),
        client_id=document.client_id,
        document_id=document.id,
        state=state.value,
        state_entered_at=now,
        created_at=now,
        updated_at=now,
        attempts=0,
    )
    session.add(task)
    session.flush()
    record_event(
        session,
        task,
        now=now,
        actor=actor,
        actor_kind=Actor.MACHINE,
        action="received",
        to_state=state,
        details={"filename": document.filename, "document_id": document.id},
    )
    return task


def transition(
    session: Session,
    task: Task,
    target: TaskState,
    *,
    now: datetime,
    actor: str,
    actor_kind: Actor,
    action: str,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Move a task to ``target`` (validated against the lifecycle) and audit the change."""
    current = TaskState(task.state)
    check_transition(current, target, actor_kind)
    task.state = target.value
    task.state_entered_at = now
    task.updated_at = now
    session.flush()
    return record_event(
        session,
        task,
        now=now,
        actor=actor,
        actor_kind=actor_kind,
        action=action,
        details=details,
        from_state=current,
        to_state=target,
    )


def audit_trail(session: Session, task_id: str) -> list[AuditEvent]:
    return list(
        session.execute(
            select(AuditEvent).where(AuditEvent.task_id == task_id).order_by(AuditEvent.seq)
        ).scalars()
    )


def verify_chain(session: Session, task_id: str) -> tuple[bool, int | None]:
    """Recompute the task's hash chain. Returns (intact, first broken seq)."""
    prev = GENESIS_HASH
    for event in audit_trail(session, task_id):
        if event.prev_hash != prev or event.hash != _event_hash(prev, _hash_fields(event)):
            return False, event.seq
        prev = event.hash
    return True, None
