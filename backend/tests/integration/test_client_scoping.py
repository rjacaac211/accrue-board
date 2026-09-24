"""Two clients, one shared vendor: each client's knowledge stays its own.

The retailer and the contractor both buy paper towels at the same wholesale club. The retailer
books them to Cleaning & Breakroom, the contractor to Job Materials. Coding signals for one
client must never draw on the other's history.
"""

import pytest

from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import Split
from accrueboard.db.models import Task
from accrueboard.domain.duplicates import normalize_vendor
from accrueboard.retrieval.pg_store import PgKnowledgeStore

from .helpers import EMBEDDER, World, make_world

pytestmark = pytest.mark.integration

VENDOR = "metrowholesale"
TOWELS = "towel"  # any of the item's printed wordings


def towel_receipt(world: World) -> tuple[GroundTruth, int]:
    for record in sorted(
        (r for r in world.records if r.split is Split.VALIDATION and r.vendor_id == VENDOR),
        key=lambda r: (r.received_at, r.doc_id),
    ):
        for i, line in enumerate(record.document.lines):
            if TOWELS in line.description.lower() and not record.anomalies:
                return record, i
    raise AssertionError("no paper-towel receipt in the validation split")


@pytest.fixture(scope="module")
def worlds() -> tuple[World, World]:
    return make_world("sr", "fernhill"), make_world("sc", "ridgeline")


def test_shared_vendor_is_coded_by_each_clients_own_history(worlds: tuple[World, World]) -> None:
    retailer, contractor = worlds
    expected = {retailer.spec.id: "6750", contractor.spec.id: "5000"}
    for world, other in ((retailer, contractor), (contractor, retailer)):
        record, line = towel_receipt(world)
        assert record.line_accounts[line] == expected[world.spec.id]
        task_id = world.process(record)

        with world.sessions() as session:
            task = session.get(Task, task_id)
            assert task is not None
            coding = task.coding or {}
            own_accounts = {a.code for a in world.spec.accounts}
            # Vendor memory: only this client's past purchases from the shared vendor.
            assert coding["vendor_known"] is True
            assert set(coding["vendor_history"]) <= own_accounts
            assert expected[world.spec.id] in coding["vendor_history"]
            assert expected[other.spec.id] not in coding["vendor_history"]
            # The classifier, trained on this client alone, predicts this client's account. The
            # neighbour vote may differ (similar wording such as "copy paper"), but it can only
            # ever name one of this client's own accounts.
            signals = coding["lines"][line]
            assert signals["classifier_account"] == expected[world.spec.id]
            assert signals["neighbour_account"] in own_accounts

            # Every retrieved example belongs to this client.
            store = PgKnowledgeStore(session, EMBEDDER, now=world.clock.now)
            own_ids = {e.entry_id for e in store.entries(world.spec.id)}
            other_ids = {e.entry_id for e in store.entries(other.spec.id)}
            neighbours = {n for ln in coding["lines"] for n in ln["neighbour_ids"]}
            assert neighbours
            assert neighbours <= own_ids
            assert not neighbours & other_ids


def test_both_clients_know_the_vendor_separately(worlds: tuple[World, World]) -> None:
    retailer, contractor = worlds
    key = normalize_vendor("Metro Wholesale Club")
    assert key is not None
    with retailer.sessions() as session:
        store = PgKnowledgeStore(session, EMBEDDER, now=retailer.clock.now)
        mine = store.vendor_accounts(retailer.spec.id, key)
        theirs = store.vendor_accounts(contractor.spec.id, key)
    assert mine
    assert theirs
    assert set(mine) != set(theirs)
    assert "6750" in mine
    assert "6750" not in theirs  # the contractor's chart has no such account
