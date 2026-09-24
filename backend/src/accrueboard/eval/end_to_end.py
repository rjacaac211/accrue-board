"""End-to-end evaluation: the whole system on the validation and test splits.

How a run works:
1. A fresh database is seeded with the client's history (the only knowledge it starts with).
2. Warm-up: classification and extraction depend only on the file, so they are requested for
   every document in parallel first. The ordered pass below then replays them.
3. The validation and test documents arrive in arrival order and go through the real pipeline
   (real models, retrieval, rules and routing).
4. A simulated reviewer resolves every held document the way the ground truth says a careful
   person would:
   - rejects duplicates and non-bills
   - holds documents that need the vendor or the client
   - approves the rest with the correct values and accounts
   Approvals feed the knowledge store, as in production. Each document is scored before its
   own review, so later documents may learn from earlier ones: a test-then-train
   (prequential) evaluation.
5. After the validation split, the auto-post threshold is calibrated: the most automation whose
   auto-posted documents are at most 1% wrong. The test split then runs at that threshold, with
   the review assistant investigating every held document.

A document is "wrong to post" (as the pipeline read and coded it) if either:
- it should not be posted at all (a duplicate, a non-bill, a vendor error, sales tax on
  resale stock, an injected amount outlier); or
- its posting would differ from the correct one: booked against another vendor, or a different
  journal entry (date, accounts, debits and credits, after the capitalization rule).
"""

import hashlib
import json
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from accrueboard.agents.review_assistant.service import AssistantRun, ReviewAssistant
from accrueboard.agents.review_assistant.tools import SuggestedAction
from accrueboard.clock import FixedClock
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import ClientSpec, Split, load_anomaly_catalog
from accrueboard.db.models import Client, LLMCall, Task
from accrueboard.domain.capitalization import apply_capitalization
from accrueboard.domain.documents import DocumentType, ExtractedDocument
from accrueboard.domain.journal import JournalEntry, PostingError, build_entry
from accrueboard.domain.lifecycle import TaskState
from accrueboard.eval.metrics import (
    Candidate,
    OperatingPoint,
    Proportion,
    calibrate,
    operating_point,
    percentile,
)
from accrueboard.eval.review_assistant import expected_actions
from accrueboard.llm.client import ModelClient
from accrueboard.llm.types import LLMError
from accrueboard.pipeline.extraction import classify, extract, field_values
from accrueboard.pipeline.files import SourceFile
from accrueboard.pipeline.process import PipelineModels, Processor, ingest
from accrueboard.retrieval.embeddings import Embedder
from accrueboard.retrieval.pg_store import PgKnowledgeStore
from accrueboard.services import review
from accrueboard.services.seed import seed_client

MAX_ESCAPE_RATE = 0.01
START_THRESHOLD = 0.9
"""Auto-post threshold for the validation split, before calibration."""
REVIEWER = "u_alex"
NOT_TO_POST = frozenset(
    {
        "exact_file_duplicate",
        "renumbered_duplicate",
        "near_duplicate",
        "duplicate_credit_note",
        "unsupported_document",
        "arithmetic_error",
        "tax_on_resale_inventory",
    }
)
Progress = Callable[[str], None]


def _silent(_: str) -> None:
    pass


# ---------------------------------------------------------------------------- per document


@dataclass
class DocOutcome:
    doc_id: str
    split: str
    file_format: str
    true_type: str
    predicted_type: str | None
    state: str
    """The state the pipeline left it in (before any review)."""
    fields: dict[str, bool]
    """Per printed field: did extraction get it exactly right (only for correctly typed bills)."""
    lines: int
    lines_correct: int
    anomalies: list[str]
    injected_outlier: bool
    hard_negatives: dict[str, list[str]]
    rules: list[str]
    held_by_rule: bool
    score: float
    wrong_to_post: bool
    posting_differs: bool
    cost_usd: Decimal
    latency_ms: int
    calls: dict[str, int]
    reviewer_action: str | None = None
    assistant_action: str | None = None
    assistant_expected: list[str] = field(default_factory=list)
    assistant_error: str | None = None

    @property
    def candidate(self) -> Candidate:
        return Candidate(
            held_by_rule=self.held_by_rule, score=self.score, wrong_if_posted=self.wrong_to_post
        )


def _should_not_post(record: GroundTruth) -> bool:
    return any(
        a.type in NOT_TO_POST or (a.type == "amount_outlier" and a.injected)
        for a in record.anomalies
    )


def _entry_key(entry: JournalEntry) -> tuple[object, ...]:
    return (entry.entry_date, sorted((ln.account_code, ln.debit, ln.credit) for ln in entry.lines))


def _posting_differs(
    record: GroundTruth,
    predicted: ExtractedDocument | None,
    posted_accounts: list[str],
    spec: ClientSpec,
) -> bool:
    """Would posting the pipeline's reading put a different entry in the ledger than the truth?

    Compared: the vendor it is booked against, and the journal entry itself (date, accounts,
    debits and credits, after the capitalization rule). How the document was labelled does not
    matter if the entry is the same: an invoice already charged to the card and a card receipt
    post identically.
    """
    truth = record.document
    if predicted is None:
        return True
    if field_values(truth).get("vendor_name") != field_values(predicted).get("vendor_name"):
        return True
    correct, _ = apply_capitalization(
        truth, record.line_accounts, spec.chart, threshold=spec.capitalization_threshold
    )
    try:
        want = build_entry(truth, list(correct), spec.chart)
        got = build_entry(predicted, posted_accounts, spec.chart)
    except (PostingError, ValueError, KeyError):
        return True
    return _entry_key(want) != _entry_key(got)


def _field_results(truth: ExtractedDocument, predicted: ExtractedDocument) -> dict[str, bool]:
    want, got = field_values(truth), field_values(predicted)
    return {name: want.get(name) == got.get(name) for name in sorted(set(want) | set(got))}


def score_document(
    session: Session, record: GroundTruth, task: Task, spec: ClientSpec
) -> DocOutcome:
    """Compare what the pipeline did with one document against its ground truth."""
    truth = record.document
    extracted = task.document.extracted
    predicted = ExtractedDocument.model_validate(extracted) if extracted else None
    if task.state == TaskState.FAILED.value:
        predicted = None
    typed_right = predicted is not None and predicted.doc_type is truth.doc_type
    supported = truth.doc_type is not DocumentType.OTHER

    proposed = list((task.coding or {}).get("accounts") or [])
    lines_correct = 0
    if typed_right and predicted is not None and len(predicted.lines) == len(truth.lines):
        lines_correct = sum(p == t for p, t in zip(proposed, record.line_accounts, strict=False))

    routing: dict[str, Any] = task.routing or {}
    hits = routing.get("hits") or []
    calls = session.execute(select(LLMCall).where(LLMCall.task_id == task.id)).scalars().all()
    should_not = _should_not_post(record) or not supported
    differs = supported and _posting_differs(
        record, predicted, list(task.line_accounts or []), spec
    )
    return DocOutcome(
        doc_id=record.doc_id,
        split=record.split.value,
        file_format=record.file_format,
        true_type=truth.doc_type.value,
        predicted_type=predicted.doc_type.value if predicted else None,
        state=task.state,
        fields=_field_results(truth, predicted)
        if typed_right and supported and predicted is not None
        else {},
        lines=len(record.line_accounts) if supported else 0,
        lines_correct=lines_correct,
        anomalies=sorted({a.type for a in record.anomalies}),
        injected_outlier=any(a.type == "amount_outlier" and a.injected for a in record.anomalies),
        hard_negatives={h.type: [r.value for r in h.must_not_fire] for h in record.hard_negatives},
        rules=[h["rule"] for h in hits],
        held_by_rule=not routing or any(h["severity"] == "hard" for h in hits),
        score=float(routing.get("score", 0.0)),
        wrong_to_post=should_not or differs,
        posting_differs=differs,
        cost_usd=sum((c.cost_usd for c in calls), Decimal(0)),
        latency_ms=sum(c.latency_ms for c in calls if c.purpose != "review_assistant"),
        calls=dict(Counter(c.purpose for c in calls)),
    )


# ---------------------------------------------------------------------------- the reviewer


def simulated_review(
    session: Session, record: GroundTruth, task_id: str, store: PgKnowledgeStore, clock: FixedClock
) -> str:
    """Resolve a held document the way the ground truth says it should be resolved."""
    actions = expected_actions(record)
    now = clock.now()
    if record.document.doc_type is DocumentType.OTHER or actions == {SuggestedAction.REJECT}:
        review.reject(session, task_id, reviewer_id=REVIEWER, now=now, note="evaluation reviewer")
        return "reject"
    if actions == {SuggestedAction.BLOCK}:
        review.block(session, task_id, reviewer_id=REVIEWER, now=now, note="evaluation reviewer")
        return "block"
    try:
        review.approve(
            session,
            task_id,
            reviewer_id=REVIEWER,
            now=now,
            store=store,
            document=record.document,
            accounts=list(record.line_accounts),
            note="evaluation reviewer",
        )
    except review.ReviewError:
        review.reject(session, task_id, reviewer_id=REVIEWER, now=now, note="cannot post")
        return "reject"
    return "approve"


# ---------------------------------------------------------------------------- the run


UNBILLABLE = ("credit balance", "authentication_error", "permission_error", "invalid x-api-key")


class AccountError(RuntimeError):
    """The model API refused for account reasons; results would be meaningless."""


def _stop_if_unbillable(task: Task) -> None:
    """Stop the run instead of scoring an account problem as a pipeline failure. Every response
    recorded so far is kept, so running again resumes where this stopped at no extra cost."""
    error = (task.last_error or "").lower()
    if task.state == TaskState.FAILED.value and any(m in error for m in UNBILLABLE):
        raise AccountError(f"{task.id}: the model API refused the account: {task.last_error}")


def _set_threshold(sessions: sessionmaker[Session], client_id: str, threshold: float) -> None:
    with sessions() as session, session.begin():
        client = _client(session, client_id)
        client.config = {**client.config, "auto_post_threshold": threshold}


def _client(session: Session, client_id: str) -> Client:
    client = session.get(Client, client_id)
    if client is None:
        raise RuntimeError(f"client {client_id} was not seeded")
    return client


@dataclass
class RunResult:
    outcomes: list[DocOutcome]
    calibration: OperatingPoint | None
    threshold_used: dict[str, float]


def _sources(records: Sequence[GroundTruth], spec: ClientSpec) -> dict[str, SourceFile]:
    return {
        r.doc_id: SourceFile.from_bytes(f"{r.doc_id}.{r.file_format}", render(r, spec))
        for r in records
    }


def warm_up(
    llm: ModelClient,
    items: Sequence[tuple[GroundTruth, SourceFile]],
    models: PipelineModels,
    *,
    workers: int,
    progress: Progress = _silent,
) -> None:
    """Request classification and extraction for every distinct file, in parallel."""
    unique: dict[str, tuple[GroundTruth, SourceFile]] = {}
    for record, source in items:
        unique.setdefault(hashlib.sha256(source.data).hexdigest(), (record, source))
    done = 0
    lock = threading.Lock()

    def one(item: tuple[GroundTruth, SourceFile]) -> None:
        nonlocal done
        record, source = item
        try:
            kind = classify(llm, source, models.classify).doc_type
            if kind is not DocumentType.OTHER:
                extract(llm, source, kind, models.extraction, today=record.received_at.date())
        except LLMError as exc:
            progress(f"warm-up: {record.doc_id} failed ({exc}); the ordered pass will retry")
        with lock:
            done += 1
            if done % 25 == 0:
                progress(f"warm-up: {done}/{len(unique)} files read")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, unique.values()))


def run(
    records: Sequence[GroundTruth],
    spec: ClientSpec,
    sessions: sessionmaker[Session],
    *,
    llm: ModelClient,
    models: PipelineModels,
    assistant_model: str,
    embedder: Embedder,
    limit: int | None = None,
    workers: int = 4,
    max_escape_rate: float = MAX_ESCAPE_RATE,
    progress: Progress = _silent,
) -> RunResult:
    """Run the validation then the test split through the system. See the module docstring.

    ``sessions`` must point at an empty, migrated database. ``limit`` takes only the first
    documents of each split (for quick checks).
    """
    by_split = {
        split: sorted(
            (r for r in records if r.split is split and r.file is not None),
            key=lambda r: (r.received_at, r.doc_id),
        )[:limit]
        for split in (Split.VALIDATION, Split.TEST)
    }
    arriving = [*by_split[Split.VALIDATION], *by_split[Split.TEST]]
    sources = _sources(arriving, spec)
    progress(f"{len(arriving)} documents; warming up with {workers} workers")
    warm_up(
        llm, [(r, sources[r.doc_id]) for r in arriving], models, workers=workers, progress=progress
    )

    clock = FixedClock(arriving[0].received_at)
    with sessions() as session, session.begin():
        seed_client(session, spec, list(records), embedder, now=clock.now())
    # Validation runs at a fixed, uncalibrated starting threshold (not the product default,
    # which is itself the result of this calibration).
    _set_threshold(sessions, spec.id, START_THRESHOLD)
    processor = Processor(sessions, llm, models, embedder, clock)
    assistant = ReviewAssistant(llm, assistant_model, embedder, clock)

    outcomes: list[DocOutcome] = []
    calibration: OperatingPoint | None = None
    used: dict[str, float] = {}
    for index, record in enumerate(arriving):
        if record.split is Split.TEST and calibration is None:
            validation = [o.candidate for o in outcomes if o.split == Split.VALIDATION.value]
            calibration = calibrate(validation, max_escape_rate)
            _set_threshold(sessions, spec.id, calibration.threshold)
            processor.assistant = assistant
            progress(f"calibrated auto-post threshold: {calibration.threshold:.4f}")
        if record.received_at > clock.now():
            clock.advance(record.received_at - clock.now())
        with sessions() as session, session.begin():
            client = _client(session, spec.id)
            used[record.split.value] = float(client.config["auto_post_threshold"])
            task_id = ingest(
                session,
                spec.id,
                sources[record.doc_id],
                received_at=record.received_at,
                document_id=record.doc_id,
                task_id=f"task_{record.doc_id}",
            ).id
        claimed = processor.claim_next(spec.id)
        if claimed != task_id:
            raise RuntimeError(f"expected to claim {task_id}, got {claimed}")
        processor.process(task_id)

        with sessions() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                raise RuntimeError(f"task {task_id} disappeared")
            _stop_if_unbillable(task)
            outcome = score_document(session, record, task, spec)
            if task.assistant is not None:
                run_ = AssistantRun.model_validate(task.assistant)
                outcome.assistant_action = run_.suggestion.action.value if run_.suggestion else None
                outcome.assistant_error = run_.error
                outcome.assistant_expected = sorted(a.value for a in expected_actions(record))
            if task.state == TaskState.NEEDS_REVIEW.value:
                store = PgKnowledgeStore(session, embedder, now=clock.now)
                outcome.reviewer_action = simulated_review(session, record, task_id, store, clock)
            outcomes.append(outcome)
        if (index + 1) % 25 == 0:
            progress(f"{index + 1}/{len(arriving)} documents processed")
    return RunResult(outcomes, calibration, used)


# ---------------------------------------------------------------------------- report


def _rate(hits: int, n: int) -> dict[str, Any]:
    p = Proportion(hits, n)
    interval = p.interval
    return {"hits": hits, "n": n, "rate": p.rate, "ci": list(interval) if interval else None}


def summarize(result: RunResult, split: Split) -> dict[str, Any]:
    """Every reported number for one split, as plain data (the Markdown report reads this)."""
    docs = [o for o in result.outcomes if o.split == split.value]
    bills = [o for o in docs if o.true_type != DocumentType.OTHER.value]

    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for o in docs:
        confusion[o.true_type][o.predicted_type or "failed"] += 1

    field_hits: dict[str, list[bool]] = defaultdict(list)
    for o in bills:
        for name, ok in o.fields.items():
            key = name.split("].", 1)[-1] if name.startswith("lines[") else name
            field_hits[f"line {key}" if name.startswith("lines[") else key].append(ok)

    by_format: dict[str, list[DocOutcome]] = defaultdict(list)
    for o in bills:
        by_format[o.file_format].append(o)

    anomaly_recall: dict[str, dict[str, Any]] = {}
    expected_rule = _expected_rules()
    for kind in sorted({a for o in docs for a in o.anomalies}):
        labelled = [o for o in docs if kind in o.anomalies]
        caught = [o for o in labelled if expected_rule.get(kind) in o.rules]
        held = [o for o in labelled if o.state != TaskState.POSTED.value]
        anomaly_recall[kind] = {
            "expected_rule": expected_rule.get(kind),
            "rule_fired": _rate(len(caught), len(labelled)),
            "held": _rate(len(held), len(labelled)),
        }

    rule_precision: dict[str, dict[str, Any]] = {}
    for rule in sorted({r for o in docs for r in o.rules}):
        fired = [o for o in docs if rule in o.rules]
        true = [o for o in fired if any(expected_rule.get(a) == rule for a in o.anomalies)]
        rule_precision[rule] = _rate(len(true), len(fired))

    hard_negatives: dict[str, dict[str, Any]] = {}
    for kind in sorted({h for o in docs for h in o.hard_negatives}):
        cases = [o for o in docs if kind in o.hard_negatives]
        false_alarms = [o for o in cases if set(o.hard_negatives[kind]) & set(o.rules)]
        hard_negatives[kind] = _rate(len(false_alarms), len(cases))

    candidates = [o.candidate for o in docs]
    threshold = result.threshold_used.get(split.value, 0.9)
    points = sorted(
        {round(t, 4) for t in (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.97, 1.0, threshold)}
    )
    curve = [_point(operating_point(candidates, t)) for t in points]
    actual = operating_point(candidates, threshold)

    posted_auto = [o for o in docs if o.state == TaskState.POSTED.value]
    held = [o for o in docs if o.state in (TaskState.NEEDS_REVIEW.value, TaskState.BLOCKED.value)]
    assisted = [o for o in docs if o.assistant_expected]
    latencies = [float(o.latency_ms) for o in docs if o.latency_ms]
    cost_by_purpose: dict[str, int] = Counter()
    for o in docs:
        cost_by_purpose.update(o.calls)

    return {
        "documents": len(docs),
        "by_format": {k: len(v) for k, v in sorted(by_format.items())},
        "classification": {
            "accuracy": _rate(sum(o.predicted_type == o.true_type for o in docs), len(docs)),
            "confusion": {k: dict(v) for k, v in sorted(confusion.items())},
        },
        "extraction": {
            "documents_exact": _rate(
                sum(bool(o.fields) and all(o.fields.values()) for o in bills), len(bills)
            ),
            "by_format_exact": {
                fmt: _rate(
                    sum(bool(o.fields) and all(o.fields.values()) for o in group), len(group)
                )
                for fmt, group in sorted(by_format.items())
            },
            "fields": {
                name: _rate(sum(hits), len(hits)) for name, hits in sorted(field_hits.items())
            },
        },
        "coding": {
            "lines": _rate(sum(o.lines_correct for o in bills), sum(o.lines for o in bills)),
        },
        "anomalies": anomaly_recall,
        "rule_precision": rule_precision,
        "hard_negative_false_alarms": hard_negatives,
        "routing": {
            "threshold": threshold,
            "calibrated_on_validation": result.calibration.threshold
            if result.calibration
            else None,
            "at_threshold": _point(actual),
            "actual": {
                "auto_posted": len(posted_auto),
                "held": len(held),
                "failed": sum(o.state == TaskState.FAILED.value for o in docs),
                "auto_posted_wrong": sum(o.wrong_to_post for o in posted_auto),
            },
            "curve": curve,
        },
        "assistant": {
            "agreement": _rate(
                sum(o.assistant_action in o.assistant_expected for o in assisted), len(assisted)
            ),
            "failed": sum(o.assistant_action is None for o in assisted),
        }
        if assisted
        else None,
        "cost": {
            "total_usd": str(sum((o.cost_usd for o in docs), Decimal(0))),
            "per_document_usd": str(
                (sum((o.cost_usd for o in docs), Decimal(0)) / len(docs)).quantize(
                    Decimal("0.0001")
                )
            )
            if docs
            else None,
            "latency_ms_p50": percentile(latencies, 50),
            "latency_ms_p95": percentile(latencies, 95),
            "calls": dict(sorted(cost_by_purpose.items())),
        },
    }


def _point(point: OperatingPoint) -> dict[str, Any]:
    return {
        "threshold": point.threshold,
        "automation": _rate(point.auto_posted, point.documents),
        "escape_rate": _rate(point.escaped, point.auto_posted),
        "errors_caught": _rate(point.errors - point.escaped, point.errors),
    }


def _expected_rules() -> dict[str, str]:
    return {a.id: a.expected_rule.value for a in load_anomaly_catalog().anomalies}


def write_results(result: RunResult, out_dir: Path, *, meta: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "calibration": _point(result.calibration) if result.calibration else None,
        "validation": summarize(result, Split.VALIDATION),
        "test": summarize(result, Split.TEST),
        "documents": [asdict(o) for o in result.outcomes],
    }
    path = out_dir / "results.json"
    path.write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )
    return path
