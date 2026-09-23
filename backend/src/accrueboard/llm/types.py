"""Provider-neutral request/response types for the pipeline's LLM calls.

Every call is a single structured-output request: a system prompt, one user turn made of text
and document parts, and a JSON schema the reply must follow. Requests are canonicalised and
hashed, which is what the record/replay layer keys on.
"""

import base64
import hashlib
import json
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
        blob = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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


class LLMError(RuntimeError):
    """A call completed but did not produce a usable structured answer."""


class RefusalError(LLMError):
    pass


class TruncatedError(LLMError):
    pass


class ReplayMissError(LLMError):
    """Replay-only mode found no recording for this request."""
