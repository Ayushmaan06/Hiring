"""Re-record a live SERP response and check it still parses.

ARCHITECTURE.md §9a.3.4 — this is the FIRST diagnostic when discovery stops returning
people. A committed fixture keeps passing forever even after the real shape changes,
so the fixture alone cannot tell you anything is wrong. This can.

    python scripts/refresh_fixture.py            # check only
    python scripts/refresh_fixture.py --write    # also overwrite the committed fixture

Exit code 0 = live data still parses. Exit code 1 = it does not, and the error tells
you what changed. The fix is almost always one line in
`src/hi/adapters/linkedin_selectors.py`.

Costs one SERP search per run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hi.adapters import linkedin_selectors as selectors  # noqa: E402
from hi.adapters import linkedin_serp  # noqa: E402
from hi.db import pool  # noqa: E402
from hi.models import Query  # noqa: E402

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests" / "fixtures" / "linkedin_serp" / "bangalore_backend.json"
)

CANARY_QUERY = 'site:linkedin.com/in/ "Backend Engineer" (Bengaluru OR Bangalore)'


def check(payload: dict) -> list[str]:
    """Return a list of problems. Empty means the live shape still works."""
    problems: list[str] = []

    results = payload.get(selectors.SERP_RESULTS_KEY) or []
    if not results:
        problems.append(
            "No 'organic_results' in the response. Either the query returned nothing, "
            "the API key is out of quota, or the response shape changed."
        )
        return problems

    refs = linkedin_serp.refs_from_serpapi(payload, query_text=CANARY_QUERY)
    if not refs:
        problems.append(
            f"{len(results)} results came back but none parsed into a person. "
            "Check normalise_linkedin_url() and the URL shapes in the response."
        )
        return problems

    named = [r for r in refs if r.snippet_name]
    if not named:
        problems.append("No result yielded a name — check parse_title() against the 'title' field.")

    structured = [r for r in refs if r.snippet_title or r.snippet_employer]
    if not structured:
        problems.append(
            "No result yielded a current title/employer — 'rich_snippet.top.extensions' "
            "has probably changed shape. See linkedin_selectors.SERP_RICH_EXTENSIONS."
        )

    located = [r for r in refs if r.snippet_location]
    if not located:
        problems.append("No result yielded a location — check parse_extensions().")

    print(f"parsed {len(refs)} people from {len(results)} results")
    print(f"  with a name:              {len(named)}")
    print(f"  with a title or employer: {len(structured)}")
    print(f"  with a location:          {len(located)}")
    return problems


async def main(write: bool) -> int:
    print(f"querying live: {CANARY_QUERY}\n")
    try:
        refs = await linkedin_serp.discover(Query(adapter=linkedin_serp.ADAPTER, query_text=CANARY_QUERY))
    except linkedin_serp.SerpApiKeyMissing as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    # discover() already parsed; re-read the cached body for the raw payload.
    with pool.connection() as conn:
        row = conn.execute(
            "select body_path from \"fetch\" where adapter = %s order by fetched_at desc limit 1",
            (linkedin_serp.ADAPTER,),
        ).fetchone()
    if row is None:
        print("FAIL: nothing was fetched — is serpapi.com enabled in source_policy?", file=sys.stderr)
        return 1

    payload = json.loads(Path(row[0]).read_text(encoding="utf-8"))
    problems = check(payload)

    if problems:
        print("\nFAIL — live data no longer parses the way we expect:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\nFix: src/hi/adapters/linkedin_selectors.py, then re-run this script.",
            file=sys.stderr,
        )
        return 1

    print(f"\nOK — live data still parses ({len(refs)} people).")
    if write:
        FIXTURE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"rewrote {FIXTURE}")
        print("Review the diff before committing — it contains real people's public data.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="overwrite the committed fixture")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(args.write)))
    finally:
        pool.close()
