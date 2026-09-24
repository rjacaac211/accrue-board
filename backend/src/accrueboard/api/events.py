"""Server-Sent Events: live task changes from Postgres NOTIFY, plus a periodic heartbeat.

Each connected browser gets its own LISTEN connection. Postgres delivers a notification only
when the transaction that caused it commits, so the board never shows a change that was rolled
back. The heartbeat carries the shared clock, so a fast-forwarded demo clock shows up live.
"""

import asyncio
import json
import threading
from collections.abc import AsyncGenerator
from typing import Any

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from accrueboard.api.deps import get_clock
from accrueboard.config import get_settings

router = APIRouter(prefix="/api")

HEARTBEAT_SECONDS = 10.0


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _raw_url() -> str:
    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")


def _pump(
    conn: psycopg.Connection[Any],
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[str],
    stop: threading.Event,
) -> None:
    """Forward every notification to the event loop until told to stop."""
    while not stop.is_set():
        for notify in conn.notifies(timeout=1.0):
            loop.call_soon_threadsafe(queue.put_nowait, notify.payload)


async def stream(request: Request, client_id: str | None) -> AsyncGenerator[str]:
    """SSE messages for task changes.

    The LISTEN connection is synchronous and drained by a background thread into an asyncio
    queue. That works with every event loop (psycopg's async mode does not run on the Windows
    default loop) and never drops a notification that arrives in the same read as another.
    """
    clock = get_clock(request)
    conn = await asyncio.to_thread(psycopg.connect, _raw_url(), autocommit=True)
    await asyncio.to_thread(conn.execute, "LISTEN task_events")
    queue: asyncio.Queue[str] = asyncio.Queue()
    stop = threading.Event()
    pump = threading.Thread(
        target=_pump, args=(conn, asyncio.get_running_loop(), queue, stop), daemon=True
    )
    pump.start()
    try:
        yield _sse("hello", {"now": clock.now().isoformat()})
        while not await request.is_disconnected():
            try:
                payload = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
            except TimeoutError:
                clock.invalidate()
                yield _sse("heartbeat", {"now": clock.now().isoformat()})
                continue
            data = json.loads(payload)
            if client_id is None or data.get("client_id") == client_id:
                yield _sse("task", data)
    finally:
        stop.set()
        await asyncio.to_thread(pump.join, 5)
        await asyncio.to_thread(conn.close)


@router.get("/events")
async def events(request: Request, client_id: str | None = None) -> StreamingResponse:
    return StreamingResponse(
        stream(request, client_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
