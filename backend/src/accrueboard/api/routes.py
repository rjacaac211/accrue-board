"""REST endpoints for the board, task detail, review actions, ledger, knowledge and stats."""

from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from accrueboard.agents.review_assistant.service import (
    AssistantRun,
    AssistantUnavailableError,
    ReviewAssistant,
)
from accrueboard.api.deps import get_assistant, get_clock, get_embedder, get_session
from accrueboard.config import get_settings
from accrueboard.datagen.spec import Split
from accrueboard.datagen.writer import read_records
from accrueboard.db.models import Client, Document, Task
from accrueboard.domain.documents import ExtractedDocument
from accrueboard.pipeline.files import SourceFile, UnsupportedFileError
from accrueboard.pipeline.process import ingest
from accrueboard.retrieval.pg_store import PgKnowledgeStore
from accrueboard.services import clock as shared_clock
from accrueboard.services import queries, review
from accrueboard.services.clock import SharedClock

router = APIRouter(prefix="/api")

SessionDep = Annotated[Session, Depends(get_session)]
ClockDep = Annotated[SharedClock, Depends(get_clock)]
AssistantDep = Annotated[ReviewAssistant, Depends(get_assistant)]


def _client(session: Session, client_id: str) -> Client:
    client = session.get(Client, client_id)
    if client is None:
        raise HTTPException(404, f"unknown client {client_id}")
    return client


# ---------------------------------------------------------------------------- reference data


@router.get("/clients")
def list_clients(session: SessionDep) -> list[queries.ClientSummary]:
    return queries.clients(session)


@router.get("/users")
def list_users(session: SessionDep) -> list[queries.UserSummary]:
    return queries.users(session)


@router.get("/clients/{client_id}/accounts")
def list_accounts(client_id: str, session: SessionDep) -> list[queries.AccountSummary]:
    _client(session, client_id)
    return queries.accounts(session, client_id)


# ---------------------------------------------------------------------------- board and tasks


@router.get("/clients/{client_id}/board")
def board(client_id: str, session: SessionDep, clock: ClockDep) -> list[queries.Card]:
    _client(session, client_id)
    return queries.board(session, client_id, clock.now())


@router.get("/clients/{client_id}/bottlenecks")
def bottlenecks(client_id: str, session: SessionDep, clock: ClockDep) -> queries.Bottlenecks:
    _client(session, client_id)
    return queries.bottlenecks(session, client_id, clock.now())


@router.get("/clients/{client_id}/stats")
def stats(client_id: str, session: SessionDep) -> queries.Stats:
    _client(session, client_id)
    return queries.stats(session, client_id)


@router.get("/clients/{client_id}/ledger")
def ledger_view(client_id: str, session: SessionDep, limit: int = 50) -> queries.LedgerView:
    _client(session, client_id)
    return queries.ledger_view(session, client_id, limit=min(limit, 500))


@router.get("/clients/{client_id}/knowledge")
def knowledge(
    client_id: str, session: SessionDep, vendor: str | None = None, limit: int = 100
) -> list[queries.KnowledgeItem]:
    _client(session, client_id)
    return queries.knowledge(session, client_id, vendor=vendor, limit=min(limit, 500))


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, session: SessionDep, clock: ClockDep) -> queries.TaskDetail:
    detail = queries.task_detail(session, task_id, clock.now())
    if detail is None:
        raise HTTPException(404, f"unknown task {task_id}")
    return detail


@router.get("/tasks/{task_id}/file")
def task_file(task_id: str, session: SessionDep) -> Response:
    found = queries.document_file(session, task_id)
    if found is None:
        raise HTTPException(404, "no file for this task")
    data, media_type, filename = found
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/clients/{client_id}/documents", status_code=201)
async def upload(
    client_id: str, file: UploadFile, session: SessionDep, clock: ClockDep
) -> dict[str, str]:
    _client(session, client_id)
    try:
        source = SourceFile.from_bytes(file.filename or "upload", await file.read())
    except UnsupportedFileError as exc:
        raise HTTPException(415, str(exc)) from exc
    task = ingest(session, client_id, source, received_at=clock.now())
    session.commit()
    return {"task_id": task.id}


# ---------------------------------------------------------------------------- review actions


class ActionRequest(BaseModel):
    reviewer_id: str
    note: str = ""


class ApproveRequest(ActionRequest):
    document: ExtractedDocument | None = None
    """The corrected document, if the reviewer changed any extracted value."""
    accounts: list[str] | None = None
    """The account per line, if the reviewer changed any coding."""


class AssignRequest(ActionRequest):
    assignee_id: str | None = None


class ActionResult(BaseModel):
    task_id: str
    state: str
    journal_entry: str | None = None
    knowledge_entries: int = 0


def _result(result: review.ReviewResult) -> ActionResult:
    return ActionResult(
        task_id=result.task_id,
        state=result.state.value,
        journal_entry=result.journal_entry,
        knowledge_entries=result.knowledge_entries,
    )


@router.post("/tasks/{task_id}/approve")
def approve(
    task_id: str, body: ApproveRequest, session: SessionDep, clock: ClockDep
) -> ActionResult:
    try:
        with session.begin():
            store = PgKnowledgeStore(session, get_embedder(), now=clock.now)
            result = review.approve(
                session,
                task_id,
                reviewer_id=body.reviewer_id,
                now=clock.now(),
                store=store,
                document=body.document,
                accounts=body.accounts,
                note=body.note,
            )
    except review.ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _result(result)


@router.post("/tasks/{task_id}/assign")
def assign(task_id: str, body: AssignRequest, session: SessionDep, clock: ClockDep) -> ActionResult:
    try:
        with session.begin():
            result = review.assign(
                session,
                task_id,
                reviewer_id=body.reviewer_id,
                assignee_id=body.assignee_id,
                now=clock.now(),
            )
    except review.ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _result(result)


@router.post("/tasks/{task_id}/assistant")
def run_assistant(task_id: str, session: SessionDep, assistant: AssistantDep) -> AssistantRun:
    """Investigate a held task with the review assistant (suggest-only) and store the result."""
    try:
        with session.begin():
            if session.get(Task, task_id) is None:
                raise HTTPException(404, f"unknown task {task_id}")
            return assistant.run(session, task_id)
    except AssistantUnavailableError as exc:
        raise HTTPException(409, str(exc)) from exc


# Declared after /assign and /assistant so the specific routes win.
SimpleAction = Literal["reject", "block", "unblock", "reopen", "retry"]
_SIMPLE = {
    "reject": review.reject,
    "block": review.block,
    "unblock": review.unblock,
    "reopen": review.reopen,
    "retry": review.retry,
}


@router.post("/tasks/{task_id}/{action}")
def simple_action(
    task_id: str, action: SimpleAction, body: ActionRequest, session: SessionDep, clock: ClockDep
) -> ActionResult:
    try:
        with session.begin():
            result = _SIMPLE[action](
                session, task_id, reviewer_id=body.reviewer_id, now=clock.now(), note=body.note
            )
    except review.ReviewError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _result(result)


# ---------------------------------------------------------------------------- clock and demo


class ClockView(BaseModel):
    now: str
    offset_hours: float
    demo_mode: bool


@router.get("/clock")
def get_clock_view(clock: ClockDep) -> ClockView:
    return ClockView(
        now=clock.now().isoformat(),
        offset_hours=clock.offset().total_seconds() / 3600,
        demo_mode=get_settings().demo_mode,
    )


def _require_demo() -> None:
    if not get_settings().demo_mode:
        raise HTTPException(404, "demo endpoints are disabled (set DEMO_MODE=true)")


class AdvanceRequest(BaseModel):
    hours: float = Field(gt=0, le=24 * 30)


@router.post("/demo/clock/advance", dependencies=[Depends(_require_demo)])
def advance_clock(body: AdvanceRequest, session: SessionDep, clock: ClockDep) -> ClockView:
    with session.begin():
        shared_clock.advance(session, timedelta(hours=body.hours))
    clock.invalidate()
    return get_clock_view(clock)


@router.post("/demo/clock/reset", dependencies=[Depends(_require_demo)])
def reset_clock(session: SessionDep, clock: ClockDep) -> ClockView:
    with session.begin():
        shared_clock.reset(session)
    clock.invalidate()
    return get_clock_view(clock)


class FeedRequest(BaseModel):
    split: Literal["validation", "test"] = "validation"
    count: int = Field(default=5, ge=1, le=50)


@router.post("/demo/clients/{client_id}/feed", dependencies=[Depends(_require_demo)])
def feed(
    client_id: str, body: FeedRequest, session: SessionDep, clock: ClockDep
) -> dict[str, list[str]]:
    """Queue the next generated documents that have not been ingested yet (arrival order)."""
    _client(session, client_id)
    dataset = get_settings().data_dir / "generated" / client_id
    if not dataset.is_dir():
        raise HTTPException(404, "no generated dataset; run `accrueboard datagen` first")
    seen = set(
        session.execute(
            Document.__table__.select()
            .with_only_columns(Document.filename)
            .where(Document.client_id == client_id)
        ).scalars()
    )
    queued: list[str] = []
    for record in read_records(dataset, Split(body.split)):
        if record.file is None:
            continue
        name = record.file.rsplit("/", 1)[-1]
        if name in seen:
            continue
        source = SourceFile.from_path(dataset / record.file)
        queued.append(ingest(session, client_id, source, received_at=clock.now()).id)
        if len(queued) >= body.count:
            break
    session.commit()
    return {"task_ids": queued}
