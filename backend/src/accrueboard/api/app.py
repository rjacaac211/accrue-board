"""FastAPI application factory."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from accrueboard import __version__
from accrueboard.config import get_settings
from accrueboard.db.session import get_engine


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    database: Literal["ok", "unavailable"]


router = APIRouter(prefix="/api")


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
    app = FastAPI(title="AccrueBoard", version=__version__)
    app.include_router(router)

    dist = get_settings().frontend_dist
    if dist and Path(dist).is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app
