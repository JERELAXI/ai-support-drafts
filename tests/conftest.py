import os
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql

# DB tests use a separate database in the compose Postgres: the live worker and beat
# work on the main one, and tests truncate tables. Forced (not setdefault) so tests
# never touch the main database. Must be set before app modules are imported.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://support:change-me@127.0.0.1:5432/support_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# Required settings for tests that do not touch the services.
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPERATOR_USER", "operator")
os.environ.setdefault("OPERATOR_PASSWORD", "secret")


def _create_test_database() -> None:
    """CREATE DATABASE support_test if it does not exist (via the maintenance db)."""
    info = psycopg.conninfo.conninfo_to_dict(TEST_DATABASE_URL)
    name = info["dbname"]
    with psycopg.connect(
        **{**info, "dbname": "postgres"}, autocommit=True, connect_timeout=3
    ) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def _test_database() -> None:
    try:
        _create_test_database()
    except psycopg.OperationalError as exc:
        pytest.skip(f"compose Postgres is not reachable ({exc}); run `docker compose up -d`")

    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def db(_test_database: None) -> Iterator[None]:
    """Empty tickets table for a DB-dependent test."""
    from sqlalchemy import text

    from app.db import sync_engine

    with sync_engine.begin() as conn:
        conn.execute(text("TRUNCATE tickets"))
    yield
