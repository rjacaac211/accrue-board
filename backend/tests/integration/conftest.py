"""Integration fixtures: a real Postgres (DATABASE_URL) with migrations applied.

Most tests run inside a transaction that is rolled back, so they leave no trace. Each test that
needs a client gets one with a unique id, so tests never collide with each other or with data
seeded for local development.
"""

import os
import uuid
from collections.abc import Iterator
from importlib import resources

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session

from accrueboard.config import get_settings
from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import ClientSpec, load_anomaly_catalog, load_client
from accrueboard.db.session import get_engine


@pytest.fixture(scope="session", autouse=True)
def test_database() -> Iterator[str]:
    """Run integration tests against their own database, never the development one.

    Uses TEST_DATABASE_URL if set, otherwise ``<configured database>_test`` on the same server.
    The database is created if missing and migrated to the latest schema.
    """
    settings = get_settings()
    original = settings.database_url
    configured = make_url(original)
    url = make_url(
        os.environ.get("TEST_DATABASE_URL")
        or configured.set(database=f"{configured.database}_test")
    )
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    settings.database_url = url.render_as_string(hide_password=False)
    get_engine.cache_clear()
    ini = resources.files("accrueboard.db").joinpath("alembic.ini")
    command.upgrade(Config(str(ini)), "head")
    try:
        yield settings.database_url
    finally:
        get_engine().dispose()
        settings.database_url = original
        get_engine.cache_clear()


@pytest.fixture
def session() -> Iterator[Session]:
    connection = get_engine().connect()
    outer = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield db
    finally:
        db.close()
        outer.rollback()
        connection.close()


def unique_spec() -> ClientSpec:
    return load_client("fernhill").model_copy(update={"id": f"it{uuid.uuid4().hex[:10]}"})


@pytest.fixture
def spec() -> ClientSpec:
    return unique_spec()


@pytest.fixture
def records(spec: ClientSpec) -> list[GroundTruth]:
    return generate(spec, load_anomaly_catalog(), 7)
