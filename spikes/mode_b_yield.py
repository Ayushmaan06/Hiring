"""Mode B yield spike — IMPLEMENTATION.md 1.4a. Throwaway: not shipped, not tested, delete after use.

Fetches a list of LinkedIn profile URLs *logged out* through the real fetcher.py (so it gets the
real rate limit / robots / policy gate) and reports the one number the architecture forks on: the
share of profiles that render dated employment history.

Requires a source_policy row for the profile URLs' domain (normally linkedin.com), enabled by a
named human decision — see README / this session's chat for the sign-off requirement. This script
does not enable it for you.

Usage:
    python spikes/mode_b_yield.py spikes/mode_b_urls.txt
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from hi.fetcher import FetchFailed, PolicyDenied, RobotsDenied, fetch

DATE_RANGE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\.?\s+\d{4}\s*"
    r"[-–—]\s*(Present|[A-Z][a-z]{2}\w*\.?\s+\d{4})",
    re.IGNORECASE,
)
AUTHWALL_MARKERS = ("Join LinkedIn", "authwall", "Sign in to see")


def classify(text: str | None) -> dict:
    if not text:
        return {"rendered": False, "authwalled": False, "has_experience": False, "dated_entries": 0}

    authwalled = any(marker.lower() in text.lower() for marker in AUTHWALL_MARKERS)
    rendered = len(text) > 5000 and not authwalled
    has_experience = "experience" in text.lower()
    dated_entries = len(DATE_RANGE_RE.findall(text))

    return {
        "rendered": rendered,
        "authwalled": authwalled,
        "has_experience": has_experience,
        "dated_entries": dated_entries,
    }


async def run(urls: list[str]) -> list[dict]:
    results = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}", file=sys.stderr)
        row = {"url": url}
        try:
            fetched = await fetch(url, adapter="mode_b_yield_spike", max_age_hours=0)
            row["status"] = fetched.status
            row.update(classify(fetched.text))
        except PolicyDenied as exc:
            row["error"] = f"PolicyDenied: {exc}"
        except RobotsDenied as exc:
            row["error"] = f"RobotsDenied: {exc}"
            row["stop_condition"] = True
        except FetchFailed as exc:
            row["error"] = f"FetchFailed: {exc}"
        results.append(row)
        if row.get("stop_condition"):
            print("STOP: robots/challenge encountered — recording and halting per 1.4a.", file=sys.stderr)
            break
    return results


def summarise(results: list[dict]) -> None:
    total = len(results)
    rendered = sum(1 for r in results if r.get("rendered"))
    dated = sum(1 for r in results if r.get("dated_entries", 0) > 0)
    authwalled = sum(1 for r in results if r.get("authwalled"))
    errors = sum(1 for r in results if "error" in r)

    print("\n--- mode B yield spike ---")
    print(f"attempted:            {total}")
    print(f"rendered a profile:   {rendered}")
    print(f"authwalled:           {authwalled}")
    print(f"errors:               {errors}")
    print(f"WITH DATED EMPLOYMENT HISTORY: {dated}/{total} ({100 * dated / total:.0f}%)" if total else "no urls")
    print("\nThis number goes into docs/PRD.md §13.1 with a go/no-go on mode C recorded alongside it.")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python spikes/mode_b_yield.py <urls-file>")
        raise SystemExit(1)

    urls = [line.strip() for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
    results = asyncio.run(run(urls))

    out_path = Path("spikes/mode_b_yield_results.json")
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nper-profile results written to {out_path}")

    summarise(results)


if __name__ == "__main__":
    main()
