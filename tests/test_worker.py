import threading
import uuid

import pytest

from hi import worker
from hi.db import pool


def queued(conn) -> int:
    return conn.execute("select count(*) from job where state = 'queued'").fetchone()[0]


def state_of(conn, job_id) -> str:
    return conn.execute("select state from job where id = %s", (job_id,)).fetchone()[0]


# --- idempotent enqueue ------------------------------------------------------


def test_duplicate_enqueue_is_a_noop(db_conn):
    payload = {"role_id": "abc"}
    first = worker.enqueue("score", payload)
    second = worker.enqueue("score", payload)

    assert first is not None
    assert second is None, "a double-clicked button must not double the spend"
    assert queued(db_conn) == 1


def test_payload_hash_ignores_key_order(db_conn):
    assert worker.payload_hash("score", {"a": 1, "b": 2}) == worker.payload_hash(
        "score", {"b": 2, "a": 1}
    )


def test_different_payloads_both_queue(db_conn):
    worker.enqueue("score", {"role_id": "a"})
    worker.enqueue("score", {"role_id": "b"})
    assert queued(db_conn) == 2


def test_same_work_can_be_requeued_after_it_finishes(db_conn):
    payload = {"role_id": "abc"}
    job_id = worker.enqueue("score", payload)
    worker.complete(job_id)
    # Re-running a role later is legitimate; only concurrent duplicates are refused.
    assert worker.enqueue("score", payload) is not None


def test_unknown_kind_is_refused(db_conn):
    with pytest.raises(ValueError, match="unknown job kind"):
        worker.enqueue("mine_bitcoin", {})


# --- claiming ----------------------------------------------------------------


def test_claim_returns_oldest_first(db_conn):
    first = worker.enqueue("score", {"n": 1})
    second = worker.enqueue("score", {"n": 2})

    assert worker.claim("w1").id == first
    assert worker.claim("w1").id == second
    assert worker.claim("w1") is None


def test_claim_marks_running_and_counts_the_attempt(db_conn):
    job_id = worker.enqueue("score", {"n": 1})
    job = worker.claim("w1")

    assert job.id == job_id
    assert job.attempts == 1
    assert state_of(db_conn, job_id) == "running"


def test_two_concurrent_workers_never_claim_the_same_job(db_conn):
    for n in range(8):
        worker.enqueue("score", {"n": n})

    claimed: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(4)

    def grab() -> None:
        start.wait()
        while (job := worker.claim(f"w{threading.get_ident()}")) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=grab) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 8
    assert len(set(claimed)) == 8, "a job was claimed twice — SKIP LOCKED is not holding"


# --- failure and retry -------------------------------------------------------


def test_failure_requeues_until_max_attempts(db_conn):
    job_id = worker.enqueue("score", {"n": 1})

    for attempt in range(1, worker.MAX_ATTEMPTS):
        worker.claim("w1")
        assert worker.fail(job_id, f"boom {attempt}") == "queued"

    worker.claim("w1")
    assert worker.fail(job_id, "final boom") == "failed"
    assert state_of(db_conn, job_id) == "failed"


def test_failed_job_is_not_reclaimed(db_conn):
    job_id = worker.enqueue("score", {"n": 1})
    for _ in range(worker.MAX_ATTEMPTS):
        worker.claim("w1")
        worker.fail(job_id, "boom")
    assert worker.claim("w1") is None


def test_error_text_is_recorded(db_conn):
    job_id = worker.enqueue("score", {"n": 1})
    worker.claim("w1")
    worker.fail(job_id, "ConnectionError: upstream down")
    error = db_conn.execute("select last_error from job where id = %s", (job_id,)).fetchone()[0]
    assert "upstream down" in error


# --- the reaper (kill -9 recovery) -------------------------------------------


def test_stale_lease_is_requeued(db_conn):
    job_id = worker.enqueue("score", {"n": 1})
    worker.claim("w1")
    # Simulate a worker killed while holding the job.
    db_conn.execute(
        "update job set locked_at = now() - interval '30 minutes' where id = %s", (job_id,)
    )

    assert worker.reap(lease_minutes=15) == 1
    assert state_of(db_conn, job_id) == "queued"
    assert worker.claim("w2").id == job_id


def test_a_fresh_lease_is_left_alone(db_conn):
    worker.enqueue("score", {"n": 1})
    worker.claim("w1")
    assert worker.reap(lease_minutes=15) == 0


def test_repeatedly_dying_job_eventually_fails(db_conn):
    job_id = worker.enqueue("score", {"n": 1})
    for _ in range(worker.MAX_ATTEMPTS):
        worker.claim("w1")
        db_conn.execute(
            "update job set locked_at = now() - interval '30 minutes' where id = %s", (job_id,)
        )
        worker.reap(lease_minutes=15)
    # A job that kills its worker every time must not loop forever.
    assert state_of(db_conn, job_id) == "failed"


# --- run_once ----------------------------------------------------------------


async def test_run_once_returns_false_on_an_empty_queue(db_conn):
    assert await worker.run_once("w1") is False


async def test_run_once_completes_a_job(db_conn, monkeypatch):
    seen = []

    async def handler(job):
        seen.append(job.payload)

    monkeypatch.setitem(worker.HANDLERS, "score", handler)
    job_id = worker.enqueue("score", {"role_id": "abc"})

    assert await worker.run_once("w1") is True
    assert seen == [{"role_id": "abc"}]
    assert state_of(db_conn, job_id) == "done"


async def test_a_raising_handler_does_not_kill_the_worker(db_conn, monkeypatch):
    async def handler(job):
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(worker.HANDLERS, "score", handler)
    job_id = worker.enqueue("score", {"role_id": "abc"})

    assert await worker.run_once("w1") is True  # survived
    assert state_of(db_conn, job_id) == "queued"  # and will retry
    error = db_conn.execute("select last_error from job where id = %s", (job_id,)).fetchone()[0]
    assert "handler exploded" in error


async def test_discover_enqueues_its_children(db_conn, monkeypatch):
    from hi import discovery
    from hi.models import RoleSpec

    role_id = discovery.create_role("Backend", RoleSpec(titles=["Backend Engineer"]))

    async def fake_run(role_id_, spec, **kw):
        return {"queries": 0, "new_refs": 0, "passed": 0, "failed": 0}

    monkeypatch.setattr(discovery, "run_discovery", fake_run)
    await worker.handle_discover(
        worker.Job(id=0, kind="discover", payload={"role_id": str(role_id)}, attempts=1)
    )

    kinds = {
        r[0]
        for r in db_conn.execute("select kind from job where state = 'queued'").fetchall()
    }
    assert kinds == {"collect", "score"}


async def test_missing_role_fails_the_job_rather_than_guessing(db_conn):
    worker.enqueue("discover", {"role_id": str(uuid.uuid4())})
    await worker.run_once("w1")
    row = db_conn.execute("select state, last_error from job").fetchone()
    assert row[0] == "queued"  # retryable
    assert "no such role" in row[1]


def test_queue_counts(db_conn):
    worker.enqueue("score", {"n": 1})
    worker.enqueue("score", {"n": 2})
    worker.claim("w1")
    counts = worker.queue_counts()
    assert counts.get("queued") == 1
    assert counts.get("running") == 1


# --- enrichment from the queue (IMPLEMENTATION.md 1.11d) ---------------------


def a_job(**payload) -> worker.Job:
    """A claimed enrich job, as the handler always sees one."""
    job_id = worker.enqueue("enrich", payload)
    with pool.connection() as conn:
        conn.execute("update job set state = 'running' where id = %s", (job_id,))
    return worker.Job(id=job_id, kind="enrich", payload=payload, attempts=1)


async def test_enrich_writes_a_note_per_profile_and_a_summary(db_conn, monkeypatch):
    """At 25s a profile, a note is the only honest progress indicator."""
    from hi.adapters import linkedin_profile as lp

    role_id = uuid.uuid4()
    monkeypatch.setattr(lp, "targets_for_role", lambda _r, **kw: [(uuid.uuid4(), "https://x/in/ann")])

    notes = []

    async def fake_enrich(targets, *, limit=None, on_progress=None):
        on_progress(1, 1, "https://www.linkedin.com/in/ann/")
        notes.append(note_of(db_conn))
        return lp.EnrichResult(attempted=1, enriched=1, with_dated_experience=1)

    monkeypatch.setattr(lp, "enrich", fake_enrich)
    await worker.handle_enrich(a_job(role_id=str(role_id), limit=1))

    assert notes == ["Reading 1 of 1: ann"]
    assert note_of(db_conn) == "Read 1 of 1 person, 1 with employment history."


async def test_the_scope_decides_which_gate_verdicts_are_read(db_conn, monkeypatch):
    """A recruiter can spend their own quota on the people the search ruled out."""
    from hi.adapters import linkedin_profile as lp

    asked = []
    monkeypatch.setattr(
        lp,
        "targets_for_role",
        lambda _r, gate_state="passed": asked.append(gate_state) or [(uuid.uuid4(), "https://x/in/ann")],
    )

    async def nothing(targets, **kw):
        return lp.EnrichResult()

    monkeypatch.setattr(lp, "enrich", nothing)
    for scope, expected in (("passed", "passed"), ("ruled_out", "failed"), ("all", None)):
        await worker.handle_enrich(a_job(role_id=str(uuid.uuid4()), scope=scope))
    assert asked == ["passed", "failed", None]


async def test_a_stopped_run_says_stopped_in_the_note(db_conn, monkeypatch):
    """Silent success is the characteristic LinkedIn failure — it must reach the page."""
    from hi.adapters import linkedin_profile as lp

    monkeypatch.setattr(lp, "targets_for_role", lambda _r, **kw: [(uuid.uuid4(), "https://x/in/ann")])

    async def blocked(targets, **kw):
        return lp.EnrichResult(stopped_reason="authwall — LinkedIn disabled")

    monkeypatch.setattr(lp, "enrich", blocked)
    await worker.handle_enrich(a_job(role_id=str(uuid.uuid4())))

    assert note_of(db_conn).startswith("Stopped: authwall")
    assert queued(db_conn) == 0, "a blocked run must not queue more work"


async def test_enrich_queues_the_ranking_behind_it(db_conn, monkeypatch):
    from hi.adapters import linkedin_profile as lp

    role_id = uuid.uuid4()
    monkeypatch.setattr(lp, "targets_for_role", lambda _r, **kw: [(uuid.uuid4(), "https://x/in/ann")])

    async def produced(targets, **kw):
        return lp.EnrichResult(attempted=1, enriched=1, evidence_written=4)

    monkeypatch.setattr(lp, "enrich", produced)
    await worker.handle_enrich(a_job(role_id=str(role_id)))

    kinds = {r[0] for r in db_conn.execute(
        "select kind from job where state = 'queued'").fetchall()}
    assert kinds == {"collect", "score"}


async def test_nobody_to_read_is_a_note_not_a_failure(db_conn, monkeypatch):
    from hi.adapters import linkedin_profile as lp

    monkeypatch.setattr(lp, "targets_for_role", lambda _r, **kw: [])
    await worker.handle_enrich(a_job(role_id=str(uuid.uuid4())))
    assert "already been read" in note_of(db_conn)


def note_of(conn) -> str:
    return conn.execute(
        "select note from job where kind = 'enrich' order by id desc limit 1"
    ).fetchone()[0]
