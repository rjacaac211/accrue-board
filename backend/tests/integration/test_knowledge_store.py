"""The Postgres knowledge store must behave like the in-memory one used by the evaluation."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import EVAL_SPLITS, ClientSpec
from accrueboard.db.models import Client
from accrueboard.domain.documents import DocumentType
from accrueboard.domain.duplicates import normalize_vendor
from accrueboard.retrieval.embeddings import HashingEmbedder
from accrueboard.retrieval.knowledge import MemoryKnowledgeStore
from accrueboard.retrieval.pg_store import PgKnowledgeStore
from accrueboard.retrieval.seed import history_entries

pytestmark = pytest.mark.integration

NOW = datetime(2026, 3, 2, tzinfo=UTC)
EMBEDDER = HashingEmbedder(dimensions=384)


@pytest.fixture
def stores(
    session: Session, spec: ClientSpec, records: list[GroundTruth]
) -> tuple[PgKnowledgeStore, MemoryKnowledgeStore]:
    session.add(Client(id=spec.id, name=spec.name, business=spec.business, config={}))
    session.flush()
    entries = history_entries(records)
    pg = PgKnowledgeStore(session, EMBEDDER, now=lambda: NOW)
    pg.add(entries)
    memory = MemoryKnowledgeStore(EMBEDDER)
    memory.add(entries)
    return pg, memory


def test_entries_and_vendor_history_match(
    stores: tuple[PgKnowledgeStore, MemoryKnowledgeStore], spec: ClientSpec
) -> None:
    pg, memory = stores
    assert [e.entry_id for e in pg.entries(spec.id)] == [
        e.entry_id for e in memory.entries(spec.id)
    ]
    for key in {e.vendor_key for e in memory.entries(spec.id)}:
        assert pg.vendor_accounts(spec.id, key) == memory.vendor_accounts(spec.id, key)
        assert pg.resolve_vendor(spec.id, key) == key


def test_fuzzy_vendor_resolution(
    stores: tuple[PgKnowledgeStore, MemoryKnowledgeStore], spec: ClientSpec
) -> None:
    pg, memory = stores
    for variant, expected in (
        ("oakridge textile", "oakridge textiles"),
        ("boxcraft packaging suply", "boxcraft packaging supply"),
    ):
        assert pg.resolve_vendor(spec.id, variant) == expected
        assert memory.resolve_vendor(spec.id, variant) == expected
    assert pg.resolve_vendor(spec.id, "completely different vendor") is None


def test_search_agrees_on_the_retrieved_accounts(
    stores: tuple[PgKnowledgeStore, MemoryKnowledgeStore],
    spec: ClientSpec,
    records: list[GroundTruth],
) -> None:
    pg, memory = stores
    queries = [
        f"{r.document.vendor_name} | {line.description}"
        for r in records
        if r.split in EVAL_SPLITS and r.document.doc_type is not DocumentType.OTHER
        for line in r.document.lines
    ][:120]
    same_top = 0
    for query in queries:
        pg_hits = pg.search(spec.id, query, k=6)
        mem_hits = memory.search(spec.id, query, k=6)
        assert len(pg_hits) == len(mem_hits) == 6
        same_top += pg_hits[0].entry.account == mem_hits[0].entry.account
    assert same_top / len(queries) >= 0.95


def test_search_is_scoped_to_the_client(
    stores: tuple[PgKnowledgeStore, MemoryKnowledgeStore],
) -> None:
    pg, _ = stores
    assert pg.search("someone-else", "linen throw", k=3) == []
    assert normalize_vendor("Oakridge Textiles Inc.") == "oakridge textiles"
