"""Build the configured LLM client (record/replay around the Anthropic API)."""

from accrueboard.config import Settings
from accrueboard.llm.client import AnthropicLLM, FakeLLM, ModelClient, RecordingLLM, ReplayMode


class MissingCredentialsError(RuntimeError):
    pass


ORACLE = "oracle"
"""LLM_MODE for tests and keyless demos: answers from the synthetic dataset's ground truth."""


def build_llm(settings: Settings) -> ModelClient:
    if settings.llm_mode == ORACLE:
        from accrueboard.eval.oracle import Oracle  # noqa: PLC0415 - only in this mode

        return FakeLLM(Oracle.from_datasets(settings.data_dir / "generated"))
    mode = ReplayMode(settings.llm_mode)
    if mode is ReplayMode.REPLAY:
        return RecordingLLM(settings.recordings_dir, mode)
    if not settings.anthropic_api_key:
        raise MissingCredentialsError(
            f"LLM_MODE={mode.value} needs ANTHROPIC_API_KEY (set it in .env), "
            "or use LLM_MODE=replay to serve recorded responses only"
        )
    import anthropic  # noqa: PLC0415 - only needed for live calls

    live = AnthropicLLM(anthropic.Anthropic(api_key=settings.anthropic_api_key))
    if mode is ReplayMode.LIVE:
        return live
    return RecordingLLM(settings.recordings_dir, mode, live)
