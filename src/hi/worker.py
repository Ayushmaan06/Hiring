"""Job queue and worker (IMPLEMENTATION.md 1.10).

A Postgres table and one process. `FOR UPDATE SKIP LOCKED` is what makes that safe
without Redis or Celery — two workers can never claim the same row, and a crashed
worker's job returns to the queue via the reaper rather than being lost.

Run with:  python -m hi.worker
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from hi.db import pool

MAX_ATTEMPTS = 3
LEASE_MINUTES = 15
IDLE_SLEEP_SECONDS = 2.0

JOB_KINDS = ("discover", "collect", "score", "enrich")

# An enrich job's `scope` -> the `candidate_ref.gate_state` it reads. `None` means every
# verdict. Kept here because both the button and the CLI resolve a scope the same way.
GATE_SCOPES = {"passed": "passed", "ruled_out": "failed", "all": None}


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int


def payload_hash(kind: str, payload: dict) -> str:
    """Stable hash of a job's identity. sort_keys matters — dict order must not
    produce two 'different' jobs for the same work."""
    blob = json.dumps({"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def enqueue(kind: str, payload: dict) -> int | None:
    """Queue a job. Returns None if identical work is already queued or running.

    The no-op-on-duplicate behaviour is deliberate: a double-clicked button must not
    double the API spend.
    """
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind {kind!r}; expected one of {JOB_KINDS}")

    with pool.connection() as conn:
        row = conn.execute(
            "insert into job (kind, payload_json, payload_hash, state) "
            "values (%s, %s, %s, 'queued') "
            "on conflict do nothing returning id",
            (kind, json.dumps(payload), payload_hash(kind, payload)),
        ).fetchone()
    return row[0] if row else None


def claim(locked_by: str) -> Job | None:
    """Atomically take the oldest queued job, or None.

    SKIP LOCKED means a concurrent worker steps over a row another has locked instead
    of blocking on it, which is what allows more than one worker without coordination.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "update job set state = 'running', locked_at = now(), locked_by = %s, "
            "               attempts = attempts + 1 "
            "where id = ("
            "  select id from job where state = 'queued' "
            "  order by created_at for update skip locked limit 1"
            ") returning id, kind, payload_json, attempts",
            (locked_by,),
        ).fetchone()
    if row is None:
        return None
    return Job(id=row[0], kind=row[1], payload=row[2] or {}, attempts=row[3])


def complete(job_id: int) -> None:
    # `stopped` is a recruiter's decision; a handler finishing afterwards must not undo it.
    with pool.connection() as conn:
        conn.execute(
            "update job set state = 'done', finished_at = now(), last_error = null "
            "where id = %s and state <> 'stopped'",
            (job_id,),
        )


def stop_discovery(role_id, actor: str | None = None) -> int:
    """The "Stop search" button: halt a role's queued or running SERP search.

    A running handler notices through `is_stopped` before its next SERP page, so at most
    the request already in flight is paid for. What it found so far is kept and ranked.
    """
    with pool.connection() as conn:
        rows = conn.execute(
            "update job set state = 'stopped', finished_at = now(), locked_at = null, "
            "  locked_by = null, last_error = %s "
            "where kind = 'discover' and payload_json->>'role_id' = %s "
            "and state in ('queued', 'running') returning id",
            (f"stopped by {actor or 'a recruiter'}", str(role_id)),
        ).fetchall()
    return len(rows)


def is_stopped(job_id: int) -> bool:
    with pool.connection() as conn:
        row = conn.execute("select state from job where id = %s", (job_id,)).fetchone()
    return row is not None and row[0] == "stopped"


def fail(job_id: int, error: str) -> str:
    """Mark a failure. Requeues for another attempt, or gives up at MAX_ATTEMPTS.

    Giving up matters: a job that retries forever is how a queue turns into an
    unbounded bill.
    """
    with pool.connection() as conn:
        state = conn.execute(
            "update job set "
            "  state = case when attempts >= %s then 'failed' else 'queued' end,"
            "  last_error = %s,"
            "  locked_at = null, locked_by = null,"
            "  finished_at = case when attempts >= %s then now() else null end "
            "where id = %s and state <> 'stopped' returning state",
            (MAX_ATTEMPTS, error[:2000], MAX_ATTEMPTS, job_id),
        ).fetchone()
    # A stopped job that then errors stays stopped — requeueing it would restart the spend.
    return state[0] if state else "stopped"


def reap(lease_minutes: int = LEASE_MINUTES) -> int:
    """Requeue jobs whose worker died holding them.

    A process killed mid-job leaves state='running' forever; without this the work is
    silently lost and the role never finishes. Attempts were already incremented at
    claim time, so a job that repeatedly kills its worker still hits MAX_ATTEMPTS.
    """
    with pool.connection() as conn:
        rows = conn.execute(
            "update job set "
            "  state = case when attempts >= %s then 'failed' else 'queued' end,"
            "  locked_at = null, locked_by = null,"
            "  last_error = coalesce(last_error, 'lease expired: worker died holding this job') "
            "where state = 'running' and locked_at < now() - make_interval(mins => %s) "
            "returning id",
            (MAX_ATTEMPTS, lease_minutes),
        ).fetchall()
    return len(rows)


def set_note(job_id: int, note: str) -> None:
    """One line of progress on a running job (migration 014).

    Written per profile by the enrichment handler, read by the page that polls. Truncated
    rather than validated: a note is a courtesy to a watcher, never a reason to fail a run.
    """
    with pool.connection() as conn:
        conn.execute("update job set note = %s where id = %s", (note[:500], job_id))


def pending(kind: str) -> bool:
    """True if a job of this kind is queued or running anywhere.

    Asked of `enrich` across *every* role, not just this one: the LinkedIn account is one
    account, so a read running for another role is a reason to refuse this one.
    """
    with pool.connection() as conn:
        return conn.execute(
            "select exists (select 1 from job where kind = %s and state in ('queued', 'running'))",
            (kind,),
        ).fetchone()[0]


def latest_job(kind: str, role_id) -> dict | None:
    """The most recent job of `kind` for a role — state, progress note, error."""
    with pool.connection() as conn:
        row = conn.execute(
            "select id, state, note, last_error, finished_at from job "
            "where kind = %s and payload_json->>'role_id' = %s "
            "order by created_at desc limit 1",
            (kind, str(role_id)),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(("id", "state", "note", "error", "finished_at"), row))


def queue_counts() -> dict[str, int]:
    with pool.connection() as conn:
        rows = conn.execute("select state, count(*) from job group by state").fetchall()
    return {state: n for state, n in rows}


# --------------------------------------------------------------------------
# handlers — each may enqueue children
# --------------------------------------------------------------------------


async def handle_discover(job: Job) -> None:
    """Run a role's discovery, then queue verification and scoring behind it."""
    from hi import discovery
    from hi.models import RoleSpec

    payload = job.payload
    role_id = uuid.UUID(payload["role_id"])
    role = discovery.get_role(role_id)
    if role is None:
        raise ValueError(f"no such role: {role_id}")
    _, spec = role
    assert isinstance(spec, RoleSpec)

    result = await discovery.run_discovery(
        role_id, spec, pages=int(payload.get("pages", 1)), max_queries=payload.get("max_queries"),
        should_stop=lambda: is_stopped(job.id),
    )
    print(f"discover {role_id}: {result}")

    enqueue("collect", {"role_id": str(role_id)})
    enqueue("score", {"role_id": str(role_id)})


async def handle_collect(job: Job) -> None:
    """Verify gate-passing refs against public artefacts.

    Only runs on refs that passed the snippet gate, and only resolves via strong keys —
    a ref with none simply has no artefact evidence, which is a valid outcome.
    """
    from hi import discovery, identity
    from hi.adapters import github
    from hi.extract import write_evidence

    role_id = uuid.UUID(job.payload["role_id"])
    written = resolved = skipped = 0

    for ref_row in discovery.saved_refs(role_id, gate_state="passed"):
        ref = discovery.ref_from_row(ref_row)
        strong_keys = {"linkedin_slug": ref.ref_value.rsplit("/", 1)[-1]}
        raw = ref.snippet_raw or ""
        if login := github.login_from_url(raw if "github.com" in raw else ""):
            strong_keys["github_login"] = login

        result = identity.resolve(
            strong_keys, display_name=ref.snippet_name, location_text=ref.snippet_location
        )
        if result.candidate_id is None:
            skipped += 1
            continue

        with pool.connection() as conn:
            conn.execute(
                "update candidate_ref set candidate_id = %s where role_id = %s and ref_value = %s",
                (result.candidate_id, role_id, ref.ref_value),
            )

        login = github.resolve(identity.strong_keys_for(result.candidate_id))
        if login is None:
            continue  # no artefact evidence reachable; visible, not fatal
        collected = await github.collect(login)
        if collected is None:
            continue
        outcome = write_evidence(
            result.candidate_id, github.rows_from_collected(collected), source_text=None
        )
        written += outcome.written
        resolved += 1

    print(f"collect {role_id}: resolved {resolved}, evidence {written}, ambiguous {skipped}")


async def handle_score(job: Job) -> None:
    from hi import matching

    role_id = uuid.UUID(job.payload["role_id"])
    print(f"score {role_id}: {matching.score_role(role_id)}")


async def handle_enrich(job: Job) -> None:
    """Read LinkedIn profiles for a role — what the button starts (IMPLEMENTATION.md 1.11d).

    Deliberately thin, and thin is the point: the daily cap, the pacing, the wall
    detection, the kill switch and the advisory lock all live in `linkedin_profile`, so a
    read started from a page and a read started from the CLI cannot drift apart on any of
    them. This handler decides nothing except what to write in the note.
    """
    from hi.adapters import linkedin_profile as lp

    role_id = uuid.UUID(job.payload["role_id"])
    limit = int(job.payload.get("limit") or 0) or None
    # Which snippet-gate verdicts this run is allowed to read. Default is the gate's
    # own answer; a recruiter can spend their own quota on the people it refused.
    gate_state = GATE_SCOPES[job.payload.get("scope") or "passed"]

    targets = lp.targets_for_role(role_id, gate_state=gate_state)
    # Picked by hand from the queue: only these people, whichever side of the gate.
    if slugs := job.payload.get("slugs"):
        wanted = set(slugs)
        targets = [t for t in targets if lp.slug_of(t[1]) in wanted]
    if not targets:
        set_note(job.id, "Everyone found for this role has already been read.")
        return

    def progress(done: int, total: int, url: str) -> None:
        set_note(job.id, f"Reading {done} of {total}: {lp.slug_of(url)}")

    result = await lp.enrich(targets, limit=limit, on_progress=progress)

    # "Stopped" has to be visible on the page, not just in a log. A run that hit a wall
    # and a run that read nobody look identical from the outside, and silent success is
    # the characteristic LinkedIn failure (ARCHITECTURE.md §9a.5).
    if result.stopped_reason:
        set_note(job.id, f"Stopped: {result.stopped_reason}")
    else:
        set_note(
            job.id,
            f"Read {result.enriched} of {result.attempted} "
            f"{'person' if result.attempted == 1 else 'people'}, "
            f"{result.with_dated_experience} with employment history.",
        )
    print(f"enrich {role_id}: {result}")

    if result.evidence_written:
        # New strong keys make artefact verification reachable, and a new experience row
        # changes every ranking for the role.
        enqueue("collect", {"role_id": str(role_id)})
        enqueue("score", {"role_id": str(role_id)})
        # The same people may sit on other roles; their rankings changed too.
        with pool.connection() as conn:
            others = conn.execute(
                "select distinct role_id from candidate_ref "
                "where candidate_id = any(%s) and role_id <> %s",
                ([cid for cid, _ in targets if cid], role_id),
            ).fetchall()
        for (other,) in others:
            enqueue("score", {"role_id": str(other)})


HANDLERS = {
    "discover": handle_discover,
    "collect": handle_collect,
    "score": handle_score,
    "enrich": handle_enrich,
}


# --------------------------------------------------------------------------
# loop
# --------------------------------------------------------------------------


async def run_once(locked_by: str) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    job = claim(locked_by)
    if job is None:
        return False

    handler = HANDLERS.get(job.kind)
    if handler is None:
        fail(job.id, f"no handler for kind {job.kind!r}")
        return True

    try:
        await handler(job)
        complete(job.id)
    except Exception as exc:  # a bad job must not take the worker down with it
        state = fail(job.id, f"{type(exc).__name__}: {exc}")
        print(f"job {job.id} ({job.kind}) failed -> {state}: {exc}", file=sys.stderr)
    return True


async def run_forever(locked_by: str, *, handle_signals: bool = True) -> None:
    """Drain the queue until stopped.

    `handle_signals=False` when embedded in the web app: uvicorn owns SIGINT there, and
    installing our own handler over it means Ctrl-C stops the worker and leaves the
    server up. Cancelling the task is the caller's job instead.
    """
    stopping = asyncio.Event()

    def request_stop(*_: object) -> None:
        print("stop requested; finishing current job", file=sys.stderr)
        stopping.set()

    if handle_signals:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, request_stop)
            except (ValueError, OSError):
                pass  # not on the main thread, or unsupported on this platform

    print(f"worker {locked_by} started")
    while not stopping.is_set():
        reap()
        if not await run_once(locked_by):
            try:
                await asyncio.wait_for(stopping.wait(), timeout=IDLE_SLEEP_SECONDS)
            except asyncio.TimeoutError:
                pass
    print(f"worker {locked_by} stopped")


def main() -> None:
    locked_by = f"{os.uname().nodename if hasattr(os, 'uname') else 'host'}:{os.getpid()}"
    try:
        asyncio.run(run_forever(locked_by))
    finally:
        pool.close()


if __name__ == "__main__":
    main()
