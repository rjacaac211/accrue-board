"""LLM clients: the real Anthropic client, a record/replay wrapper, and a fake for tests."""

import json
import time
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel

from accrueboard.llm.types import (
    AssistantTurn,
    FilePart,
    LLMError,
    LLMRequest,
    LLMResponse,
    RefusalError,
    ReplayMissError,
    ToolCall,
    ToolUseRequest,
    ToolUseResponse,
    TruncatedError,
    Turn,
    Usage,
)

# USD per million tokens (input, output). Cache writes cost 1.25x input, cache reads 0.1x.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
}
_MILLION = Decimal(1_000_000)


def cost_of(model: str, usage: Usage) -> Decimal:
    """Dollar cost of one call. Unknown models are priced at zero rather than guessed."""
    rates = PRICING.get(model)
    if rates is None:
        return Decimal(0)
    rate_in, rate_out = rates
    tokens_in = (
        Decimal(usage.input_tokens)
        + Decimal(usage.cache_creation_input_tokens) * Decimal("1.25")
        + Decimal(usage.cache_read_input_tokens) * Decimal("0.1")
    )
    cost = tokens_in * rate_in / _MILLION + Decimal(usage.output_tokens) * rate_out / _MILLION
    return cost.quantize(Decimal("0.000001"))


class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


class ToolUseClient(Protocol):
    def use_tools(self, request: ToolUseRequest) -> ToolUseResponse: ...


class ModelClient(LLMClient, ToolUseClient, Protocol):
    """A client for both pipeline calls and tool-using conversations."""


# ---------------------------------------------------------------------------- Anthropic


def _content_blocks(request: LLMRequest) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in request.parts:
        if isinstance(part, FilePart):
            block_type = "document" if part.media_type == "application/pdf" else "image"
            blocks.append(
                {
                    "type": block_type,
                    "source": {
                        "type": "base64",
                        "media_type": part.media_type,
                        "data": part.base64,
                    },
                }
            )
        else:
            blocks.append({"type": "text", "text": part.text})
    return blocks


class AnthropicLLM:
    """Calls the Messages API with a JSON-schema output format."""

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        self._client = client or anthropic.Anthropic()

    def complete(self, request: LLMRequest) -> LLMResponse:
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": request.output_schema}
        }
        if request.effort is not None:
            output_config["effort"] = request.effort
        started = time.perf_counter()
        message = self._client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=[{"role": "user", "content": _content_blocks(request)}],  # type: ignore[list-item]
            output_config=output_config,  # type: ignore[arg-type]
        )
        latency_ms = round((time.perf_counter() - started) * 1000)

        if message.stop_reason == "refusal":
            raise RefusalError(f"{request.purpose}: model declined the request")
        if message.stop_reason == "max_tokens":
            raise TruncatedError(f"{request.purpose}: output truncated at max_tokens")
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise LLMError(f"{request.purpose}: response had no text block")
        try:
            output = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{request.purpose}: response was not valid JSON") from exc

        usage = _usage(message.usage)
        return LLMResponse(
            output=output,
            model=message.model,
            stop_reason=str(message.stop_reason),
            usage=usage,
            cost_usd=cost_of(request.model, usage),
            latency_ms=latency_ms,
            request_id=getattr(message, "_request_id", None),
        )

    def use_tools(self, request: ToolUseRequest) -> ToolUseResponse:
        """One model turn of a tool-using conversation. The model must call a tool."""
        tools: list[dict[str, Any]] = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "strict": True,
            }
            for t in request.tools
        ]
        # The tools and system prompt are identical on every turn of an investigation, so the
        # last tool definition carries a cache breakpoint.
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        choice: dict[str, Any] = (
            {"type": "tool", "name": request.force_tool} if request.force_tool else {"type": "any"}
        )
        started = time.perf_counter()
        message = self._client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            system=request.system,
            messages=_tool_messages(request.turns),  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
            tool_choice=choice,  # type: ignore[arg-type]
        )
        latency_ms = round((time.perf_counter() - started) * 1000)

        if message.stop_reason == "refusal":
            raise RefusalError(f"{request.purpose}: model declined the request")
        if message.stop_reason == "max_tokens":
            raise TruncatedError(f"{request.purpose}: output truncated at max_tokens")
        calls = tuple(
            ToolCall(id=b.id, name=b.name, input=dict(b.input))  # type: ignore[arg-type]
            for b in message.content
            if b.type == "tool_use"
        )
        if not calls:
            raise LLMError(f"{request.purpose}: the model called no tool")
        text = "".join(b.text for b in message.content if b.type == "text")
        usage = _usage(message.usage)
        return ToolUseResponse(
            text=text,
            calls=calls,
            model=message.model,
            stop_reason=str(message.stop_reason),
            usage=usage,
            cost_usd=cost_of(request.model, usage),
            latency_ms=latency_ms,
            request_id=getattr(message, "_request_id", None),
        )


def _usage(u: Any) -> Usage:
    return Usage(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_input_tokens=u.cache_read_input_tokens or 0,
        cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
    )


def _tool_messages(turns: tuple[Turn, ...]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in turns:
        content: list[dict[str, Any]] = []
        if isinstance(turn, AssistantTurn):
            if turn.text:
                content.append({"type": "text", "text": turn.text})
            content += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                for c in turn.calls
            ]
        else:
            # Tool results must come first in a user turn.
            content += [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in turn.results
            ]
            if turn.text:
                content.append({"type": "text", "text": turn.text})
        messages.append({"role": turn.role, "content": content})
    return messages


# ---------------------------------------------------------------------------- record / replay


class ReplayMode(StrEnum):
    LIVE = "live"
    """Always call the model; never read or write recordings."""
    RECORD = "record"
    """Always call the model and (over)write the recording."""
    REPLAY = "replay"
    """Only serve recordings; a missing one is an error. Needs no API key."""
    AUTO = "auto"
    """Serve a recording if present, otherwise call the model and record it."""


class RecordingLLM:
    """Wraps a client with a directory of recorded responses keyed by request hash.

    Recordings are small JSON files (``<store>/<purpose>/<key>.json``) holding the canonical
    request and the response, so they are readable in review and diffable in git.
    """

    def __init__(self, store: Path, mode: ReplayMode, inner: LLMClient | None = None) -> None:
        if mode is not ReplayMode.REPLAY and inner is None:
            raise ValueError(f"mode {mode.value} needs a live client")
        self.store = store
        self.mode = mode
        self.inner = inner

    def path_for(self, request: LLMRequest | ToolUseRequest) -> Path:
        return self.store / request.purpose / f"{request.key}.json"

    def complete(self, request: LLMRequest) -> LLMResponse:
        def call() -> LLMResponse:
            if self.inner is None:  # unreachable: enforced in __init__
                raise ReplayMissError("no live client configured")
            return self.inner.complete(request)

        return self._serve(request, LLMResponse, call)

    def use_tools(self, request: ToolUseRequest) -> ToolUseResponse:
        def call() -> ToolUseResponse:
            use_tools = getattr(self.inner, "use_tools", None)
            if use_tools is None:
                raise LLMError("the live client does not support tool use")
            response: ToolUseResponse = use_tools(request)
            return response

        return self._serve(request, ToolUseResponse, call)

    def _serve[R: (LLMResponse, ToolUseResponse)](
        self, request: LLMRequest | ToolUseRequest, kind: type[R], call: Callable[[], R]
    ) -> R:
        path = self.path_for(request)
        if self.mode in (ReplayMode.REPLAY, ReplayMode.AUTO) and path.is_file():
            recorded = json.loads(path.read_text(encoding="utf-8"))
            response = kind.model_validate(recorded["response"])
            return response.model_copy(update={"replayed": True})
        if self.mode is ReplayMode.REPLAY:
            raise ReplayMissError(f"no recording for {request.purpose} request {request.key[:12]}")
        response = call()
        if self.mode in (ReplayMode.RECORD, ReplayMode.AUTO):
            _write_recording(path, request.canonical(), response)
        return response


def _write_recording(path: Path, canonical: dict[str, Any], response: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "request": canonical,
        "response": response.model_dump(mode="json", exclude={"replayed"}),
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# ---------------------------------------------------------------------------- fake


ToolHandler = Callable[[ToolUseRequest], list[ToolCall]]


class FakeLLM:
    """Test double: answers each request with ``handler(request)`` and records the calls.

    Tool-use turns are answered by ``tool_handler``, which returns the tool calls to make.
    """

    def __init__(
        self,
        handler: Callable[[LLMRequest], dict[str, Any]],
        tool_handler: ToolHandler | None = None,
    ) -> None:
        self.handler = handler
        self.tool_handler = tool_handler
        self.calls: list[LLMRequest] = []
        self.tool_requests: list[ToolUseRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(
            output=self.handler(request),
            model=request.model,
            stop_reason="end_turn",
            usage=Usage(),
            cost_usd=Decimal(0),
            latency_ms=0,
        )

    def use_tools(self, request: ToolUseRequest) -> ToolUseResponse:
        if self.tool_handler is None:
            raise LLMError(f"{request.purpose}: this fake has no tool handler")
        self.tool_requests.append(request)
        return ToolUseResponse(
            text="",
            calls=tuple(self.tool_handler(request)),
            model=request.model,
            stop_reason="tool_use",
            usage=Usage(),
            cost_usd=Decimal(0),
            latency_ms=0,
        )
