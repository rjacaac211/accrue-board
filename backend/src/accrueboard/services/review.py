"""Human review actions: approve (optionally with edits), reject, block, unblock, reopen, retry.

A human decision is final: it is re-validated (so a typo cannot post an unbalanced or
inconsistent document) but never re-scored by the pipeline. Every action is audited with the
reviewer's identity, and an approval records a field-by-field diff of what the reviewer changed.

The feedback loop: each approved line becomes a knowledge-store entry, marked ``confirmed`` if
the reviewer kept the proposed account or ``corrected`` if they changed it. The next similar
document retrieves it as an example, and the classifier retrains on it.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from accrueboard.db.models import Client, Task, User
from accrueboard.domain.capitalization import apply_capitalization
from accrueboard.domain.documents import DocumentType, ExtractedDocument
from accrueboard.domain.duplicates import normalize_vendor
from accrueboard.domain.journal import PostingError, build_entry
from accrueboard.domain.lifecycle import Actor, InvalidTransitionError, TaskState
from accrueboard.domain.validation import validate_document
from accrueboard.pipeline.extraction import field_values
from accrueboard.retrieval.knowledge import EntrySource, KnowledgeEntry, KnowledgeStore
from accrueboard.services import ledger
from accrueboard.services.clients import capitalization_threshold, load_chart
from accrueboard.services.seed import fingerprint_columns
from accrueboard.services.tasks import record_event, transition


class ReviewError(ValueError):
    """The requested review action is not possible as asked (the message says why)."""


@dataclass(frozen=True)
class ReviewResult:
    task_id: str
    state: TaskState
    journal_entry: str | None = None
    knowledge_entries: int = 0


def _reviewer(session: Session, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise ReviewError(f"unknown reviewer {user_id!r}")
    return user


def _task(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id, with_for_update=True)
    if task is None:
        raise ReviewError(f"unknown task {task_id!r}")
    return task


def _require(task: Task, *states: TaskState) -> None:
    if TaskState(task.state) not in states:
        allowed = ", ".join(s.value for s in states)
        raise ReviewError(f"task is {task.state}; this action needs it to be {allowed}")


def diff_documents(before: ExtractedDocument, after: ExtractedDocument) -> list[dict[str, Any]]:
    """Fields that differ between two readings of a document, with old and new values."""
    old, new = field_values(before), field_values(after)
    return [
        {"field": name, "before": _plain(old.get(name)), "after": _plain(new.get(name))}
        for name in sorted(set(old) | set(new))
        if old.get(name) != new.get(name)
    ]


def _plain(value: object) -> object:
    return value if value is None or isinstance(value, bool | int) else str(value)


def approve(
    session: Session,
    task_id: str,
    *,
    reviewer_id: str,
    now: datetime,
    store: KnowledgeStore,
    document: ExtractedDocument | None = None,
    accounts: list[str] | None = None,
    note: str = "",
) -> ReviewResult:
    """Approve a reviewed task, with optional corrections, post it, and learn from it."""
    reviewer = _reviewer(session, reviewer_id)
    task = _task(session, task_id)
    _require(task, TaskState.NEEDS_REVIEW)
    record = task.document
    client = session.get(Client, task.client_id)
    if client is None or record.extracted is None:
        raise ReviewError("task has no extracted document to approve")

    proposed = ExtractedDocument.model_validate(record.extracted)
    final = document or proposed
    if final.doc_type is DocumentType.OTHER:
        raise ReviewError("an unsupported document cannot be posted; reject it instead")
    proposed_accounts: list[str] = list((task.coding or {}).get("accounts", []))
    chosen = list(accounts) if accounts is not None else proposed_accounts
    if len(chosen) != len(final.lines):
        raise ReviewError(
            f"need one account per line ({len(final.lines)} lines, {len(chosen)} accounts)"
        )
    issues = validate_document(final, today=record.received_at.date())
    if issues:
        raise ReviewError(
            "the document still has problems: " + "; ".join(i.message for i in issues)
        )

    chart = load_chart(session, client.id)
    posted_accounts, capitalized = apply_capitalization(
        final, chosen, chart, threshold=capitalization_threshold(client)
    )
    try:
        entry = build_entry(final, posted_accounts, chart)
    except PostingError as exc:
        raise ReviewError(f"cannot post: {exc}") from exc

    changes = diff_documents(proposed, final)
    account_changes = [
        {
            "line": i,
            "before": proposed_accounts[i] if i < len(proposed_accounts) else None,
            "after": code,
        }
        for i, code in enumerate(chosen)
        if i >= len(proposed_accounts) or proposed_accounts[i] != code
    ]
    for name, value in fingerprint_columns(final).items():
        setattr(record, name, value)
    task.line_accounts = list(posted_accounts)
    transition(
        session,
        task,
        TaskState.APPROVED,
        now=now,
        actor=reviewer.id,
        actor_kind=Actor.HUMAN,
        action="approved",
        details={
            "note": note,
            "field_changes": changes,
            "account_changes": account_changes,
            "capitalized": [e.model_dump(mode="json") for e in capitalized],
        },
    )
    posted = ledger.post_entry(session, entry, client_id=client.id, task_id=task.id, now=now)
    transition(
        session,
        task,
        TaskState.POSTED,
        now=now,
        actor="ledger",
        actor_kind=Actor.MACHINE,
        action="posted",
        details={"journal_entry": posted.id, "total": str(final.total)},
    )

    learned = _learn(store, task, final, chosen, proposed_accounts)
    record_event(
        session,
        task,
        now=now,
        actor=reviewer.id,
        actor_kind=Actor.HUMAN,
        action="knowledge_updated",
        details={
            "entries": len(learned),
            "corrected": sum(e.source is EntrySource.CORRECTED for e in learned),
        },
    )
    return ReviewResult(task.id, TaskState.POSTED, posted.id, len(learned))


def _learn(
    store: KnowledgeStore,
    task: Task,
    doc: ExtractedDocument,
    accounts: list[str],
    proposed: list[str],
) -> list[KnowledgeEntry]:
    vendor_key = normalize_vendor(doc.vendor_name)
    if vendor_key is None or doc.vendor_name is None:
        return []
    entries = [
        KnowledgeEntry(
            entry_id=f"{task.id}:{i}",
            client_id=task.client_id,
            vendor_key=vendor_key,
            vendor_name=doc.vendor_name,
            description=item.description,
            amount=item.amount,
            account=account,
            source=EntrySource.CONFIRMED
            if i < len(proposed) and proposed[i] == account
            else EntrySource.CORRECTED,
            document_ref=task.document_id,
        )
        for i, (item, account) in enumerate(zip(doc.lines, accounts, strict=True))
    ]
    existing = {e.entry_id for e in store.entries(task.client_id)}
    fresh = [e for e in entries if e.entry_id not in existing]  # a reopened task re-approved
    store.add(fresh)
    return fresh


def _simple(
    session: Session,
    task_id: str,
    *,
    reviewer_id: str,
    now: datetime,
    target: TaskState,
    action: str,
    note: str,
    require_note: bool = False,
) -> ReviewResult:
    reviewer = _reviewer(session, reviewer_id)
    task = _task(session, task_id)
    if require_note and not note.strip():
        raise ReviewError(f"{action} needs a reason")
    try:
        transition(
            session,
            task,
            target,
            now=now,
            actor=reviewer.id,
            actor_kind=Actor.HUMAN,
            action=action,
            details={"note": note},
        )
    except InvalidTransitionError as exc:
        raise ReviewError(str(exc)) from exc
    return ReviewResult(task.id, target)


def reject(
    session: Session, task_id: str, *, reviewer_id: str, now: datetime, note: str
) -> ReviewResult:
    return _simple(
        session,
        task_id,
        reviewer_id=reviewer_id,
        now=now,
        target=TaskState.REJECTED,
        action="rejected",
        note=note,
        require_note=True,
    )


def block(
    session: Session, task_id: str, *, reviewer_id: str, now: datetime, note: str
) -> ReviewResult:
    return _simple(
        session,
        task_id,
        reviewer_id=reviewer_id,
        now=now,
        target=TaskState.BLOCKED,
        action="blocked",
        note=note,
        require_note=True,
    )


def unblock(
    session: Session, task_id: str, *, reviewer_id: str, now: datetime, note: str = ""
) -> ReviewResult:
    return _simple(
        session,
        task_id,
        reviewer_id=reviewer_id,
        now=now,
        target=TaskState.NEEDS_REVIEW,
        action="unblocked",
        note=note,
    )


def retry(
    session: Session, task_id: str, *, reviewer_id: str, now: datetime, note: str = ""
) -> ReviewResult:
    return _simple(
        session,
        task_id,
        reviewer_id=reviewer_id,
        now=now,
        target=TaskState.QUEUED,
        action="retried",
        note=note,
    )


def reopen(
    session: Session, task_id: str, *, reviewer_id: str, now: datetime, note: str
) -> ReviewResult:
    """Reverse a posted document's journal entry and send it back to review."""
    reviewer = _reviewer(session, reviewer_id)
    task = _task(session, task_id)
    _require(task, TaskState.POSTED)
    if not note.strip():
        raise ReviewError("reopening needs a reason")
    entries = ledger.entries_for_task(session, task.id)
    reversed_ids = {e.reverses for e in entries if e.reverses}
    open_entries = [e for e in entries if e.reverses is None and e.id not in reversed_ids]
    reversals = [
        ledger.reverse(session, e, now=now, entry_date=now.date(), task_id=task.id).id
        for e in open_entries
    ]
    transition(
        session,
        task,
        TaskState.NEEDS_REVIEW,
        now=now,
        actor=reviewer.id,
        actor_kind=Actor.HUMAN,
        action="reopened",
        details={"note": note, "reversing_entries": reversals},
    )
    return ReviewResult(task.id, TaskState.NEEDS_REVIEW, reversals[0] if reversals else None)


def assign(
    session: Session, task_id: str, *, reviewer_id: str, assignee_id: str | None, now: datetime
) -> ReviewResult:
    reviewer = _reviewer(session, reviewer_id)
    task = _task(session, task_id)
    if assignee_id is not None:
        _reviewer(session, assignee_id)
    previous = task.assignee_id
    task.assignee_id = assignee_id
    task.updated_at = now
    record_event(
        session,
        task,
        now=now,
        actor=reviewer.id,
        actor_kind=Actor.HUMAN,
        action="assigned",
        details={"from": previous, "to": assignee_id},
    )
    return ReviewResult(task.id, TaskState(task.state))
