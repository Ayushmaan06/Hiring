"""Role creation, discovery runs, and the persisted ref store.

Discovery results are written to `candidate_ref`, whose `unique (role_id, ref_kind,
ref_value)` makes a re-run idempotent — so a role's people are looked up from the DB
rather than re-searched, and SERP budget is only spent when we actually want more
depth. `saved_refs()` is the read path and touches no network at all.

Local DB search: when a new role is created, we also query existing candidates for
matches before searching externally. This reuses internal hires and rich profiles.
"""

from __future__ import annotations

import json
import uuid

from hi.adapters import linkedin_serp
from hi.db import pool
from hi.gating import snippet_gate
from hi.models import CandidateRef, RoleSpec


def create_role(title: str, spec: RoleSpec, *, jd_text: str | None = None, created_by: str | None = None) -> uuid.UUID:
    with pool.connection() as conn:
        return conn.execute(
            "insert into role (title, jd_text, spec_json, status, created_by) "
            "values (%s, %s, %s, 'open', %s) returning id",
            (title, jd_text, json.dumps(spec.model_dump()), created_by),
        ).fetchone()[0]


def get_role(role_id: uuid.UUID) -> tuple[str | None, RoleSpec] | None:
    with pool.connection() as conn:
        row = conn.execute("select title, spec_json from role where id = %s", (role_id,)).fetchone()
    if row is None:
        return None
    return row[0], RoleSpec.model_validate(row[1])


def _display_headline(ref: CandidateRef) -> str | None:
    """What the shortlist row shows. Structured title+employer when we have it."""
    if ref.snippet_title and ref.snippet_employer:
        return f"{ref.snippet_title} at {ref.snippet_employer}"
    return ref.snippet_title or ref.snippet_headline


def persist_refs(role_id: uuid.UUID, refs: list[CandidateRef], spec: RoleSpec) -> dict[str, int]:
    """Gate each ref and upsert it. Pure DB + pure gate: no network, so it is testable.

    Re-running re-evaluates the gate (the spec may have been edited) but never
    clobbers `candidate_id`, which is set later by enrichment.
    """
    counts = {"passed": 0, "failed": 0}
    with pool.connection() as conn:
        for ref in refs:
            verdict = snippet_gate(ref, spec)
            state = "passed" if verdict.passed else "failed"
            counts[state] += 1
            conn.execute(
                "insert into candidate_ref "
                "(role_id, adapter, ref_kind, ref_value, snippet_name, snippet_headline, "
                " snippet_location, snippet_raw, source_url, gate_state, gate_reason) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (role_id, ref_kind, ref_value) do update set "
                "  snippet_name = excluded.snippet_name,"
                "  snippet_headline = excluded.snippet_headline,"
                "  snippet_location = excluded.snippet_location,"
                "  snippet_raw = excluded.snippet_raw,"
                "  gate_state = excluded.gate_state,"
                "  gate_reason = excluded.gate_reason",
                (
                    role_id, ref.adapter, ref.ref_kind, ref.ref_value, ref.snippet_name,
                    _display_headline(ref), ref.snippet_location, ref.snippet_raw,
                    ref.source_url, state, verdict.reason,
                ),
            )
    return counts


def ref_from_row(row: dict) -> CandidateRef:
    """Rebuild a full CandidateRef from a stored row.

    Everything gate-relevant is recovered from `snippet_raw`, never from the other
    columns. Those are *display* values — `snippet_headline` in particular holds the
    composed "Title at Employer" string, which would overwrite the person's own
    headline and silently flip their gate verdict on a re-run. `snippet_raw` is the
    verbatim SERP payload and is the only lossless source.
    """
    location = title = employer = None
    headline = row["snippet_headline"]
    name = row["snippet_name"]

    try:
        raw = json.loads(row["snippet_raw"])
        location, title, employer = linkedin_serp.parse_extensions(raw.get("extensions") or [])
        raw_name, raw_headline = linkedin_serp.parse_title(raw.get("title") or "")
        name = raw_name or name
        headline = raw_headline  # the person's own headline, not our composed one
    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    return CandidateRef(
        adapter=row["adapter"],
        ref_kind=row["ref_kind"],
        ref_value=row["ref_value"],
        source_url=row["source_url"],
        snippet_raw=row["snippet_raw"],
        snippet_name=name,
        snippet_headline=headline,
        snippet_location=location or row["snippet_location"],
        snippet_title=title,
        snippet_employer=employer,
    )


def saved_refs(role_id: uuid.UUID, *, gate_state: str | None = None) -> list[dict]:
    """Read back a role's discovered people. No network — this is the whole point."""
    sql = (
        "select adapter, ref_kind, ref_value, snippet_name, snippet_headline, "
        "       snippet_location, snippet_raw, source_url, gate_state, gate_reason, "
        "       candidate_id, discovered_at "
        "from candidate_ref where role_id = %s"
    )
    params: list = [role_id]
    if gate_state:
        sql += " and gate_state = %s"
        params.append(gate_state)
    sql += " order by gate_state, discovered_at"

    with pool.connection() as conn:
        cur = conn.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def role_counts(role_id: uuid.UUID) -> dict[str, int]:
    with pool.connection() as conn:
        rows = conn.execute(
            "select gate_state, count(*) from candidate_ref where role_id = %s group by gate_state",
            (role_id,),
        ).fetchall()
    return {state: n for state, n in rows}


async def run_discovery(
    role_id: uuid.UUID, spec: RoleSpec, *, pages: int = 1, max_queries: int | None = None,
    should_stop=lambda: False,
) -> dict:
    """Plan -> search -> gate -> persist. Each page of each query is one SERP search.

    Also searches local DB for existing candidates first (no cost, instant).
    """
    total = {"passed": 0, "failed": 0}
    seen: set[str] = set()

    # First pass: search SERP
    queries = linkedin_serp.plan(spec)
    if max_queries is not None:
        queries = queries[:max_queries]

    for q in queries:
        if should_stop():
            break
        refs = await linkedin_serp.discover(q, pages=pages, should_stop=should_stop)
        fresh = [r for r in refs if r.ref_value not in seen]
        seen.update(r.ref_value for r in fresh)

        counts = persist_refs(role_id, fresh, spec)
        total["passed"] += counts["passed"]
        total["failed"] += counts["failed"]

        with pool.connection() as conn:
            conn.execute(
                "insert into role_query (role_id, adapter, query_text, run_at, results_count) "
                "values (%s, %s, %s, now(), %s)",
                (role_id, q.adapter, q.query_text, len(refs)),
            )

    # Second pass: people we already know about from earlier searches (free, instant).
    # Never let a bug here fail a search that SERP has already paid for.
    try:
        local_refs = local_db_candidates(spec) + known_candidates(spec, role_id)
    except Exception:
        import logging
        logging.exception("local lookup failed for role %s; keeping SERP results", role_id)
        local_refs = []
    fresh = [r for r in local_refs if r.ref_value not in seen]
    if fresh:
        seen.update(r.ref_value for r in fresh)
        counts = persist_refs(role_id, fresh, spec)
        total["passed"] += counts["passed"]
        total["failed"] += counts["failed"]

        with pool.connection() as conn:
            conn.execute(
                "insert into role_query (role_id, adapter, query_text, run_at, results_count) "
                "values (%s, %s, %s, now(), %s)",
                (role_id, "local_db", "existing candidates matching spec", len(fresh)),
            )

    # The rescue is a judgement about the whole role — it needs the final pass rate,
    # which does not exist until every query has been persisted. Re-gating here spends
    # no network and is what makes a fresh search and the "find more people" button
    # arrive at the same verdicts instead of two different ones.
    regate(role_id, spec)

    counts = role_counts(role_id)
    return {
        "queries": len(queries),
        "new_refs": len(seen),
        "searched": total,
        "passed": counts.get("passed", 0),
        "failed": counts.get("failed", 0),
    }


# A gate that refuses most of what the search found is failing at its job: the pool is
# not the problem, the filter is. Title matching is where this bites. A business role is
# posted as "Senior Manager — Strategy & Business Operations" and the people who do that
# job write "BizOps", "Strategy & Ops", "Chief of Staff" on their profile, so the title
# test threw away 39 of 40 on one live role (2026-09-18). Below this share we stop
# trusting the title test for that role.
RESCUE_BELOW = 0.5

MANUAL_ADAPTER = "manual"


def _rescue(rows: list[dict], verdicts: dict[str, object], spec: RoleSpec) -> dict:
    """Re-judge a thin role's refusals with the title test switched off.

    Everything else still applies — an excluded employer is still excluded, and someone
    abroad is still abroad. That is the point of doing it by re-running the same gate
    against a spec with no titles rather than by writing a second, looser gate: there
    stays exactly one definition of "outside India" in this codebase.
    """
    kept = sum(1 for v in verdicts.values() if v.passed)
    if not rows or kept / len(rows) >= RESCUE_BELOW:
        return verdicts

    refused = len(rows) - kept
    without_titles = spec.model_copy(update={"titles": [], "must_have_skills": []})
    out = dict(verdicts)
    for row in rows:
        if verdicts[row["ref_value"]].passed:
            continue
        verdict = snippet_gate(ref_from_row(row), without_titles)
        if not verdict.passed:
            continue  # abroad, or an excluded employer: those refusals stand
        note = (
            f"admitted: the job-title filter refused {refused} of {len(rows)} people this "
            f"search found, so it is not being trusted for this role — check the headline"
        )
        out[row["ref_value"]] = verdict.model_copy(
            update={"reason": f"{note} ({verdict.reason})" if verdict.reason else note}
        )
    return out


def regate(role_id: uuid.UUID, spec: RoleSpec, *, apply: bool = True) -> list[tuple]:
    """Re-run the snippet gate over refs already stored. No network, no SERP spend.

    A gate fix changes what *future* searches keep and nothing else, because the verdict
    is written to `candidate_ref` at discovery time. Two people abroad sat on the ops
    role for a week that way. Re-reads each stored `snippet_raw` — the verbatim SERP
    payload and the only lossless source, see `ref_from_row` — and rewrites the verdict.

    Only `gate_state` and `gate_reason` change. A ref that flips to `failed` keeps
    everything already learned about that person (ARCHITECTURE.md 2.5: candidate data is
    never deleted). Re-score the role afterwards so the page catches up.
    """
    # Judged in one pass rather than gate-then-rescue-as-a-second-write: a rescued ref
    # fails the plain gate, so two passes would demote it and re-admit it every run and
    # report both halves of that churn as people who "moved".
    # A `manual` ref was filed by a person, not by a gate verdict on a snippet, so
    # re-gating it would only undo that decision.
    rows = [r for r in saved_refs(role_id) if r["adapter"] != MANUAL_ADAPTER]
    verdicts = {row["ref_value"]: snippet_gate(ref_from_row(row), spec) for row in rows}
    verdicts = _rescue(rows, verdicts, spec)

    changes = []
    for row in rows:
        verdict = verdicts[row["ref_value"]]
        new_state = "passed" if verdict.passed else "failed"
        if new_state == row["gate_state"]:
            continue
        changes.append((row["snippet_name"], row["gate_state"], new_state, verdict.reason))
        if apply:
            with pool.connection() as conn:
                # The schema refuses a `failed` ref with no reason, which is the point:
                # an exclusion nobody can read is indistinguishable from a bug.
                conn.execute(
                    "update candidate_ref set gate_state = %s, gate_reason = %s "
                    "where role_id = %s and ref_value = %s",
                    (new_state, verdict.reason or None, role_id, row["ref_value"]),
                )
    return changes


def queries_run(role_id: uuid.UUID) -> int:
    """How many distinct SERP queries this role has actually run (not just planned)."""
    with pool.connection() as conn:
        return conn.execute(
            "select count(distinct query_text) from role_query "
            "where role_id = %s and adapter = %s",
            (role_id, linkedin_serp.ADAPTER),
        ).fetchone()[0]


def search_depth(role_id: uuid.UUID) -> int:
    """How many pages of SERP results this role has been searched to.

    Read off the discover jobs' payloads rather than a column on `role`, because the
    payload is already the record of what was asked for and a second copy would drift.
    A job queued before `pages` existed counts as 1.
    """
    with pool.connection() as conn:
        deepest = conn.execute(
            "select max(coalesce((payload_json->>'pages')::int, 1)) from job "
            "where kind = 'discover' and payload_json->>'role_id' = %s",
            (str(role_id),),
        ).fetchone()[0]
    return deepest or 1


def known_candidates(spec: RoleSpec, role_id: uuid.UUID, limit: int = 100) -> list[CandidateRef]:
    """People other roles already found whose stored snippet passes this role's gate.

    Any source, any role — whoever is in `candidate_ref` already. Pre-gated so a new
    role gets the plausible ones rather than every person in the database, and people
    already on this role are left alone so their verdict is not rewritten.
    """
    with pool.connection() as conn:
        cur = conn.execute(
            "select distinct on (ref_value) adapter, ref_kind, ref_value, snippet_name, "
            "       snippet_headline, snippet_location, snippet_raw, source_url "
            "from candidate_ref r where gate_state = 'passed' and role_id <> %s "
            "and not exists (select 1 from candidate_ref o where o.role_id = %s "
            "                and o.ref_kind = r.ref_kind and o.ref_value = r.ref_value)",
            (role_id, role_id),
        )
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    refs = []
    for row in rows:
        ref = ref_from_row(row).model_copy(update={"adapter": "local_db"})
        if snippet_gate(ref, spec).passed:
            refs.append(ref)
            if len(refs) >= limit:
                break
    return refs


def local_db_candidates(spec: RoleSpec) -> list[CandidateRef]:
    """Find existing candidates in the local DB who might match the role spec.

    Searches by must-have skills and location (if specified). Returns CandidateRef
    objects with adapter='local_db' so they can be gated and persisted alongside
    SERP results. This reuses rich profiles we already have, avoiding redundant
    external searches and SERP budget spend.

    ponytail: simple keyword match, not semantic. Upgrade to embedding search if
    recall matters more than precision.
    """
    if not spec.must_have_skills and not spec.locations:
        return []

    where = ["c.status = 'active'"]
    params: list = []

    if spec.must_have_skills:
        where.append(
            "exists (select 1 from evidence e where e.candidate_id = c.id "
            "and e.claim_type = 'skill' and e.claim_value = any(%s))"
        )
        params.append(spec.must_have_skills)

    if spec.locations:
        where.append("c.location_region = any(%s)")
        params.append(spec.locations)

    sql = f"""
        select c.id, c.display_name, c.primary_location_text, c.location_region,
               (select i.value from identity i where i.candidate_id = c.id
                and i.kind = 'linkedin_slug' limit 1) as linkedin_slug,
               (select e.claim_value from evidence e where e.candidate_id = c.id
                and e.claim_type = 'title' order by e.observed_at desc limit 1) as current_title
        from candidate c
        where {' and '.join(where)}
        limit 100
    """
    with pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    refs = []
    for candidate_id, display_name, location_text, location_region, linkedin_slug, current_title in rows:
        if not linkedin_slug:
            continue  # candidate_ref is keyed on a linkedin ref; skip people we can't link back to LinkedIn
        location = location_text or location_region
        # Match linkedin_serp's canonical form so a person found both ways dedupes
        # on the same (ref_kind, ref_value) instead of creating a second row.
        canonical = f"linkedin.com/in/{linkedin_slug}"
        refs.append(CandidateRef(
            adapter="local_db",
            ref_kind="linkedin_url",
            ref_value=canonical,
            source_url=f"https://{canonical}",
            snippet_raw=json.dumps({"location": location, "candidate_id": str(candidate_id)}),
            snippet_name=display_name,
            snippet_headline=current_title,
            snippet_location=location,
        ))
    return refs
