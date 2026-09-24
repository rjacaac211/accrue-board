"""Read models for the API: board cards, task detail, bottlenecks, ledger, knowledge, stats."""

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accrueboard.db.models import (
    Account,
    Client,
    Document,
    JournalEntry,
    KnowledgeRow,
    LLMCall,
    Task,
    User,
)
from accrueboard.domain.bottleneck import (
    AgeAlert,
    AlertLevel,
    CongestionAlert,
    DueAlert,
    TaskAge,
    age_alerts,
    review_congestion,
)
from accrueboard.domain.lifecycle import TaskState
from accrueboard.services import ledger, sla
from accrueboard.services.tasks import audit_trail, verify_chain

REVIEWER_DAILY_CAPACITY = 40
ACTIVE_STATES = (
    TaskState.QUEUED.value,
    TaskState.PROCESSING.value,
    TaskState.NEEDS_REVIEW.value,
    TaskState.BLOCKED.value,
    TaskState.FAILED.value,
)


class ClientSummary(BaseModel):
    id: str
    name: str
    business: str
    auto_post_threshold: float


class UserSummary(BaseModel):
    id: str
    name: str
    role: str


class AccountSummary(BaseModel):
    code: str
    name: str
    type: str
    description: str
    role: str | None


class Card(BaseModel):
    task_id: str
    state: str
    state_entered_at: datetime
    age_seconds: int
    alert: AlertLevel | None
    received_at: datetime
    filename: str
    doc_type: str | None
    vendor: str | None
    document_number: str | None
    total: Decimal | None
    score: float | None
    rules: list[str]
    summary: str | None
    assignee_id: str | None
    due: DueAlert | None = None
    """Set when an unpaid invoice waiting on a person is close to or past its due date."""


class AuditItem(BaseModel):
    seq: int
    occurred_at: datetime
    actor: str
    actor_kind: str
    action: str
    from_state: str | None
    to_state: str | None
    details: dict[str, Any]
    hash: str


class EntryLine(BaseModel):
    account_code: str
    account_name: str
    debit: Decimal
    credit: Decimal


class EntryView(BaseModel):
    id: str
    task_id: str | None
    entry_date: str
    memo: str
    reverses: str | None
    posted_at: datetime
    lines: list[EntryLine]


class CallView(BaseModel):
    purpose: str
    model: str
    prompt_version: str
    cost_usd: Decimal
    latency_ms: int
    replayed: bool


class TaskDetail(BaseModel):
    card: Card
    client_id: str
    media_type: str | None
    has_file: bool
    extracted: dict[str, Any] | None
    extraction: dict[str, Any] | None
    coding: dict[str, Any] | None
    routing: dict[str, Any] | None
    line_accounts: list[str] | None
    assistant: dict[str, Any] | None
    last_error: str | None
    attempts: int
    audit: list[AuditItem]
    audit_intact: bool
    entries: list[EntryView]
    calls: list[CallView]
    cost_usd: Decimal


class Bottlenecks(BaseModel):
    now: datetime
    alerts: list[AgeAlert]
    due: list[DueAlert]
    congestion: CongestionAlert | None
    counts: dict[str, int]


class LedgerView(BaseModel):
    entries: list[EntryView]
    trial_balance: list[dict[str, Any]]
    balanced: bool


class KnowledgeItem(BaseModel):
    id: str
    vendor_name: str
    description: str
    amount: Decimal
    account: str
    source: str
    document_ref: str | None
    created_at: datetime


class Stats(BaseModel):
    counts: dict[str, int]
    processed: int
    auto_posted: int
    human_reviewed: int
    automation_rate: float | None
    llm_cost_usd: Decimal
    llm_calls: int
    replayed_calls: int


# ---------------------------------------------------------------------------- helpers


def clients(session: Session) -> list[ClientSummary]:
    return [
        ClientSummary(
            id=c.id,
            name=c.name,
            business=c.business,
            auto_post_threshold=float((c.config or {}).get("auto_post_threshold", 0.9)),
        )
        for c in session.execute(select(Client).order_by(Client.id)).scalars()
    ]


def users(session: Session) -> list[UserSummary]:
    return [
        UserSummary(id=u.id, name=u.name, role=u.role)
        for u in session.execute(select(User).order_by(User.id)).scalars()
    ]


def accounts(session: Session, client_id: str) -> list[AccountSummary]:
    return [
        AccountSummary(
            code=a.code, name=a.name, type=a.type, description=a.description, role=a.role
        )
        for a in session.execute(
            select(Account).where(Account.client_id == client_id).order_by(Account.code)
        ).scalars()
    ]


def _alert_levels(tasks: list[Task], now: datetime) -> dict[str, AlertLevel]:
    ages = [
        TaskAge(task_id=t.id, state=TaskState(t.state), entered_at=t.state_entered_at)
        for t in tasks
    ]
    return {a.task_id: a.level for a in age_alerts(ages, now=now)}


def _card(
    task: Task,
    document: Document,
    now: datetime,
    alert: AlertLevel | None,
    due: DueAlert | None = None,
) -> Card:
    extracted = document.extracted or {}
    routing = task.routing or {}
    return Card(
        task_id=task.id,
        state=task.state,
        state_entered_at=task.state_entered_at,
        age_seconds=max(0, int((now - task.state_entered_at).total_seconds())),
        alert=alert,
        received_at=document.received_at,
        filename=document.filename,
        doc_type=document.doc_type,
        vendor=extracted.get("vendor_name"),
        document_number=extracted.get("document_number"),
        total=document.total,
        score=routing.get("score"),
        rules=[hit["rule"] for hit in routing.get("hits", [])],
        summary=routing.get("summary"),
        assignee_id=task.assignee_id,
        due=due,
    )


# ---------------------------------------------------------------------------- board


def board(
    session: Session, client_id: str, now: datetime, *, include_settled: int = 25
) -> list[Card]:
    """Active tasks plus the most recently settled ones (posted, approved, rejected)."""
    active = session.execute(
        select(Task, Document)
        .join(Document, Document.id == Task.document_id)
        .where(Task.client_id == client_id, Task.state.in_(ACTIVE_STATES))
        .order_by(Task.state_entered_at)
    ).all()
    settled = session.execute(
        select(Task, Document)
        .join(Document, Document.id == Task.document_id)
        .where(
            Task.client_id == client_id,
            Task.state.not_in(ACTIVE_STATES),
            Document.content.is_not(None),  # skip imported history
        )
        .order_by(Task.updated_at.desc())
        .limit(include_settled)
    ).all()
    rows = [*active, *settled]
    levels = _alert_levels([t for t, _ in rows], now)
    due = {a.task_id: a for a in sla.alerts(session, now, client_id)}
    return [_card(t, d, now, levels.get(t.id), due.get(t.id)) for t, d in rows]


def _entry_view(entry: JournalEntry, names: dict[str, str]) -> EntryView:
    return EntryView(
        id=entry.id,
        task_id=entry.task_id,
        entry_date=entry.entry_date.isoformat(),
        memo=entry.memo,
        reverses=entry.reverses,
        posted_at=entry.posted_at,
        lines=[
            EntryLine(
                account_code=line.account_code,
                account_name=names.get(line.account_code, ""),
                debit=line.debit,
                credit=line.credit,
            )
            for line in entry.lines
        ],
    )


def _account_names(session: Session, client_id: str) -> dict[str, str]:
    return {a.code: a.name for a in accounts(session, client_id)}


def task_detail(session: Session, task_id: str, now: datetime) -> TaskDetail | None:
    task = session.get(Task, task_id)
    if task is None:
        return None
    document = task.document
    level = _alert_levels([task], now).get(task.id)
    due = next((a for a in sla.alerts(session, now, task.client_id) if a.task_id == task.id), None)
    names = _account_names(session, task.client_id)
    calls = (
        session.execute(select(LLMCall).where(LLMCall.task_id == task.id).order_by(LLMCall.id))
        .scalars()
        .all()
    )
    intact, _ = verify_chain(session, task.id)
    return TaskDetail(
        card=_card(task, document, now, level, due),
        client_id=task.client_id,
        media_type=document.media_type,
        has_file=document.content is not None,
        extracted=document.extracted,
        extraction=task.extraction,
        coding=task.coding,
        routing=task.routing,
        line_accounts=task.line_accounts,
        assistant=task.assistant,
        last_error=task.last_error,
        attempts=task.attempts,
        audit=[
            AuditItem(
                seq=e.seq,
                occurred_at=e.occurred_at,
                actor=e.actor,
                actor_kind=e.actor_kind,
                action=e.action,
                from_state=e.from_state,
                to_state=e.to_state,
                details=e.details,
                hash=e.hash,
            )
            for e in audit_trail(session, task.id)
        ],
        audit_intact=intact,
        entries=[_entry_view(e, names) for e in ledger.entries_for_task(session, task.id)],
        calls=[
            CallView(
                purpose=c.purpose,
                model=c.model,
                prompt_version=c.prompt_version,
                cost_usd=c.cost_usd,
                latency_ms=c.latency_ms,
                replayed=c.replayed,
            )
            for c in calls
        ],
        cost_usd=sum((c.cost_usd for c in calls), Decimal(0)),
    )


def document_file(session: Session, task_id: str) -> tuple[bytes, str, str] | None:
    task = session.get(Task, task_id)
    if task is None:
        return None
    doc = task.document
    if doc.content is None:
        return None
    return doc.content, doc.media_type or "application/octet-stream", doc.filename


# ---------------------------------------------------------------------------- bottlenecks


def bottlenecks(session: Session, client_id: str, now: datetime) -> Bottlenecks:
    tasks = (
        session.execute(
            select(Task).where(Task.client_id == client_id, Task.state.in_(ACTIVE_STATES))
        )
        .scalars()
        .all()
    )
    ages = [
        TaskAge(task_id=t.id, state=TaskState(t.state), entered_at=t.state_entered_at)
        for t in tasks
    ]
    counts = Counter(t.state for t in tasks)
    reviewers = session.execute(select(func.count()).select_from(User)).scalar_one()
    return Bottlenecks(
        now=now,
        alerts=list(age_alerts(ages, now=now)),
        due=list(sla.alerts(session, now, client_id)),
        congestion=review_congestion(
            needs_review_count=counts.get(TaskState.NEEDS_REVIEW.value, 0),
            reviewers=reviewers,
            per_reviewer_daily_capacity=REVIEWER_DAILY_CAPACITY,
        ),
        counts=dict(counts),
    )


# ---------------------------------------------------------------------------- ledger and knowledge


def ledger_view(session: Session, client_id: str, *, limit: int = 50) -> LedgerView:
    names = _account_names(session, client_id)
    entries = (
        session.execute(
            select(JournalEntry)
            .where(JournalEntry.client_id == client_id)
            .order_by(JournalEntry.posted_at.desc(), JournalEntry.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    balances = ledger.trial_balance(session, client_id)
    return LedgerView(
        entries=[_entry_view(e, names) for e in entries],
        trial_balance=[
            {"account_code": code, "account_name": names.get(code, ""), "balance": str(amount)}
            for code, amount in balances.items()
        ],
        balanced=sum(balances.values(), Decimal(0)) == 0,
    )


def knowledge(
    session: Session, client_id: str, *, vendor: str | None = None, limit: int = 100
) -> list[KnowledgeItem]:
    query = select(KnowledgeRow).where(KnowledgeRow.client_id == client_id)
    if vendor:
        query = query.where(KnowledgeRow.vendor_name.ilike(f"%{vendor}%"))
    rows = session.execute(query.order_by(KnowledgeRow.seq.desc()).limit(limit)).scalars()
    return [
        KnowledgeItem(
            id=r.id,
            vendor_name=r.vendor_name,
            description=r.description,
            amount=r.amount,
            account=r.account,
            source=r.source,
            document_ref=r.document_ref,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------- stats


def stats(session: Session, client_id: str) -> Stats:
    """Processing statistics for documents that went through the pipeline (not history)."""
    rows = session.execute(
        select(Task.id, Task.state, Task.routing)
        .join(Document, Document.id == Task.document_id)
        .where(Task.client_id == client_id, Document.content.is_not(None))
    ).all()
    counts = Counter(state for _, state, _ in rows)
    routed = [r for r in rows if r[2] is not None]
    auto = sum(1 for _, _, routing in routed if routing.get("outcome") == "auto_post")
    call_totals = session.execute(
        select(
            func.coalesce(func.sum(LLMCall.cost_usd), 0),
            func.count(),
            func.count().filter(LLMCall.replayed),
        )
        .join(Task, Task.id == LLMCall.task_id)
        .where(Task.client_id == client_id)
    ).one()
    return Stats(
        counts=dict(counts),
        processed=len(routed),
        auto_posted=auto,
        human_reviewed=len(routed) - auto,
        automation_rate=auto / len(routed) if routed else None,
        llm_cost_usd=Decimal(call_totals[0]),
        llm_calls=int(call_totals[1]),
        replayed_calls=int(call_totals[2]),
    )
