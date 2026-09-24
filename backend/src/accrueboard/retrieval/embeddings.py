"""Text embedders: a local ONNX model for real use and a hashing embedder for tests."""

import hashlib
from collections.abc import Sequence
from typing import Any, Protocol

import numpy as np

from accrueboard.retrieval.text import tokenize

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    dimensions: int

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Unit-length vectors, one row per text."""
        ...


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class FastEmbedder:
    """A small local sentence-embedding model run with ONNX (no GPU, no API calls).

    The model (~70 MB) is downloaded once on first use and cached.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, cache_dir: str | None = None) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: Any = None
        self.dimensions = 384

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding  # noqa: PLC0415 - heavy import, loaded lazily

            self._model = TextEmbedding(model_name=self.model_name, cache_dir=self.cache_dir)
        return self._model

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimensions))
        vectors = np.array(list(self._load().embed(list(texts))), dtype=np.float64)
        return _normalize(vectors)


class HashingEmbedder:
    """Deterministic bag-of-words embedder for tests: similar wording -> similar vectors."""

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dimensions))
        for row, text in enumerate(texts):
            for token in tokenize(text):
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                matrix[row, bucket] += sign
        return _normalize(matrix)


def configured_embedder() -> Embedder:
    """The embedder chosen by the settings (``EMBEDDER``): the local model, or hashing."""
    from accrueboard.config import get_settings  # noqa: PLC0415 - keeps this module standalone

    settings = get_settings()
    if settings.embedder == "hashing":
        return HashingEmbedder(dimensions=384)
    return FastEmbedder(settings.embedding_model, cache_dir=str(settings.embedding_cache_dir))
