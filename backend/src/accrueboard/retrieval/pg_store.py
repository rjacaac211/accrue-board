"""Postgres-backed knowledge store: full-text search, trigram vendor matching and pgvector.

Implements the same interface and the same rank fusion as the in-memory store. The lexical
ranking uses Postgres full-text rank (``ts_rank_cd``) rather than BM25, so orderings can differ
slightly; an integration test checks that both stores agree on the retrieved accounts.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from accrueboard.db.models import KnowledgeRow
from accrueboard.retrieval.embeddings import Embedder
from accrueboard.retrieval.knowledge import (
    EntrySource,
    Hit,
    KnowledgeEntry,
    reciprocal_rank_fusion,
)
from accrueboard.retrieval.text import tokenize

TRIGRAM_SIMILARITY = 0.6


def _entry(row: KnowledgeRow) -> KnowledgeEntry:
    return KnowledgeEntry(
        entry_id=row.id,
        client_id=row.client_id,
        vendor_key=row.vendor_key,
        vendor_name=row.vendor_name,
        description=row.description,
        amount=row.amount,
        account=row.account,
        source=EntrySource(row.source),
        document_ref=row.document_ref,
    )


class PgKnowledgeStore:
    def __init__(
        self,
        session: Session,
        embedder: Embedder,
        now: Callable[[], datetime],
    ) -> None:
        self.session = session
        self.embedder = embedder
        self.now = now

    def add(self, entries: Iterable[KnowledgeEntry]) -> None:
        batch = list(entries)
        if not batch:
            return
        vectors = self.embedder.embed([e.text for e in batch])
        created = self.now()
        self.session.add_all(
            KnowledgeRow(
                id=e.entry_id,
                client_id=e.client_id,
                vendor_key=e.vendor_key,
                vendor_name=e.vendor_name,
                description=e.description,
                amount=e.amount,
                account=e.account,
                source=e.source.value,
                document_ref=e.document_ref,
                created_at=created,
                embedding=vector.tolist(),
            )
            for e, vector in zip(batch, vectors, strict=True)
        )
        self.session.flush()

    def entries(self, client_id: str) -> Sequence[KnowledgeEntry]:
        rows = self.session.execute(
            select(KnowledgeRow)
            .where(KnowledgeRow.client_id == client_id)
            .order_by(KnowledgeRow.seq)
        ).scalars()
        return tuple(_entry(row) for row in rows)

    def resolve_vendor(self, client_id: str, vendor_key: str) -> str | None:
        exact = self.session.execute(
            select(KnowledgeRow.vendor_key)
            .where(KnowledgeRow.client_id == client_id, KnowledgeRow.vendor_key == vendor_key)
            .limit(1)
        ).scalar_one_or_none()
        if exact is not None:
            return exact
        similarity = func.similarity(KnowledgeRow.vendor_key, vendor_key)
        best = self.session.execute(
            select(KnowledgeRow.vendor_key, similarity.label("s"))
            .where(KnowledgeRow.client_id == client_id, similarity >= TRIGRAM_SIMILARITY)
            .group_by(KnowledgeRow.vendor_key)
            .order_by(similarity.desc(), KnowledgeRow.vendor_key)
            .limit(1)
        ).first()
        return best[0] if best else None

    def vendor_accounts(self, client_id: str, vendor_key: str) -> Counter[str]:
        rows = self.session.execute(
            select(KnowledgeRow.account, func.count())
            .where(KnowledgeRow.client_id == client_id, KnowledgeRow.vendor_key == vendor_key)
            .group_by(KnowledgeRow.account)
        )
        return Counter({account: count for account, count in rows})

    def search(self, client_id: str, query: str, k: int) -> list[Hit]:
        depth = max(k * 4, 20)
        tokens = sorted(set(tokenize(query)))
        lexical: list[str] = []
        if tokens:
            tsquery = func.to_tsquery("simple", " | ".join(tokens))
            rank = func.ts_rank_cd(KnowledgeRow.tsv, tsquery)
            lexical = list(
                self.session.execute(
                    select(KnowledgeRow.id)
                    .where(KnowledgeRow.client_id == client_id, KnowledgeRow.tsv.op("@@")(tsquery))
                    .order_by(rank.desc(), KnowledgeRow.seq)
                    .limit(depth)
                ).scalars()
            )
        vector = self.embedder.embed([query])[0].tolist()
        dense = list(
            self.session.execute(
                select(KnowledgeRow.id)
                .where(KnowledgeRow.client_id == client_id)
                .order_by(KnowledgeRow.embedding.cosine_distance(vector), KnowledgeRow.seq)
                .limit(depth)
            ).scalars()
        )
        fused = reciprocal_rank_fusion([lexical, dense])
        top = sorted(fused.items(), key=lambda item: (-round(item[1], 9), item[0]))[:k]
        if not top:
            return []
        rows = {
            row.id: row
            for row in self.session.execute(
                select(KnowledgeRow).where(KnowledgeRow.id.in_([i for i, _ in top]))
            ).scalars()
        }
        return [Hit(entry=_entry(rows[i]), score=score) for i, score in top]
