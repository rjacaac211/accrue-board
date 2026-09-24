from decimal import Decimal

import numpy as np
import pytest

from accrueboard.retrieval.classifier import AccountClassifier
from accrueboard.retrieval.embeddings import HashingEmbedder
from accrueboard.retrieval.knowledge import (
    EntrySource,
    KnowledgeEntry,
    MemoryKnowledgeStore,
    reciprocal_rank_fusion,
)


def entry(i: int, vendor: str, description: str, account: str) -> KnowledgeEntry:
    return KnowledgeEntry(
        entry_id=f"e{i:03d}",
        client_id="c1",
        vendor_key=vendor.lower(),
        vendor_name=vendor,
        description=description,
        amount=Decimal("10.00"),
        account=account,
        source=EntrySource.HISTORY,
    )


ENTRIES = [
    entry(1, "Paperline", "Copy paper letter case", "6100"),
    entry(2, "Paperline", "Black ink cartridge 2-pack", "6100"),
    entry(3, "Boxworks", "Corrugated shipping box bundle", "5300"),
    entry(4, "Boxworks", "Kraft padded mailer case", "5300"),
    entry(5, "Oak Textiles", "Linen throw blanket natural", "1300"),
    entry(6, "Oak Textiles", "Waffle bath towel set", "1300"),
    entry(7, "Oak Textiles", "Cotton napkins set of four", "1300"),
]


@pytest.fixture
def store() -> MemoryKnowledgeStore:
    s = MemoryKnowledgeStore(HashingEmbedder())
    s.add(ENTRIES)
    return s


def test_rrf_rewards_items_ranked_well_by_both_lists() -> None:
    fused = reciprocal_rank_fusion([[1, 2, 3], [2, 1, 4]])
    assert fused[1] == fused[2]
    assert fused[1] > fused[3]
    assert fused[1] > fused[4]


def test_hashing_embedder_is_deterministic_and_unit_length() -> None:
    embedder = HashingEmbedder()
    a = embedder.embed(["linen blanket", "linen blanket", "shipping box"])
    np.testing.assert_allclose(a[0], a[1])
    np.testing.assert_allclose(np.linalg.norm(a, axis=1), 1.0)
    assert a[0] @ a[1] > a[0] @ a[2]


def test_search_finds_similar_items_first(store: MemoryKnowledgeStore) -> None:
    hits = store.search("c1", "Boxworks | shipping box 12x10x6", k=3)
    assert hits[0].entry.entry_id == "e003"
    assert {h.entry.account for h in hits[:2]} == {"5300"}


def test_search_is_scoped_to_the_client(store: MemoryKnowledgeStore) -> None:
    assert store.search("someone-else", "shipping box", k=3) == []


def test_search_ordering_is_stable(store: MemoryKnowledgeStore) -> None:
    first = [h.entry.entry_id for h in store.search("c1", "set", k=5)]
    second = [h.entry.entry_id for h in store.search("c1", "set", k=5)]
    assert first == second


def test_vendor_resolution(store: MemoryKnowledgeStore) -> None:
    assert store.resolve_vendor("c1", "oak textiles") == "oak textiles"
    assert store.resolve_vendor("c1", "oak textile") == "oak textiles"  # fuzzy
    assert store.resolve_vendor("c1", "globex") is None


def test_vendor_accounts(store: MemoryKnowledgeStore) -> None:
    assert store.vendor_accounts("c1", "boxworks") == {"5300": 2}


def test_classifier_learns_accounts() -> None:
    clf = AccountClassifier().fit(ENTRIES)
    prediction = clf.predict("Oak Textiles", "Linen table runner")
    assert prediction is not None
    assert prediction.account == "1300"
    assert 0 < prediction.probability <= 1
    assert prediction.ranked[0][0] == "1300"


def test_classifier_edge_cases() -> None:
    assert AccountClassifier().fit([]).predict("x", "y") is None
    single = AccountClassifier().fit(ENTRIES[:2])
    prediction = single.predict("x", "anything")
    assert prediction is not None
    assert (prediction.account, prediction.probability) == ("6100", 1.0)
