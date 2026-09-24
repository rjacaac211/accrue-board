"""Shared API dependencies: database sessions, the shared clock and the embedder."""

from collections.abc import Iterator
from functools import lru_cache

from fastapi import Request
from sqlalchemy.orm import Session

from accrueboard.config import get_settings
from accrueboard.db.session import get_sessionmaker
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
