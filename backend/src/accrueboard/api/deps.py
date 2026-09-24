"""Shared API dependencies: database sessions, the shared clock, the embedder, the assistant."""

from collections.abc import Iterator
from functools import lru_cache

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from accrueboard.agents.review_assistant.service import ReviewAssistant
from accrueboard.config import get_settings
from accrueboard.db.session import get_sessionmaker
from accrueboard.llm.client import ModelClient
from accrueboard.llm.factory import MissingCredentialsError, build_llm
from accrueboard.retrieval.embeddings import Embedder, FastEmbedder, HashingEmbedder
from accrueboard.services.clock import SharedClock


def get_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def get_clock(request: Request) -> SharedClock:
    clock: SharedClock = request.app.state.clock
    return clock


@lru_cache
def get_embedder() -> Embedder:
    settings = get_settings()
    if settings.embedder == "hashing":
        return HashingEmbedder(dimensions=384)
    return FastEmbedder(settings.embedding_model, cache_dir=str(settings.embedding_cache_dir))


@lru_cache
def _llm() -> ModelClient:
    return build_llm(get_settings())


def get_assistant(request: Request) -> ReviewAssistant:
    try:
        llm = _llm()
    except MissingCredentialsError as exc:
        raise HTTPException(503, str(exc)) from exc
    return ReviewAssistant(llm, get_settings().model_assistant, get_embedder(), get_clock(request))
