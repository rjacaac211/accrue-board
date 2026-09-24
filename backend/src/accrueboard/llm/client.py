"""LLM clients: the real Anthropic client, a record/replay wrapper, and a fake for tests."""

import json
import time
from collections.abc import Callable
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import anthropic

from accrueboard.llm.types import (
    FilePart,
    LLMError,
    LLMRequest,
    LLMResponse,
    RefusalError,
    ReplayMissError,
    TruncatedError,
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

        u = message.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
        )
        return LLMResponse(
            output=output,
            model=message.model,
            stop_reason=str(message.stop_reason),
            usage=usage,
            cost_usd=cost_of(request.model, usage),
            latency_ms=latency_ms,
            request_id=getattr(message, "_request_id", None),
        )


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

    def path_for(self, request: LLMRequest) -> Path:
        return self.store / request.purpose / f"{request.key}.json"

    def complete(self, request: LLMRequest) -> LLMResponse:
        path = self.path_for(request)
        if self.mode in (ReplayMode.REPLAY, ReplayMode.AUTO) and path.is_file():
            recorded = json.loads(path.read_text(encoding="utf-8"))
            response = LLMResponse.model_validate(recorded["response"])
            return response.model_copy(update={"replayed": True})
        if self.mode is ReplayMode.REPLAY:
            raise ReplayMissError(f"no recording for {request.purpose} request {request.key[:12]}")
        if self.inner is None:  # unreachable: enforced in __init__
            raise ReplayMissError("no live client configured")
        response = self.inner.complete(request)
        if self.mode in (ReplayMode.RECORD, ReplayMode.AUTO):
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "request": request.canonical(),
                "response": response.model_dump(mode="json", exclude={"replayed"}),
            }
            path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        return response


# ---------------------------------------------------------------------------- fake


class FakeLLM:
    """Test double: answers each request with ``handler(request)`` and records the calls."""

    def __init__(self, handler: Callable[[LLMRequest], dict[str, Any]]) -> None:
        self.handler = handler
        self.calls: list[LLMRequest] = []

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
