"""The advisory lock that keeps mode C at concurrency 1 (ARCHITECTURE.md §7.4).

Without it a queued job and a CLI run can both attach to one LinkedIn account, which is
the thing that gets an account restricted.
"""

import psycopg
import pytest

import hi.config
from hi import lock

KEY = 999_000_111  # not MODE_C: a real run must never be blocked by a test


def test_one_holder_at_a_time(db_conn):
    with lock.exclusive(KEY) as first:
        assert first is True
        with lock.exclusive(KEY) as second:
            # Refused, not queued: every caller would rather say "already running" than
            # block a web request for the minutes a read takes.
            assert second is False


def test_the_lock_is_released_on_the_way_out(db_conn):
    with lock.exclusive(KEY):
        pass
    with lock.exclusive(KEY) as got:
        assert got is True


def test_held_sees_a_lock_taken_by_another_connection(db_conn):
    """`held()` is the preflight, so it has to see across processes, not just this one."""
    assert lock.held(KEY) is False
    with psycopg.connect(hi.config.settings.database_url) as other:
        other.execute("select pg_try_advisory_lock(%s)", (KEY,))
        other.commit()
        assert lock.held(KEY) is True
    assert lock.held(KEY) is False, "closing the connection frees it — no janitor needed"


def test_a_crashed_holder_does_not_wedge_the_next_run(db_conn):
    """The property a `state = 'running'` row would not have given us."""
    other = psycopg.connect(hi.config.settings.database_url)
    other.execute("select pg_try_advisory_lock(%s)", (KEY,))
    other.commit()
    other.close()  # stands in for the killed process
    with lock.exclusive(KEY) as got:
        assert got is True


def test_the_lock_survives_an_exception_without_leaking(db_conn):
    with pytest.raises(RuntimeError):
        with lock.exclusive(KEY):
            raise RuntimeError("boom")
    assert lock.held(KEY) is False
