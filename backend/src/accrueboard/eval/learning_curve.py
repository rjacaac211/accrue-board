"""Learning curve: does coding accuracy improve as reviewed documents are fed back?

Setup (no leakage):
- The knowledge store starts with the history split only.
- Reviewed documents are taken from the validation split in arrival order and added with their
  correct accounts, as a reviewer's confirmations and corrections would be.
- Accuracy is always measured on the test split, which never enters the store.

At each step every method codes every test line:
- ``vendor_rule``: the vendor's most common past account (abstains for unknown vendors)
- ``classifier``: the per-client TF-IDF + logistic regression model
- ``knn``: majority account of the five most similar confirmed items
- ``cascade``: the full coder (vendor memory, retrieval, the language model, classifier check)

Lines are split by whether their vendor appears in the history split. Learning should show most
on the second group: vendors the system first meets through reviewed documents.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import ClientSpec, Split
from accrueboard.domain.documents import DocumentType
from accrueboard.domain.duplicates import normalize_vendor
from accrueboard.llm.client import LLMClient
from accrueboard.llm.types import LLMRequest, LLMResponse
from accrueboard.pipeline.coding import Coder
from accrueboard.retrieval.embeddings import Embedder
from accrueboard.retrieval.knowledge import EntrySource, KnowledgeEntry, MemoryKnowledgeStore
from accrueboard.retrieval.seed import client_context, history_entries

BASELINES = ("vendor_rule", "classifier", "knn")
ALL = -1
"""Step value meaning "every validation document"."""
DEFAULT_STEPS = (0, 50, 100, ALL)


@dataclass
class Tally:
    lines: int = 0
    correct: int = 0

    def add(self, ok: bool) -> None:
        self.lines += 1
        self.correct += ok

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.lines if self.lines else None


@dataclass
class CurvePoint:
    reviewed_documents: int
    knowledge_entries: int
    method: str
    overall: Tally = field(default_factory=Tally)
    history_vendor: Tally = field(default_factory=Tally)
    new_vendor: Tally = field(default_factory=Tally)
    abstained: int = 0

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        for name in ("overall", "history_vendor", "new_vendor"):
            data[name]["accuracy"] = getattr(self, name).accuracy
        return data


def _bills(records: Iterable[GroundTruth], split: Split) -> list[GroundTruth]:
    return sorted(
        (r for r in records if r.split is split and r.document.doc_type is not DocumentType.OTHER),
        key=lambda r: (r.received_at, r.doc_id),
    )


def feedback_entries(record: GroundTruth) -> list[KnowledgeEntry]:
    """A reviewed document's lines as confirmed knowledge (the reviewer's final accounts)."""
    entries = history_entries([record.model_copy(update={"split": Split.HISTORY})])
    return [e.model_copy(update={"source": EntrySource.CONFIRMED}) for e in entries]


def learning_curve(
    records: Sequence[GroundTruth],
    spec: ClientSpec,
    embedder: Embedder,
    *,
    steps: Sequence[int] = DEFAULT_STEPS,
    llm: LLMClient | None = None,
    model: str = "claude-sonnet-5",
    test_limit: int | None = None,
    new_vendors_only: bool = False,
) -> list[CurvePoint]:
    """Accuracy of each coding method on the test split after N reviewed documents."""
    feedback = _bills(records, Split.VALIDATION)
    history_vendors = {e.vendor_key for e in history_entries(records)}
    test = _bills(records, Split.TEST)
    if new_vendors_only:
        test = [r for r in test if normalize_vendor(r.document.vendor_name) not in history_vendors]
    test = test[:test_limit]
    test_ids = {r.doc_id for r in test}
    methods = [*BASELINES, *(["cascade"] if llm is not None else [])]

    store = MemoryKnowledgeStore(embedder)
    store.add(history_entries(records))
    coder = Coder(store, llm or _NoModel(), model, client_context(spec))
    added = 0
    points: list[CurvePoint] = []
    resolved = sorted({len(feedback) if s == ALL else min(s, len(feedback)) for s in steps})
    for step in resolved:
        batch = feedback[added:step]
        if any(r.doc_id in test_ids for r in batch):
            raise AssertionError("test documents must never enter the knowledge store")
        for record in batch:
            store.add(feedback_entries(record))
        added = step
        coder.refresh()
        size = len(store.entries(spec.id))
        for method in methods:
            point = CurvePoint(reviewed_documents=added, knowledge_entries=size, method=method)
            for record in test:
                predicted = _predict(coder, method, record)
                vendor_key, _ = coder.vendor_key(record.document)
                bucket = point.history_vendor if vendor_key in history_vendors else point.new_vendor
                for guess, truth in zip(predicted, record.line_accounts, strict=True):
                    point.abstained += guess is None
                    point.overall.add(guess == truth)
                    bucket.add(guess == truth)
            points.append(point)
    return points


def _predict(coder: Coder, method: str, record: GroundTruth) -> list[str | None]:
    doc = record.document
    if method == "vendor_rule":
        return coder.baseline_vendor_rule(doc)
    if method == "classifier":
        return coder.baseline_classifier(doc)
    if method == "knn":
        return coder.baseline_knn(doc)
    return list(coder.code(doc).accounts)


class _NoModel:
    """Placeholder model for baseline-only runs; the cascade is never evaluated with it."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError(f"no language model configured ({request.purpose})")


# ---------------------------------------------------------------------------- reporting


def to_markdown(points: Sequence[CurvePoint]) -> str:
    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.1%}"

    lines = [
        "| Reviewed docs | Knowledge entries | Method | All lines | Vendors in history "
        "| Vendors new after history |",
        "|---:|---:|---|---:|---:|---:|",
    ]
    for p in points:
        lines.append(
            f"| {p.reviewed_documents} | {p.knowledge_entries} | {p.method} "
            f"| {pct(p.overall.accuracy)} ({p.overall.lines}) "
            f"| {pct(p.history_vendor.accuracy)} ({p.history_vendor.lines}) "
            f"| {pct(p.new_vendor.accuracy)} ({p.new_vendor.lines}) |"
        )
    return "\n".join(lines) + "\n"


def write_report(points: Sequence[CurvePoint], out_dir: Path, *, meta: dict[str, object]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "learning-curve.json").write_text(
        json.dumps({"meta": meta, "points": [p.as_dict() for p in points]}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    path = out_dir / "learning-curve.md"
    path.write_text(to_markdown(points), encoding="utf-8", newline="\n")
    return path
