import psycopg
import pytest
from psycopg import sql

# Point the whole test session at a SEPARATE database before anything imports
# hi.db (which builds its pool at import time from this value). Without this the
# suite drops the schema of the development database on every run and takes any
# real discovery results with it.
import hi.config

TEST_DB = "hi_test"

_base, _dev_db = hi.config.settings.database_url.rsplit("/", 1)
_test_url = f"{_base}/{TEST_DB}"

if _dev_db != TEST_DB:
    with psycopg.connect(f"{_base}/postgres", autocommit=True) as _conn:
        if not _conn.execute(
            "select 1 from pg_database where datname = %s", (TEST_DB,)
        ).fetchone():
            _conn.execute(sql.SQL("create database {}").format(sql.Identifier(TEST_DB)))
    hi.config.settings.database_url = _test_url

from hi.db import migrate  # noqa: E402  (must follow the URL swap above)


@pytest.fixture()
def db_conn():
    assert hi.config.settings.database_url.endswith(TEST_DB), "refusing to wipe a non-test database"
    with psycopg.connect(_test_url, autocommit=True) as conn:
        conn.execute("drop schema public cascade")
        conn.execute("create schema public")
    migrate()
    with psycopg.connect(_test_url, autocommit=True) as conn:
        yield conn
