from pathlib import Path

import pytest

from accrueboard.config import Settings
from accrueboard.datagen.render import render
from accrueboard.datagen.spec import load_client
from accrueboard.llm.client import FakeLLM, RecordingLLM, ReplayMode
from accrueboard.llm.factory import MissingCredentialsError, build_llm
from accrueboard.llm.types import LLMError, ToolUseRequest
from accrueboard.pipeline.extraction import Models, classify, extract
from accrueboard.pipeline.files import SourceFile
from tests.unit.pipeline.helpers import eval_records


def settings(tmp_path: Path, mode: str, key: str | None = None) -> Settings:
    return Settings(
        llm_mode=mode,
        anthropic_api_key=key,
        data_dir=tmp_path,
        recordings_dir=tmp_path / "recordings",
        _env_file=None,  # type: ignore[call-arg]
    )


def test_replay_needs_no_key_and_live_modes_do(tmp_path: Path) -> None:
    replay = build_llm(settings(tmp_path, "replay"))
    assert isinstance(replay, RecordingLLM)
    assert replay.mode is ReplayMode.REPLAY
    with pytest.raises(MissingCredentialsError, match="ANTHROPIC_API_KEY"):
        build_llm(settings(tmp_path, "auto"))


def test_oracle_mode_reads_the_generated_dataset(tmp_path: Path) -> None:
    spec = load_client("fernhill")
    records = [r for r in eval_records() if r.split.value == "validation" and r.file][:2]
    dataset = tmp_path / "generated" / spec.id
    for record in records:
        assert record.file is not None
        (dataset / record.file).parent.mkdir(parents=True, exist_ok=True)
        (dataset / record.file).write_bytes(render(record, spec))
    (dataset / "validation.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8"
    )

    llm = build_llm(settings(tmp_path, "oracle"))
    assert isinstance(llm, FakeLLM)
    for record in records:
        assert record.file is not None
        source = SourceFile.from_bytes(record.file, (dataset / record.file).read_bytes())
        kind = classify(llm, source, "m").doc_type
        assert kind is record.document.doc_type
        models = Models(classify="m", extract="m", verify="m")
        result = extract(llm, source, kind, models, today=record.received_at.date())
        assert result.document == record.document
    with pytest.raises(LLMError, match="no tool handler"):
        llm.use_tools(ToolUseRequest.model_construct(purpose="review_assistant"))
