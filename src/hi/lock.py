"""Cross-process mutual exclusion, on the database we already have.

Mode C drives one signed-in LinkedIn account and ARCHITECTURE.md §7.4 caps concurrency at
one. Until the button existed that was enforced by one human running one command at a
time. A button breaks that: a queued job, a second click and a CLI run can all start a
read, and two overlapping reads on one account is what gets the account restricted.

A Postgres advisory lock is the whole mechanism, and it is the right one for two reasons:
it spans processes (a `threading.Lock` does not, and the CLI is a different process from
uvicorn), and it is released when the connection closes, so a run killed with Ctrl-C does
not wedge every later run the way a `state = 'running'` row would.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from hi.db import pool

# Arbitrary, stable, and the only key in use.
# ponytail: one global lock — mode C is concurrency 1 by policy, so there is no per-account
# key to want. If a second account is ever authorised, key it by account and cap each at 1.
MODE_C = 700_361_022


@contextlib.contextmanager
def exclusive(key: int = MODE_C) -> Iterator[bool]:
    """Hold `key` for the duration of the block.

    Yields False rather than waiting if someone else holds it: every caller here would
    rather refuse in a sentence than queue behind a run that lasts minutes.
    """
    with pool.connection() as conn:
        got = conn.execute("select pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
        # End the transaction the execute() opened. The lock is session-scoped and
        # survives this, and a run holds it for minutes — which would otherwise mean
        # minutes of idle-in-transaction pinning a snapshot for no reason.
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.execute("select pg_advisory_unlock(%s)", (key,))
                conn.commit()


def held(key: int = MODE_C) -> bool:
    """True if any session holds `key`.

    A preflight, never a guard: it can be stale the instant it returns, which is why
    `exclusive()` still wraps the run itself. It exists so the page can say "already
    running" instead of enqueueing work that will refuse itself later.
    """
    with pool.connection() as conn:
        return conn.execute(
            "select exists (select 1 from pg_locks where locktype = 'advisory' "
            "and classid = 0 and objid = %s and objsubid = 1 and granted)",
            (key,),
        ).fetchone()[0]
