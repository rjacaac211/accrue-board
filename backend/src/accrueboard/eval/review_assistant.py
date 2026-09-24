"""Review assistant evaluation: does its recommendation match what should happen?

Setup:
- A fresh database is seeded with the client's history. The evaluation split's documents then
  arrive one at a time, in arrival order, and go through the real pipeline.
- Classification, extraction and coding are answered by the ground-truth oracle, so every
  document held for review was held by the routing rules and not by a reading mistake. This
  isolates the assistant's judgement; the end-to-end evaluation covers the full system.
- Each held document is investigated by the assistant at the moment it is held, seeing only
  what had arrived by then (as in production).

The expected action comes from the generator's labels and the firm's review policy (see the
assistant's prompt): duplicates and non-bills are rejected; documents the vendor must correct
or the client must confirm are held; anything else is approved. An amount outlier that occurred
naturally (rather than being injected) may be either held or approved: the label only says the
amount crossed the outlier definition, not that it is wrong.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from accrueboard.agents.review_assistant.service import AssistantRun, ReviewAssistant
from accrueboard.agents.review_assistant.tools import SuggestedAction
from accrueboard.clock import FixedClock
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import ClientSpec, Split
from accrueboard.db.models import Task
from accrueboard.domain.lifecycle import TaskState
from accrueboard.eval.oracle import Oracle
from accrueboard.llm.client import FakeLLM, ToolUseClient
from accrueboard.pipeline.files import SourceFile
from accrueboard.pipeline.process import PipelineModels, Processor, ingest
from accrueboard.retrieval.embeddings import Embedder
from accrueboard.services.seed import seed_client

_A = SuggestedAction
REJECT = frozenset({_A.REJECT})
BLOCK = frozenset({_A.BLOCK})
APPROVE = frozenset({_A.APPROVE})
POLICY: dict[str, frozenset[SuggestedAction]] = {
    "exact_file_duplicate": REJECT,
    "renumbered_duplicate": REJECT,
    "near_duplicate": REJECT,
    "duplicate_credit_note": REJECT,
    "unsupported_document": REJECT,
    "arithmetic_error": BLOCK,
    "tax_on_resale_inventory": BLOCK,
    "amount_outlier": BLOCK,
    "over_materiality": APPROVE,
    "first_time_vendor": APPROVE,
}
NATURAL_OUTLIER = frozenset({_A.APPROVE, _A.BLOCK})
PIPELINE_MODELS = PipelineModels(
    classify="oracle", extract="oracle", verify="oracle", code="oracle"
)


def expected_actions(record: GroundTruth) -> frozenset[SuggestedAction]:
    """The acceptable recommendations for a document, strictest label first."""
    options: list[frozenset[SuggestedAction]] = []
    for label in record.anomalies:
        if label.type == "amount_outlier" and not label.injected:
            options.append(NATURAL_OUTLIER)
        else:
            options.append(POLICY[label.type])
    for strict in (REJECT, BLOCK):
        if strict in options:
            return strict
    return frozenset().union(*options) if options else APPROVE


@dataclass
class Case:
    doc_id: str
    anomalies: list[str]
    rules: list[str]
    expected: list[str]
    suggested: str | None
    agrees: bool
    lines: int
    lines_correct: int
    proposed_correct: int
    cost_usd: Decimal
    model_turns: int
    tools: list[str]
    replayed: bool
    error: str | None = None


def _case(record: GroundTruth, task: Task) -> Case:
    run = AssistantRun.model_validate(task.assistant)
    expected = expected_actions(record)
    suggestion = run.suggestion
    truth = list(record.line_accounts)
    suggested = suggestion.accounts if suggestion else []
    return Case(
        doc_id=record.doc_id,
        anomalies=sorted({a.type for a in record.anomalies}),
        rules=[h["rule"] for h in (task.routing or {}).get("hits", [])],
        expected=sorted(a.value for a in expected),
        suggested=suggestion.action.value if suggestion else None,
        agrees=suggestion is not None and suggestion.action in expected,
        lines=len(truth),
        lines_correct=sum(s == t for s, t in zip(suggested, truth, strict=False)),
        proposed_correct=sum(p == t for p, t in zip(run.proposed_accounts, truth, strict=False)),
        cost_usd=run.cost_usd,
        model_turns=run.model_turns,
        tools=[s.tool for s in run.steps],
        replayed=run.replayed,
        error=run.error,
    )


def evaluate(
    records: Sequence[GroundTruth],
    spec: ClientSpec,
    sessions: sessionmaker[Session],
    *,
    llm: ToolUseClient,
    model: str,
    embedder: Embedder,
    split: Split = Split.VALIDATION,
    limit: int | None = None,
) -> list[Case]:
    """Run the split through the pipeline and score the assistant on every held document.

    ``sessions`` must point at an empty, migrated database (see ``db.scratch``). ``limit``
    stops after that many held documents.
    """
    arriving = sorted(
        (r for r in records if r.split is split and r.file is not None),
        key=lambda r: (r.received_at, r.doc_id),
    )
    if not arriving:
        return []
    clock = FixedClock(arriving[0].received_at)
    with sessions() as session, session.begin():
        seed_client(session, spec, list(records), embedder, now=clock.now())

    oracle = Oracle()
    processor = Processor(
        sessions,
        FakeLLM(oracle),
        PIPELINE_MODELS,
        embedder,
        clock,
        assistant=ReviewAssistant(llm, model, embedder, clock),
    )
    cases: list[Case] = []
    for record in arriving:
        if limit is not None and len(cases) >= limit:
            break
        data = render(record, spec)
        oracle.register(hashlib.sha256(data).hexdigest(), record)
        if record.received_at > clock.now():
            clock.advance(record.received_at - clock.now())
        with sessions() as session, session.begin():
            source = SourceFile.from_bytes(f"{record.doc_id}.{record.file_format}", data)
            ingest(
                session,
                spec.id,
                source,
                received_at=record.received_at,
                document_id=record.doc_id,
                task_id=f"task_{record.doc_id}",
            )
        outcome = processor.run_once(spec.id)
        if outcome is None or outcome.state is not TaskState.NEEDS_REVIEW:
            continue
        with sessions() as session:
            task = session.get(Task, outcome.task_id)
            if task is not None and task.assistant is not None:
                cases.append(_case(record, task))
    return cases


# ---------------------------------------------------------------------------- reporting


@dataclass
class Tally:
    cases: int = 0
    agree: int = 0

    @property
    def rate(self) -> float | None:
        return self.agree / self.cases if self.cases else None


@dataclass
class Summary:
    cases: int
    agreement: Tally
    by_expected: dict[str, Tally]
    by_anomaly: dict[str, Tally]
    lines: int
    lines_correct: int
    proposed_correct: int
    failed: int
    cost_usd: Decimal
    mean_turns: float
    tool_use: dict[str, int] = field(default_factory=dict)


def summarize(cases: Iterable[Case]) -> Summary:
    cases = list(cases)
    agreement = Tally()
    by_expected: dict[str, Tally] = defaultdict(Tally)
    by_anomaly: dict[str, Tally] = defaultdict(Tally)
    tools: dict[str, int] = defaultdict(int)
    for case in cases:
        for tally in (
            agreement,
            by_expected[" or ".join(case.expected)],
            *(by_anomaly[a] for a in case.anomalies or ["none"]),
        ):
            tally.cases += 1
            tally.agree += case.agrees
        for tool in case.tools:
            tools[tool] += 1
    return Summary(
        cases=len(cases),
        agreement=agreement,
        by_expected=dict(sorted(by_expected.items())),
        by_anomaly=dict(sorted(by_anomaly.items())),
        lines=sum(c.lines for c in cases),
        lines_correct=sum(c.lines_correct for c in cases),
        proposed_correct=sum(c.proposed_correct for c in cases),
        failed=sum(c.suggested is None for c in cases),
        cost_usd=sum((c.cost_usd for c in cases), Decimal(0)),
        mean_turns=sum(c.model_turns for c in cases) / len(cases) if cases else 0.0,
        tool_use=dict(sorted(tools.items(), key=lambda item: -item[1])),
    )


def _pct(tally: Tally) -> str:
    return "—" if tally.rate is None else f"{tally.rate:.1%} ({tally.agree}/{tally.cases})"


def to_markdown(summary: Summary) -> str:
    out = [
        f"Held documents investigated: {summary.cases} (no recommendation: {summary.failed})",
        f"Recommendation matches the expected action: {_pct(summary.agreement)}",
        "",
        "| Expected action | Agreement |",
        "|---|---:|",
        *(f"| {name} | {_pct(t)} |" for name, t in summary.by_expected.items()),
        "",
        "| Label | Agreement |",
        "|---|---:|",
        *(f"| {name} | {_pct(t)} |" for name, t in summary.by_anomaly.items()),
        "",
        f"Suggested accounts correct: {summary.lines_correct}/{summary.lines} lines "
        f"(the pipeline's proposal: {summary.proposed_correct}/{summary.lines})",
        f"Cost: ${summary.cost_usd:.4f} in total, {summary.mean_turns:.1f} model turns per "
        "document on average",
        "Tool calls: " + ", ".join(f"{name} {n}" for name, n in summary.tool_use.items()),
    ]
    return "\n".join(out) + "\n"


def write_report(cases: Sequence[Case], out_dir: Path, *, meta: dict[str, object]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(cases)
    payload = {
        "meta": meta,
        "summary": asdict(summary),
        "cases": [asdict(c) for c in cases],
    }
    (out_dir / "review-assistant.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8", newline="\n"
    )
    path = out_dir / "review-assistant.md"
    path.write_text(to_markdown(summary), encoding="utf-8", newline="\n")
    return path
