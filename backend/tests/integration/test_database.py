"""Requires a running Postgres at DATABASE_URL with migrations applied."""

import pytest
from sqlalchemy import text

from accrueboard.db.session import get_engine

pytestmark = pytest.mark.integration


def test_required_extensions_are_installed() -> None:
    with get_engine().connect() as conn:
        names = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
    assert {"vector", "pg_trgm"} <= names
