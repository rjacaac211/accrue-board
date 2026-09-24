"""The review assistant on real Postgres: investigates held documents, suggests, never decides."""

import json
import re
from typing import Any

import pytest
from sqlalchemy import select

from accrueboard.agents.review_assistant.service import (
    ACTOR,
    AssistantUnavailableError,
    ReviewAssistant,
)
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import Split
from accrueboard.db.models import LLMCall, Task
from accrueboard.llm.client import FakeLLM
from accrueboard.llm.types import LLMError, ToolCall, ToolUseRequest, UserTurn
from accrueboard.services.tasks import audit_trail, verify_chain

from .helpers import EMBEDDER, World, make_world

pytestmark = pytest.mark.integration


def duplicate_of_history(world: World) -> GroundTruth:
    history = {r.doc_id for r in world.records if r.split is Split.HISTORY}
    return next(
        r
        for r in world.records
        if r.split is Split.VALIDATION
        and any(
            a.type == "renumbered_duplicate" and a.source_doc_id in history for a in r.anomalies
        )
    )


class Investigator:
    """A scripted model: checks the flagged document, a foreign id and the page, then rejects."""

    def __init__(self, accounts: list[str], foreign_id: str) -> None:
        self.accounts = accounts
        self.foreign_id = foreign_id
        self.brief = ""

    def __call__(self, request: ToolUseRequest) -> list[ToolCall]:
        first = request.turns[0]
        assert isinstance(first, UserTurn)
        self.brief = first.text
        if len(request.turns) == 1:
            flagged = re.search(r"\(see ([\w-]+)\)", self.brief)
            assert flagged is not None
            return [
                ToolCall(id="c1", name="get_document", input={"document_id": flagged.group(1)}),
                ToolCall(id="c2", name="get_document", input={"document_id": self.foreign_id}),
                ToolCall(id="c3", name="get_document_text", input={}),
                ToolCall(id="c4", name="vendor_history", input={"vendor_name": "anything"}),
            ]
        return [
            ToolCall(
                id="c5",
                name="submit_review",
                input={
                    "action": "reject",
                    "summary": "Already posted under the same number.",
                    "question": None,
                    "rule_assessments": [
                        {"rule": "duplicate_number", "verdict": "confirmed", "reason": "same"}
                    ],
                    "lines": [
                        {"line": i, "account": a, "reason": "as before"}
                        for i, a in enumerate(self.accounts)
                    ],
                    "evidence": [
                        {"source": "get_document", "detail": "same bill", "document_id": None}
                    ],
                },
            )
        ]


def with_assistant(world: World, llm: FakeLLM) -> World:
    world.assistant = ReviewAssistant(llm, "claude-sonnet-5", EMBEDDER, world.clock)
    return world


@pytest.fixture(scope="module")
def world() -> World:
    return make_world("ra")


def test_assistant_investigates_a_held_duplicate_and_only_suggests(world: World) -> None:
    other = make_world("rb")  # a second client whose documents must stay invisible
    foreign = next(r for r in other.records if r.split is Split.HISTORY).doc_id
    record = duplicate_of_history(world)
    script = Investigator(list(record.line_accounts), foreign)
    llm = FakeLLM(lambda _: {}, tool_handler=script)
    task_id = with_assistant(world, llm).process(record)

    source = record.anomalies[0].source_doc_id
    assert f"(see {source})" in script.brief
    assert record.document.vendor_name is not None
    assert record.document.vendor_name in script.brief

    with world.sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.state == "needs_review"  # suggest-only: the state is the pipeline's
        run: dict[str, Any] = task.assistant or {}
        assert run["status"] == "done"
        assert run["suggestion"]["action"] == "reject"
        assert run["proposed_accounts"] == list(record.line_accounts)
        steps = {s["input"].get("document_id", s["tool"]): s for s in run["steps"]}

        original = json.loads(steps[source]["output"])
        assert original["status"] == "posted"
        assert original["same_file_as_document_under_review"] is False
        assert original["total"] == str(record.document.total)
        assert original["posted_accounts"]
        assert steps[foreign]["is_error"]
        assert "no document" in steps[foreign]["output"]
        page = json.loads(steps["get_document_text"]["output"])
        assert record.document.document_number in page["text"]

        events = audit_trail(session, task_id)
        last = events[-1]
        assert last.actor == ACTOR
        assert last.action == "assistant_suggested"
        assert last.from_state is None
        assert last.to_state is None
        assert last.details["suggested_action"] == "reject"
        assert [c["tool"] for c in last.details["tool_calls"]] == [
            "get_document",
            "get_document",
            "get_document_text",
            "vendor_history",
            "submit_review",
        ]
        assert verify_chain(session, task_id) == (True, None)
        purposes = session.execute(
            select(LLMCall.purpose).where(LLMCall.task_id == task_id)
        ).scalars()
        assert list(purposes).count("review_assistant") == 2


def test_a_failed_investigation_leaves_the_task_held(world: World) -> None:
    def outage(_: ToolUseRequest) -> list[ToolCall]:
        raise LLMError("simulated outage")

    record = next(
        r
        for r in world.records
        if r.split is Split.VALIDATION and any(a.type == "arithmetic_error" for a in r.anomalies)
    )
    task_id = with_assistant(world, FakeLLM(lambda _: {}, tool_handler=outage)).process(record)
    with world.sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.state == "needs_review"
        assert task.assistant is not None
        assert task.assistant["status"] == "failed"
        assert "simulated outage" in task.assistant["error"]
        assert audit_trail(session, task_id)[-1].action == "assistant_failed"


def test_a_crashing_assistant_never_breaks_the_pipeline(world: World) -> None:
    def crash(_: ToolUseRequest) -> list[ToolCall]:
        raise RuntimeError("bug")

    record = next(
        r
        for r in world.records
        if r.split is Split.VALIDATION and any(a.type == "over_materiality" for a in r.anomalies)
    )
    task_id = with_assistant(world, FakeLLM(lambda _: {}, tool_handler=crash)).process(record)
    with world.sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.state == "needs_review"
        assert task.assistant is None


def test_assistant_only_looks_at_held_tasks(world: World) -> None:
    posted = next(
        r
        for r in world.records
        if r.split is Split.VALIDATION
        and not r.anomalies
        and not r.hard_negatives
        and r.vendor_id == "oakridge"
    )
    world.assistant = None
    task_id = world.process(posted)
    assistant = ReviewAssistant(FakeLLM(lambda _: {}), "claude-sonnet-5", EMBEDDER, world.clock)
    with world.sessions() as session, session.begin():
        assert session.get(Task, task_id).state == "posted"  # type: ignore[union-attr]
        with pytest.raises(AssistantUnavailableError, match="held for review"):
            assistant.run(session, task_id)
