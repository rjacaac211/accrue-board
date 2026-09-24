"""Provider-neutral request/response types for the pipeline's LLM calls.

Pipeline calls are single structured-output requests: a system prompt, one user turn made of
text and document parts, and a JSON schema the reply must follow. The review assistant uses
tool-use requests instead (a conversation plus tools). Both kinds are canonicalised and hashed,
which is what the record/replay layer keys on.
"""

import base64
import hashlib
import json
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _hash(canonical: dict[str, Any]) -> str:
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TextPart(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["text"] = "text"
    text: str


class FilePart(BaseModel):
    """A PDF or image sent to the model. ``data`` is the raw file bytes."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["file"] = "file"
    media_type: Literal["application/pdf", "image/png", "image/jpeg"]
    data: bytes = Field(repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def base64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")


Part = TextPart | FilePart


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    purpose: str
    """What the call is for (classify, extract, verify, code, ...)."""
    prompt_version: str
    model: str
    system: str
    parts: tuple[Part, ...]
    output_schema: dict[str, Any]
    max_tokens: int = 4096
    effort: Literal["low", "medium", "high"] | None = None

    def canonical(self) -> dict[str, Any]:
        """A JSON-safe description of the request with file bytes replaced by their hash."""
        parts: list[dict[str, Any]] = []
        for part in self.parts:
            if isinstance(part, FilePart):
                parts.append({"kind": "file", "media_type": part.media_type, "sha256": part.sha256})
            else:
                parts.append({"kind": "text", "text": part.text})
        return {
            "purpose": self.purpose,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "system": self.system,
            "parts": parts,
            "output_schema": self.output_schema,
            "max_tokens": self.max_tokens,
            "effort": self.effort,
        }

    @property
    def key(self) -> str:
        """Stable hash of the canonical request (the record/replay key)."""
        return _hash(self.canonical())


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    output: dict[str, Any]
    model: str
    stop_reason: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    request_id: str | None = None
    replayed: bool = False


# ---------------------------------------------------------------------------- tool use
#
# The review assistant holds a conversation: the model asks for tools, the caller runs them
# and sends back the results. Each model turn is one request whose canonical form contains the
# whole conversation so far, so a recorded investigation replays turn by turn.


class ToolSpec(BaseModel):
    """A tool the model may call. Schemas follow the strict tool-use subset of JSON Schema."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    input: dict[str, Any]


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    content: str
    is_error: bool = False


class UserTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user"] = "user"
    text: str = ""
    results: tuple[ToolResult, ...] = ()


class AssistantTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["assistant"] = "assistant"
    text: str = ""
    calls: tuple[ToolCall, ...] = ()


Turn = UserTurn | AssistantTurn


class ToolUseRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    purpose: str
    prompt_version: str
    model: str
    system: str
    turns: tuple[Turn, ...]
    tools: tuple[ToolSpec, ...]
    force_tool: str | None = None
    """Name of a tool the model must call this turn; otherwise it must call some tool."""
    max_tokens: int = 4096

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def key(self) -> str:
        return _hash(self.canonical())


class ToolUseResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    calls: tuple[ToolCall, ...]
    model: str
    stop_reason: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    request_id: str | None = None
    replayed: bool = False

    @property
    def turn(self) -> AssistantTurn:
        return AssistantTurn(text=self.text, calls=self.calls)


class LLMError(RuntimeError):
    """A call completed but did not produce a usable structured answer."""


class RefusalError(LLMError):
    pass


class TruncatedError(LLMError):
    pass


class ReplayMissError(LLMError):
    """Replay-only mode found no recording for this request."""
