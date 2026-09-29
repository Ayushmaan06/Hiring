"""Delete the `employer` and `title` rows the pre-@2 experience parser got wrong.

IMPLEMENTATION.md §2.1a. The parser fix stops *new* wrong rows; it cannot repair the
ones already written, because the evidence key is
`(candidate_id, claim_type, claim_key, source_url)` and the corrected parse produces a
different `claim_key`. Re-enriching therefore *adds* the right row and leaves the wrong
one beside it — so a card would show both "Employer: Data N Stats" and
"Employer: Full-time" for the same job.

**No heuristics.** An earlier draft of this script guessed from the stored values alone
and got two things wrong in opposite directions: it wanted to delete
`employer = "Freelance"`, which is correct data (those profiles really do name Freelance
as the organisation, on a `Freelance · Part-time` company line), and it missed
`employer = "Senior Software Engineer"`, which was the row that started all of this.

So it does not guess. Mode C cached the full page text of every profile it read, and the
fixed parser is right here: re-parse those pages, and delete only the stored rows whose
value does not appear in what the @2 parser actually produces. That is exact, it explains
itself in the output, and it needs no rules about what a company name looks like.

It touches no network and reads no LinkedIn page — only `var/cache` bodies already on
disk. Dry run by default.

    python scripts/prune_bad_employer_rows.py            # report only
    python scripts/prune_bad_employer_rows.py --apply

A deleted row leaves that job with no employer until the profile is read again:

    python -m hi.adapters.linkedin_profile run --role <prefix> --redo
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hi.adapters.linkedin_profile import ADAPTER, experience_entries  # noqa: E402
from hi.db import pool  # noqa: E402


def cached_bodies(candidate_id) -> list[str]:
    """Every cached page text this adapter stored for this candidate."""
    with pool.connection() as conn:
        paths = conn.execute(
            'select distinct f.body_path from "fetch" f '
            "join evidence e on e.fetch_id = f.id "
            "where e.candidate_id = %s and f.adapter = %s and f.body_path is not null",
            (candidate_id, ADAPTER),
        ).fetchall()

    texts = []
    for (path,) in paths:
        file = Path(path)
        if file.exists():
            texts.append(file.read_text(encoding="utf-8", errors="replace"))
    return texts


def experience_section(text: str) -> str | None:
    """The page text from its "Experience" heading onwards, or None if it has none.

    Necessary because a *whole* profile page is not what the parser is fed in
    production — `enrich()` gives it the `details/experience/` sub-page. On a full
    profile capture, `_significant_lines` stops at the first "people also viewed"
    marker, which on these pages appears in a sidebar rendered *before* the experience
    section, so the parse returned zero roles and this script would have concluded that
    every stored row was stale and deleted all of them.
    """
    for index, line in enumerate(text.splitlines()):
        if line.strip() == "Experience":
            return "\n".join(text.splitlines()[index:])
    return None


def parser_would_produce(texts: list[str]) -> tuple[set[str], set[str]]:
    """(titles, employers) the @2 parser produces across every cached page, lowercased.

    A union across pages: a profile is read as several sub-pages and the same role can
    appear on more than one, so anything any page supports is legitimate.
    """
    titles: set[str] = set()
    employers: set[str] = set()
    for text in texts:
        section = experience_section(text)
        if section is None:
            continue
        for title, employer, _from, _to in experience_entries(section):
            titles.add(title.strip().lower())
            if employer:
                employers.add(employer.strip().lower())
    return titles, employers


def find_stale_rows() -> tuple[list[tuple], list[str]]:
    """(rows to delete, warnings). A row is stale if the @2 parser does not produce it."""
    with pool.connection() as conn:
        candidates = conn.execute(
            "select distinct e.candidate_id, c.display_name "
            "from evidence e join candidate c on c.id = e.candidate_id "
            "where e.extractor = %s and e.claim_type in ('employer', 'title') "
            "order by c.display_name",
            (ADAPTER,),
        ).fetchall()

    stale: list[tuple] = []
    warnings: list[str] = []

    for candidate_id, name in candidates:
        texts = cached_bodies(candidate_id)
        if not texts:
            warnings.append(f"{name}: no cached page on disk — skipped, nothing verified")
            continue

        titles, employers = parser_would_produce(texts)
        if not titles and not employers:
            warnings.append(f"{name}: cached pages parse to zero roles — skipped, not pruned")
            continue

        with pool.connection() as conn:
            rows = conn.execute(
                "select id, claim_type, claim_value from evidence "
                "where candidate_id = %s and extractor = %s "
                "and claim_type in ('employer', 'title') order by claim_type",
                (candidate_id, ADAPTER),
            ).fetchall()

        for row_id, claim_type, value in rows:
            value = (value or "").strip()
            if not value:
                continue
            expected = employers if claim_type == "employer" else titles
            if value.lower() not in expected:
                stale.append((row_id, name, claim_type, value))

    return stale, warnings


def main() -> None:
    apply = "--apply" in sys.argv
    stale, warnings = find_stale_rows()

    for warning in warnings:
        print(f"  ! {warning}")
    if warnings:
        print()

    if not stale:
        print("nothing to prune — every stored employer and title matches the @2 parser")
        return

    print(f"{len(stale)} row(s) the @2 parser does not produce from the same pages:\n")
    for _row_id, name, claim_type, value in stale:
        print(f"  {name:24} {claim_type:9} {value!r}")

    if not apply:
        print("\ndry run. re-run with --apply to delete these rows.")
        return

    with pool.connection() as conn:
        conn.execute("delete from evidence where id = any(%s)", ([r[0] for r in stale],))
    print(f"\ndeleted {len(stale)} row(s).")
    print("those jobs now have no employer until the profiles are read again:")
    print("  python -m hi.adapters.linkedin_profile run --role <prefix> --redo")


if __name__ == "__main__":
    main()
