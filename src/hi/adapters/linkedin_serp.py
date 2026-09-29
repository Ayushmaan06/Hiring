"""`linkedin_serp` — discovery, ARCHITECTURE.md §7.4 mode A / IMPLEMENTATION.md 1.4b.

The spine's front door and the only discovery route that exists (the vendored
`linkedin_scraper/` has no people-search). Queries a SERP API for
`site:linkedin.com/in/` results and reads the profile URL, name, headline and
location straight out of the snippet metadata.

**This adapter touches no LinkedIn surface.** It talks to SerpAPI, which talks to
Google. No authwall, no account, no page fetched — which is exactly why the snippet
gate runs on this output before anything gets enriched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from urllib.parse import urlencode, urlsplit

from hi.adapters import linkedin_selectors as S
from hi.canon import expects_artifacts, region_cities
from hi.config import settings
from hi.fetcher import FetchFailed, fetch
from hi.models import CandidateRef, Query, RoleSpec

ADAPTER = "linkedin_serp"
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"

# A role that fans out past this is a spec problem, not a coverage problem. The
# overflow is logged rather than silently dropped.
MAX_QUERIES_PER_ROLE = 24

# How deep a role may be searched. Google stops returning useful organic results for a
# `site:` query well before this, and each page is one paid search per planned query, so
# the ceiling is there to stop "find more" turning into an unbounded bill.
MAX_PAGES = 5

# Everything under linkedin.com that is not a person.
NON_PERSON_SEGMENTS = {
    "company", "school", "jobs", "pulse", "posts", "groups", "showcase",
    "learning", "events", "feed", "help", "legal", "directory", "checkpoint",
}

_TITLE_SPLIT = re.compile(r"\s+[-–—]\s+")
_LINKEDIN_SUFFIX = re.compile(r"\s*\|\s*linkedin\s*$", re.IGNORECASE)

# Role-word expansion lives in gating so the planner and the gate cannot drift:
# searching for "Backend Developer" while gating only on "Backend Engineer" means
# paying to discover people and then discarding them.
from hi.gating import ROLE_WORDS, title_variants  # noqa: E402  (re-exported)

# Many Indian profiles list only "India" as their location (Google's structured
# location field included), so a city-only region clause silently loses them.
COUNTRY_FALLBACK = "India"


class SerpApiKeyMissing(RuntimeError):
    pass


# --------------------------------------------------------------------------
# URL normalisation — three URL forms reach us for the same person, and if they
# are not collapsed first the same candidate enters the shortlist three times.
# --------------------------------------------------------------------------


def normalise_linkedin_url(url: str) -> str | None:
    """Canonicalise to `linkedin.com/in/<slug>`, or None if not a person profile.

    Handles the forms SERP actually returns: `in.linkedin.com`, `www.linkedin.com`,
    bare `linkedin.com`, and the legacy `/pub/<slug>/1/2a/3b` shape.
    """
    parts = urlsplit(url if "//" in url else f"https://{url}")
    host = parts.netloc.lower().split(":")[0]

    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None

    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None

    # Locale prefixes: /in/foo is the norm, but /e/in/foo and similar appear.
    if segments[0].lower() in NON_PERSON_SEGMENTS:
        return None

    try:
        anchor = next(i for i, s in enumerate(segments) if s.lower() in {"in", "pub"})
    except StopIteration:
        return None

    if anchor + 1 >= len(segments):
        return None

    slug = segments[anchor + 1].lower()
    if not slug or slug in NON_PERSON_SEGMENTS:
        return None

    return f"linkedin.com/in/{slug}"


# --------------------------------------------------------------------------
# Snippet parsing — every parser here fails to None rather than guessing.
# A wrong employer is worse than a missing one.
# --------------------------------------------------------------------------


def parse_title(serp_title: str) -> tuple[str | None, str | None]:
    """SERP title -> (name, headline). Typically "Name - Title - Company | LinkedIn"."""
    cleaned = _LINKEDIN_SUFFIX.sub("", (serp_title or "").strip())
    if not cleaned:
        return None, None

    parts = [p.strip() for p in _TITLE_SPLIT.split(cleaned) if p.strip()]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " - ".join(parts[1:])


def parse_extensions(
    extensions: list[str],
    *,
    region_resolver: Callable[[str], str | None] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """`rich_snippet.top.extensions` -> (location, title, employer).

    Google emits these for LinkedIn results in the observed order
    `[location, current_title, current_employer]`. Rather than trust position
    blindly, the location is identified by asking the region canon which element
    resolves to a place; the remaining elements keep their relative order. When
    nothing resolves (e.g. a bare "India", too coarse to gate on) we fall back to
    the positional convention, because being wrong about *which* string is the
    location is better than discarding the title and employer as well.
    """
    items = [e.strip() for e in extensions if e and e.strip()]
    if not items:
        return None, None, None

    if region_resolver is None:
        from hi.canon import region_of

        region_resolver = region_of

    location_idx = next((i for i, e in enumerate(items) if region_resolver(e)), None)

    if location_idx is None:
        # Positional fallback: a place-ish leading element (has a comma, or is a
        # single short phrase) is treated as the location.
        head = items[0]
        looks_placeish = "," in head or len(head.split()) <= 3
        location_idx = 0 if looks_placeish and len(items) > 1 else None

    location = items.pop(location_idx) if location_idx is not None else None
    title = items[0] if items else None
    employer = items[1] if len(items) > 1 else None
    return location, title, employer


# --------------------------------------------------------------------------
# plan / discover
# --------------------------------------------------------------------------


def _quote(text: str) -> str:
    return f'"{text}"' if " " in text else text


def _or_clause(terms: list[str]) -> str:
    unique = list(dict.fromkeys(t for t in terms if t))
    if not unique:
        return ""
    if len(unique) == 1:
        return _quote(unique[0])
    return "(" + " OR ".join(_quote(t) for t in unique) + ")"


def _region_clause(spec: RoleSpec) -> str:
    """One OR clause over every member city of every wanted region, plus India."""
    terms: list[str] = []
    for code in spec.locations:
        cities = region_cities(code)
        terms.extend(cities if cities else [code])
    if terms:
        terms.append(COUNTRY_FALLBACK)
    return _or_clause(terms)


def plan(spec: RoleSpec) -> list[Query]:
    """Build the discovery query set.

    Two shapes, because they find different people:

    - **title queries** — every role-word variant of each wanted title, ORed, against
      the region clause. Broad and highest-yield.
    - **skill queries** — each must-have skill against the role words ("Python"
      AND (Engineer OR Developer ...)). Finds people whose headline names a stack
      rather than a job title, which title queries miss entirely.

    Seniority is deliberately absent: matching "5+ years" as a search phrase only
    finds profiles that literally wrote that string and loses everyone who phrased
    it differently or not at all. Experience is scored from evidence
    (`seniority_fit`), not filtered lexically here.
    """
    region = _region_clause(spec)
    extras = " ".join(_quote(k) for k in spec.extra_keywords)
    # ROLE_WORDS ("Engineer"/"Developer"/"SDE"...) only belongs in a skill query for a
    # role where those words are plausible titles — appending it for a business role
    # ANDs "Excel" against "Engineer OR Developer OR SDE", which matches nobody real.
    role_clause = _or_clause(ROLE_WORDS) if expects_artifacts(spec.must_have_skills) else ""

    def assemble(core: str) -> str:
        return " ".join(p for p in ("site:linkedin.com/in/", core, region, extras) if p)

    title_queries = [
        assemble(_or_clause(title_variants(t))) for t in spec.titles if t
    ]
    skill_queries = [
        assemble(f"{_quote(skill)} {role_clause}".strip()) for skill in spec.must_have_skills
    ]

    # Title queries are the broad net, so if the cap bites it should bite the
    # narrower skill queries first.
    ordered = list(dict.fromkeys(title_queries + skill_queries))
    queries = [Query(adapter=ADAPTER, query_text=q) for q in ordered[:MAX_QUERIES_PER_ROLE]]

    dropped = len(ordered) - len(queries)
    if dropped:
        print(f"{ADAPTER}: query plan capped at {MAX_QUERIES_PER_ROLE}, dropped {dropped} queries")
    return queries


def _serpapi_url(query_text: str, *, start: int = 0) -> str:
    if not settings.serpapi_key:
        raise SerpApiKeyMissing("SERPAPI_KEY is not set — put it in .env (see .env.example)")
    params = {
        "engine": "google",
        "q": query_text,
        "num": settings.serpapi_results_per_query,
        "api_key": settings.serpapi_key,
    }
    if start:
        params["start"] = start
    return f"{SERPAPI_ENDPOINT}?{urlencode(params)}"


def refs_from_serpapi(payload: dict, *, query_text: str) -> list[CandidateRef]:
    """Pure: SerpAPI JSON -> deduped CandidateRefs. Split out so it tests off fixtures."""
    refs: dict[str, CandidateRef] = {}

    for result in payload.get(S.SERP_RESULTS_KEY) or []:
        link = result.get(S.SERP_LINK_KEY) or ""
        canonical = normalise_linkedin_url(link)
        if canonical is None:
            continue  # company/school/jobs page, or not LinkedIn at all

        name, headline = parse_title(result.get(S.SERP_TITLE_KEY) or "")
        snippet_text = result.get(S.SERP_SNIPPET_KEY) or ""
        extensions = (
            (result.get(S.SERP_RICH_KEY) or {}).get(S.SERP_RICH_SECTION) or {}
        ).get(S.SERP_RICH_EXTENSIONS) or []
        location, cur_title, employer = parse_extensions(extensions)

        # snippet_raw is evidence-grade: it is the quote behind any claim derived
        # from discovery, so keep what the API actually returned, not a rewrite.
        snippet_raw = json.dumps(
            {
                "title": result.get(S.SERP_TITLE_KEY),
                "snippet": snippet_text,
                "link": link,
                "extensions": extensions,
            },
            ensure_ascii=False,
        )

        # First occurrence wins: SERP order is relevance order.
        refs.setdefault(
            canonical,
            CandidateRef(
                adapter=ADAPTER,
                ref_kind="linkedin_url",
                ref_value=canonical,
                source_url=f"https://{canonical}",
                snippet_raw=snippet_raw,
                snippet_name=name,
                snippet_headline=headline,
                snippet_location=location,
                snippet_title=cur_title,
                snippet_employer=employer,
            ),
        )

    return list(refs.values())


async def _fetch_page(query_text: str, start: int) -> dict | None:
    try:
        fetched = await fetch(_serpapi_url(query_text, start=start), adapter=ADAPTER, max_age_hours=24)
    except FetchFailed as exc:
        print(f"{ADAPTER}: query failed ({query_text} @{start}): {exc}")
        return None

    if not fetched.text:
        return None
    try:
        payload = json.loads(fetched.text)
    except json.JSONDecodeError:
        print(f"{ADAPTER}: non-JSON response for query: {query_text}")
        return None

    if payload.get(S.SERP_ERROR_KEY):
        print(f"{ADAPTER}: SerpAPI error: {payload[S.SERP_ERROR_KEY]}")
        return None
    return payload


async def discover(q: Query, *, pages: int = 1) -> list[CandidateRef]:
    """Run one planned query and return normalised, deduped refs.

    Google returns ~10 organic results per request for a `site:` query regardless
    of `num`, so more depth means more requests. `pages` therefore maps 1:1 onto
    SERP API spend and defaults to 1 — raise it deliberately, per role.
    """
    refs: dict[str, CandidateRef] = {}
    for page in range(pages):
        payload = await _fetch_page(q.query_text, start=page * 10)
        if payload is None:
            break

        page_refs = refs_from_serpapi(payload, query_text=q.query_text)
        if not page_refs:
            break  # exhausted: no point paying for the next page

        before = len(refs)
        for ref in page_refs:
            refs.setdefault(ref.ref_value, ref)
        if len(refs) == before:
            break  # page was entirely duplicates; further pages will be too

    return list(refs.values())
