"""Persist scores (ARCHITECTURE.md §4.6).

`scoring.py` is pure and knows nothing about the database; this module is the boundary
that loads evidence, calls it, and writes `match` rows. A re-score is a new row rather
than a mutation — `unique (role_id, candidate_id, scorer_version, weights_version)`
is what lets you still answer "why was this ranked 3rd last Tuesday".
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from hi import canon, scoring
from hi.db import pool
from hi.models import RoleSpec
from hi.scoring import Evidence, MatchResult, Weights


ENGINEERING_PROFILE = "engineering"
BUSINESS_PROFILE = "business"


def active_weights(profile: str = ENGINEERING_PROFILE) -> Weights:
    """The approved weight row for a profile. Falls back to code defaults if none."""
    with pool.connection() as conn:
        row = conn.execute(
            "select version, weights_json from scoring_weights "
            "where active and profile = %s order by id desc limit 1",
            (profile,),
        ).fetchone()
    if row is None:
        return scoring.DEFAULT_WEIGHTS
    return Weights(version=row[0], values={k: float(v) for k, v in row[1].items()})


def weights_profile_for(spec: RoleSpec) -> str:
    """Which weight profile this role is scored on (ARCHITECTURE.md §2.3).

    A role naming a language or framework among its must-haves is one where public
    artefacts are a fair expectation, so `skill_depth` and `activity_recency` can earn
    their weight. A role naming only tools, practices, domains or qualifications is not,
    and under the engineering profile 40% of its candidates' score is unreachable.

    A role with **no** must-have skills keeps the engineering profile: there is no
    positive signal that it is non-technical, and scoring should not change on an
    absence.
    """
    if not spec.must_have_skills:
        return ENGINEERING_PROFILE
    return ENGINEERING_PROFILE if canon.expects_artifacts(spec.must_have_skills) else BUSINESS_PROFILE


def weights_for_role(spec: RoleSpec) -> Weights:
    return active_weights(weights_profile_for(spec))


def evidence_for(candidate_id: uuid.UUID) -> list[Evidence]:
    with pool.connection() as conn:
        rows = conn.execute(
            "select id, claim_type, tier, claim_key, claim_value, value_num, observed_at, source_url "
            "from evidence where candidate_id = %s",
            (candidate_id,),
        ).fetchall()
    return [
        Evidence(
            id=r[0],
            claim_type=r[1],
            tier=r[2],
            claim_key=r[3],
            claim_value=r[4],
            value_num=float(r[5]) if r[5] is not None else None,
            observed_at=r[6],
            source_url=r[7] or "",
        )
        for r in rows
    ]


def candidates_for_role(role_id: uuid.UUID) -> list[uuid.UUID]:
    """Candidates attached to this role's refs. Excludes archived (merged-away) people."""
    with pool.connection() as conn:
        rows = conn.execute(
            "select distinct r.candidate_id from candidate_ref r "
            "join candidate c on c.id = r.candidate_id "
            "where r.role_id = %s and r.candidate_id is not null and c.status = 'active'",
            (role_id,),
        ).fetchall()
    return [r[0] for r in rows]


def write_match(
    role_id: uuid.UUID,
    candidate_id: uuid.UUID,
    result: MatchResult,
    weights: Weights,
    *,
    now: datetime,
) -> None:
    with pool.connection() as conn:
        conn.execute(
            "insert into match "
            "(role_id, candidate_id, score, components_json, gates_json, scorer_version, "
            " weights_version, scored_at) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (role_id, candidate_id, scorer_version, weights_version) do update set "
            "  score = excluded.score,"
            "  components_json = excluded.components_json,"
            "  gates_json = excluded.gates_json,"
            "  scored_at = excluded.scored_at",
            (
                role_id, candidate_id, result.score,
                json.dumps(result.components, sort_keys=True),
                json.dumps({**result.gates, "seniority_basis": result.seniority_basis}, sort_keys=True),
                scoring.SCORER_VERSION, weights.version, now,
            ),
        )


def score_role(role_id: uuid.UUID, *, now: datetime | None = None) -> dict:
    """Score every candidate attached to a role. Returns a small summary."""
    from hi.discovery import get_role

    role = get_role(role_id)
    if role is None:
        raise ValueError(f"no such role: {role_id}")
    _, spec = role

    now = now or datetime.now(timezone.utc)
    weights = weights_for_role(spec)

    scored = passed = 0
    for candidate_id in candidates_for_role(role_id):
        result = scoring.evaluate(spec, evidence_for(candidate_id), weights, now=now)
        write_match(role_id, candidate_id, result, weights, now=now)
        scored += 1
        passed += 1 if result.passed_gates else 0

    return {"scored": scored, "passed_gates": passed, "weights_version": weights.version}


def shortlist(role_id: uuid.UUID, *, include_excluded: bool = False) -> list[dict]:
    """Ranked matches for a role. Excluded candidates are available, never hidden."""
    with pool.connection() as conn:
        cur = conn.execute(
            "select m.id as match_id, m.candidate_id, c.display_name, c.primary_location_text, "
            "       m.score, m.components_json, m.gates_json, m.scorer_version, m.weights_version, "
            "       (select action from recruiter_action a where a.match_id = m.id "
            "        order by a.created_at desc limit 1) as last_action, "
            "       (select value from identity i where i.candidate_id = c.id "
            "        and i.kind = 'linkedin_slug' order by value limit 1) as linkedin_slug "
            "from match m join candidate c on c.id = m.candidate_id "
            "where m.role_id = %s order by m.score desc, c.display_name",
            (role_id,),
        )
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Who has actually been looked at. A candidate with no evidence at all has been
        # discovered and nothing more, and their gate verdict says "no evidence for
        # Python" — which reads as "we checked and they lack it" when the truth is
        # "we never looked". Callers need to be able to tell those apart, because
        # presenting the second as the first discards real candidates on a false reason.
        checked = {
            r[0]
            for r in conn.execute(
                "select distinct candidate_id from evidence where candidate_id = any(%s)",
                ([row["candidate_id"] for row in rows],),
            ).fetchall()
        }

    for row in rows:
        gates = row["gates_json"] or {}
        row["checked"] = row["candidate_id"] in checked
        row["passed_gates"] = not any(
            isinstance(v, str) and v.startswith("fail") for v in gates.values()
        )
        row["reasons_ruled_out"] = [
            v for k, v in gates.items() if isinstance(v, str) and v.startswith("fail")
        ]
        row["top_proven"] = _top_proven_skills(row["candidate_id"])
    return rows if include_excluded else [r for r in rows if r["passed_gates"]]


def _top_proven_skills(candidate_id: uuid.UUID, limit: int = 3) -> list[str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "select claim_key, sum(coalesce(value_num, 0)) as vol from evidence "
            "where candidate_id = %s and claim_type = 'skill' and tier = 'artifact_backed' "
            "  and claim_key is not null "
            "group by claim_key order by vol desc limit %s",
            (candidate_id, limit),
        ).fetchall()
    if rows:
        return [r[0] for r in rows]
    # Nothing proven: fall back to what they say, so the row is never blank.
    with pool.connection() as conn:
        rows = conn.execute(
            "select distinct claim_key from evidence where candidate_id = %s "
            "and claim_type = 'skill' and claim_key is not null limit %s",
            (candidate_id, limit),
        ).fetchall()
    return [r[0] for r in rows]


PROVEN_TIER = "artifact_backed"


def _weighted_components(components: dict, weights_version: str | None) -> dict:
    """Drop components that this role's weight profile gives no weight to."""
    if not components or not weights_version:
        return components
    with pool.connection() as conn:
        row = conn.execute(
            "select weights_json from scoring_weights where version = %s", (weights_version,)
        ).fetchone()
    if row is None:
        return components
    values = row[0] or {}
    return {k: v for k, v in components.items() if float(values.get(k, 1.0)) > 0}


def candidate_detail(role_id: uuid.UUID, candidate_id: uuid.UUID) -> dict | None:
    """Everything the side panel shows. This screen is the point of the product."""
    with pool.connection() as conn:
        candidate = conn.execute(
            "select id, display_name, primary_location_text, location_region "
            "from candidate where id = %s",
            (candidate_id,),
        ).fetchone()
        if candidate is None:
            return None

        match = conn.execute(
            "select id, score, components_json, gates_json, weights_version, rationale_text "
            "from match where role_id = %s and candidate_id = %s "
            "order by scored_at desc limit 1",
            (role_id, candidate_id),
        ).fetchone()

        claims = conn.execute(
            "select claim_type, claim_key, claim_value, tier, source_url, snippet, observed_at "
            "from evidence where candidate_id = %s "
            "order by (tier = 'artifact_backed') desc, claim_type, claim_key",
            (candidate_id,),
        ).fetchall()

        links = conn.execute(
            "select kind, value from identity where candidate_id = %s order by kind",
            (candidate_id,),
        ).fetchall()

        # Prior decisions on OTHER roles — prevents re-presenting someone already rejected.
        previously = conn.execute(
            "select r.title, a.action, a.note, a.created_at from recruiter_action a "
            "join match m on m.id = a.match_id join role r on r.id = m.role_id "
            "where m.candidate_id = %s and m.role_id <> %s order by a.created_at desc limit 5",
            (candidate_id, role_id),
        ).fetchall()

    proven, says_so = [], []
    for claim_type, claim_key, claim_value, tier, source_url, snippet, observed_at in claims:
        item = {
            "claim_type": claim_type,
            "label": claim_key or claim_value or claim_type,
            "value": claim_value,
            "source_url": source_url,
            "snippet": snippet,
            "observed_at": observed_at,
        }
        (proven if tier == PROVEN_TIER else says_so).append(item)

    gates = (match[3] if match else {}) or {}
    return {
        "candidate_id": candidate[0],
        "display_name": candidate[1],
        "location_text": candidate[2],
        "location_region": candidate[3],
        "match_id": match[0] if match else None,
        "score": float(match[1]) if match else None,
        # Components that carry no weight under this role's profile are dropped rather
        # than rendered as an empty bar. Under the business profile `skill_depth` and
        # `activity_recency` are weighted 0 because artefacts are not expected, and a
        # 0% bar labelled "Depth of proven work" reads as "this person has none" —
        # a judgement the score deliberately is not making.
        "components": _weighted_components(
            (match[2] if match else {}) or {}, match[4] if match else None
        ),
        "gates": gates,
        "seniority_basis": gates.get("seniority_basis", "unknown"),
        "rationale": match[5] if match else None,
        "proven": proven,
        "says_so": says_so,
        "links": [{"kind": k, "value": v} for k, v in links],
        "previously_seen": [
            {"role_title": t, "action": a, "note": n, "when": w} for t, a, n, w in previously
        ],
        "experience_floor": _experience_floor(candidate_id),
    }


def _experience_floor(candidate_id: uuid.UUID) -> int | None:
    """Years of *public work*, shown as a floor ("5+ years"), never as a precise age."""
    with pool.connection() as conn:
        row = conn.execute(
            "select min(observed_at) from evidence "
            "where candidate_id = %s and tier = 'artifact_backed'",
            (candidate_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    years = (datetime.now(timezone.utc) - row[0]).days / 365.25
    return int(years) if years >= 1 else None


def record_action(match_id: int, action: str, *, note: str | None = None, actor: str | None = None) -> None:
    if action not in {"shortlisted", "rejected", "contacted", "needs_info"}:
        raise ValueError(f"unknown action {action!r}")
    with pool.connection() as conn:
        conn.execute(
            "insert into recruiter_action (match_id, action, note, actor) values (%s, %s, %s, %s)",
            (match_id, action, note, actor),
        )


def roles_overview() -> list[dict]:
    """Screen 1. One row per role with the counts a recruiter cares about."""
    with pool.connection() as conn:
        cur = conn.execute(
            "select r.id, r.title, r.status, r.created_at,"
            "  (select count(*) from candidate_ref cr where cr.role_id = r.id) as found,"
            "  (select count(*) from candidate_ref cr where cr.role_id = r.id and cr.gate_state = 'passed') as kept,"
            "  (select count(*) from match m where m.role_id = r.id) as ranked,"
            "  (select count(*) from recruiter_action a join match m on m.id = a.match_id"
            "    where m.role_id = r.id and a.action = 'shortlisted') as shortlisted,"
            "  (select max(run_at) from role_query q where q.role_id = r.id) as last_run "
            "from role r order by r.created_at desc"
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def role_progress(role_id: uuid.UUID) -> dict:
    """Numbers for the "Searched 140 profiles, found 22 so far" line.

    Never a spinner with no number — a recruiter watching a blank screen closes the tab.
    """
    with pool.connection() as conn:
        found, kept = conn.execute(
            "select count(*), count(*) filter (where gate_state = 'passed') "
            "from candidate_ref where role_id = %s",
            (role_id,),
        ).fetchone()
        # Only people we have actually read. A match row exists the moment a candidate
        # is discovered, so counting rows made the banner say "11 ranked" directly above
        # a section reading "nothing to rank" (2026-08-31). Ranking someone nobody has
        # opened is the same false claim the three-bucket split exists to prevent.
        ranked = conn.execute(
            "select count(*) from match m where m.role_id = %s and exists "
            "(select 1 from evidence e where e.candidate_id = m.candidate_id)",
            (role_id,),
        ).fetchone()[0]
        pending, stalled, doing, since = conn.execute(
            "select count(*), count(*) filter (where "
            "  state = 'queued' and attempts = 0 and created_at < now() - interval '90 seconds'), "
            # What it is doing right now, and for how long. A recruiter watching a page
            # that only says "Searching" cannot tell working from hung; a stage name and
            # a number that moves is the difference (2026-09-18).
            # Prefer what is actually running; fall back to what is queued, so a job
            # waiting its turn still names its stage instead of reading "Starting up".
            "  coalesce(min(kind) filter (where state = 'running'), min(kind)), "
            "  extract(epoch from now() - min(created_at))::int "
            "from job where state in ('queued', 'running') "
            "and payload_json->>'role_id' = %s",
            (str(role_id),),
        ).fetchone()
    # `stalled` is the honest answer to "why has this said Searching for ten minutes".
    # A queued job nobody has even attempted means no worker process is running — the
    # search is not slow, it has not started. Ten live minutes were lost to this on
    # 2026-08-31 with the page cheerfully reporting progress the whole time.
    return {
        "found": found,
        "kept": kept,
        "ranked": ranked,
        "running": pending > 0,
        "stalled": stalled > 0,
        "searching": doing == "discover",  # shows the Stop button: only SERP spends credits
        "doing": {"discover": "Searching LinkedIn", "enrich": "Reading profiles",
                  "score": "Ranking"}.get(doing, "Starting up"),
        "waiting_secs": since or 0,
    }


def enrichment_degraded(role_id: uuid.UUID) -> bool:
    """True when nobody on this shortlist has real employment history behind them.

    Drives the banner that explains why years of experience are estimated. Asked of the
    data rather than of a config flag, because the honest answer varies per role: a role
    enriched last week is not degraded, and one discovered this morning is.
    """
    with pool.connection() as conn:
        computed = conn.execute(
            "select 1 from match m join evidence e on e.candidate_id = m.candidate_id "
            "where m.role_id = %s and e.claim_type = 'experience_years' limit 1",
            (role_id,),
        ).fetchone()
    return computed is None


def spec_of(role_id: uuid.UUID) -> RoleSpec | None:
    from hi.discovery import get_role

    role = get_role(role_id)
    return role[1] if role else None
