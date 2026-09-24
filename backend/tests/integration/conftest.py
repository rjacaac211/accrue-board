"""Integration fixtures: a real Postgres (DATABASE_URL) with migrations applied.

Most tests run inside a transaction that is rolled back, so they leave no trace. Each test that
needs a client gets one with a unique id, so tests never collide with each other or with data
seeded for local development.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from accrueboard.datagen.generate import generate
from accrueboard.datagen.records import GroundTruth
from accrueboard.datagen.spec import ClientSpec, load_anomaly_catalog, load_client
from accrueboard.db.session import get_engine


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
