from typing import Any

import pytest

from accrueboard.agents.review_assistant.graph import investigate
from accrueboard.agents.review_assistant.prompts import (
    INVESTIGATION_TOOLS,
    REVIEW_VERSION,
    SUBMIT,
    submit_tool,
)
from accrueboard.agents.review_assistant.tools import (
    InvalidSuggestionError,
    SuggestedAction,
    parse_suggestion,
)
from accrueboard.llm.client import FakeLLM
from accrueboard.llm.types import AssistantTurn, ToolCall, ToolUseRequest, UserTurn

ACCOUNTS = ["1300", "5300", "6100"]


def suggestion(action: str = "approve", lines: int = 2, **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "action": action,
        "summary": "Looks like a routine restock.",
        "question": None,
        "rule_assessments": [
            {"rule": "first_time_vendor", "verdict": "confirmed", "reason": "no history"}
        ],
        "lines": [{"line": i, "account": "1300", "reason": "stock"} for i in range(lines)],
        "evidence": [{"source": "vendor_history", "detail": "no history", "document_id": None}],
    }
    return raw | overrides


class StubTools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name == "get_document":
            return {"error": "no such document"}
        return {"tool": name, "ok": True}


def scripted(*turns: list[ToolCall]) -> FakeLLM:
    """A model that makes the given tool calls, one list per turn."""
    queue = list(turns)
    return FakeLLM(lambda _: {}, tool_handler=lambda _: queue.pop(0))


def call(name: str, n: int, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{n}", name=name, input=args)


def run(llm: FakeLLM, tools: StubTools | None = None, max_turns: int = 8) -> Any:
    return investigate(
        llm,
        model="claude-sonnet-5",
        system="system prompt",
        brief="the case",
        toolbox=tools or StubTools(),
        accounts=ACCOUNTS,
        line_count=2,
        max_turns=max_turns,
    )


# ------------------------------------------------------------------ suggestion checks


def test_parse_accepts_a_complete_suggestion() -> None:
    parsed = parse_suggestion(suggestion(), line_count=2, accounts=set(ACCOUNTS))
    assert parsed.action is SuggestedAction.APPROVE
    assert parsed.accounts == ["1300", "1300"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (suggestion(lines=1), "one entry per line"),
        (
            suggestion(lines=0) | {"lines": [{"line": 0, "account": "9999", "reason": "x"}] * 2},
            "one entry per line",
        ),
        (
            suggestion()
            | {
                "lines": [
                    {"line": 0, "account": "9999", "reason": "x"},
                    {"line": 1, "account": "1300", "reason": "x"},
                ]
            },
            "unknown account",
        ),
        (suggestion("block"), "needs the question"),
        (suggestion("escalate"), "schema"),
    ],
)
def test_parse_explains_what_to_fix(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(InvalidSuggestionError, match=message):
        parse_suggestion(raw, line_count=2, accounts=set(ACCOUNTS))


def test_unsupported_documents_need_no_lines() -> None:
    parsed = parse_suggestion(suggestion("reject", lines=0), line_count=0, accounts=set(ACCOUNTS))
    assert parsed.lines == ()


# ------------------------------------------------------------------ tool definitions


def _strict_objects(schema: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found += _strict_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            found += _strict_objects(value)
    return found


def test_tool_schemas_follow_the_strict_subset() -> None:
    specs = (*INVESTIGATION_TOOLS, submit_tool(ACCOUNTS, 2))
    assert len({s.name for s in specs}) == len(specs)
    for spec in specs:
        for obj in _strict_objects(spec.input_schema):
            assert obj["additionalProperties"] is False, spec.name
            assert set(obj["required"]) == set(obj["properties"]), spec.name


def test_submit_tool_limits_accounts_to_the_chart() -> None:
    schema = submit_tool(ACCOUNTS, 3).input_schema
    line = schema["properties"]["lines"]["items"]
    assert line["properties"]["account"]["enum"] == ACCOUNTS
    assert "3 line(s)" in submit_tool(ACCOUNTS, 3).description


# ------------------------------------------------------------------ the loop


def test_investigation_runs_tools_then_retries_an_invalid_submission() -> None:
    tools = StubTools()
    llm = scripted(
        [call("vendor_history", 1, vendor_name="Acme"), call("get_document", 2, document_id="x")],
        [call(SUBMIT, 3, **suggestion(lines=1))],  # wrong number of lines
        [call(SUBMIT, 4, **suggestion())],
    )
    result = run(llm, tools)

    assert result.suggestion is not None
    assert result.suggestion.action is SuggestedAction.APPROVE
    assert result.model_turns == 3
    assert tools.calls == [
        ("vendor_history", {"vendor_name": "Acme"}),
        ("get_document", {"document_id": "x"}),
    ]
    assert [(s.turn, s.tool, s.is_error) for s in result.steps] == [
        (1, "vendor_history", False),
        (1, "get_document", True),
        (2, SUBMIT, True),
        (3, SUBMIT, False),
    ]
    assert len(result.calls) == 3
    assert {c.purpose for c in result.calls} == {"review_assistant"}
    assert {c.prompt_version for c in result.calls} == {REVIEW_VERSION}

    # Each request carries the whole conversation: the brief, then turn/result pairs whose
    # result ids answer the calls of the turn before.
    last: ToolUseRequest = llm.tool_requests[-1]
    assert len(llm.tool_requests) == 3
    first = last.turns[0]
    assert isinstance(first, UserTurn)
    assert first.text == "the case"
    for asked, answered in zip(last.turns[1::2], last.turns[2::2], strict=True):
        assert isinstance(asked, AssistantTurn)
        assert isinstance(answered, UserTurn)
        assert [r.call_id for r in answered.results] == [c.id for c in asked.calls]
    rejected = last.turns[4]
    assert isinstance(rejected, UserTurn)
    assert rejected.results[0].is_error
    assert "one entry per line" in rejected.results[0].content
    assert all(r.force_tool is None for r in llm.tool_requests)


def test_last_turn_forces_a_submission() -> None:
    def handler(request: ToolUseRequest) -> list[ToolCall]:
        n = len(request.turns)
        if request.force_tool == SUBMIT:
            return [call(SUBMIT, n, **suggestion("block", question="Ask the vendor for a credit."))]
        return [call("get_document_text", n)]

    llm = FakeLLM(lambda _: {}, tool_handler=handler)
    result = run(llm, max_turns=3)
    assert [r.force_tool for r in llm.tool_requests] == [None, None, SUBMIT]
    assert result.suggestion is not None
    assert result.suggestion.action is SuggestedAction.BLOCK
    assert result.suggestion.question == "Ask the vendor for a credit."


def test_no_suggestion_when_the_turns_run_out() -> None:
    llm = FakeLLM(
        lambda _: {}, tool_handler=lambda r: [call(SUBMIT, len(r.turns), **suggestion(lines=5))]
    )
    result = run(llm, max_turns=2)
    assert result.suggestion is None
    assert result.model_turns == 2
    assert all(s.is_error for s in result.steps)
