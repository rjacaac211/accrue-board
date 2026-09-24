"""End to end on real Postgres: seeded history, ingested documents, processed by the pipeline.

The model is a fake that reads documents perfectly (answers from ground truth, keyed by file
hash), so this test checks the plumbing and the routing rules, not model quality.
"""

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from accrueboard.clock import FixedClock
from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import ClientSpec, Split, load_anomaly_catalog, load_client
from accrueboard.db.models import JournalEntry, LLMCall, Task
from accrueboard.db.session import get_engine
from accrueboard.llm.client import FakeLLM
from accrueboard.llm.types import FilePart, LLMError, LLMRequest
from accrueboard.pipeline.files import SourceFile
from accrueboard.pipeline.process import LEASE, PipelineModels, Processor, ingest
from accrueboard.retrieval.embeddings import HashingEmbedder
from accrueboard.services import ledger
from accrueboard.services.seed import seed_client
from accrueboard.services.tasks import verify_chain
from tests.unit.pipeline.helpers import truth_output as ground_truth_output

pytestmark = pytest.mark.integration

MODELS = PipelineModels(
    classify="claude-haiku-4-5",
    extract="claude-sonnet-5",
    verify="claude-sonnet-5",
    code="claude-sonnet-5",
)
EMBEDDER = HashingEmbedder(dimensions=384)


class Oracle:
    """Fake model: answers classify/extract from the file's ground truth, coding from the last
    document it extracted (documents are processed one at a time)."""

    def __init__(self, by_sha: dict[str, GroundTruth]) -> None:
        self.by_sha = by_sha
        self.current: GroundTruth | None = None
        self.fail_for: set[str] = set()

    def __call__(self, request: LLMRequest) -> dict[str, Any]:
        if request.purpose == "code":
            assert self.current is not None
            return {
                "lines": [
                    {"line": i, "account": a, "reason": "history"}
                    for i, a in enumerate(self.current.line_accounts)
                ]
            }
        part = next(p for p in request.parts if isinstance(p, FilePart))
        record = self.by_sha[part.sha256]
        if record.doc_id in self.fail_for:
            raise LLMError("simulated outage")
        self.current = record
        if request.purpose == "classify":
            return {"doc_type": record.document.doc_type.value, "evidence": "title"}
        return ground_truth_output(record.document)


@pytest.fixture(scope="module")
def world() -> tuple[ClientSpec, list[GroundTruth], sessionmaker[Session]]:
    """A committed, uniquely named client seeded with history (left in the test database)."""
    spec = load_client("fernhill").model_copy(update={"id": f"e2e{uuid.uuid4().hex[:8]}"})
    records = generate(spec, load_anomaly_catalog(), 7)
    sessions = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with sessions() as session, session.begin():
        seed_client(session, spec, records, EMBEDDER, now=datetime(2025, 12, 1, tzinfo=UTC))
    return spec, records, sessions


def pick(records: list[GroundTruth], predicate: Callable[[GroundTruth], bool]) -> GroundTruth:
    return next(r for r in records if r.split is Split.VALIDATION and predicate(r))


def labelled(kind: str) -> Callable[[GroundTruth], bool]:
    return lambda r: any(a.type == kind for a in r.anomalies)


def test_pipeline_routes_documents_end_to_end(
    world: tuple[ClientSpec, list[GroundTruth], sessionmaker[Session]],
) -> None:
    spec, records, sessions = world
    by_id = {r.doc_id: r for r in records}
    duplicate = pick(records, labelled("exact_file_duplicate"))
    assert duplicate.copy_of is not None
    cases = {
        "clean": pick(
            records,
            lambda r: (
                r.vendor_id == "oakridge"
                and not r.anomalies
                and not r.hard_negatives
                and r.file_format == "pdf"
            ),
        ),
        "png_receipt": pick(
            records,
            lambda r: r.vendor_id == "staplewise" and r.file_format == "png" and not r.anomalies,
        ),
        "duplicate_source": by_id[duplicate.copy_of],
        "duplicate": duplicate,
        "first_time_vendor": pick(records, labelled("first_time_vendor")),
        "unsupported": pick(records, labelled("unsupported_document")),
        "arithmetic": pick(records, labelled("arithmetic_error")),
        "fails": pick(records, lambda r: r.vendor_id == "boxcraft" and not r.anomalies),
    }
    ordered = sorted(cases.items(), key=lambda item: (item[1].received_at, item[1].doc_id))

    files = {name: render(r, spec) for name, r in cases.items()}
    oracle = Oracle({hashlib.sha256(data).hexdigest(): cases[name] for name, data in files.items()})
    oracle.fail_for.add(cases["fails"].doc_id)
    clock = FixedClock(ordered[0][1].received_at)
    processor = Processor(sessions, FakeLLM(oracle), MODELS, EMBEDDER, clock)

    task_ids: dict[str, str] = {}
    for name, record in ordered:
        clock.advance(record.received_at - clock.now())
        with sessions() as session, session.begin():
            source = SourceFile.from_bytes(f"{record.doc_id}.{record.file_format}", files[name])
            task_ids[name] = ingest(session, spec.id, source, received_at=record.received_at).id
        outcome = processor.run_once(spec.id)
        assert outcome is not None
        assert outcome.task_id == task_ids[name]

    with sessions() as session:
        tasks = {name: session.get(Task, tid) for name, tid in task_ids.items()}
        states = {name: t.state for name, t in tasks.items() if t and name != "duplicate_source"}
        assert states == {
            "clean": "posted",
            "png_receipt": "posted",
            "duplicate": "needs_review",
            "first_time_vendor": "needs_review",
            "unsupported": "needs_review",
            "arithmetic": "needs_review",
            "fails": "failed",
        }

        def rules(name: str) -> set[str]:
            task = tasks[name]
            assert task is not None
            assert task.routing is not None
            return {hit["rule"] for hit in task.routing["hits"]}

        assert "duplicate_file" in rules("duplicate")
        assert "first_time_vendor" in rules("first_time_vendor")
        assert rules("unsupported") == {"unsupported_type"}
        assert "validation_failed" in rules("arithmetic")
        assert rules("clean") == set()

        failed = tasks["fails"]
        assert failed is not None
        assert "simulated outage" in (failed.last_error or "")

        # Posted documents have balanced entries; the whole ledger still balances.
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        for name in ("clean", "png_receipt"):
            entries = (
                session.execute(select(JournalEntry).where(JournalEntry.task_id == task_ids[name]))
                .scalars()
                .all()
            )
            assert len(entries) == 1
            total = sum(line.debit for line in entries[0].lines)
            assert total == cases[name].document.total
        assert sum(ledger.trial_balance(session, spec.id).values()) == Decimal("0.00")

        # Every task's audit chain is intact; model calls were recorded.
        for tid in task_ids.values():
            assert verify_chain(session, tid) == (True, None)
        calls = session.execute(
            select(LLMCall.purpose, func.count())
            .where(LLMCall.task_id.in_(list(task_ids.values())))
            .group_by(LLMCall.purpose)
        ).all()
        purposes: dict[str, int] = {purpose: count for purpose, count in calls}
        assert purposes["classify"] >= 6
        assert purposes["extract"] >= 5
        assert purposes["verify"] >= 2  # the PNG receipt and the arithmetic error
        assert purposes["code"] >= 5


def test_expired_leases_return_to_the_queue(
    world: tuple[ClientSpec, list[GroundTruth], sessionmaker[Session]],
) -> None:
    spec, records, sessions = world
    record = pick(records, lambda r: r.file_format == "pdf" and not r.anomalies)
    clock = FixedClock(record.received_at)
    processor = Processor(sessions, FakeLLM(lambda _: {}), MODELS, EMBEDDER, clock)
    with sessions() as session, session.begin():
        source = SourceFile.from_bytes("a.pdf", render(record, spec))
        task_id = ingest(session, spec.id, source, received_at=record.received_at).id
    assert processor.claim_next(spec.id) == task_id
    assert processor.claim_next(spec.id) is None  # nothing else queued
    clock.advance(LEASE * 2)
    assert processor.requeue_expired() >= 1
    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.state == "queued"
