"""`serp_web` — open-web corroboration of an employer or title claim.

IMPLEMENTATION.md 2.1. This is the answer to the measurement in PRD.md §13.1: **nobody
links a GitHub** (0 of 9 profiles), so the `artifact_backed` tier reaches almost no
LinkedIn-discovered candidate, and for a non-technical or MBA role it is empty by
nature. Without a third party saying anything, a shortlist for those roles ranks people
purely on what they wrote about themselves.

A company team page, a conference programme or a published interview is that third
party. It is not an artefact — it does not prove someone can do the work — so it lands
as `third_party_stated` (0.6), above `self_reported` (0.35) and below `artifact_backed`.
That is exactly the right altitude for "someone other than the candidate confirms this".

**Deterministic parse, no LLM.** AGENTS.md §2.3 (page → evidence rows) is still dark, so
the parse here is plain string work: find the person's name in the page text, take a
verbatim window around it, and keep the window only if the employer we are checking also
appears in it. That is narrower than an LLM would be — it reads a team page listing
"Priya Sharma, VP Finance" and skips a paragraph that names the two facts four sentences
apart. Narrow and honest beats broad and unverifiable: every row it writes quotes text
you can go and read at `source_url`.

**Spend.** Two rationing mechanisms, both in code:

  * a candidate stops costing money the moment one page corroborates them, so the common
    case is one SERP query and one fetch, not the full query plan;
  * `DAILY_FETCH_BUDGET` caps open-web fetches per day regardless of how many roles run.

The wildcard `source_policy` row this adapter fetches under is migration 012, and the
reasoning for it is in that file. robots.txt is honoured per host, and `BLOCKLIST` below
keeps aggregators out of the pipeline entirely.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from datetime import datetime, timezone

from selectolax.parser import HTMLParser

from hi.adapters import linkedin_selectors as S

# Shared SerpAPI plumbing. `_serpapi_url` is engine/query/key only — nothing about it is
# LinkedIn-specific — and duplicating it would mean two places to fix when the vendor
# changes a parameter name.
from hi.adapters.linkedin_serp import _serpapi_url
from hi.db import pool
from hi.extract.writer import EvidenceRow, write_evidence
from hi.fetcher import FetchFailed, PolicyDenied, RobotsDenied, fetch

ADAPTER = "serp_web"
EXTRACTOR_VERSION = "serp_web_text@1"

# Open-web fetches per day, across every role. Deliberately in code, not config: this is
# the ceiling on how much of the internet the tool reads in a day, and it should take a
# code review to raise it.
DAILY_FETCH_BUDGET = 40

# Pages fetched per candidate before giving up on them. Most corroboration lands on the
# first or second result; past that the results are usually namesakes.
MAX_FETCHES_PER_CANDIDATE = 3

# Per IMPLEMENTATION.md 2.1: cap results per query and log what was dropped.
MAX_RESULTS_PER_QUERY = 20

# Text either side of a name occurrence that forms the quoted snippet.
WINDOW = 160

# Domains that flood this adapter with pages that corroborate nothing.
#
# Three kinds, and the third matters most. Job boards and aggregators (naukri, indeed)
# republish the candidate's own resume, so a "third party" hit there is the candidate
# talking, mis-tiered. Social platforms are self-published for the same reason. The
# people-data brokers (rocketreach, zoominfo, lusha…) are worse than useless: their
# pages are scraped personal data resold without the person's knowledge, and treating
# one as corroboration would launder a DPDP problem into our evidence table.
#
# LinkedIn is blocked here too. It has its own adapters, its own policy rows and its own
# caps; reaching it through the wildcard row would route around all three.
BLOCKLIST = frozenset(
    {
        # job boards / resume aggregators
        "naukri.com", "indeed.com", "glassdoor.com", "glassdoor.co.in", "monster.com",
        "monsterindia.com", "shine.com", "timesjobs.com", "foundit.in", "hirist.com",
        "instahyre.com", "iimjobs.com", "cutshort.io", "internshala.com", "ziprecruiter.com",
        # people-data brokers — scraped personal data, resold
        "rocketreach.co", "zoominfo.com", "signalhire.com", "lusha.com", "apollo.io",
        "contactout.com", "leadiq.com", "clearbit.com", "peopledatalabs.com", "kaspr.io",
        "salesql.com", "snov.io", "hunter.io", "getprospect.com", "adapt.io",
        # self-published, so not third-party
        "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
        "threads.net", "pinterest.com", "reddit.com", "quora.com", "tumblr.com",
        # low-signal noise
        "youtube.com", "tiktok.com", "scribd.com", "coursehero.com", "studocu.com",
    }
)

# Words that make a phrase read like a job title. Covers non-technical and technical
# roles both, because the scope widened to MBA and business roles on 2026-08-27 and this
# adapter exists mainly for them.
TITLE_CUES = frozenset(
    {
        "manager", "director", "head", "lead", "leader", "vp", "svp", "avp", "president",
        "chief", "officer", "ceo", "cto", "coo", "cfo", "cmo", "chro", "founder",
        "cofounder", "partner", "principal", "associate", "analyst", "consultant",
        "strategist", "specialist", "coordinator", "executive", "engineer", "developer",
        "designer", "architect", "scientist", "researcher", "advisor", "counsel",
        "controller", "treasurer", "recruiter", "generalist",
    }
)

# Employer-name words that identify nobody. "Priya Sharma" on a page mentioning
# "Solutions" is not corroboration that she works at "Acme Solutions Pvt Ltd".
#
# The employment-type words at the end are here because of a live data problem, not a
# theoretical one: the LinkedIn text parser writes "Full-time", "Freelance" and
# "Internship" into `employer` claims (see the note on `looks_like_a_title`). Searching
# for `"Priya Sharma" "Full-time"` spends real money to learn nothing.
EMPLOYER_STOPWORDS = frozenset(
    {
        "private", "limited", "ltd", "pvt", "inc", "incorporated", "llc", "llp", "plc",
        "corp", "corporation", "company", "co", "group", "holdings", "ventures",
        "technologies", "technology", "tech", "solutions", "systems", "services",
        "software", "labs", "consulting", "consultants", "partners", "global",
        "international", "india", "asia", "digital", "media", "online", "the", "and",
        # employment types, not employers
        "fulltime", "full", "time", "parttime", "part", "contract", "freelance",
        "internship", "intern", "permanent", "temporary", "selfemployed", "self",
        "employed", "apprenticeship", "seasonal",
    }
)

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[A-Za-z0-9]+")


class SerpWebError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# query planning
# --------------------------------------------------------------------------


def plan(name: str, employer: str | None) -> list[str]:
    """Queries in cost order — the cheapest, most likely one first.

    The loop stops at the first query that produces evidence, so a query late in this
    list only costs anything for a candidate the earlier ones could not corroborate.
    """
    name = (name or "").strip()
    if not name:
        return []

    queries = []
    if employer:
        queries.append(f'"{name}" "{employer.strip()}"')
        queries.append(f'"{name}" "{employer.strip()}" (team OR leadership OR "about us")')
    queries.append(f'"{name}" (speaker OR panelist OR keynote OR conference)')
    return queries


def domain_of(url: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_blocked(url: str) -> str | None:
    """The blocklist entry this URL matches, or None. Subdomains count."""
    host = domain_of(url)
    if not host:
        return "no host"
    for blocked in BLOCKLIST:
        if host == blocked or host.endswith("." + blocked):
            return blocked
    return None


def urls_from_serpapi(payload: dict) -> tuple[list[str], list[str]]:
    """(urls to fetch, human-readable notes about what was dropped and why)."""
    urls: list[str] = []
    dropped: dict[str, int] = {}
    seen: set[str] = set()

    for result in payload.get(S.SERP_RESULTS_KEY) or []:
        link = (result.get(S.SERP_LINK_KEY) or "").strip()
        if not link:
            continue
        if blocked := is_blocked(link):
            dropped[blocked] = dropped.get(blocked, 0) + 1
            continue
        if link in seen:
            continue
        seen.add(link)
        urls.append(link)

    notes = [f"{count} from {host}" for host, count in sorted(dropped.items())]
    if len(urls) > MAX_RESULTS_PER_QUERY:
        notes.append(f"{len(urls) - MAX_RESULTS_PER_QUERY} past the per-query cap")
        urls = urls[:MAX_RESULTS_PER_QUERY]
    return urls, notes


# --------------------------------------------------------------------------
# parse — pure, so it tests off fixtures
# --------------------------------------------------------------------------


def page_text(html: str) -> str:
    """Visible text of a page, one block per line.

    The newlines are load-bearing, and the first version of this function did not have
    them. Flattening a page to a single space-joined string throws away the only
    boundary information the document has, and two failures follow immediately:

      * a title runs past its end — `<h3>Priya Sharma, VP Supply Chain</h3>` followed by
        a bio parses as "VP Supply Chain Priya joined Kestrel Foods in", because with
        the tag boundary gone there is nothing left to stop at;
      * a page that merely *mentions* a person and a company reads as corroboration,
        because a fixed character window straddles the gap between two unrelated
        paragraphs.

    Splitting per text node over-splits (an inline link divides a sentence) rather than
    under-splits, which is the safe direction: an over-split window is a tighter quote,
    an under-split one is a false claim. `_block_window` puts adjacent blocks back
    together where the meaning needs it.

    The evidence writer normalises whitespace on both sides of its substring check, so
    newlines here cost nothing there.
    """
    tree = HTMLParser(html or "")
    for node in tree.css("script, style, noscript, template, svg"):
        node.decompose()
    body = tree.body or tree.root
    if body is None:
        return ""
    blocks = [_WS.sub(" ", block).strip() for block in body.text(separator="\n").split("\n")]
    return "\n".join(block for block in blocks if block)


def looks_like_a_title(employer: str | None) -> bool:
    """True if this "employer" is really a job title or an employment type.

    Live data, 2026-08-28: the `employer` claims in the dev database include
    "Senior Software Engineer", "Python Developer", "Full-time" and "Freelance". The
    LinkedIn text parser (`linkedin_profile.parse_experience_text`) is mis-assigning
    those lines to `institution_name` — a real bug on the enrichment side, filed
    separately. This adapter has to survive the bad rows regardless, because searching
    for a job title as though it were a company is money spent to find nothing.

    Deliberately biased toward rejection. A false reject skips one corroboration; a
    false accept spends a query and can write a claim about the wrong thing. It does
    cost us the genuinely title-shaped company names ("Engineers India Limited"), which
    is the price of the rule and worth revisiting only if one shows up.
    """
    if not employer:
        return False
    words = {w.lower() for w in _WORD.findall(employer)}
    return bool(words & TITLE_CUES)


def employer_keys(employer: str | None) -> list[str]:
    """Words from an employer name that actually identify it.

    The floor is two characters, not four. Short names are the *most* identifying ones
    in this market — IBM, TCS, HCL, SAP, EY, PwC, Ola — and an earlier four-character
    floor silently dropped every one of them, which on live data left the sole
    shortlisted candidate at "IBM" with nothing to check against. Genericness is what
    disqualifies a word, and `EMPLOYER_STOPWORDS` is what measures that; length never
    did. Short keys are safe here because `_matches_employer` compares whole words.
    """
    if not employer or looks_like_a_title(employer):
        return []
    return [
        word.lower()
        for word in _WORD.findall(employer)
        if len(word) >= 2 and word.lower() not in EMPLOYER_STOPWORDS
    ]


def _matches_employer(text: str, keys: list[str]) -> bool:
    """Whole-word match, so "EY" does not corroborate an employer named in "theyre"."""
    words = {w.lower() for w in _WORD.findall(text)}
    return any(key in words for key in keys)


def _trim(window: str, name: str) -> str:
    """Bound a window to a quotable length, word-aligned, keeping the name inside it.

    A page with no markup arrives as one enormous block. A 4,000-character "quote" is
    not a quote — nobody checks it — so it is trimmed to something a recruiter reads.
    """
    if len(window) <= 2 * WINDOW:
        return window
    match = re.search(re.escape(name), window, re.IGNORECASE)
    if match is None:
        return window[: 2 * WINDOW]

    start = max(0, match.start() - WINDOW)
    end = min(len(window), match.end() + WINDOW)
    if start > 0:
        space = window.find(" ", start)
        start = match.start() if space == -1 or space > match.start() else space + 1
    if end < len(window):
        space = window.rfind(" ", match.end(), end)
        end = end if space == -1 else space
    return window[start:end].strip()


def _block_window(blocks: list[str], index: int, keys: list[str]) -> str | None:
    """The block naming the person, plus the following block if the employer is there.

    Team pages put the name and title in a heading and the employer in the bio
    underneath, so a name-only block is not enough. Two blocks is the whole allowance:
    the further apart the two facts sit, the less the page is actually asserting.

    ponytail: name-block + next only. Widen to the preceding block too if measurement
    shows real corroboration being missed — recall is cheap to add, a false employer
    claim is not.
    """
    if _matches_employer(blocks[index], keys):
        return blocks[index]

    if index + 1 < len(blocks) and _matches_employer(blocks[index + 1], keys):
        return blocks[index] + "\n" + blocks[index + 1]
    return None


def _windows(text: str, name: str, keys: list[str]) -> list[str]:
    """Verbatim slices of `text` where the name and the employer appear together."""
    blocks = text.split("\n")
    pattern = re.compile(re.escape(name), re.IGNORECASE)

    out: list[str] = []
    for index, block in enumerate(blocks):
        if not pattern.search(block):
            continue
        window = _block_window(blocks, index, keys)
        if window is None:
            continue
        window = _trim(window, name)
        if window and window not in out:
            out.append(window)
    return out


def title_near(window: str, name: str) -> str | None:
    """A job title written immediately after the name, e.g. "Priya Sharma, VP Finance".

    Bounded by the end of the block, by punctuation, and by a word count — a title is a
    few words, and anything longer is a sentence that happens to start with one.
    """
    pattern = re.escape(name) + r"\s*[,:|\-–—]\s*([A-Za-z][A-Za-z&/'. ]{2,80})"
    for match in re.finditer(pattern, window, re.IGNORECASE):
        phrase = re.split(r"[\n|;.]", match.group(1).strip())[0].strip(" .,-")
        words = _WORD.findall(phrase)
        if not words or len(words) > 6:
            continue
        if {w.lower() for w in words} & TITLE_CUES:
            return phrase
    return None


def rows_from_page(
    *,
    name: str,
    employer: str | None,
    text: str,
    source_url: str,
    fetch_id: int | None = None,
    observed_at: datetime | None = None,
) -> list[EvidenceRow]:
    """Evidence a single page supports about a single person. Pure.

    Corroboration requires the name and the employer to appear **in the same window**.
    A page that mentions both far apart is a page about a company that happens to
    mention a person, and a claim built from it would not survive someone reading the
    quote.
    """
    if not name or not text:
        return []

    keys = employer_keys(employer)
    if not keys:
        return []  # nothing to corroborate against; a bare name match proves nothing

    observed_at = observed_at or datetime.now(timezone.utc)
    rows: list[EvidenceRow] = []

    for window in _windows(text, name, keys):
        rows.append(
            EvidenceRow(
                claim_type="employer",
                claim_key=employer.lower(),
                claim_value=employer,
                tier="third_party_stated",
                source_url=source_url,
                snippet=window,
                fetch_id=fetch_id,
                observed_at=observed_at,
                extractor=ADAPTER,
                extractor_version=EXTRACTOR_VERSION,
            )
        )

        if title := title_near(window, name):
            rows.append(
                EvidenceRow(
                    claim_type="title",
                    claim_key=title.lower(),
                    claim_value=title,
                    tier="third_party_stated",
                    source_url=source_url,
                    snippet=window,
                    fetch_id=fetch_id,
                    observed_at=observed_at,
                    extractor=ADAPTER,
                    extractor_version=EXTRACTOR_VERSION,
                )
            )
        break  # one page corroborates once; a second window is the same fact again

    return rows


# --------------------------------------------------------------------------
# spend
# --------------------------------------------------------------------------


def spent_today() -> int:
    with pool.connection() as conn:
        return conn.execute(
            'select count(*) from "fetch" '
            "where adapter = %s and fetched_at > now() - interval '24 hours' and not from_cache",
            (ADAPTER,),
        ).fetchone()[0]


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


async def _serp(query_text: str) -> dict | None:
    try:
        fetched = await fetch(_serpapi_url(query_text), adapter=ADAPTER, max_age_hours=24 * 7)
    except (PolicyDenied, RobotsDenied, FetchFailed) as exc:
        print(f"{ADAPTER}: query failed ({query_text}): {exc}")
        return None
    if not fetched.text:
        return None
    try:
        payload = json.loads(fetched.text)
    except json.JSONDecodeError:
        print(f"{ADAPTER}: non-JSON SerpAPI response for: {query_text}")
        return None
    if payload.get(S.SERP_ERROR_KEY):
        print(f"{ADAPTER}: SerpAPI error: {payload[S.SERP_ERROR_KEY]}")
        return None
    return payload


async def corroborate(
    candidate_id: uuid.UUID,
    *,
    name: str,
    employer: str | None,
    max_fetches: int = MAX_FETCHES_PER_CANDIDATE,
) -> dict:
    """Try to get one third party to confirm this person's employer. Returns a report."""
    report = {"candidate_id": str(candidate_id), "fetched": 0, "written": 0, "notes": []}

    for query in plan(name, employer):
        payload = await _serp(query)
        if payload is None:
            continue
        urls, notes = urls_from_serpapi(payload)
        report["notes"].extend(f"dropped {note}" for note in notes)

        for url in urls:
            if report["fetched"] >= max_fetches:
                report["notes"].append(f"stopped at {max_fetches} pages for this candidate")
                return report
            if spent_today() >= DAILY_FETCH_BUDGET:
                report["notes"].append(f"daily fetch budget ({DAILY_FETCH_BUDGET}) reached")
                return report

            try:
                fetched = await fetch(url, adapter=ADAPTER, max_age_hours=24 * 30)
            except RobotsDenied:
                report["notes"].append(f"robots.txt refused {domain_of(url)}")
                continue
            except (PolicyDenied, FetchFailed) as exc:
                report["notes"].append(f"{domain_of(url)}: {exc}")
                continue

            report["fetched"] += 1
            text = page_text(fetched.text or "")
            rows = rows_from_page(
                name=name,
                employer=employer,
                text=text,
                source_url=fetched.url,
                fetch_id=fetched.fetch_id,
            )
            if not rows:
                report["notes"].append(f"{domain_of(url)}: name and employer not found together")
                continue

            result = write_evidence(candidate_id, rows, source_text=text)
            report["written"] += result.written
            report["notes"].extend(result.reasons)
            if result.written:
                return report  # corroborated; stop spending on this person

    return report


def current_employer(candidate_id: uuid.UUID) -> str | None:
    """The employer we are trying to corroborate — the most recently observed one."""
    with pool.connection() as conn:
        row = conn.execute(
            "select claim_value from evidence "
            "where candidate_id = %s and claim_type = 'employer' and claim_value is not null "
            "and tier <> 'third_party_stated' "
            "order by observed_at desc nulls last limit 1",
            (candidate_id,),
        ).fetchone()
    return row[0] if row else None


def already_corroborated() -> set:
    with pool.connection() as conn:
        return {
            row[0]
            for row in conn.execute(
                "select distinct candidate_id from evidence where extractor = %s", (ADAPTER,)
            ).fetchall()
        }


def targets_for_role(role_id: uuid.UUID, *, skip_done: bool = True) -> list[tuple]:
    """(candidate_id, name, employer) for shortlisted people worth corroborating.

    Ranked order, because the budget should be spent on the people a recruiter will
    actually read. Someone with no employer claim is skipped: there is nothing to check.
    """
    from hi import matching

    seen = already_corroborated() if skip_done else set()
    targets = []
    for row in matching.shortlist(role_id):
        candidate_id = row["candidate_id"]
        if skip_done and candidate_id in seen:
            continue
        name = (row["display_name"] or "").strip()
        employer = current_employer(candidate_id)
        if not name or not employer_keys(employer):
            continue
        targets.append((candidate_id, name, employer))
    return targets


async def run(role_id: uuid.UUID, *, limit: int = 10, skip_done: bool = True) -> dict:
    targets = targets_for_role(role_id, skip_done=skip_done)
    totals = {"candidates": 0, "fetched": 0, "corroborated": 0}

    print(f"{ADAPTER}: {len(targets)} candidates worth corroborating, doing up to {limit}")
    print(f"{ADAPTER}: {spent_today()} of {DAILY_FETCH_BUDGET} daily fetches spent")

    for candidate_id, name, employer in targets[:limit]:
        report = await corroborate(candidate_id, name=name, employer=employer)
        totals["candidates"] += 1
        totals["fetched"] += report["fetched"]
        totals["corroborated"] += 1 if report["written"] else 0

        verdict = "corroborated" if report["written"] else "nothing found"
        print(f"  {name} @ {employer}: {verdict} ({report['fetched']} pages)")
        for note in report["notes"]:
            print(f"      {note}")

        if any("daily fetch budget" in note for note in report["notes"]):
            break

    print(
        f"{ADAPTER}: {totals['corroborated']} of {totals['candidates']} corroborated, "
        f"{totals['fetched']} pages fetched"
    )
    return totals


def main() -> None:
    args = sys.argv[1:]

    def flag(name: str, default=None):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default

    if not args or args[0] != "run":
        print(f"{ADAPTER}: {spent_today()} of {DAILY_FETCH_BUDGET} daily fetches spent")
        print("usage: python -m hi.adapters.serp_web run --role <id-prefix> [--limit N] [--redo]")
        return

    # Same prefix-or-list resolver the enrichment CLI uses; role ids get retyped by hand.
    from hi.adapters.linkedin_profile import _resolve_role

    role_id = _resolve_role(flag("--role"))
    asyncio.run(
        run(
            role_id,
            limit=int(flag("--limit", 10)),
            skip_done="--redo" not in args,
        )
    )


if __name__ == "__main__":
    main()
