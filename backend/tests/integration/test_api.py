"""The coordination API on real Postgres: board, detail, review actions, feedback, demo clock."""

import asyncio
import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from accrueboard.api import deps
from accrueboard.api.app import create_app
from accrueboard.api.events import stream
from accrueboard.config import get_settings
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import Split
from accrueboard.db.models import Task
from accrueboard.services.clock import SharedClock

from .helpers import World, make_world

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def world() -> World:
    return make_world("api")


@pytest.fixture(scope="module")
def api() -> Iterator[TestClient]:
    settings = get_settings()
    previous = settings.embedder
    settings.embedder = "hashing"
    deps.get_embedder.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    settings.embedder = previous
    deps.get_embedder.cache_clear()


def pick(world: World, predicate: Callable[[GroundTruth], bool]) -> GroundTruth:
    return next(r for r in world.records if r.split is Split.VALIDATION and predicate(r))


def labelled(kind: str) -> Callable[[GroundTruth], bool]:
    return lambda r: any(a.type == kind for a in r.anomalies)


def detail(api: TestClient, task_id: str) -> dict[str, Any]:
    response = api.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ reads


def test_reference_data(api: TestClient, world: World) -> None:
    assert world.spec.id in {c["id"] for c in api.get("/api/clients").json()}
    assert {u["role"] for u in api.get("/api/users").json()} >= {"reviewer", "senior"}
    codes = {a["code"] for a in api.get(f"/api/clients/{world.spec.id}/accounts").json()}
    assert {"1300", "2000", "6100"} <= codes
    assert api.get("/api/clients/nobody/board").status_code == 404


def test_board_detail_and_file(api: TestClient, world: World) -> None:
    clean = pick(
        world,
        lambda r: (
            r.vendor_id == "oakridge"
            and not r.anomalies
            and not r.hard_negatives
            and r.file_format == "pdf"
        ),
    )
    task_id = world.process(clean)
    cards = {c["task_id"]: c for c in api.get(f"/api/clients/{world.spec.id}/board").json()}
    assert cards[task_id]["state"] == "posted"
    assert cards[task_id]["vendor"] == clean.document.vendor_name
    body = detail(api, task_id)
    assert body["audit_intact"]
    assert [e["action"] for e in body["audit"]][-2:] == ["auto_approved", "posted"]
    assert len(body["entries"]) == 1
    assert {c["purpose"] for c in body["calls"]} >= {"classify", "extract", "code"}
    file = api.get(f"/api/tasks/{task_id}/file")
    assert file.status_code == 200
    assert file.headers["content-type"] == "application/pdf"
    assert file.content.startswith(b"%PDF")
    stats = api.get(f"/api/clients/{world.spec.id}/stats").json()
    assert stats["auto_posted"] >= 1


# ------------------------------------------------------------------ review actions


def test_approve_with_correction_feeds_the_knowledge_store(api: TestClient, world: World) -> None:
    record = pick(world, labelled("first_time_vendor"))
    task_id = world.process(record)
    body = detail(api, task_id)
    assert body["card"]["state"] == "needs_review"
    assert "first_time_vendor" in body["card"]["rules"]

    corrected = ["6000"] * len(record.document.lines)  # reviewer recodes every line
    result = api.post(
        f"/api/tasks/{task_id}/approve",
        json={"reviewer_id": "u_alex", "accounts": corrected, "note": "per policy"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "posted"
    assert result.json()["knowledge_entries"] == len(record.document.lines)

    body = detail(api, task_id)
    approved = next(e for e in body["audit"] if e["action"] == "approved")
    assert approved["actor"] == "u_alex"
    assert approved["actor_kind"] == "human"
    assert len(approved["details"]["account_changes"]) >= 1
    assert body["audit_intact"]

    vendor = record.document.vendor_name or ""
    learned = api.get(f"/api/clients/{world.spec.id}/knowledge", params={"vendor": vendor}).json()
    assert {k["account"] for k in learned} == {"6000"}
    assert {k["source"] for k in learned} <= {"corrected", "confirmed"}
    assert api.get(f"/api/clients/{world.spec.id}/ledger").json()["balanced"]

    again = api.post(f"/api/tasks/{task_id}/approve", json={"reviewer_id": "u_alex"})
    assert again.status_code == 409


def test_edited_document_must_still_validate(api: TestClient, world: World) -> None:
    record = pick(world, labelled("unsupported_document"))
    task_id = world.process(record)
    response = api.post(f"/api/tasks/{task_id}/approve", json={"reviewer_id": "u_sam"})
    assert response.status_code == 409
    assert "unsupported" in response.json()["detail"]

    arithmetic = pick(world, labelled("arithmetic_error"))
    task_id = world.process(arithmetic)
    response = api.post(f"/api/tasks/{task_id}/approve", json={"reviewer_id": "u_sam"})
    assert response.status_code == 409
    assert "problems" in response.json()["detail"]

    # Block it while the vendor is asked for a corrected invoice, then reject it.
    assert (
        api.post(f"/api/tasks/{task_id}/block", json={"reviewer_id": "u_sam"}).status_code == 409
    )  # needs a reason
    blocked = api.post(
        f"/api/tasks/{task_id}/block",
        json={"reviewer_id": "u_sam", "note": "asked vendor for corrected copy"},
    )
    assert blocked.json()["state"] == "blocked"
    assert (
        api.post(f"/api/tasks/{task_id}/unblock", json={"reviewer_id": "u_sam"}).json()["state"]
        == "needs_review"
    )
    rejected = api.post(
        f"/api/tasks/{task_id}/reject",
        json={"reviewer_id": "u_jordan", "note": "vendor reissued it"},
    )
    assert rejected.json()["state"] == "rejected"
    assert (
        api.post(f"/api/tasks/{task_id}/unblock", json={"reviewer_id": "u_sam"}).status_code == 409
    )


def test_reopen_reverses_the_posting(api: TestClient, world: World) -> None:
    record = pick(
        world, lambda r: r.vendor_id == "boxcraft" and not r.anomalies and not r.hard_negatives
    )
    task_id = world.process(record)
    assert detail(api, task_id)["card"]["state"] == "posted"
    assert (
        api.post(f"/api/tasks/{task_id}/reopen", json={"reviewer_id": "u_jordan"}).status_code
        == 409
    )  # needs a reason
    reopened = api.post(
        f"/api/tasks/{task_id}/reopen", json={"reviewer_id": "u_jordan", "note": "wrong period"}
    )
    assert reopened.json()["state"] == "needs_review"
    entries = detail(api, task_id)["entries"]
    assert len(entries) == 2
    assert entries[1]["reverses"] == entries[0]["id"]
    net = sum(
        Decimal(line["debit"]) - Decimal(line["credit"]) for e in entries for line in e["lines"]
    )
    assert net == 0
    assert api.get(f"/api/clients/{world.spec.id}/ledger").json()["balanced"]


def test_unknown_reviewer_and_retry(api: TestClient, world: World) -> None:
    record = pick(world, lambda r: r.vendor_id == "harborline" and not r.anomalies)
    world.oracle.fail_for.add(record.doc_id)
    task_id = world.process(record)
    assert detail(api, task_id)["card"]["state"] == "failed"
    assert (
        api.post(f"/api/tasks/{task_id}/retry", json={"reviewer_id": "nobody"}).status_code == 409
    )
    retried = api.post(f"/api/tasks/{task_id}/retry", json={"reviewer_id": "u_alex"})
    assert retried.json()["state"] == "queued"
    world.oracle.fail_for.discard(record.doc_id)
    outcome = world.processor.run_once(world.spec.id)
    assert outcome is not None
    assert outcome.task_id == task_id


def test_assign(api: TestClient, world: World) -> None:
    record = pick(world, labelled("near_duplicate"))
    task_id = world.process(record)
    response = api.post(
        f"/api/tasks/{task_id}/assign", json={"reviewer_id": "u_jordan", "assignee_id": "u_sam"}
    )
    assert response.status_code == 200
    assert detail(api, task_id)["card"]["assignee_id"] == "u_sam"


def test_upload_queues_a_task(api: TestClient, world: World) -> None:
    record = pick(world, lambda r: r.vendor_id == "copperleaf" and not r.anomalies)
    from accrueboard.datagen.render import render  # noqa: PLC0415

    response = api.post(
        f"/api/clients/{world.spec.id}/documents",
        files={"file": ("bill.pdf", render(record, world.spec), "application/pdf")},
    )
    assert response.status_code == 201, response.text
    assert detail(api, response.json()["task_id"])["card"]["state"] == "queued"
    bad = api.post(
        f"/api/clients/{world.spec.id}/documents",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert bad.status_code == 415


def test_feed_queues_generated_documents(api: TestClient, world: World, tmp_path: Any) -> None:
    from accrueboard.datagen.writer import write_dataset  # noqa: PLC0415

    settings = get_settings()
    previous = settings.data_dir
    write_dataset(world.records, world.spec, 7, tmp_path / "generated")
    settings.data_dir = tmp_path
    settings.demo_mode = True
    try:
        first = api.post(f"/api/demo/clients/{world.spec.id}/feed", json={"count": 2})
        assert first.status_code == 200, first.text
        second = api.post(f"/api/demo/clients/{world.spec.id}/feed", json={"count": 2})
        assert len(first.json()["task_ids"]) == 2
        assert set(first.json()["task_ids"]).isdisjoint(second.json()["task_ids"])
    finally:
        settings.data_dir = previous
        settings.demo_mode = False


# ------------------------------------------------------------------ bottlenecks and demo clock


def test_demo_clock_ages_review_tasks(api: TestClient, world: World) -> None:
    record = pick(world, labelled("tax_on_resale_inventory"))
    task_id = world.process(record)
    # Make the task's stage start "now" on the shared clock, then fast-forward.
    now = api.app.state.clock.now()  # type: ignore[attr-defined]
    with world.sessions() as session, session.begin():
        session.execute(update(Task).where(Task.id == task_id).values(state_entered_at=now))

    assert api.post("/api/demo/clock/advance", json={"hours": 30}).status_code == 404
    settings = get_settings()
    settings.demo_mode = True
    try:
        advanced = api.post("/api/demo/clock/advance", json={"hours": 30})
        assert advanced.status_code == 200
        assert advanced.json()["offset_hours"] >= 30
        alerts = api.get(f"/api/clients/{world.spec.id}/bottlenecks").json()["alerts"]
        mine = [a for a in alerts if a["task_id"] == task_id]
        assert mine
        assert mine[0]["level"] == "warning"
        assert api.post("/api/demo/clock/reset").json()["offset_hours"] == 0
    finally:
        settings.demo_mode = False


# ------------------------------------------------------------------ server-sent events


def test_event_stream_delivers_committed_task_changes(world: World) -> None:
    record = pick(world, lambda r: r.vendor_id == "swiftlane" and not r.anomalies)

    async def run() -> list[tuple[str, dict[str, Any]]]:
        disconnected = asyncio.Event()

        async def is_disconnected() -> bool:
            return disconnected.is_set()

        request: Any = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(clock=SharedClock(world.sessions))),
            is_disconnected=is_disconnected,
        )
        events: list[tuple[str, dict[str, Any]]] = []
        generator = stream(request, world.spec.id)
        events.append(_parse(await anext(generator)))  # hello
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, world.process, record)
        deadline = loop.time() + 120
        while loop.time() < deadline:
            event = _parse(await asyncio.wait_for(anext(generator), 30))
            events.append(event)
            if event[0] == "task" and event[1]["state"] in ("posted", "needs_review"):
                break
        disconnected.set()
        await generator.aclose()
        return events

    events = asyncio.run(run())
    assert events[0][0] == "hello"
    states = [data["state"] for name, data in events if name == "task"]
    assert states[:2] == ["queued", "processing"]
    assert states[-1] in ("posted", "needs_review")
    assert all(data["client_id"] == world.spec.id for name, data in events if name == "task")


def _parse(message: str) -> tuple[str, dict[str, Any]]:
    lines = dict(line.split(": ", 1) for line in message.strip().splitlines())
    return lines["event"], json.loads(lines["data"])
