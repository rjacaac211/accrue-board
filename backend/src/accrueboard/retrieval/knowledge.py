"""The per-client knowledge store: confirmed line-item codings used as retrieval examples.

Only human-confirmed codings (plus the seeded history) are ever added, never the system's own
unreviewed guesses, so the store cannot reinforce its own mistakes.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict

from accrueboard.retrieval.embeddings import Embedder
from accrueboard.retrieval.text import tokenize

FUZZY_VENDOR_RATIO = 0.9
RRF_K = 60


class EntrySource(StrEnum):
    HISTORY = "history"
    CONFIRMED = "confirmed"
    """A reviewer approved the proposed account."""
    CORRECTED = "corrected"
    """A reviewer changed the proposed account."""


class KnowledgeEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_id: str
    client_id: str
    vendor_key: str
    vendor_name: str
    description: str
    amount: Decimal
    account: str
    source: EntrySource
    document_ref: str | None = None

    @property
    def text(self) -> str:
        return f"{self.vendor_name} | {self.description}"


@dataclass(frozen=True)
class Hit:
    entry: KnowledgeEntry
    score: float


class KnowledgeStore(Protocol):
    def add(self, entries: Iterable[KnowledgeEntry]) -> None: ...
    def entries(self, client_id: str) -> Sequence[KnowledgeEntry]: ...
    def resolve_vendor(self, client_id: str, vendor_key: str) -> str | None: ...
    def vendor_accounts(self, client_id: str, vendor_key: str) -> Counter[str]: ...
    def search(self, client_id: str, query: str, k: int) -> list[Hit]: ...


# ---------------------------------------------------------------------------- lexical (BM25)


@dataclass
class _Bm25:
    k1: float = 1.2
    b: float = 0.75
    docs: list[list[str]] = field(default_factory=list)
    df: Counter[str] = field(default_factory=Counter)

    def add(self, tokens: list[str]) -> None:
        self.docs.append(tokens)
        self.df.update(set(tokens))

    def scores(self, query: list[str]) -> np.ndarray:
        n = len(self.docs)
        scores = np.zeros(n)
        if n == 0:
            return scores
        avg = sum(len(d) for d in self.docs) / n
        for term in set(query):
            df = self.df.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, doc in enumerate(self.docs):
                tf = doc.count(term)
                if tf:
                    norm = tf + self.k1 * (1 - self.b + self.b * len(doc) / avg)
                    scores[i] += idf * tf * (self.k1 + 1) / norm
        return scores


def reciprocal_rank_fusion[T: Hashable](
    rankings: Sequence[Sequence[T]], k: int = RRF_K
) -> dict[T, float]:
    """Merge ranked lists of items: each item scores sum(1 / (k + rank))."""
    fused: dict[T, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            fused[item] += 1.0 / (k + rank)
    return dict(fused)


# ---------------------------------------------------------------------------- in-memory store


class MemoryKnowledgeStore:
    """In-process store: BM25 + embedding cosine similarity fused with RRF.

    Used by tests and the evaluation (including the learning-curve experiment, which needs cheap
    scratch copies). The Postgres store implements the same interface for the running app.
    """

    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder
        self._entries: dict[str, list[KnowledgeEntry]] = defaultdict(list)
        self._bm25: dict[str, _Bm25] = defaultdict(_Bm25)
        self._vectors: dict[str, list[np.ndarray]] = defaultdict(list)

    def add(self, entries: Iterable[KnowledgeEntry]) -> None:
        batch = list(entries)
        if not batch:
            return
        vectors = self.embedder.embed([e.text for e in batch])
        for entry, vector in zip(batch, vectors, strict=True):
            self._entries[entry.client_id].append(entry)
            self._bm25[entry.client_id].add(tokenize(entry.text))
            self._vectors[entry.client_id].append(vector)

    def entries(self, client_id: str) -> Sequence[KnowledgeEntry]:
        return tuple(self._entries[client_id])

    def resolve_vendor(self, client_id: str, vendor_key: str) -> str | None:
        """The known vendor key matching ``vendor_key`` exactly, else the closest fuzzy match."""
        known = {e.vendor_key for e in self._entries[client_id]}
        if vendor_key in known:
            return vendor_key
        best, best_ratio = None, 0.0
        for candidate in sorted(known):
            ratio = SequenceMatcher(None, vendor_key, candidate).ratio()
            if ratio > best_ratio:
                best, best_ratio = candidate, ratio
        return best if best_ratio >= FUZZY_VENDOR_RATIO else None

    def vendor_accounts(self, client_id: str, vendor_key: str) -> Counter[str]:
        return Counter(e.account for e in self._entries[client_id] if e.vendor_key == vendor_key)

    def search(self, client_id: str, query: str, k: int) -> list[Hit]:
        entries = self._entries[client_id]
        if not entries:
            return []
        depth = min(len(entries), max(k * 4, 20))
        lexical = self._bm25[client_id].scores(tokenize(query))
        lexical_rank = [
            int(i) for i in np.argsort(-lexical, kind="stable")[:depth] if lexical[i] > 0
        ]
        matrix = np.vstack(self._vectors[client_id])
        query_vector = self.embedder.embed([query])[0]
        dense = np.round(matrix @ query_vector, 6)
        dense_rank = [int(i) for i in np.argsort(-dense, kind="stable")[:depth]]
        fused = reciprocal_rank_fusion([lexical_rank, dense_rank])
        ordered = sorted(
            fused.items(), key=lambda item: (-round(item[1], 9), entries[item[0]].entry_id)
        )
        return [Hit(entry=entries[i], score=score) for i, score in ordered[:k]]
