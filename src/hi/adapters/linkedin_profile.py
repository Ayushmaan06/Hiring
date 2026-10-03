"""`linkedin_profile` — mode C enrichment (ARCHITECTURE.md §7.4, IMPLEMENTATION.md 1.4c).

The gap this closes: discovery (mode A, SERP) knows a person's profile URL and nothing
else. GitHub verification needs a *strong key* — a github.com link, a personal domain —
and those live inside the profile page. Without this adapter, a LinkedIn-discovered
person has no artefact evidence and cannot be scored at all.

**How it reaches the page.** It attaches over CDP to a browser the operator has already
signed into, and drives that. It never launches a browser, never logs in, never reads a
credential, and there is no code path here that could: `linkedin_scraper/core/auth.py`
is deliberately not imported. `CLAUDE.md` bans stored credentials and this is what the
ban looks like in practice.

**The limits are in this file, not in config**, because ARCHITECTURE.md §7.4 requires
that they cannot be tuned away by editing a row:

    at most 30 profiles per day · one at a time · at least 20s + jitter between them
    · never headless · abort and trip the kill switch on the first wall

**On seeing a wall it stops and goes red.** A run that quietly records zero experiences
looks identical to a run that was blocked, and "silent success" is the characteristic
LinkedIn failure this project is built to notice.

**Education is stored and shown, never scored** (ARCHITECTURE.md §2.2, 2026-08-27). The
institution and degree become an `education` evidence row so a recruiter screening for
IIT/IIM can see it. `FEATURE_ALLOWLIST` is untouched, so no scoring component can read
it. Graduation dates are still never read: a graduation year dates a person, and age is
banned independently of that decision. `interests` is never fetched at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import random
import re
import socket
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from hi import canon, lock
from hi.adapters import github
from hi.adapters.linkedin_selectors import BLOCK_MARKERS
from hi.db import pool
from hi.extract import EvidenceRow, quarantine, write_evidence
from hi.fetcher import PolicyDenied, browser_gate, disable_source, record_browser_fetch

ADAPTER = "linkedin_profile"
# Bumped to @2 on 2026-08-28 with the experience-layout fix (IMPLEMENTATION.md §2.1a).
# The rule is the same one AGENTS.md §4 sets for prompts: a parser change that alters
# what a row says must change the version, or one table silently mixes the output of two
# different extractors and there is no way to tell which rows to distrust.
EXTRACTOR_VERSION = "linkedin_mode_c@2"

# --- Mode C limits. In code on purpose (§7.4). Do not move these to settings. ---
CDP_URL = "http://127.0.0.1:9222"
# Raised 15 -> 30 on 2026-08-27 at the tool owner's request (ARCHITECTURE.md §7.4).
# Still a hard ceiling in code: a run stops at it, it is not a target to reach.
MAX_PROFILES_PER_DAY = 30
MIN_DELAY_SECONDS = 20
JITTER_SECONDS = 10
# Between the sub-pages of ONE profile. Human click speed, not a burst.
SUBPAGE_DELAY_SECONDS = 2.0
SUBPAGE_JITTER_SECONDS = 3.0
CONCURRENCY = 1  # stated for the reader; enforced by the fact that the loop is serial

MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


class ModeCBlocked(RuntimeError):
    """A wall, a challenge, or a 999. Trips the kill switch — never retried in-run."""


class ModeCUnavailable(RuntimeError):
    """No browser to attach to. An operator problem, not a LinkedIn one."""


# --------------------------------------------------------------------------
# date handling — pure
# --------------------------------------------------------------------------


def parse_date(text: str | None) -> date | None:
    """'Jan 2020' / 'January 2020' / '2020' -> a date. 'Present' and junk -> None.

    Year-only is common and is read as January, which biases a span *longer* by up to
    eleven months. That is why `total_years` floors rather than rounds.
    """
    if not text:
        return None
    cleaned = text.strip().lower().replace(".", "")
    if not cleaned or cleaned in ("present", "current", "now"):
        return None
    if month_year := re.search(r"([a-z]{3,9})\s+(\d{4})", cleaned):
        if (month := MONTHS.get(month_year.group(1)[:3])) is not None:
            return date(int(month_year.group(2)), month, 1)
    if year := re.search(r"\b(?:19|20)\d{2}\b", cleaned):
        return date(int(year.group(0)), 1, 1)
    return None


def spans(experiences: list, *, today: date) -> list[tuple[date, date]]:
    """(start, end) per experience that has a usable start. Undated entries are dropped.

    An open end ('Present') closes at `today`. An entry with no parseable start is not
    guessed at — it is left out and the caller reports the count, because a silently
    dropped job is a silently wrong number of years.
    """
    out = []
    for exp in experiences:
        start = parse_date(getattr(exp, "from_date", None))
        if start is None or start > today:
            continue
        end = parse_date(getattr(exp, "to_date", None)) or today
        out.append((start, min(max(end, start), today)))
    return out


def total_years(intervals: list[tuple[date, date]]) -> float:
    """Union of the spans in years, so two overlapping jobs are not counted twice.

    Floors to one decimal: every input here is month-precision at best, so more
    precision than that would be a claim the source does not support.
    """
    if not intervals:
        return 0.0
    days, cursor = 0, None
    for start, end in sorted(intervals):
        if cursor is None or start > cursor:
            days += (end - start).days
            cursor = end
        elif end > cursor:
            days += (end - cursor).days
            cursor = end
    return int(days / 365.25 * 10) / 10


# --------------------------------------------------------------------------
# person -> evidence — pure, so it tests off a fixture
# --------------------------------------------------------------------------


def _present(value: str | None, page_text: str) -> str | None:
    """Return `value` only if it actually appears in the page. Anti-fabrication.

    The snippets below are composed from the scraper's structured fields rather than
    quoted from prose, so they take `write_evidence(source_text=None)` — the same route
    the GitHub adapter takes for API JSON. This restores the property that check would
    otherwise give us: nothing the parser invented, mis-joined, or carried over from a
    previous page can become a row.
    """
    if not value or not value.strip():
        return None
    return value.strip() if value.strip().lower() in page_text.lower() else None


def strong_keys_from(person, page_text: str) -> dict[str, str]:
    """Links on the profile that identify the same human elsewhere. The point of 1.4c.

    Only kinds `identity.STRONG_KEY_KINDS` accepts, and only from text on the page.
    """
    keys: dict[str, str] = {}
    haystack = " ".join(
        [page_text, person.about or ""]
        + [c.value or "" for c in getattr(person, "contacts", []) or []]
    )
    for url in re.findall(r"https?://[^\s\"'<>)\]]+", haystack):
        if login := github.login_from_url(url):
            keys.setdefault("github_login", login)
    for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", haystack):
        keys.setdefault("email_sha256", hashlib.sha256(email.strip().lower().encode()).hexdigest())
    return keys


def _observed(exp, today: date) -> datetime | None:
    end = parse_date(getattr(exp, "to_date", None)) or (
        today if parse_date(getattr(exp, "from_date", None)) else None
    )
    return datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc) if end else None


def rows_from_person(
    person,
    *,
    page_text: str,
    source_url: str,
    skill_lines: list[str] | None = None,
    fetch_id: int | None = None,
    today: date | None = None,
) -> tuple[list[EvidenceRow], int]:
    """(evidence rows, count of undated experiences). Never reads `person.educations`."""
    today = today or datetime.now(timezone.utc).date()
    rows: list[EvidenceRow] = []

    def row(**kwargs) -> EvidenceRow:
        return EvidenceRow(
            tier="self_reported",  # a profile is what a person says about themselves
            source_url=source_url,
            fetch_id=fetch_id,
            extractor=ADAPTER,
            extractor_version=EXTRACTOR_VERSION,
            **kwargs,
        )

    for exp in person.experiences or []:
        title = _present(exp.position_title, page_text)
        employer = _present(exp.institution_name, page_text)
        dates = " - ".join(d for d in (exp.from_date, exp.to_date) if _present(d, page_text))
        where = f" at {employer}" if employer else ""
        when = f" ({dates})" if dates else ""

        if title:
            rows.append(
                row(
                    claim_type="title",
                    claim_key=title.lower(),
                    claim_value=title,
                    snippet=f"{title}{where}{when} — listed on LinkedIn profile",
                    observed_at=_observed(exp, today),
                )
            )
        if employer:
            rows.append(
                row(
                    claim_type="employer",
                    claim_key=employer.lower(),
                    claim_value=employer,
                    snippet=f"{title or 'Role'}{where}{when} — listed on LinkedIn profile",
                    observed_at=_observed(exp, today),
                )
            )

    intervals = spans(person.experiences or [], today=today)
    undated = len(person.experiences or []) - len(intervals)
    if intervals:
        years = total_years(intervals)
        plural = "role" if len(intervals) == 1 else "roles"
        note = ""
        if undated:
            note = f"; {undated} undated {'entry' if undated == 1 else 'entries'} not counted"
        rows.append(
            row(
                claim_type="experience_years",
                claim_key="linkedin_experience_total",
                claim_value=f"{years} years",
                value_num=years,
                snippet=(
                    f"{years} years of dated employment across {len(intervals)} {plural} "
                    f"listed on LinkedIn{note}"
                ),
                observed_at=datetime.combine(
                    max(end for _, end in intervals), datetime.min.time(), tzinfo=timezone.utc
                ),
            )
        )

    # Skills, `self_reported`. This is what makes the product work with no GitHub at
    # all: `skill_match` reads any tier and multiplies by TIER_MULTIPLIER, so a listed
    # skill earns 0.35 of what a proven one earns. `skill_depth` still requires
    # artefacts and stays 0, which is correct — "lists Python" and "has 40 Python
    # repos" are different facts and the three tiers never merge (ARCHITECTURE.md §2).
    for line in skill_lines or []:
        canonical = canon.canonical_skill(line)
        if canonical is None:
            continue  # unknown: proposed for review by canon, never guessed into a skill
        if not _present(line, page_text):
            continue
        rows.append(
            row(
                claim_type="skill",
                claim_key=canonical,
                claim_value=canonical,
                snippet=f"{line} — listed in the Skills section of the LinkedIn profile",
            )
        )
        # A framework is evidence of the language it is written in. Without this, a
        # Django developer fails a `must_have: Python` gate, which is how 34 of 34
        # candidates were ruled out on 2026-08-27: LinkedIn Skills sections name
        # frameworks where GitHub reports languages. The snippet names the basis, so
        # a recruiter reading the card can see it is inferred from Django rather than
        # claimed directly, and it stays `self_reported` like its source.
        for implied in canon.implied_skills(canonical):
            rows.append(
                row(
                    claim_type="skill",
                    claim_key=implied,
                    claim_value=implied,
                    snippet=(
                        f"{line} — listed in the Skills section of the LinkedIn "
                        f"profile ({canonical} is written in {implied})"
                    ),
                )
            )

    # Education: stored and shown, never scored (ARCHITECTURE.md §2.2, 2026-08-27).
    # Dates are deliberately not read from `edu.from_date`/`to_date` — a graduation year
    # dates a person, and age is banned independently of the education decision. That is
    # also why `observed_at` is left to default to now() rather than the end of study:
    # putting the year in a timestamp column would re-encode exactly what we dropped.
    for edu in person.educations or []:
        institution = _present(edu.institution_name, page_text)
        if not institution:
            continue
        degree = _present(edu.degree, page_text)
        rows.append(
            row(
                claim_type="education",
                claim_key=institution,
                claim_value=degree or institution,
                snippet=(
                    f"{institution}{f' — {degree}' if degree else ''}"
                    " — listed on LinkedIn profile"
                ),
            )
        )

    if location := _present(person.location, page_text):
        rows.append(
            row(
                claim_type="location",
                claim_key=canon.region_of(location),
                claim_value=location,
                snippet=f"Location on LinkedIn profile: {location}",
            )
        )

    if person.open_to_work:
        rows.append(
            row(
                claim_type="availability",
                claim_value="open_to_work",
                snippet="LinkedIn profile is marked Open to work",
            )
        )

    for kind, value in strong_keys_from(person, page_text).items():
        if kind == "github_login":
            rows.append(
                row(
                    claim_type="link",
                    claim_key="github_login",
                    claim_value=value,
                    snippet=f"GitHub account linked from LinkedIn profile: {value}",
                )
            )
    return rows, undated


# --------------------------------------------------------------------------
# the browser — attach only, never launch
# --------------------------------------------------------------------------


def blocked_by(page_text: str) -> str | None:
    """The marker that means we were served a wall, or None."""
    lowered = page_text.lower()
    return next((m for m in BLOCK_MARKERS if m.lower() in lowered), None)


@contextlib.asynccontextmanager
async def attached_page():
    """Yield a page in the operator's already-running browser. Never launches one.

    `connect_over_cdp` can only attach to a browser that is already up, which is the
    property that makes 'never headless' and 'never a stored credential' true by
    construction rather than by promise: the session belongs to the human, and this
    process cannot create one.
    """
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser = None
    try:
        try:
            browser = await playwright.chromium.connect_over_cdp(CDP_URL, timeout=10_000)
        except Exception as exc:
            raise ModeCUnavailable(
                f"no browser listening on {CDP_URL}. Start Edge with "
                f"--remote-debugging-port=9222 and sign in to LinkedIn first "
                f"(RUNBOOK.md 'Run LinkedIn enrichment'). Underlying error: {exc}"
            ) from exc
        if not browser.contexts:
            raise ModeCUnavailable(f"attached to {CDP_URL} but it has no open window")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        yield page
    finally:
        # Close our client, never the operator's browser.
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        with contextlib.suppress(Exception):
            await playwright.stop()


# --------------------------------------------------------------------------
# parsing the details pages — text, not selectors, and pure
# --------------------------------------------------------------------------
#
# Verified against live LinkedIn on 2026-08-27: the profile pages ship
# **obfuscated, build-generated class names** (`cc605e5a`, `_4680641a`), the entries
# are not list items, and `main ul`, `.pvs-list__container` and
# `.pvs-list__paged-list-item` all match zero elements. Every selector in the
# vendored scraper therefore returns nothing, which is why a run reported five
# profiles enriched and wrote no evidence at all.
#
# Hashed class names change when LinkedIn rebuilds, so a selector fix would be a
# fix with a shelf life measured in weeks. The *rendered text* of these pages is
# regular and has been stable for years, so that is what we parse. It also means
# this adapter contributes almost nothing to `linkedin_selectors.py`, which is the
# best possible outcome for ARCHITECTURE.md §9a.3.3.

# "Aug 2025 - Present · 1 yr 1 mo", "Feb 2022 - Dec 2023", "2016 – 2020".
DATE_RANGE_LINE = re.compile(
    r"^(?P<from>[A-Za-z]{3,9}\.?\s+\d{4}|\d{4})\s*[-–—]\s*"
    r"(?P<to>Present|Current|[A-Za-z]{3,9}\.?\s+\d{4}|\d{4})\b"
)

# Where the person's own data ends and LinkedIn's furniture begins. Without this,
# the "More profiles for you" sidebar becomes somebody else's job history.
SECTION_END_MARKERS = (
    "more profiles for you",
    "people also viewed",
    "people you may know",
    "explore premium profiles",
)


# LinkedIn's employment-type vocabulary. These are metadata about a role, never the
# name of an employer, and telling the two apart is what §2.1a is about.
EMPLOYMENT_TYPES = frozenset(
    {
        "full-time", "part-time", "self-employed", "freelance", "contract",
        "internship", "apprenticeship", "seasonal", "temporary", "permanent",
    }
)

# A whole line that is only a tenure: "1 yr 7 mos", "2 yrs 3 mos", "6 mos".
DURATION_ONLY = re.compile(r"^\d+\s*(?:yrs?|mos?)(?:\s+\d+\s*(?:yrs?|mos?))?$", re.IGNORECASE)

# Lines that are page furniture rather than anybody's job history.
NOT_A_COMPANY_PREFIXES = ("●", "•", "-", "skills:", "… more", "...more", "show ")


def _dot_parts(line: str) -> list[str]:
    return [part.strip() for part in line.split("·")]


def _is_employment_type(line: str) -> bool:
    return line.strip().lower().replace(" ", "-") in EMPLOYMENT_TYPES


def _is_company_line(line: str) -> bool:
    """`Naviq · Full-time` — a company with its employment type appended.

    The employment type is what identifies the line. `Bengaluru, Karnataka, India ·
    Hybrid` has the same shape but ends in a work *mode*, and reading it as a company
    is how a location became an employer.
    """
    parts = _dot_parts(line)
    return len(parts) >= 2 and bool(parts[0]) and _is_employment_type(parts[-1])


def _is_duration_header(line: str) -> bool:
    """The second line of a promotion group: `1 yr 7 mos` or `Full-time · 2 yrs 3 mos`.

    Its presence is what marks the line *above* it as an employer named once for
    several roles. A dated entry line ("Jun 2021 - Jul 2022 · 1 yr 2 mos") also ends in
    a duration, hence the check that anything before the separator is an employment
    type rather than a date.
    """
    parts = _dot_parts(line)
    if not DURATION_ONLY.match(parts[-1]):
        return False
    return len(parts) == 1 or (len(parts) == 2 and _is_employment_type(parts[0]))


def _is_plausible_company(line: str) -> bool:
    lowered = line.lower()
    return bool(
        line
        and len(line) < 120
        and not DATE_RANGE_LINE.match(line)
        and not _is_duration_header(line)
        and not _is_employment_type(line)
        and not any(lowered.startswith(prefix) for prefix in NOT_A_COMPANY_PREFIXES)
    )


def _significant_lines(text: str) -> list[str]:
    """Non-blank lines of a details page, stopping where LinkedIn's furniture begins."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(marker in line.lower() for marker in SECTION_END_MARKERS):
            break
        lines.append(line)
    return lines


def experience_entries(text: str) -> list[tuple[str, str | None, str, str]]:
    """(title, employer, from_date, to_date) per dated role. IMPLEMENTATION.md §2.1a.

    The previous version took the two lines immediately above each date line as
    (title, employer). That is correct for the common layout and wrong for two others,
    and on 2026-08-28 four of the dev database's `employer` claims were consequences:
    `"Full-time"`, `"Internship"`, `"Senior Software Engineer"`, `"Python Developer"`.
    A recruiter reading "Employer: Full-time" on a card stops trusting the card.

    The three layouts LinkedIn actually ships, all four confirmed against cached pages:

        Lead Solution Engineer                <- one role, one company
        Naviq · Full-time                        the company line carries the type
        Jul 2026 - Present · 2 mos

        Data N Stats                          <- promotion group: the company is
        1 yr 7 mos                               named ONCE, above a tenure line
        New Delhi, Delhi, India · Remote
        Associate Software Engineer
        Full-time                             <- type on its own line, no company
        Jun 2021 - Jul 2022 · 1 yr 2 mos

        Hyreo                                 <- same group, and the inner roles
        Full-time · 2 yrs 3 mos                  carry nothing but a title
        Trivandrum, Kerala, India · On-site
        Senior Software Engineer
        Jul 2023 - Oct 2024 · 1 yr 4 mos

    So a duration line marks the line above it as the group's employer, and each dated
    entry is classified by what sits directly above it rather than by a fixed offset.
    The date line remains the anchor: an undated entry is not guessed at.

    Where no group is open the old two-line reading is kept exactly, so every layout
    that parsed correctly before still does — this widens the parser, it does not
    replace it.
    """
    lines = _significant_lines(text)
    group_employer: str | None = None
    out: list[tuple[str, str | None, str, str]] = []

    for index, line in enumerate(lines):
        if _is_duration_header(line) and index >= 1 and _is_plausible_company(lines[index - 1]):
            group_employer = _dot_parts(lines[index - 1])[0]
            continue

        match = DATE_RANGE_LINE.match(line)
        if match is None or index < 1:
            continue

        above = lines[index - 1]
        two_above = lines[index - 2] if index >= 2 else None

        if _is_employment_type(above):
            # "Associate Software Engineer / Full-time / <dates>" — no company line.
            title, employer = two_above, group_employer
        elif _is_company_line(above):
            title, employer = two_above, _dot_parts(above)[0]
            group_employer = None  # a standalone entry closes any open group
        elif group_employer:
            # An inner role of a group: a title and nothing else.
            title, employer = above, group_employer
        elif two_above:
            # No group open: the original reading, unchanged.
            title, employer = two_above, _dot_parts(above)[0]
        else:
            continue

        if not title or not _is_plausible_company(title):
            continue  # a bullet or a stray line is not a job title
        out.append((title, employer or None, match.group("from"), match.group("to")))

    return out


def _entries(text: str) -> list[tuple[str, str, str, str]]:
    """(first_line, second_line, from_date, to_date) per dated entry on a details page.

    The education layout — two lines, then a date range:

        Don Bosco Institute of Technology  <- institution
        Bachelor of Technology, ...        <- degree
        2016 – 2020

    Experience used to share this function and no longer does: its lines come in three
    arrangements and in the reverse order (title above company), so it has its own
    classifier in `experience_entries`. Education has no promotion groups and no
    employment types, so the fixed two-line reading is correct for it and left alone.

    The date line is the anchor: an entry without one is not guessed at.
    """
    lines = _significant_lines(text)

    out = []
    for index, line in enumerate(lines):
        match = DATE_RANGE_LINE.match(line)
        if match is None or index < 2:
            continue
        first = lines[index - 2].split("·")[0].strip()
        second = lines[index - 1].split("·")[0].strip()
        if not first or not second:
            continue
        out.append((first, second, match.group("from"), match.group("to")))
    return out


def parse_experience_text(text: str) -> list:
    """The `details/experience/` page text -> Experience models."""
    from linkedin_scraper.models import Experience

    return [
        Experience(
            position_title=title,
            institution_name=employer,
            from_date=from_date,
            to_date=None if to_date.lower() in ("present", "current") else to_date,
        )
        for title, employer, from_date, to_date in experience_entries(text)
    ]


def parse_education_text(text: str) -> list:
    """The `details/education/` page text -> Education models.

    Dates are matched only to locate the entry and are then thrown away: a graduation
    year dates a person, and age is banned (ARCHITECTURE.md §2.2).
    """
    from linkedin_scraper.models import Education

    return [
        Education(institution_name=institution, degree=degree)
        for institution, degree, _from, _to in _entries(text)
    ]


def parse_skills_text(text: str) -> list[str]:
    """The `details/skills/` page text -> candidate skill lines, in page order.

    No attempt is made to tell a skill line from its context line ("Back End Developer
    at ReelUp") by position, because the page interleaves them and a missing context
    line would shift everything. Instead every line is a candidate and `canon` decides:
    a line that maps to a known skill becomes evidence, a line that does not is
    proposed for review and never guessed at. That is the same rule the GitHub adapter
    applies to languages, and it is why no code here compares a raw skill string.

    Lines that are obviously not skills are dropped before `canon` ever sees them, for
    one specific reason: an unknown skill is *recorded as a proposal for a human to
    review*, so passing "Back End Developer at ReelUp" through would fill that review
    queue with rubbish and make it useless. The filter is deliberately crude — it only
    has to be right about what is not a skill.
    """
    skip_exact = {"skills", "all", "industry knowledge", "tools & technologies", "show all"}
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(marker in line.lower() for marker in SECTION_END_MARKERS):
            break
        lowered = line.lower()
        if lowered in skip_exact:
            continue
        if " at " in lowered:
            continue  # "<Title> at <Employer>" — the context line under each skill
        if "endorsement" in lowered or lowered.startswith(("passed ", "· ")):
            continue
        if len(line) > 60 or line[0].isdigit():
            continue
        if line not in out:
            out.append(line)
    return out


def profile_base(url: str) -> str:
    """Ensure a trailing slash. Without one, every sub-page request loses the person.

    The vendored scraper builds sub-pages with `urljoin(base, "details/patents/")`, and
    `urljoin` replaces the last path segment when the base does not end in a slash:

        urljoin("https://linkedin.com/in/ajith-b",  "details/patents/")
            -> "https://linkedin.com/in/details/patents/"   # a page about nobody
        urljoin("https://linkedin.com/in/ajith-b/", "details/patents/")
            -> "https://linkedin.com/in/ajith-b/details/patents/"

    Our `candidate_ref.source_url` has no trailing slash, so on 2026-08-27 every
    experience, education and accomplishment page was requested for a nonexistent
    profile. Those error pages contain "try again later", which is what the vendored
    rate-limit heuristic tripped on — the phantom block was this bug wearing a costume.
    """
    return url if url.endswith("/") else url + "/"


async def after_navigation(page) -> None:
    """Run after every page load the scraper performs: check for a wall, then pace.

    Substituted in for the vendored `check_rate_limit`, which is called on each
    navigation. Pacing here rather than only once per profile is the point: a profile
    now costs several page loads, and firing them back to back is exactly the signature
    mode C is supposed not to have. A few seconds is roughly how fast a person clicks
    through the sections of a profile they are reading; the long ≥20s gap stays between
    *people*, where it belongs.
    """
    await refuse_walls(page)
    await asyncio.sleep(SUBPAGE_DELAY_SECONDS + random.uniform(0, SUBPAGE_JITTER_SECONDS))


async def refuse_walls(page) -> None:
    """Raise ModeCBlocked if this page is a wall. Our detector, not the vendored one.

    `linkedin_scraper`'s `detect_rate_limit` flags any page whose text contains
    'rate limit', 'slow down', 'too many requests' or 'try again later'. That last
    phrase in particular appears on ordinary LinkedIn pages, and on 2026-08-27 it
    failed three healthy profiles in a row (verified: the profile pages contained none
    of those phrases; the trigger was one of the sub-pages this adapter no longer
    visits). A false wall is not harmless — it trips the kill switch and reads as a
    block. So we substitute this into the scraper instance and use `BLOCK_MARKERS`,
    which is the single-file contract for what a wall actually looks like.
    """
    if any(marker in page.url.lower() for marker in ("/checkpoint", "authwall")):
        raise ModeCBlocked(f"{page.url}: redirected to a challenge or authwall")
    if marker := blocked_by(await page.inner_text("body")):
        raise ModeCBlocked(f"{page.url}: served a wall ({marker!r})")


# A dropped connection is not a candidate with a thin profile. Keeping the two apart
# matters because the 1.4a yield is a ratio: on 2026-08-27 one ERR_CONNECTION_CLOSED
# turned a 100% yield into 67% while nothing was wrong with the profile at all.
TRANSIENT_MARKERS = (
    "net::err_",
    "timeout",
    "econnreset",
    "connection closed",
    "connection reset",
    "target closed",
)


def is_transient(exc: BaseException) -> bool:
    return any(marker in str(exc).lower() for marker in TRANSIENT_MARKERS)


class TransientFetchError(RuntimeError):
    """The network dropped. Not the person's fault and not a measurement."""


async def scrape_with_retry(page, url: str):
    """One retry on a transient network error, then give up and say which it was.

    Deliberately not the fetcher's 4-attempt backoff: this is a browser a human is
    watching, and hammering a reload is the opposite of the human pace mode C rests
    on. One retry, one full inter-profile delay apart.
    """
    for attempt in (1, 2):
        try:
            return await scrape_profile(page, url)
        except (ModeCBlocked, PolicyDenied):
            raise  # a wall or the cap — never retry either
        except Exception as exc:
            if not is_transient(exc):
                raise
            if attempt == 2:
                raise TransientFetchError(str(exc)) from exc
            await asyncio.sleep(MIN_DELAY_SECONDS + random.uniform(0, JITTER_SECONDS))
    raise AssertionError("unreachable")


async def scrape_profile(page, url: str):
    """Navigate and read one profile. Raises ModeCBlocked if we hit a wall.

    Reads three pages — the profile, `details/experience/`, `details/education/` — and
    parses their **text**. It calls none of the vendored getters: verified live on
    2026-08-27, every one of them returns empty against LinkedIn's current obfuscated
    markup, which is how a run reported five profiles enriched and zero evidence.

    Not fetched, deliberately:

    - `interests` — the one section that routinely reveals religion and politics, and
      nothing asks for it.
    - `contacts` (the contact-info overlay) — see the ponytail note below.
    - accomplishments — authorised on 2026-08-27 and dropped again the same day once
      it was clear their parser was as dead as the rest and their page has no date
      line to anchor a text parse on. Nothing consumed them (no scoring component
      reads `project`), so three page loads per person bought nothing.
    """
    from linkedin_scraper.core.exceptions import AuthenticationError
    from linkedin_scraper.models import Person
    from linkedin_scraper.scrapers.person import PersonScraper

    await browser_gate(url, adapter=ADAPTER)

    # The vendored scraper is still worth having for navigation, scrolling and the
    # login check — it is only its *parsing* that the markup change killed.
    scraper = PersonScraper(page)
    scraper.check_rate_limit = lambda: after_navigation(page)

    base = profile_base(url)
    pages_text: list[str] = []

    async def load(target: str) -> str:
        """Navigate, settle, and return the page's visible text."""
        await scraper.navigate_and_wait(target)
        await page.wait_for_selector("main", timeout=10_000)
        await scraper.wait_and_focus(1)
        await scraper.scroll_page_to_bottom(pause_time=0.5, max_scrolls=3)
        text = await page.inner_text("main")
        pages_text.append(text)
        return text

    await scraper.navigate_and_wait(base)
    try:
        await scraper.ensure_logged_in()
    except AuthenticationError as exc:
        # The session is gone or we were bounced to a wall. Either way, stop.
        raise ModeCBlocked(f"{url}: not signed in ({exc})") from exc
    await page.wait_for_selector("main", timeout=10_000)
    await scraper.scroll_page_to_bottom(pause_time=0.5, max_scrolls=3)
    profile_text = await page.inner_text("main")
    pages_text.append(profile_text)

    experiences = parse_experience_text(await load(base + "details/experience/"))
    educations = parse_education_text(await load(base + "details/education/"))
    skill_lines = parse_skills_text(await load(base + "details/skills/"))
    await refuse_walls(page)

    page_text = "\n".join(dict.fromkeys(pages_text))
    person = Person(
        linkedin_url=url,
        experiences=experiences,
        educations=educations,
        # Read from text rather than a selector: "#OpenToWork" and the Open-to-work
        # banner both render as visible text, and a stale selector silently returning
        # False is exactly the quiet wrong answer this adapter is built to avoid.
        open_to_work="open to work" in profile_text.lower(),
        # `name` and `location` are deliberately absent. Discovery already has both
        # from the SERP snippet, the location gate has already run on it, and neither
        # is worth a fragile parse to restate.
        #
        # ponytail: no `contacts`, so no contact-info overlay — often where a linked
        # GitHub or personal site lives, which is the strong key this adapter exists
        # to find. If `strong keys found` stays at zero across a run, add that one
        # sub-page and parse its text.
    )
    fetched = record_browser_fetch(url, adapter=ADAPTER, status=200, html=page_text)
    return person, skill_lines, page_text, fetched.fetch_id


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


@dataclass
class EnrichResult:
    attempted: int = 0
    enriched: int = 0
    evidence_written: int = 0
    with_dated_experience: int = 0
    undated_entries: int = 0
    strong_keys_found: int = 0
    failed: int = 0
    transient_failures: int = 0
    stopped_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def dated_yield(self) -> float:
        """The number IMPLEMENTATION.md 1.4a forks the architecture on."""
        return self.with_dated_experience / self.attempted if self.attempted else 0.0


def spent_today() -> int:
    """Profiles fetched in the last 24h. The per-account cap, counted from the audit trail."""
    with pool.connection() as conn:
        return conn.execute(
            'select count(*) from "fetch" where adapter = %s '
            "and fetched_at > now() - interval '24 hours'",
            (ADAPTER,),
        ).fetchone()[0]


async def enrich(
    targets: list[tuple[uuid.UUID | None, str]],
    *,
    limit: int | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> EnrichResult:
    """Enrich (candidate_id, profile_url) pairs, one run at a time across the machine.

    The advisory lock is here rather than in the CLI or the worker on purpose: this is the
    one function all three routes into mode C pass through, so concurrency 1 (§7.4) cannot
    be lost by adding a fourth caller that forgets it.
    """
    with lock.exclusive() as got:
        if not got:
            return EnrichResult(
                stopped_reason="another profile read is already running "
                "(mode C is one at a time, ARCHITECTURE.md §7.4)"
            )
        return await _enrich(targets, limit=limit, on_progress=on_progress)


async def _enrich(
    targets: list[tuple[uuid.UUID | None, str]],
    *,
    limit: int | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> EnrichResult:
    """Serial, paced, capped, self-disabling. Holds the mode C lock — call `enrich`.

    A `candidate_id` of None means "scrape but write nothing" — that is the 1.4a
    measurement, which needs the yield number without storing anyone.
    """
    result = EnrichResult()
    budget = MAX_PROFILES_PER_DAY - spent_today()
    if budget <= 0:
        result.stopped_reason = f"daily cap reached ({MAX_PROFILES_PER_DAY}/day, in code)"
        return result
    todo = targets[: min(budget, limit or budget)]
    if len(todo) < len(targets):
        result.notes.append(f"capped at {len(todo)} of {len(targets)} requested")

    async with attached_page() as page:
        for index, (candidate_id, url) in enumerate(todo):
            if on_progress is not None:
                # Before the sleep, not after: 20s of silence at the start of a profile
                # is exactly when a watcher needs to be told which one it is on.
                on_progress(index + 1, len(todo), url)
            if index:
                await asyncio.sleep(MIN_DELAY_SECONDS + random.uniform(0, JITTER_SECONDS))
            result.attempted += 1
            try:
                person, skill_lines, page_text, fetch_id = await scrape_with_retry(page, url)
            except ModeCBlocked as exc:
                disable_source("linkedin.com", str(exc))
                disable_source("www.linkedin.com", str(exc))
                result.stopped_reason = f"{exc} — LinkedIn disabled, a human must re-enable"
                break
            except PolicyDenied as exc:
                # Refused before it left the building, so it is not an attempt against
                # LinkedIn and must not land in the 1.4a yield denominator.
                result.attempted -= 1
                result.stopped_reason = str(exc)
                break
            except TransientFetchError as exc:
                # The network dropped twice. We never saw this person's profile, so
                # this is not evidence about their profile: it leaves the yield
                # denominator, and it is not an extraction failure to quarantine.
                result.attempted -= 1
                result.transient_failures += 1
                result.notes.append(f"{url}: network dropped, not counted — {exc}")
                continue
            except Exception as exc:  # one bad profile must not end the run
                result.failed += 1
                result.notes.append(f"{url}: {type(exc).__name__}: {exc}")
                quarantine(
                    extractor=ADAPTER,
                    extractor_version=EXTRACTOR_VERSION,
                    reason=f"{type(exc).__name__}: {exc}",
                    candidate_id=candidate_id,
                    source_url=url,
                )
                continue

            rows, undated = rows_from_person(
                person,
                page_text=page_text,
                source_url=url,
                skill_lines=skill_lines,
                fetch_id=fetch_id,
            )
            result.undated_entries += undated
            if any(r.claim_type == "experience_years" for r in rows):
                result.with_dated_experience += 1
            keys = strong_keys_from(person, page_text)
            result.strong_keys_found += len(keys)

            # `enriched` means "produced something", never "did not throw". On
            # 2026-08-27 this counted every profile that loaded and reported
            # "enriched 5, evidence 0" — a green light over a dead parser, which is
            # the exact failure mode ARCHITECTURE.md §9a.5 says must be impossible.
            if rows:
                result.enriched += 1
            else:
                result.notes.append(
                    f"{url}: page loaded but nothing could be read from it "
                    "— suspect a markup change, run scripts/refresh_fixture.py"
                )

            if candidate_id is None:
                continue
            outcome = write_evidence(candidate_id, rows, source_text=None)
            result.evidence_written += outcome.written
            if outcome.refused:
                result.notes.append(
                    f"{url}: refused {outcome.refused} ({'; '.join(outcome.reasons)})"
                )
            if keys:
                from hi import identity

                identity.resolve(keys | {"linkedin_slug": url.rstrip("/").rsplit("/", 1)[-1]})
    return result


def already_enriched() -> set:
    """Candidate ids this adapter has already read a profile for.

    The daily budget has to buy *new* people. `only_missing_keys` alone does not do
    that: it looks for a GitHub strong key, and since almost nobody links one, every
    candidate looked equally un-enriched and a second run re-read the same profiles.
    """
    with pool.connection() as conn:
        return {
            row[0]
            for row in conn.execute(
                "select distinct candidate_id from evidence where extractor = %s", (ADAPTER,)
            ).fetchall()
        }


def _by_promise(role_id: uuid.UUID, gate_state: str | None = "passed") -> list[dict]:
    """Refs, in the order the daily budget should be spent on them.

    `gate_state` is what the recruiter chose to read: the default `"passed"` is the
    people the snippet gate kept, `"failed"` the ones it ruled out before reading, and
    `None` everybody. Overriding the gate is a decision a person makes about their own
    quota — the gate stays the default, and the reason it gave is still recorded.

    Refs whose SERP snippet already names a must-have skill go first. It costs nothing
    (the snippet is already stored), it is deterministic, and it excludes nobody — the
    rest are still enriched, just later.
    """
    from hi import discovery, matching

    spec = matching.spec_of(role_id)
    must_have = [s.lower() for s in (spec.must_have_skills if spec else [])]

    def promise(row: dict) -> tuple:
        blob = " ".join(
            str(row.get(field) or "").lower()
            for field in ("snippet_raw", "snippet_headline", "snippet_title")
        )
        hits = sum(1 for skill in must_have if skill in blob)
        return (-hits, row.get("discovered_at"))

    return sorted(discovery.saved_refs(role_id, gate_state=gate_state), key=promise)


def budget_remaining() -> int:
    """Profile reads left in the rolling 24h window. Never negative."""
    return max(0, MAX_PROFILES_PER_DAY - spent_today())


def budget_frees_at() -> datetime | None:
    """When the oldest read in the window ages out, or None if nothing is spent.

    "Come back tomorrow" is wrong — the window rolls, so some of the budget returns
    within the hour. A recruiter told the wrong thing stops using the tool.
    """
    with pool.connection() as conn:
        return conn.execute(
            'select min(fetched_at) + interval \'24 hours\' from "fetch" '
            "where adapter = %s and fetched_at > now() - interval '24 hours'",
            (ADAPTER,),
        ).fetchone()[0]


def policy_blocked() -> str | None:
    """Why LinkedIn reading is refused right now, or None if it is allowed.

    The kill switch is a `source_policy` row, so this is the same fact the fetch gate
    would raise on — asked early, so the answer can be a sentence instead of a traceback
    twenty seconds into a run.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "select enabled, disabled_reason from source_policy where domain = 'linkedin.com'"
        ).fetchone()
    if row is None:
        return "there is no reviewed source_policy row for linkedin.com"
    return None if row[0] else (row[1] or "switched off, no reason recorded")


def browser_attached() -> bool:
    """True if something is listening on the CDP port.

    A socket connect, not a page load: it answers in milliseconds during a page render,
    and the only question it has to answer is whether starting a run is pointless.
    """
    host, _, port = CDP_URL.removeprefix("http://").partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def waiting_for_role(role_id: uuid.UUID, gate_state: str | None = "passed") -> list[dict]:
    """Read-only view of who enrichment would read, in the order it would read them.

    Deliberately *not* `targets_for_role`: that one resolves identities and writes to
    `candidate_ref`, and rendering a page must never mutate anything. The ordering is
    shared, so the first N rows here are the N that `--limit N` actually spends the
    budget on — a list in a different order from the run it describes is worse than no
    list at all.
    """
    seen = already_enriched() | _resolvable_on_github(role_id)
    return [
        {
            "slug": slug_of(row["source_url"]),
            "url": row["source_url"],
            "name": row["snippet_name"],
            "headline": row["snippet_headline"],
            # Shown so a recruiter picking by hand can see the two things the gate gets
            # wrong most: where someone is, and why the gate refused them.
            "location": row["snippet_location"],
            "gate_reason": row["gate_reason"],
            "enriched": row["candidate_id"] in seen,
        }
        for row in _by_promise(role_id, gate_state)
    ]


def _resolvable_on_github(role_id: uuid.UUID) -> set:
    """Candidates `targets_for_role` skips because they already have an artefact route.

    It exists only so the page and the run agree on who is next. In this market that is
    about one person in thirty, but a queue whose order differs from the run it prints a
    command for is the kind of quiet lie the whole product is built to avoid.
    """
    with pool.connection() as conn:
        rows = conn.execute(
            "select i.candidate_id, i.kind, i.value from identity i "
            "join candidate_ref c on c.candidate_id = i.candidate_id "
            "where c.role_id = %s",
            (role_id,),
        ).fetchall()
    keys: dict = {}
    for candidate_id, kind, value in rows:
        keys.setdefault(candidate_id, {})[kind] = value
    return {cid for cid, strong in keys.items() if github.resolve(strong)}


def targets_for_role(
    role_id: uuid.UUID,
    *,
    only_missing_keys: bool = True,
    skip_enriched: bool = True,
    gate_state: str | None = "passed",
) -> list[tuple]:
    """(candidate_id, profile_url) for the people worth enriching.

    Enrichment is expensive and rationed — 30 a day — so by default it goes to the
    people it can actually change: those we have never read, and those with no strong
    key and therefore no route to artefact evidence. Re-reading a profile we already
    have spends a scarce fetch to learn nothing.

    `skip_enriched=False` re-reads people we have seen before, which is what a refresh
    is: a profile from three months ago may have a new job on it.
    """
    from hi import discovery, identity, matching

    # Enrichment is rationed at 30 profiles a day, so the order it spends that budget
    # in decides whether a run produces a shortlist or nothing. Discovery order is the
    # wrong order: the broad title query ("Backend Engineer" x Bangalore) runs first
    # and returns every stack, so on 2026-08-27 the first three enriched profiles were
    # Java and Node developers for a role whose must-have was Python — three profiles
    # spent, `passed_gates 0`, while the Python-query refs sat further down the list.
    #
    # So: refs whose SERP snippet already names a must-have skill go first. It costs
    # nothing (the snippet is already stored), it is deterministic, and it does not
    # *exclude* anyone — the rest are still enriched, just later.
    rows = _by_promise(role_id, gate_state)
    seen = already_enriched() if skip_enriched else set()

    targets = []
    for row in rows:
        # `source_url` is the fetchable URL; `ref_value` is the scheme-less dedup key
        # ("linkedin.com/in/x"), which has no host and cannot be navigated to.
        url = row["source_url"]
        candidate_id = row["candidate_id"]
        if candidate_id is None:
            resolved = identity.resolve(
                {"linkedin_slug": url.rstrip("/").rsplit("/", 1)[-1]},
                display_name=row["snippet_name"],
                location_text=row["snippet_location"],
            )
            if resolved.candidate_id is None:
                continue  # ambiguous: a human decides, we do not guess
            candidate_id = resolved.candidate_id
            with pool.connection() as conn:
                conn.execute(
                    "update candidate_ref set candidate_id = %s where role_id = %s "
                    "and ref_value = %s",
                    (candidate_id, role_id, url),
                )
        if skip_enriched and candidate_id in seen:
            continue
        if only_missing_keys and github.resolve(identity.strong_keys_for(candidate_id)):
            continue
        targets.append((candidate_id, url))
    return targets


def slug_of(url: str) -> str:
    """The LinkedIn slug in a profile URL — the only stable human-typeable handle."""
    return url.rstrip("/").rsplit("/", 1)[-1].lower()


def filter_to_slugs(targets: list[tuple], slugs: list[str]) -> list[tuple]:
    """Narrow a target list to named people.

    Re-reading three specific profiles used to cost the whole day's budget, because
    target order is fixed and the people you want are rarely at the front of it.

    A slug that matches nothing is an error, not an empty result: a typo that silently
    scrapes four of the five people you asked for looks exactly like a successful run.
    """
    wanted = {s.strip().lower() for s in slugs if s.strip()}
    kept = [t for t in targets if slug_of(t[1]) in wanted]
    missing = wanted - {slug_of(t[1]) for t in kept}
    if missing:
        raise ValueError(
            "no gate-passing ref for: " + ", ".join(sorted(missing))
        )
    return kept


# --------------------------------------------------------------------------
# operator CLI
# --------------------------------------------------------------------------


def _set_enabled(enabled: bool, *, by: str | None, reason: str | None) -> None:
    with pool.connection() as conn:
        conn.execute(
            "update source_policy set enabled = %s, reviewed_at = %s, disabled_reason = %s "
            "where domain in ('linkedin.com', 'www.linkedin.com')",
            (enabled, datetime.now(timezone.utc) if enabled else None, reason),
        )
        if enabled:
            conn.execute(
                "update source_policy set tos_note = tos_note || %s "
                "where domain in ('linkedin.com', 'www.linkedin.com')",
                (f" Mode C enabled by {by} on {date.today().isoformat()}.",),
            )


def _resolve_role(text: str | None) -> uuid.UUID:
    """A full UUID, or any unique prefix of one. Otherwise print the roles and exit.

    Role ids are 36 characters and get retyped by hand at exactly the wrong moment, so
    a prefix is accepted and a miss answers with the list rather than a stack trace.
    """
    with pool.connection() as conn:
        roles = conn.execute(
            "select r.id, r.title, "
            "(select count(*) from candidate_ref c where c.role_id = r.id "
            " and c.gate_state = 'passed') "
            "from role r order by r.created_at"
        ).fetchall()

    cleaned = (text or "").strip().strip(".")
    matches = [r for r in roles if str(r[0]).startswith(cleaned)] if cleaned else []
    if len(matches) == 1:
        return matches[0][0]

    problem = "no role starts with" if cleaned else "no --role given"
    print(f"{problem} {cleaned!r}" if cleaned else problem, file=sys.stderr)
    if len(matches) > 1:
        print(f"{cleaned!r} matches {len(matches)} roles — give more characters", file=sys.stderr)
    print("\nroles in this database:", file=sys.stderr)
    for role_id, title, passed in roles:
        print(f"  {role_id}  {passed:>3} past the gate   {title or '(untitled)'}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else "status"

    if command == "enable":
        # `--by '<full name>'` was required until 2026-09-02. The tool now has one named
        # operator, so the question it asked ("whose account carries this?") has a single
        # answer and the flag was friction in front of it. What §7.4 actually requires is
        # that the answer be *recorded*, not that it be retyped: absent the flag, the
        # signed-in OS user is recorded, which on a one-operator machine is the same fact.
        by = args[args.index("--by") + 1] if "--by" in args else getpass.getuser()
        _set_enabled(True, by=by, reason=None)
        print(f"LinkedIn mode C enabled, owner recorded as {by!r}.")
        print(
            "This runs on YOUR real LinkedIn account and that account may be restricted. "
            "Never a second, made-up or shared account — it is banned, and it returns less."
        )
        print(f"Limits in code: {MAX_PROFILES_PER_DAY}/day, {MIN_DELAY_SECONDS}s+ apart, serial.")
    elif command == "disable":
        reason = args[args.index("--reason") + 1] if "--reason" in args else "disabled by operator"
        _set_enabled(False, by=None, reason=reason)
        print(f"LinkedIn disabled: {reason}")
    elif command in ("run", "measure"):
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
        if command == "measure":
            # IMPLEMENTATION.md 1.4a. Scrapes and reports; stores nothing about anyone.
            urls = [line.strip() for line in sys.stdin if line.strip()]
            if not urls:
                sys.exit("measure: pipe profile URLs on stdin, one per line")
            targets = [(None, url) for url in urls]
            role_id = None
        else:
            role_arg = args[args.index("--role") + 1] if "--role" in args else None
            role_id = _resolve_role(role_arg)
            # `--redo` re-reads people already enriched. A refresh, and the way to repair
            # the rows an older extractor got wrong (IMPLEMENTATION.md §2.1a) — until it
            # existed, `skip_enriched=False` was reachable only by editing code.
            redo = "--redo" in args
            # `--only a,b,c` names the people to read. It implies `--redo`, because the
            # reason to name someone is almost always that their rows are wrong.
            only = args[args.index("--only") + 1].split(",") if "--only" in args else None
            wide = redo or bool(only)
            targets = targets_for_role(role_id, skip_enriched=not wide, only_missing_keys=not wide)
            if only:
                try:
                    targets = filter_to_slugs(targets, only)
                except ValueError as exc:
                    sys.exit(f"--only: {exc}")
            if not targets:
                sys.exit(
                    "nothing to enrich: every gate-passing ref already has a strong key "
                    "(pass --redo to re-read profiles already enriched, or "
                    "--only <slug>,<slug> to name them)"
                )

        result = asyncio.run(enrich(targets, limit=limit))
        print(f"attempted {result.attempted}, enriched {result.enriched}, "
              f"evidence {result.evidence_written}, strong keys {result.strong_keys_found}, "
              f"failed {result.failed}"
              + (f", network drops {result.transient_failures} (not counted)"
                 if result.transient_failures else ""))
        print(f"dated-experience yield: {result.dated_yield:.0%} "
              f"({result.with_dated_experience}/{result.attempted}) "
              f"— the 1.4a number; record it in docs/PRD.md §13.1")
        for note in result.notes:
            print(f"  note: {note}")
        if result.stopped_reason:
            print(f"STOPPED: {result.stopped_reason}")

        if role_id is not None and result.evidence_written:
            # New strong keys mean GitHub verification is reachable now, and a new
            # experience_years row changes every score for the role.
            from hi.worker import enqueue

            enqueue("collect", {"role_id": str(role_id)})
            enqueue("score", {"role_id": str(role_id)})
            print("queued collect + score — run `python -m hi.worker` to pick them up")
    else:
        with pool.connection() as conn:
            rows = conn.execute(
                "select domain, enabled, reviewed_at, disabled_reason from source_policy "
                "where domain like '%linkedin.com' order by domain"
            ).fetchall()
        for domain, enabled, reviewed_at, disabled_reason in rows:
            state = "ENABLED" if enabled else "disabled"
            print(f"{domain:20} {state:9} reviewed={reviewed_at} {disabled_reason or ''}")
        print(f"spent today: {spent_today()} of {MAX_PROFILES_PER_DAY}")


if __name__ == "__main__":
    main()
