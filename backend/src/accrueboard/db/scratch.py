"""Throwaway databases for experiments that must start from a known, empty state."""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text

from accrueboard.config import get_settings
from accrueboard.db.session import get_engine


def scratch_url(suffix: str) -> str:
    """The configured database's URL with ``_<suffix>`` appended to the database name."""
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_{suffix}").render_as_string(hide_password=False)


@contextmanager
def fresh_database(url: str) -> Iterator[None]:
    """Drop and recreate ``url``'s database, migrate it, and make it the configured database
    for the duration of the block (so ``get_sessionmaker()`` and friends use it)."""
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{target.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    admin.dispose()

    settings = get_settings()
    original = settings.database_url
    settings.database_url = url
    get_engine.cache_clear()
    try:
        ini = resources.files("accrueboard.db").joinpath("alembic.ini")
        command.upgrade(Config(str(ini)), "head")
        yield
    finally:
        get_engine().dispose()
        settings.database_url = original
        get_engine.cache_clear()
