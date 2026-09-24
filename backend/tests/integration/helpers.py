"""Shared integration helpers: a seeded client and a fake model that reads perfectly."""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from accrueboard.clock import FixedClock
from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import ClientSpec, load_anomaly_catalog, load_client
from accrueboard.db.session import get_engine
from accrueboard.llm.client import FakeLLM
from accrueboard.llm.types import FilePart, LLMError, LLMRequest
from accrueboard.pipeline.files import SourceFile
from accrueboard.pipeline.process import PipelineModels, Processor, ingest
from accrueboard.retrieval.embeddings import HashingEmbedder
from accrueboard.services.seed import seed_client
from tests.unit.pipeline.helpers import truth_output

MODELS = PipelineModels(
    classify="claude-haiku-4-5",
    extract="claude-sonnet-5",
    verify="claude-sonnet-5",
    code="claude-sonnet-5",
)
EMBEDDER = HashingEmbedder(dimensions=384)


class Oracle:
    """Answers classify/extract from the file's ground truth and coding from the document it
    last extracted (the processor handles one document at a time)."""

    def __init__(self) -> None:
        self.by_sha: dict[str, GroundTruth] = {}
        self.current: GroundTruth | None = None
        self.fail_for: set[str] = set()

    def __call__(self, request: LLMRequest) -> dict[str, Any]:
        if request.purpose == "code":
            if self.current is None:
                raise LLMError("no document in context")
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
        return truth_output(record.document)


@dataclass
class World:
    spec: ClientSpec
    records: list[GroundTruth]
    sessions: sessionmaker[Session]
    oracle: Oracle = field(default_factory=Oracle)
    clock: FixedClock = field(default_factory=lambda: FixedClock(datetime(2025, 12, 1, tzinfo=UTC)))

    @property
    def processor(self) -> Processor:
        return Processor(self.sessions, FakeLLM(self.oracle), MODELS, EMBEDDER, self.clock)

    def process(self, record: GroundTruth) -> str:
        """Ingest one generated document at its arrival time and run the pipeline on it."""
        data = render(record, self.spec)
        self.oracle.by_sha[hashlib.sha256(data).hexdigest()] = record
        if record.received_at > self.clock.now():
            self.clock.advance(record.received_at - self.clock.now())
        with self.sessions() as session, session.begin():
            source = SourceFile.from_bytes(f"{record.doc_id}.{record.file_format}", data)
            task_id = ingest(session, self.spec.id, source, received_at=record.received_at).id
        outcome = self.processor.run_once(self.spec.id)
        assert outcome is not None
        assert outcome.task_id == task_id
        return task_id


def make_world(prefix: str) -> World:
    """A committed, uniquely named client seeded with its history (left in the test DB)."""
    spec = load_client("fernhill").model_copy(update={"id": f"{prefix}{uuid.uuid4().hex[:8]}"})
    records = generate(spec, load_anomaly_catalog(), 7)
    sessions = sessionmaker(bind=get_engine(), expire_on_commit=False)
    with sessions() as session, session.begin():
        seed_client(session, spec, records, EMBEDDER, now=datetime(2025, 12, 1, tzinfo=UTC))
    return World(spec, records, sessions)
