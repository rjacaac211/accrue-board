"""The feedback loop end to end: a reviewer's correction changes how the next document is coded."""

import pytest

from accrueboard.datagen.records import GroundTruth
from accrueboard.db.models import Task
from accrueboard.retrieval.pg_store import PgKnowledgeStore
from accrueboard.services import review

from .helpers import EMBEDDER, World, make_world

pytestmark = pytest.mark.integration

CORRECTED = "6000"  # deliberately different from the generator's account, so learning is visible


def northstar(world: World) -> list[GroundTruth]:
    docs = sorted(
        (r for r in world.records if r.vendor_id == "northstar"),
        key=lambda r: (r.received_at, r.doc_id),
    )
    assert len(docs) >= 2
    return docs


def test_reviewer_correction_teaches_the_next_document() -> None:
    world = make_world("fb")
    first, second = northstar(world)[:2]

    # 1. First document from a vendor never seen before: it goes to review.
    first_task = world.process(first)
    with world.sessions() as session:
        task = session.get(Task, first_task)
        assert task is not None
        assert task.state == "needs_review"
        assert task.coding is not None
        assert task.coding["vendor_known"] is False
        assert {h["rule"] for h in task.routing["hits"]} >= {"first_time_vendor"}  # type: ignore[index]

    # 2. The reviewer recodes every line and approves.
    corrected = [CORRECTED] * len(first.document.lines)
    with world.sessions() as session, session.begin():
        store = PgKnowledgeStore(session, EMBEDDER, now=world.clock.now)
        result = review.approve(
            session,
            first_task,
            reviewer_id="u_alex",
            now=world.clock.now(),
            store=store,
            accounts=corrected,
            note="policy: marketing print",
        )
    assert result.knowledge_entries == len(corrected)

    # 3. The vendor's next document is coded with that knowledge.
    second_task = world.process(second)
    with world.sessions() as session:
        task = session.get(Task, second_task)
        assert task is not None
        coding = task.coding
        assert coding is not None
        assert coding["vendor_known"] is True
        assert coding["vendor_history"] == {CORRECTED: len(corrected)}
        # The retrained classifier now predicts the reviewer's account for this vendor's items...
        assert {line["classifier_account"] for line in coding["lines"]} == {CORRECTED}
        # ...and the similar items retrieved include the reviewer's entries.
        learned = {f"{first_task}:{i}" for i in range(len(corrected))}
        retrieved = {n for line in coding["lines"] for n in line["neighbour_ids"]}
        assert learned & retrieved
        # The model (a fake that answers with the generator's accounts) disagrees with the
        # learned signals, so coding confidence is lowered instead of trusted blindly.
        assert coding["confidence"] < 0.9
