"""The investigation loop as a LangGraph state graph.

    START -> think -> act -> (suggestion submitted, or out of turns) -> END
               ^        |
               +--------+

- ``think`` asks the model for its next move. It must call a tool (tool choice "any"), and on
  the last allowed turn it must call ``submit_review``.
- ``act`` runs the requested read-only tools and returns their results. When the model submits,
  the suggestion is checked against the document; a correctable problem is sent back as a tool
  error so the model can fix it on its next turn.

The model client goes through the project's record/replay layer rather than a LangChain chat
model, so investigations replay deterministically and every turn's cost is recorded. There is
no checkpointer: an investigation is one short run, and its outcome is stored on the task.
"""

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict

from accrueboard.agents.review_assistant.prompts import (
    INVESTIGATION_TOOLS,
    PURPOSE,
    REVIEW_VERSION,
    SUBMIT,
    submit_tool,
)
from accrueboard.agents.review_assistant.tools import (
    InvalidSuggestionError,
    Suggestion,
    parse_suggestion,
    to_json,
)
from accrueboard.llm.client import ToolUseClient
from accrueboard.llm.types import (
    AssistantTurn,
    LLMError,
    ToolCall,
    ToolResult,
    ToolUseRequest,
    Turn,
    UserTurn,
)
from accrueboard.pipeline.extraction import CallRecord

MAX_TURNS = 8
TRACE_LIMIT = 4000
"""Characters of each tool result kept in the stored trace (the model sees all of it)."""


class Step(BaseModel):
    """One tool call made during an investigation, as kept in the trace."""

    model_config = ConfigDict(frozen=True)

    turn: int
    tool: str
    input: dict[str, Any]
    output: str
    is_error: bool = False


class ToolRunner(Protocol):
    """Runs an investigation tool by name (see ``tools.Toolbox``)."""

    def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


class AgentState(TypedDict):
    turns: Annotated[list[Turn], operator.add]
    steps: Annotated[list[Step], operator.add]
    calls: Annotated[list[CallRecord], operator.add]
    model_turns: int
    suggestion: Suggestion | None


@dataclass(frozen=True)
class Investigation:
    suggestion: Suggestion | None
    steps: list[Step]
    calls: list[CallRecord]
    model_turns: int
    notes: list[str]
    """What the model said alongside its tool calls, turn by turn."""


class AssistantError(LLMError):
    """The investigation ended without a usable suggestion."""


def build_graph(
    llm: ToolUseClient,
    *,
    model: str,
    system: str,
    toolbox: ToolRunner,
    accounts: list[str],
    line_count: int,
    max_turns: int = MAX_TURNS,
) -> Any:
    tools = (*INVESTIGATION_TOOLS, submit_tool(accounts, line_count))
    allowed = set(accounts)

    def think(state: AgentState) -> dict[str, Any]:
        turn = state["model_turns"] + 1
        request = ToolUseRequest(
            purpose=PURPOSE,
            prompt_version=REVIEW_VERSION,
            model=model,
            system=system,
            turns=tuple(state["turns"]),
            tools=tools,
            force_tool=SUBMIT if turn >= max_turns else None,
            max_tokens=4096,
        )
        response = llm.use_tools(request)
        call = CallRecord(
            purpose=request.purpose,
            prompt_version=request.prompt_version,
            model=request.model,
            request_key=request.key,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
            input_tokens=response.usage.input_tokens
            + response.usage.cache_read_input_tokens
            + response.usage.cache_creation_input_tokens,
            output_tokens=response.usage.output_tokens,
            replayed=response.replayed,
        )
        return {"turns": [response.turn], "calls": [call], "model_turns": turn}

    def act(state: AgentState) -> dict[str, Any]:
        last = state["turns"][-1]
        if not isinstance(last, AssistantTurn):  # unreachable: act always follows think
            raise AssistantError("no model turn to act on")
        results: list[ToolResult] = []
        steps: list[Step] = []
        suggestion: Suggestion | None = None
        for call in last.calls:
            content, is_error, submitted = _execute(call, toolbox, line_count, allowed)
            suggestion = suggestion or submitted
            results.append(ToolResult(call_id=call.id, content=content, is_error=is_error))
            steps.append(
                Step(
                    turn=state["model_turns"],
                    tool=call.name,
                    input=call.input,
                    output=content[:TRACE_LIMIT],
                    is_error=is_error,
                )
            )
        return {
            "turns": [UserTurn(results=tuple(results))],
            "steps": steps,
            "suggestion": suggestion,
        }

    def after_act(state: AgentState) -> str:
        if state["suggestion"] is not None or state["model_turns"] >= max_turns:
            return END
        return "think"

    graph = StateGraph(AgentState)
    graph.add_node("think", think)
    graph.add_node("act", act)
    graph.add_edge(START, "think")
    graph.add_edge("think", "act")
    graph.add_conditional_edges("act", after_act, ["think", END])
    return graph.compile()


def _execute(
    call: ToolCall, toolbox: ToolRunner, line_count: int, accounts: set[str]
) -> tuple[str, bool, Suggestion | None]:
    if call.name == SUBMIT:
        try:
            suggestion = parse_suggestion(call.input, line_count=line_count, accounts=accounts)
        except InvalidSuggestionError as exc:
            return f"Not accepted: {exc}. Fix it and call {SUBMIT} again.", True, None
        return "Recommendation recorded.", False, suggestion
    result = toolbox.run(call.name, call.input)
    return to_json(result), "error" in result, None


def investigate(
    llm: ToolUseClient,
    *,
    model: str,
    system: str,
    brief: str,
    toolbox: ToolRunner,
    accounts: list[str],
    line_count: int,
    max_turns: int = MAX_TURNS,
) -> Investigation:
    """Run one investigation from the case brief to a suggestion (or to the turn limit)."""
    graph = build_graph(
        llm,
        model=model,
        system=system,
        toolbox=toolbox,
        accounts=accounts,
        line_count=line_count,
        max_turns=max_turns,
    )
    initial: AgentState = {
        "turns": [UserTurn(text=brief)],
        "steps": [],
        "calls": [],
        "model_turns": 0,
        "suggestion": None,
    }
    final: AgentState = graph.invoke(initial, {"recursion_limit": 2 * max_turns + 2})
    return Investigation(
        suggestion=final["suggestion"],
        steps=list(final["steps"]),
        calls=list(final["calls"]),
        model_turns=final["model_turns"],
        notes=[t.text for t in final["turns"] if isinstance(t, AssistantTurn) and t.text],
    )
