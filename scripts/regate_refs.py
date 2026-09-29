"""Re-run the snippet gate over refs already in the database.

A gate fix changes what *future* searches keep, and nothing else: the verdict is stored
on `candidate_ref` at discovery time. So after tightening the gate, every role searched
before the fix still shows the people it should have refused — and re-running discovery
to correct that spends SERP searches on results we already have.

This re-reads each stored `snippet_raw` (the verbatim SERP payload, the only lossless
source — see `discovery.ref_from_row`) and rewrites the verdict.

    python scripts/regate_refs.py                # every role, dry run
    python scripts/regate_refs.py --role e6c2de35
    python scripts/regate_refs.py --role e6c2de35 --apply

It only ever changes `gate_state` and `gate_reason`. Evidence, candidates and matches
are untouched: a ref that flips to `failed` keeps whatever was already learned about that
person, because `ARCHITECTURE.md` §2.5 says candidate data is never deleted. Re-score the
role afterwards so the page stops listing them.
"""

from __future__ import annotations

import sys
import uuid

from hi import discovery, matching
from hi.db import pool


def regate(role_id: uuid.UUID, *, apply: bool) -> list[tuple]:
    """The work itself lives in `discovery.regate` — the "find more people" button on
    the role page runs the same pass, and two copies of a gate decision is how the two
    drift."""
    spec = matching.spec_of(role_id)
    if spec is None:
        sys.exit(f"no spec for role {role_id}")
    return discovery.regate(role_id, spec, apply=apply)


def main() -> None:
    args = sys.argv[1:]
    apply = "--apply" in args
    wanted = args[args.index("--role") + 1] if "--role" in args else None

    with pool.connection() as conn:
        roles = conn.execute("select id, title from role order by created_at").fetchall()
    if wanted:
        roles = [r for r in roles if str(r[0]).startswith(wanted.strip())]
        if not roles:
            sys.exit(f"no role starts with {wanted!r}")

    total = 0
    for role_id, title in roles:
        changes = regate(role_id, apply=apply)
        total += len(changes)
        if not changes:
            continue
        print(f"\n{str(role_id)[:8]}  {title}")
        for name, was, now, reason in changes:
            # LinkedIn text is not cp1252-safe and Windows consoles die on it.
            line = f"  {name or '?':32} {was:6} -> {now:6}  {reason or ''}"
            print(line.encode("ascii", "replace").decode())

    if not total:
        print("no verdicts change.")
    elif apply:
        print(f"\n{total} verdicts rewritten. Re-score each role so the page catches up.")
    else:
        print(f"\n{total} verdicts would change. Re-run with --apply to write them.")


if __name__ == "__main__":
    main()
