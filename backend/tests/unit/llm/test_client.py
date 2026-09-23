import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from accrueboard.llm.client import (
    AnthropicLLM,
    FakeLLM,
    RecordingLLM,
    ReplayMode,
    cost_of,
)
from accrueboard.llm.types import (
    FilePart,
    LLMError,
    LLMRequest,
    RefusalError,
    ReplayMissError,
    TextPart,
    TruncatedError,
    Usage,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def request(data: bytes = b"%PDF-1.4 fake", text: str = "go") -> LLMRequest:
    return LLMRequest(
        purpose="extract",
        prompt_version="extract-v1",
        model="claude-sonnet-5",
        system="system prompt",
        parts=(FilePart(media_type="application/pdf", data=data), TextPart(text=text)),
        output_schema=SCHEMA,
    )


# ------------------------------------------------------------------ keys and cost


def test_key_is_stable_and_content_sensitive() -> None:
    assert request().key == request().key
    assert request().key != request(data=b"%PDF-1.4 other").key
    assert request().key != request(text="stop").key
    assert request().key != request().model_copy(update={"prompt_version": "extract-v2"}).key


def test_canonical_form_hashes_file_bytes() -> None:
    canonical = json.dumps(request().canonical())
    assert "fake" not in canonical
    assert request().parts[0].sha256 in canonical  # type: ignore[union-attr]


def test_cost_uses_model_rates_and_cache_multipliers() -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    # Sonnet 5: $2 in + $1 out + $0.20 cache read + $2.50 cache write
    assert cost_of("claude-sonnet-5", usage) == Decimal("5.700000")
    assert cost_of("unknown-model", usage) == Decimal(0)


# ------------------------------------------------------------------ record / replay


def test_auto_mode_records_then_replays(tmp_path: Path) -> None:
    fake = FakeLLM(lambda _: {"answer": "42"})
    recorder = RecordingLLM(tmp_path, ReplayMode.AUTO, fake)
    first = recorder.complete(request())
    second = recorder.complete(request())
    assert first.output == second.output == {"answer": "42"}
    assert not first.replayed
    assert second.replayed
    assert len(fake.calls) == 1
    stored = json.loads(recorder.path_for(request()).read_text())
    assert stored["request"]["purpose"] == "extract"
    assert stored["response"]["output"] == {"answer": "42"}


def test_replay_mode_needs_no_client_and_fails_on_miss(tmp_path: Path) -> None:
    RecordingLLM(tmp_path, ReplayMode.RECORD, FakeLLM(lambda _: {"answer": "x"})).complete(
        request()
    )
    replay = RecordingLLM(tmp_path, ReplayMode.REPLAY)
    assert replay.complete(request()).output == {"answer": "x"}
    with pytest.raises(ReplayMissError):
        replay.complete(request(text="something new"))


def test_live_mode_never_writes(tmp_path: Path) -> None:
    RecordingLLM(tmp_path, ReplayMode.LIVE, FakeLLM(lambda _: {"answer": "x"})).complete(request())
    assert list(tmp_path.iterdir()) == []


def test_record_mode_overwrites(tmp_path: Path) -> None:
    RecordingLLM(tmp_path, ReplayMode.RECORD, FakeLLM(lambda _: {"answer": "old"})).complete(
        request()
    )
    RecordingLLM(tmp_path, ReplayMode.RECORD, FakeLLM(lambda _: {"answer": "new"})).complete(
        request()
    )
    assert RecordingLLM(tmp_path, ReplayMode.REPLAY).complete(request()).output == {"answer": "new"}


def test_modes_other_than_replay_need_a_client(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="live client"):
        RecordingLLM(tmp_path, ReplayMode.AUTO)


# ------------------------------------------------------------------ Anthropic adapter


class StubMessages:
    def __init__(self, message: Any) -> None:
        self.message = message
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.message


def stub_client(text: str | None, stop_reason: str = "end_turn") -> tuple[Any, StubMessages]:
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    message = SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        model="claude-sonnet-5",
        usage=SimpleNamespace(
            input_tokens=1200,
            output_tokens=300,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
        ),
        _request_id="req_123",
    )
    messages = StubMessages(message)
    return SimpleNamespace(messages=messages), messages


def test_adapter_sends_document_blocks_and_schema() -> None:
    client, messages = stub_client('{"answer": "ok"}')
    png = LLMRequest(
        purpose="extract",
        prompt_version="v",
        model="claude-sonnet-5",
        system="s",
        parts=(FilePart(media_type="image/png", data=b"\x89PNG"), TextPart(text="go")),
        output_schema=SCHEMA,
        effort="medium",
    )
    for req, block_type in ((request(), "document"), (png, "image")):
        response = AnthropicLLM(client).complete(req)
        blocks = messages.kwargs["messages"][0]["content"]
        assert blocks[0]["type"] == block_type
        assert blocks[0]["source"]["type"] == "base64"
        assert blocks[1] == {"type": "text", "text": "go"}
        assert messages.kwargs["output_config"]["format"]["schema"] == SCHEMA
        assert response.output == {"answer": "ok"}
        assert response.request_id == "req_123"
        assert response.usage.input_tokens == 1200
        assert response.cost_usd == Decimal("0.005400")
    assert messages.kwargs["output_config"]["effort"] == "medium"
    assert "temperature" not in messages.kwargs


@pytest.mark.parametrize(
    ("text", "stop_reason", "error"),
    [
        ('{"answer": "x"}', "refusal", RefusalError),
        ('{"answer": ', "max_tokens", TruncatedError),
        ("not json", "end_turn", LLMError),
        (None, "end_turn", LLMError),
    ],
)
def test_adapter_rejects_unusable_responses(
    text: str | None, stop_reason: str, error: type[Exception]
) -> None:
    client, _ = stub_client(text, stop_reason)
    with pytest.raises(error):
        AnthropicLLM(client).complete(request())
