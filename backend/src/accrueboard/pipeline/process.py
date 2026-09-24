"""The document pipeline: intake, claiming, processing and routing of tasks.

    ingest ──> Queued ──claim──> Processing ──> classify ─> extract ─> code ─> capitalize
                                                ─> validate, duplicates, outliers, rules
                                                ─> decide ──> AutoApproved ─> post ─> Posted
                                                          └─> NeedsReview ─> review assistant
    (any unexpected error)                      ─> Failed

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so several workers can run side by side
without taking the same task, and a lease so a task held by a crashed worker is re-queued.
Each task is processed in a single transaction: its results, audit events, journal entry and
model-call records are committed together or not at all. A document held for review is then
investigated by the review assistant (if configured) in a transaction of its own, so a failed
investigation never undoes the pipeline's work.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accrueboard.clock import Clock
from accrueboard.db.models import Client, Document, LLMCall, Task
from accrueboard.domain.accounts import AccountRole
from accrueboard.domain.capitalization import (
    apply_capitalization,
)
from accrueboard.domain.documents import DocumentType, ExtractedDocument
from accrueboard.domain.duplicates import Fingerprint, find_duplicates
from accrueboard.domain.journal import PostingError, build_entry
from accrueboard.domain.lifecycle import Actor, TaskState
from accrueboard.domain.outliers import OutlierAssessment, assess_amount
from accrueboard.domain.routing import (
    Outcome,
    RoutingDecision,
    RoutingInput,
    decide,
)
from accrueboard.domain.validation import validate_document
from accrueboard.llm.client import LLMClient
from accrueboard.pipeline.coding import ClientContext, Coder, CodingResult
from accrueboard.pipeline.extraction import (
    CallRecord,
    ExtractionResult,
    Models,
    classify,
    extract,
)
from accrueboard.pipeline.files import SourceFile
from accrueboard.retrieval.embeddings import Embedder
from accrueboard.retrieval.pg_store import PgKnowledgeStore
from accrueboard.services import ledger
from accrueboard.services.clients import capitalization_threshold, load_chart, routing_config
from accrueboard.services.seed import fingerprint_columns
from accrueboard.services.tasks import create_task, new_id, transition

if TYPE_CHECKING:
    from accrueboard.agents.review_assistant.service import ReviewAssistant

log = logging.getLogger(__name__)

LEASE = timedelta(minutes=10)
PIPELINE = "pipeline"
WORKER = "worker"
BILL_TYPES = (DocumentType.INVOICE.value, DocumentType.RECEIPT.value)
SETTLED_STATES = (TaskState.POSTED.value, TaskState.APPROVED.value)
"""Documents whose amounts count as the client's history (for outliers and credit references)."""


@dataclass(frozen=True)
class PipelineModels:
    classify: str
    extract: str
    verify: str
    code: str

    @property
    def extraction(self) -> Models:
        return Models(classify=self.classify, extract=self.extract, verify=self.verify)


@dataclass(frozen=True)
class ProcessOutcome:
    task_id: str
    state: TaskState
    summary: str


# ---------------------------------------------------------------------------- intake


def ingest(
    session: Session,
    client_id: str,
    source: SourceFile,
    *,
    received_at: datetime,
    document_id: str | None = None,
    task_id: str | None = None,
) -> Task:
    """Store an incoming file and queue a task for it."""
    if session.get(Client, client_id) is None:
        raise ValueError(f"unknown client {client_id!r}")
    document = Document(
        id=document_id or new_id("doc"),
        client_id=client_id,
        filename=source.name,
        media_type=source.media_type,
        sha256=source.sha256,
        content=source.data,
        received_at=received_at,
    )
    session.add(document)
    session.flush()
    return create_task(session, document, now=received_at, task_id=task_id)


# ---------------------------------------------------------------------------- history lookups


def _fingerprints(session: Session, client_id: str, exclude: str) -> list[Fingerprint]:
    rows = session.execute(
        select(Document, Task.state)
        .join(Task, Task.document_id == Document.id)
        .where(
            Document.client_id == client_id,
            Document.id != exclude,
            Document.doc_type.is_not(None),
            Task.state.not_in((TaskState.REJECTED.value, TaskState.FAILED.value)),
        )
        .order_by(Document.received_at, Document.id)
    ).all()
    return [
        Fingerprint(
            doc_id=doc.id,
            file_sha256=doc.sha256 or f"none:{doc.id}",
            doc_type=DocumentType(doc.doc_type),
            vendor_key=doc.vendor_key,
            number_key=doc.number_key,
            reference_key=doc.reference_key,
            total=doc.total,
            issue_date=doc.issue_date,
        )
        for doc, _ in rows
    ]


def _settled_bills(session: Session, client_id: str, exclude: str) -> list[tuple[Document, Task]]:
    return [
        (doc, task)
        for doc, task in session.execute(
            select(Document, Task)
            .join(Task, Task.document_id == Document.id)
            .where(
                Document.client_id == client_id,
                Document.id != exclude,
                Document.doc_type.in_(BILL_TYPES),
                Task.state.in_(SETTLED_STATES),
            )
        ).all()
    ]


def _main_account(doc: ExtractedDocument, accounts: tuple[str, ...] | list[str]) -> str | None:
    if not doc.lines:
        return None
    largest = max(range(len(doc.lines)), key=lambda i: doc.lines[i].amount)
    return accounts[largest]


def _outlier(
    doc: ExtractedDocument,
    vendor_key: str | None,
    accounts: tuple[str, ...],
    settled: list[tuple[Document, Task]],
) -> OutlierAssessment | None:
    if doc.total is None or doc.doc_type.value not in BILL_TYPES:
        return None
    main = _main_account(doc, accounts)
    vendor_totals = [d.total for d, _ in settled if d.total and d.vendor_key == vendor_key]
    account_totals: list[Decimal] = []
    for d, t in settled:
        if d.total and d.extracted and t.line_accounts:
            other = ExtractedDocument.model_validate(d.extracted)
            if _main_account(other, t.line_accounts) == main:
                account_totals.append(d.total)
    return assess_amount(doc.total, vendor_totals, account_totals)


# ---------------------------------------------------------------------------- processor


class Processor:
    """Runs the pipeline over queued tasks. One instance per worker process."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        llm: LLMClient,
        models: PipelineModels,
        embedder: Embedder,
        clock: Clock,
        *,
        assistant: "ReviewAssistant | None" = None,
    ) -> None:
        self.sessions = sessions
        self.assistant = assistant
        self.llm = llm
        self.models = models
        self.embedder = embedder
        self.clock = clock
        self._coders: dict[str, tuple[int, Coder]] = {}

    # ------------------------------------------------------------------ claiming

    def claim_next(self, client_id: str | None = None) -> str | None:
        """Take the oldest queued task (skipping ones other workers hold) and mark it processing."""
        with self.sessions() as session, session.begin():
            query = select(Task).where(Task.state == TaskState.QUEUED.value)
            if client_id is not None:
                query = query.where(Task.client_id == client_id)
            task = session.execute(
                query.order_by(Task.created_at, Task.id).with_for_update(skip_locked=True).limit(1)
            ).scalar_one_or_none()
            if task is None:
                return None
            now = self.clock.now()
            task.attempts += 1
            task.lease_until = now + LEASE
            transition(
                session,
                task,
                TaskState.PROCESSING,
                now=now,
                actor=WORKER,
                actor_kind=Actor.MACHINE,
                action="claimed",
                details={"attempt": task.attempts},
            )
            return task.id

    def requeue_expired(self) -> int:
        """Return tasks whose worker lease ran out to the queue."""
        now = self.clock.now()
        with self.sessions() as session, session.begin():
            stale = (
                session.execute(
                    select(Task)
                    .where(Task.state == TaskState.PROCESSING.value, Task.lease_until < now)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            for task in stale:
                transition(
                    session,
                    task,
                    TaskState.QUEUED,
                    now=now,
                    actor=WORKER,
                    actor_kind=Actor.MACHINE,
                    action="lease_expired",
                )
            return len(stale)

    def run_once(self, client_id: str | None = None) -> ProcessOutcome | None:
        task_id = self.claim_next(client_id)
        return None if task_id is None else self.process(task_id)

    def drain(self, client_id: str | None = None, limit: int | None = None) -> list[ProcessOutcome]:
        outcomes: list[ProcessOutcome] = []
        while limit is None or len(outcomes) < limit:
            outcome = self.run_once(client_id)
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes

    # ------------------------------------------------------------------ processing

    def process(self, task_id: str) -> ProcessOutcome:
        outcome = self._run_pipeline(task_id)
        if outcome.state is TaskState.NEEDS_REVIEW and self.assistant is not None:
            self.investigate(task_id)
        return outcome

    def investigate(self, task_id: str) -> None:
        """Run the review assistant on a task; a failure is logged, never raised."""
        if self.assistant is None:
            return
        try:
            with self.sessions() as session, session.begin():
                self.assistant.run(session, task_id)
        except Exception:
            log.exception("review assistant crashed on task %s", task_id)

    def _run_pipeline(self, task_id: str) -> ProcessOutcome:
        try:
            with self.sessions() as session, session.begin():
                return self._process(session, task_id)
        except Exception as exc:
            log.exception("task %s failed", task_id)
            with self.sessions() as session, session.begin():
                task = session.get(Task, task_id)
                if task is None:
                    raise
                error = f"{type(exc).__name__}: {exc}"[:2000]
                task.last_error = error
                transition(
                    session,
                    task,
                    TaskState.FAILED,
                    now=self.clock.now(),
                    actor=PIPELINE,
                    actor_kind=Actor.MACHINE,
                    action="failed",
                    details={"error": error},
                )
                return ProcessOutcome(task_id, TaskState.FAILED, error)

    def _coder(self, session: Session, context: ClientContext) -> Coder:
        store = PgKnowledgeStore(session, self.embedder, now=self.clock.now)
        size = len(store.entries(context.client_id))
        cached = self._coders.get(context.client_id)
        if cached is None or cached[0] != size:
            coder = Coder(store, self.llm, self.models.code, context)
            self._coders[context.client_id] = (size, coder)
            return coder
        coder = cached[1]
        coder.store = store  # bind to this transaction's session
        return coder

    def _process(self, session: Session, task_id: str) -> ProcessOutcome:
        task = session.get(Task, task_id)
        if task is None or task.state != TaskState.PROCESSING.value:
            raise ValueError(f"task {task_id} is not being processed")
        document = task.document
        client = session.get(Client, task.client_id)
        if client is None or document.content is None or document.media_type is None:
            raise ValueError(f"task {task_id}: document has no file content to process")
        chart = load_chart(session, client.id)
        context = ClientContext(client.id, client.name, client.business, chart)
        source = SourceFile.from_bytes(document.filename, document.content)
        today = document.received_at.date()
        calls: list[CallRecord] = []

        classification = classify(self.llm, source, self.models.classify)
        calls.append(classification.call)
        doc_type = classification.doc_type

        extraction: ExtractionResult | None = None
        coding: CodingResult | None = None
        accounts: tuple[str, ...] = ()
        capitalized: list[dict[str, Any]] = []
        if doc_type is DocumentType.OTHER:
            doc = ExtractedDocument(doc_type=doc_type)
        else:
            extraction = extract(self.llm, source, doc_type, self.models.extraction, today=today)
            calls += extraction.calls
            doc = extraction.document
            coding = self._coder(session, context).code(doc)
            if coding.call is not None:
                calls.append(coding.call)
            threshold = capitalization_threshold(client)
            accounts, events = apply_capitalization(
                doc, coding.accounts, chart, threshold=threshold
            )
            capitalized = [e.model_dump(mode="json") for e in events]

        for name, value in fingerprint_columns(doc).items():
            setattr(document, name, value)
        session.flush()

        decision = self._decide(
            session,
            client=client,
            document=document,
            doc=doc,
            extraction=extraction,
            coding=coding,
            accounts=accounts,
        )
        task.extraction = extraction.model_dump(mode="json") if extraction else None
        task.coding = coding.model_dump(mode="json") if coding else None
        task.routing = decision.model_dump(mode="json")
        task.line_accounts = list(accounts)
        task.lease_until = None
        self._record_calls(session, task.id, calls)

        now = self.clock.now()
        details = {
            "doc_type": doc_type.value,
            "classification_evidence": classification.evidence,
            "summary": decision.summary,
            "score": decision.score,
            "threshold": decision.threshold,
            "rules": [h.rule.value for h in decision.hits],
            "capitalized": capitalized,
            "cost_usd": str(sum((c.cost_usd for c in calls), Decimal(0))),
        }
        if decision.outcome is Outcome.NEEDS_REVIEW:
            transition(
                session,
                task,
                TaskState.NEEDS_REVIEW,
                now=now,
                actor=PIPELINE,
                actor_kind=Actor.MACHINE,
                action="routed_to_review",
                details=details,
            )
            return ProcessOutcome(task.id, TaskState.NEEDS_REVIEW, decision.summary)

        transition(
            session,
            task,
            TaskState.AUTO_APPROVED,
            now=now,
            actor=PIPELINE,
            actor_kind=Actor.MACHINE,
            action="auto_approved",
            details=details,
        )
        try:
            entry = ledger.post_entry(
                session,
                build_entry(doc, accounts, chart),
                client_id=client.id,
                task_id=task.id,
                now=now,
            )
        except PostingError as exc:
            transition(
                session,
                task,
                TaskState.NEEDS_REVIEW,
                now=now,
                actor=PIPELINE,
                actor_kind=Actor.MACHINE,
                action="posting_refused",
                details={"error": str(exc)},
            )
            return ProcessOutcome(task.id, TaskState.NEEDS_REVIEW, f"posting refused: {exc}")
        transition(
            session,
            task,
            TaskState.POSTED,
            now=now,
            actor=PIPELINE,
            actor_kind=Actor.MACHINE,
            action="posted",
            details={"journal_entry": entry.id, "total": str(doc.total)},
        )
        return ProcessOutcome(task.id, TaskState.POSTED, decision.summary)

    def _decide(
        self,
        session: Session,
        *,
        client: Client,
        document: Document,
        doc: ExtractedDocument,
        extraction: ExtractionResult | None,
        coding: CodingResult | None,
        accounts: tuple[str, ...],
    ) -> RoutingDecision:
        chart = load_chart(session, client.id)
        candidate = Fingerprint.from_document(
            document.id, document.sha256 or f"none:{document.id}", doc
        )
        duplicates = find_duplicates(candidate, _fingerprints(session, client.id, document.id))
        settled = _settled_bills(session, client.id, document.id)
        outlier = _outlier(doc, candidate.vendor_key, accounts, settled)
        reference_found: bool | None = None
        if doc.doc_type is DocumentType.CREDIT_NOTE:
            reference_found = any(
                d.vendor_key == candidate.vendor_key and d.number_key == candidate.reference_key
                for d, _ in settled
            )
        routing_input = RoutingInput(
            doc=doc,
            validation_issues=validate_document(doc, today=document.received_at.date())
            if doc.doc_type is not DocumentType.OTHER
            else (),
            ungrounded_fields=extraction.ungrounded if extraction else frozenset(),
            duplicates=duplicates,
            outlier=outlier,
            line_accounts=accounts,
            inventory_account=chart.role(AccountRole.INVENTORY),
            vendor_known=coding.vendor_known if coding else False,
            credit_reference_found=reference_found,
            extraction_confidence=extraction.confidence if extraction else 0.0,
            coding_confidence=coding.confidence if coding else 0.0,
        )
        return decide(routing_input, routing_config(client))

    def _record_calls(self, session: Session, task_id: str, calls: list[CallRecord]) -> None:
        now = self.clock.now()
        session.add_all(
            LLMCall(
                task_id=task_id,
                purpose=c.purpose,
                prompt_version=c.prompt_version,
                model=c.model,
                request_key=c.request_key,
                cost_usd=c.cost_usd,
                latency_ms=c.latency_ms,
                input_tokens=c.input_tokens,
                output_tokens=c.output_tokens,
                replayed=c.replayed,
                created_at=now,
            )
            for c in calls
        )
