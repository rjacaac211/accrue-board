"""FastAPI application factory."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from accrueboard import __version__
from accrueboard.config import get_settings
from accrueboard.db.session import get_engine, get_sessionmaker
from accrueboard.services.clock import SharedClock


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    database: Literal["ok", "unavailable"]


router = APIRouter(prefix="/api")


class SinglePageApp(StaticFiles):
    """Serve the built frontend; unknown non-API paths get index.html so client routes work."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("api"):
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404 and not path.startswith("api"):
            return await super().get_response("index.html", scope)
        return response


@router.get("/health")
def health() -> Health:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        database: Literal["ok", "unavailable"] = "ok"
    except SQLAlchemyError:
        database = "unavailable"
    return Health(status="ok", version=__version__, database=database)


def create_app() -> FastAPI:
    from accrueboard.api import events, routes  # noqa: PLC0415 - avoid import cycles at load

    app = FastAPI(title="AccrueBoard", version=__version__)
    app.state.clock = SharedClock(get_sessionmaker())
    app.include_router(router)
    app.include_router(routes.router)
    app.include_router(events.router)

    dist = get_settings().frontend_dist
    if dist and Path(dist).is_dir():
        app.mount("/", SinglePageApp(directory=dist, html=True), name="frontend")
    return app
