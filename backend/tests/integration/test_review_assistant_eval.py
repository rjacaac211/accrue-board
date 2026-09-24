"""The review assistant evaluation harness, end to end on Postgres with a scripted model."""

import pytest
from sqlalchemy.orm import sessionmaker

from accrueboard.datagen.generate import generate
from accrueboard.datagen.spec import load_anomaly_catalog
from accrueboard.db.session import get_engine
from accrueboard.eval.review_assistant import evaluate, summarize
from accrueboard.llm.client import FakeLLM
from accrueboard.llm.types import ToolCall, ToolUseRequest, UserTurn

from .conftest import unique_spec
from .helpers import EMBEDDER

pytestmark = pytest.mark.integration


def always_approve(request: ToolUseRequest) -> list[ToolCall]:
    """Approves every document, keeping the pipeline's proposed accounts."""
    brief = request.turns[0]
    assert isinstance(brief, UserTurn)
    proposed = [
        line.split(" -> ", 1)[1].split(" ", 1)[0]
        for line in brief.text.splitlines()
        if " -> " in line
    ]
    return [
        ToolCall(
            id="s",
            name="submit_review",
            input={
                "action": "approve",
                "summary": "Fine.",
                "question": None,
                "rule_assessments": [],
                "lines": [
                    {"line": i, "account": a, "reason": "proposed"} for i, a in enumerate(proposed)
                ],
                "evidence": [],
            },
        )
    ]


def test_evaluate_scores_each_held_document() -> None:
    spec = unique_spec()
    records = generate(spec, load_anomaly_catalog(), 7)
    llm = FakeLLM(lambda _: {}, tool_handler=always_approve)
    sessions = sessionmaker(bind=get_engine(), expire_on_commit=False)
    cases = evaluate(
        records, spec, sessions, llm=llm, model="claude-sonnet-5", embedder=EMBEDDER, limit=4
    )

    assert len(cases) == 4
    assert len(llm.tool_requests) == 4  # one turn each: the script submits straight away
    assert all(c.suggested == "approve" for c in cases)
    for c in cases:
        assert c.agrees == ("approve" in c.expected)
        assert c.lines_correct == c.proposed_correct  # it kept the proposal, right or wrong
        assert c.rules or c.anomalies or c.lines  # held for a reason
    summary = summarize(cases)
    assert summary.cases == 4
    assert summary.failed == 0
