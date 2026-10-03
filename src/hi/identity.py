"""Strong-key identity resolution (IMPLEMENTATION.md 1.7, ARCHITECTURE.md §4.2).

The rule that shapes this whole module: **a false merge is worse than a duplicate.**
A duplicate is visible and annoying; a false merge silently attributes one person's
work to another and is very hard to detect afterwards. So:

- Exactly one candidate matches a strong key -> attach.
- None match -> create.
- More than one matches -> attach to NEITHER, open an `identity_review`, keep both.
- Weak signals (same name, same city, same avatar) only ever *propose*. There is no
  code path from a display name to a merge.

Every merge records who decided it and exactly which rows moved, so it can be undone.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from hi.db import pool

# Key kinds that identify exactly one human. Anything not in here cannot drive a
# merge, no matter how suggestive it looks.
STRONG_KEY_KINDS = frozenset(
    {
        "github_login",
        "gitlab_login",
        "hf_user",
        "codeforces_handle",
        "so_user_id",
        "personal_domain",
        "email_sha256",
        "linkedin_slug",
    }
)


@dataclass
class ResolveResult:
    candidate_id: uuid.UUID | None
    created: bool = False
    matched: list[uuid.UUID] = field(default_factory=list)
    review_id: int | None = None

    @property
    def ambiguous(self) -> bool:
        return self.candidate_id is None and len(self.matched) > 1


def _clean(strong_keys: dict[str, str]) -> dict[str, str]:
    return {
        kind: value.strip()
        for kind, value in strong_keys.items()
        if kind in STRONG_KEY_KINDS and value and value.strip()
    }


def resolve(
    strong_keys: dict[str, str],
    *,
    display_name: str | None = None,
    location_text: str | None = None,
) -> ResolveResult:
    """Attach to an existing candidate, create one, or refuse on ambiguity.

    `display_name` and `location_text` are stored on a newly created candidate but are
    never used to match — they are weak signals.
    """
    keys = _clean(strong_keys)
    if not keys:
        return ResolveResult(candidate_id=None)

    with pool.connection() as conn:
        rows = conn.execute(
            "select distinct candidate_id from identity where (kind, value) in "
            "(select * from unnest(%s::text[], %s::text[]))",
            (list(keys), [keys[k] for k in keys]),
        ).fetchall()
        matched = [r[0] for r in rows]

        if len(matched) > 1:
            # Two candidates each hold a different strong key that now appear to be one
            # person. That is exactly the case that must never auto-merge.
            review_id = conn.execute(
                "insert into identity_review (candidate_a, candidate_b, reason, evidence_json) "
                "values (%s, %s, %s, %s) returning id",
                (
                    matched[0],
                    matched[1],
                    "multiple candidates hold strong keys from the same source",
                    json.dumps({"strong_keys": keys, "all_matched": [str(m) for m in matched]}),
                ),
            ).fetchone()[0]
            return ResolveResult(candidate_id=None, matched=matched, review_id=review_id)

        if len(matched) == 1:
            candidate_id, created = matched[0], False
        else:
            candidate_id = conn.execute(
                "insert into candidate (id, display_name, primary_location_text) "
                "values (%s, %s, %s) returning id",
                (uuid.uuid4(), display_name, location_text),
            ).fetchone()[0]
            created = True

        # Attach any keys this candidate does not already hold. A key owned by a
        # different candidate would have shown up as a match above, so this cannot
        # steal one.
        for kind, value in keys.items():
            conn.execute(
                "insert into identity (candidate_id, kind, value, first_seen, last_seen) "
                "values (%s, %s, %s, now(), now()) "
                "on conflict (kind, value) do update set last_seen = now()",
                (candidate_id, kind, value),
            )

        # A ref is per role, the person is not. Without this, someone read for one role
        # stayed "next run" on every other role that found them, and a second read would
        # spend the budget to learn nothing.
        if slug := keys.get("linkedin_slug"):
            conn.execute(
                "update candidate_ref set candidate_id = %s where candidate_id is null "
                "and ref_kind = 'linkedin_url' and ref_value = %s",
                (candidate_id, f"linkedin.com/in/{slug.lower()}"),
            )

    return ResolveResult(candidate_id=candidate_id, created=created, matched=matched)


def strong_keys_for(candidate_id: uuid.UUID) -> dict[str, str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "select kind, value from identity where candidate_id = %s", (candidate_id,)
        ).fetchall()
    return {kind: value for kind, value in rows}


def propose_merge(a: uuid.UUID, b: uuid.UUID, reason: str, evidence: dict) -> int:
    """Record a suspicion for a human. Never merges anything by itself."""
    with pool.connection() as conn:
        return conn.execute(
            "insert into identity_review (candidate_a, candidate_b, reason, evidence_json) "
            "values (%s, %s, %s, %s) returning id",
            (a, b, reason, json.dumps(evidence)),
        ).fetchone()[0]


def merge(
    from_candidate: uuid.UUID,
    into_candidate: uuid.UUID,
    *,
    decided_by: str,
    review_id: int | None = None,
    reason: str | None = None,
) -> int:
    """Move rows from one candidate to another. Requires a named decider.

    Rows that would collide with an existing claim on the target stay behind on the
    archived candidate rather than being deleted — that keeps the merge reversible.
    """
    if not decided_by or not decided_by.strip():
        raise ValueError("a merge requires decided_by — see IMPLEMENTATION.md 1.7")
    if from_candidate == into_candidate:
        raise ValueError("cannot merge a candidate into itself")

    with pool.connection() as conn:
        moved_identities = [
            r[0]
            for r in conn.execute(
                "update identity set candidate_id = %s, last_seen = now() "
                "where candidate_id = %s returning id",
                (into_candidate, from_candidate),
            ).fetchall()
        ]

        # Only non-colliding evidence moves; the unique index defines the collision.
        moved_evidence = [
            r[0]
            for r in conn.execute(
                "update evidence e set candidate_id = %s "
                "where e.candidate_id = %s and not exists ("
                "  select 1 from evidence t where t.candidate_id = %s"
                "    and t.claim_type = e.claim_type"
                "    and t.claim_key is not distinct from e.claim_key"
                "    and t.source_url = e.source_url"
                ") returning e.id",
                (into_candidate, from_candidate, into_candidate),
            ).fetchall()
        ]

        conn.execute(
            "update candidate set status = 'archived', updated_at = now() where id = %s",
            (from_candidate,),
        )
        merge_id = conn.execute(
            "insert into identity_merge "
            "(from_candidate_id, into_candidate_id, moved_identity_ids, moved_evidence_ids, "
            " review_id, reason, decided_by) "
            "values (%s, %s, %s, %s, %s, %s, %s) returning id",
            (
                from_candidate, into_candidate, moved_identities, moved_evidence,
                review_id, reason, decided_by,
            ),
        ).fetchone()[0]

        if review_id is not None:
            conn.execute(
                "update identity_review set status = 'merged', decided_by = %s, decided_at = now() "
                "where id = %s",
                (decided_by, review_id),
            )
    return merge_id


def unmerge(merge_id: int, *, reversed_by: str) -> None:
    """Restore the exact prior state of a merge."""
    if not reversed_by or not reversed_by.strip():
        raise ValueError("an unmerge requires reversed_by")

    with pool.connection() as conn:
        row = conn.execute(
            "select from_candidate_id, moved_identity_ids, moved_evidence_ids, reversed_at "
            "from identity_merge where id = %s",
            (merge_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no such merge: {merge_id}")
        from_candidate, identity_ids, evidence_ids, reversed_at = row
        if reversed_at is not None:
            raise ValueError(f"merge {merge_id} is already reversed")

        if identity_ids:
            conn.execute(
                "update identity set candidate_id = %s where id = any(%s)",
                (from_candidate, list(identity_ids)),
            )
        if evidence_ids:
            conn.execute(
                "update evidence set candidate_id = %s where id = any(%s)",
                (from_candidate, list(evidence_ids)),
            )
        conn.execute(
            "update candidate set status = 'active', updated_at = now() where id = %s",
            (from_candidate,),
        )
        conn.execute(
            "update identity_merge set reversed_at = now(), reversed_by = %s where id = %s",
            (reversed_by, merge_id),
        )


def open_reviews() -> list[dict]:
    """The human queue. Nothing here has been merged."""
    with pool.connection() as conn:
        cur = conn.execute(
            "select id, candidate_a, candidate_b, reason, evidence_json, status "
            "from identity_review where status = 'open' order by id"
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
