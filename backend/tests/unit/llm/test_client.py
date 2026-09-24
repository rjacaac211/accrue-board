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
    AssistantTurn,
    FilePart,
    LLMError,
    LLMRequest,
    RefusalError,
    ReplayMissError,
    TextPart,
    ToolCall,
    ToolResult,
    ToolSpec,
    ToolUseRequest,
    TruncatedError,
    Usage,
    UserTurn,
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


# ------------------------------------------------------------------ tool use

TOOLS = (
    ToolSpec(name="lookup", description="Look something up.", input_schema=SCHEMA),
    ToolSpec(name="submit", description="Answer.", input_schema=SCHEMA),
)


def tool_request(force: str | None = None, reply: str = "42") -> ToolUseRequest:
    return ToolUseRequest(
        purpose="review_assistant",
        prompt_version="review-assistant-v1",
        model="claude-sonnet-5",
        system="system prompt",
        turns=(
            UserTurn(text="the case"),
            AssistantTurn(
                text="Checking.", calls=(ToolCall(id="c1", name="lookup", input={"answer": "q"}),)
            ),
            UserTurn(results=(ToolResult(call_id="c1", content=reply),)),
        ),
        tools=TOOLS,
        force_tool=force,
    )


def test_tool_request_key_covers_the_conversation() -> None:
    assert tool_request().key == tool_request().key
    assert tool_request().key != tool_request(reply="43").key
    assert tool_request().key != tool_request(force="submit").key


def tool_stub(content: list[Any], stop_reason: str = "tool_use") -> tuple[Any, StubMessages]:
    client, messages = stub_client(None, stop_reason)
    messages.message.content = content
    return client, messages


def test_adapter_sends_the_conversation_and_parses_tool_calls() -> None:
    client, messages = tool_stub(
        [
            SimpleNamespace(type="text", text="Submitting."),
            SimpleNamespace(type="tool_use", id="c2", name="submit", input={"answer": "a"}),
        ]
    )
    response = AnthropicLLM(client).use_tools(tool_request())
    sent = messages.kwargs
    assert sent["tool_choice"] == {"type": "any"}
    assert [t["name"] for t in sent["tools"]] == ["lookup", "submit"]
    assert all(t["strict"] is True for t in sent["tools"])
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assert sent["messages"][1]["content"] == [
        {"type": "text", "text": "Checking."},
        {"type": "tool_use", "id": "c1", "name": "lookup", "input": {"answer": "q"}},
    ]
    assert sent["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "42", "is_error": False}
    ]
    assert "output_config" not in sent
    assert response.calls == (ToolCall(id="c2", name="submit", input={"answer": "a"}),)
    assert response.text == "Submitting."
    assert response.cost_usd == Decimal("0.005400")

    AnthropicLLM(client).use_tools(tool_request(force="submit"))
    assert messages.kwargs["tool_choice"] == {"type": "tool", "name": "submit"}


@pytest.mark.parametrize(
    ("content", "stop_reason", "error"),
    [
        ([], "refusal", RefusalError),
        ([], "max_tokens", TruncatedError),
        ([SimpleNamespace(type="text", text="I am done.")], "end_turn", LLMError),
    ],
)
def test_adapter_rejects_turns_without_a_tool_call(
    content: list[Any], stop_reason: str, error: type[Exception]
) -> None:
    client, _ = tool_stub(content, stop_reason)
    with pytest.raises(error):
        AnthropicLLM(client).use_tools(tool_request())


def test_tool_turns_record_and_replay(tmp_path: Path) -> None:
    fake = FakeLLM(
        lambda _: {},
        tool_handler=lambda _: [ToolCall(id="c9", name="submit", input={"answer": "a"})],
    )
    recorder = RecordingLLM(tmp_path, ReplayMode.AUTO, fake)
    first = recorder.use_tools(tool_request())
    replay = RecordingLLM(tmp_path, ReplayMode.REPLAY).use_tools(tool_request())
    assert replay.replayed
    assert not first.replayed
    assert replay.calls == first.calls
    assert len(fake.tool_requests) == 1
    stored = json.loads(recorder.path_for(tool_request()).read_text())
    assert stored["request"]["turns"][0]["text"] == "the case"
    with pytest.raises(ReplayMissError):
        RecordingLLM(tmp_path, ReplayMode.REPLAY).use_tools(tool_request(reply="other"))


def test_fake_without_tool_handler_refuses_tool_turns() -> None:
    with pytest.raises(LLMError, match="no tool handler"):
        FakeLLM(lambda _: {}).use_tools(tool_request())
