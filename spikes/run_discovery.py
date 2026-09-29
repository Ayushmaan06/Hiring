"""Run or inspect a role's discovery.

    # spend SERP budget: 2 queries x 3 pages = up to 6 searches
    python spikes/run_discovery.py --pages 3

    # spend nothing: read the people already stored for a role
    python spikes/run_discovery.py --role-id <uuid> --saved
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections import Counter

from hi import discovery
from hi.adapters import linkedin_serp as ls
from hi.db import pool
from hi.models import RoleSpec, Seniority

SPEC = RoleSpec(
    titles=["Backend Engineer"],
    must_have_skills=["Python"],
    nice_to_have_skills=["PostgreSQL"],
    locations=["IN-KA-BLR"],
    seniority=Seniority(min_years=5, max_years=9),
    remote="hybrid",
)


def show(role_id: uuid.UUID) -> None:
    counts = discovery.role_counts(role_id)
    print(f"\nstored for role {role_id}: {counts}")

    passed = discovery.saved_refs(role_id, gate_state="passed")
    print(f"\n--- {len(passed)} SHORTLISTABLE (read from DB, no search) ---")
    for row in passed[:15]:
        print(f"  {row['snippet_name']}")
        print(f"    {row['snippet_headline']}  |  {row['snippet_location']}")
        print(f"    https://{row['ref_value']}")

    failed = discovery.saved_refs(role_id, gate_state="failed")
    kinds = Counter(r["gate_reason"].split(":")[0] for r in failed)
    print(f"\n--- {len(failed)} RULED OUT, by kind: {dict(kinds)} ---")
    for row in failed[:6]:
        print(f"  {row['snippet_name']} -> {row['gate_reason'][:110]}")


async def run(role_id: uuid.UUID, pages: int, max_queries: int | None) -> None:
    queries = ls.plan(SPEC)
    n = len(queries) if max_queries is None else min(max_queries, len(queries))
    print(f"planned {len(queries)} queries, running {n} x {pages} page(s)")
    print(f"budget: up to {n * pages} SERP searches (cached are free)\n")
    for q in queries[:n]:
        print(f"  Q: {q.query_text}")

    result = await discovery.run_discovery(role_id, SPEC, pages=pages, max_queries=max_queries)
    print(f"\nrun result: {result}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role-id")
    parser.add_argument("--saved", action="store_true", help="read stored refs only, spend nothing")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--max-queries", type=int, default=None)
    args = parser.parse_args()

    if args.role_id:
        role_id = uuid.UUID(args.role_id)
    else:
        role_id = discovery.create_role("Backend Engineer (Bangalore)", SPEC)
        print(f"created role {role_id}")

    if not args.saved:
        asyncio.run(run(role_id, args.pages, args.max_queries))
    show(role_id)
    print(f"\nreuse without searching:  python spikes/run_discovery.py --role-id {role_id} --saved")


if __name__ == "__main__":
    try:
        main()
    finally:
        pool.close()
