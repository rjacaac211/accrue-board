"""Build the configured LLM client (record/replay around the Anthropic API)."""

from accrueboard.config import Settings
from accrueboard.llm.client import AnthropicLLM, ModelClient, RecordingLLM, ReplayMode


class MissingCredentialsError(RuntimeError):
    pass


def build_llm(settings: Settings) -> ModelClient:
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
