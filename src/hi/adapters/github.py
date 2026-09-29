"""`github` — the verifier (ARCHITECTURE.md §7.2, IMPLEMENTATION.md 1.5).

Not the discovery route: its job is to test the claims the profile side produced.
"Says 8 years of Python" against "has 8 years of public Python commits" is the whole
differentiator, and this adapter produces the right-hand side.

Two rules that outrank everything else here:

- **Strong keys only when resolving.** A name match that attributes someone else's
  repositories to a candidate is the worst bug this system can produce, so there is no
  name-matching code path at all. No strong key => no artefact evidence, which is a
  valid, visible outcome.
- **Evidence must be quotable.** Every row goes through `extract.write_evidence`, and
  the snippet is built from the API's own numbers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from hi import canon
from hi.config import settings
from hi.extract import EvidenceRow
from hi.fetcher import FetchFailed, PolicyDenied, fetch
from hi.models import RoleSpec

ADAPTER = "github"
API = "https://api.github.com"
EXTRACTOR_VERSION = "github_deterministic@1"

# Languages that are markup, config, or generated output. Presence of these says
# nothing about engineering skill, so they never become skill evidence.
NON_SKILL_LANGUAGES = {
    "markdown", "html", "css", "text", "tex", "roff", "makefile", "dockerfile",
    "shell", "batchfile", "powershell", "yaml", "json", "xml", "toml", "ini",
    "csv", "svg", "gitignore", "editorconfig", "jupyter notebook",
}

# A language must account for at least this share of a repo's bytes before we treat
# the repo as evidence of it — otherwise one vendored file implies a skill.
MIN_LANGUAGE_SHARE = 0.10


class RateLimited(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


async def _get(path: str, *, max_age_hours: int = 24) -> dict | list | None:
    """GET a GitHub API path. Returns None on 404 (deleted/renamed logins are normal)."""
    url = path if path.startswith("http") else f"{API}{path}"
    try:
        fetched = await fetch(url, adapter=ADAPTER, max_age_hours=max_age_hours, headers=_headers())
    except (FetchFailed, PolicyDenied):
        raise
    if fetched.status == 404:
        return None
    if fetched.status == 403 or fetched.status == 429:
        raise RateLimited(f"{url}: {fetched.status} — rate limited")
    if fetched.status >= 400:
        raise FetchFailed(f"{url}: {fetched.status}")
    if not fetched.text:
        return None
    try:
        return json.loads(fetched.text)
    except json.JSONDecodeError:
        raise FetchFailed(f"{url}: non-JSON response")


# --------------------------------------------------------------------------
# resolve — strong keys only
# --------------------------------------------------------------------------


def login_from_url(url: str) -> str | None:
    """Extract a GitHub login from a github.com URL, or None.

    Rejects the reserved paths that look like users but are not.
    """
    if not url:
        return None
    text = url.strip().rstrip("/")
    marker = "github.com/"
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1]
    login = tail.split("/")[0].split("?")[0].split("#")[0]
    reserved = {
        "orgs", "settings", "topics", "collections", "sponsors", "features",
        "marketplace", "explore", "notifications", "pulls", "issues", "about",
        "pricing", "enterprise", "login", "join", "search", "apps", "readme",
    }
    if not login or login.lower() in reserved:
        return None
    if not all(c.isalnum() or c == "-" for c in login):
        return None
    return login


def resolve(strong_keys: dict[str, str]) -> str | None:
    """Find a GitHub login from already-held strong keys. Never guesses from a name.

    `strong_keys` is the candidate's identity rows as {kind: value} — e.g.
    {"github_login": "octocat"} or {"personal_domain": "example.com"}. Returns None
    when nothing resolves, which means "no artefact evidence", not "try harder".
    """
    if login := strong_keys.get("github_login"):
        return login
    for key in ("github_url", "url", "blog"):
        if login := login_from_url(strong_keys.get(key, "")):
            return login
    return None


# --------------------------------------------------------------------------
# collect — deterministic evidence, no LLM (AGENTS.md §2: the API is already JSON)
# --------------------------------------------------------------------------


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def skill_rows_from_languages(
    *, login: str, repo: dict, languages: dict[str, int]
) -> list[EvidenceRow]:
    """Language bytes -> artifact_backed skill evidence for one repo."""
    total = sum(languages.values())
    if total <= 0:
        return []

    repo_url = repo.get("html_url") or f"https://github.com/{login}"
    pushed = _iso(repo.get("pushed_at"))
    rows: list[EvidenceRow] = []

    for language, byte_count in languages.items():
        if language.lower() in NON_SKILL_LANGUAGES:
            continue
        share = byte_count / total
        if share < MIN_LANGUAGE_SHARE:
            continue
        canonical = canon.canonical_skill(language)
        if canonical is None:
            continue  # unknown language: proposed for review, never guessed into a skill

        rows.append(
            EvidenceRow(
                claim_type="skill",
                claim_key=canonical,
                claim_value=canonical,
                value_num=byte_count,
                tier="artifact_backed",
                source_url=repo_url,
                # Synthesised from the API's own numbers, which is why this call passes
                # source_text=None: there is no prose to quote, only facts to state.
                snippet=(
                    f"{canonical}: {byte_count:,} bytes "
                    f"({share:.0%} of {repo.get('full_name', 'repository')})"
                    + (f", last pushed {pushed.date().isoformat()}" if pushed else "")
                ),
                observed_at=pushed,
                extractor=ADAPTER,
                extractor_version=EXTRACTOR_VERSION,
            )
        )
    return rows


def is_usable_repo(repo: dict, *, login: str) -> tuple[bool, str]:
    """(usable, reason). Filters the repos that prove nothing."""
    if repo.get("private"):
        return False, "private"
    if repo.get("archived") and (repo.get("stargazers_count") or 0) == 0:
        return False, "archived with no stars"
    if repo.get("fork"):
        # A fork the user never pushed to is someone else's work.
        if (repo.get("size") or 0) == 0:
            return False, "fork with no content"
        owner = (repo.get("owner") or {}).get("login", "")
        if owner.lower() != login.lower():
            return False, "fork owned by someone else"
        if not repo.get("pushed_at"):
            return False, "fork with no commits by this user"
    return True, ""


def profile_rows(user: dict) -> list[EvidenceRow]:
    """Location and availability evidence from the user object."""
    rows: list[EvidenceRow] = []
    url = user.get("html_url") or ""

    location = (user.get("location") or "").strip()
    if location:
        # Emitted even when the canon cannot map it: `claim_key` carries the region
        # code only when we resolved one, but the raw text still has to reach the
        # location gate. Dropping it here made "Berlin, Germany" indistinguishable
        # from "no location at all", which reads as *not ruled out*.
        rows.append(
            EvidenceRow(
                claim_type="location",
                claim_key=canon.region_of(location),
                claim_value=location,
                tier="self_reported",
                source_url=url,
                snippet=f"Location on GitHub profile: {location}",
                extractor=ADAPTER,
                extractor_version=EXTRACTOR_VERSION,
            )
        )

    # `hireable` is an explicit, self-set public signal. Unknown stays absent, which
    # scores 0 rather than negative (ARCHITECTURE.md §5.2).
    if user.get("hireable") is True:
        rows.append(
            EvidenceRow(
                claim_type="availability",
                claim_value="hireable",
                tier="self_reported",
                source_url=url,
                snippet="GitHub profile is marked as available for hire",
                extractor=ADAPTER,
                extractor_version=EXTRACTOR_VERSION,
            )
        )
    return rows


def rows_from_collected(collected: dict) -> list[EvidenceRow]:
    """Pure: a collected payload -> evidence rows. Split out so it tests off fixtures."""
    user = collected.get("user") or {}
    login = user.get("login") or ""

    if user.get("type") != "User":
        return []  # organisation or bot — not a person

    rows = profile_rows(user)
    for entry in collected.get("repos") or []:
        repo = entry.get("repo") or {}
        usable, _ = is_usable_repo(repo, login=login)
        if not usable:
            continue
        rows.extend(
            skill_rows_from_languages(login=login, repo=repo, languages=entry.get("languages") or {})
        )
    return rows


async def collect(login: str) -> dict | None:
    """Fetch user + repos + per-repo languages. Returns None if the login is gone."""
    user = await _get(f"/users/{login}")
    if user is None:
        return None
    if user.get("type") != "User":
        return {"user": user, "repos": []}  # skip orgs without spending repo calls

    cap = settings.github_max_repos_per_user
    repos = await _get(f"/users/{login}/repos?per_page={cap}&sort=pushed&direction=desc") or []

    collected = {"user": user, "repos": []}
    for repo in repos[:cap]:
        usable, reason = is_usable_repo(repo, login=login)
        if not usable:
            continue
        languages = await _get(f"/repos/{repo['full_name']}/languages") or {}
        collected["repos"].append({"repo": repo, "languages": languages})
    return collected


# --------------------------------------------------------------------------
# plan — supplementary discovery, OFF by default (IMPLEMENTATION.md 1.5)
# --------------------------------------------------------------------------


def plan(spec: RoleSpec) -> list[str]:
    """GitHub user-search queries. A *supplementary* discovery route, not the spine.

    Off by default per IMPLEMENTATION.md 1.5, and it hits the 30/min search ceiling
    rather than the 5,000/hr one — so a caller must opt in and pace itself. It finds
    people whose profile is thin but whose work is not, which is genuinely valuable
    and becomes more so wherever profile enrichment is unavailable.
    """
    queries: list[str] = []
    languages = [s for s in spec.must_have_skills if canon.canonical_skill(s)]
    cities: list[str] = []
    for code in spec.locations:
        cities.extend(canon.region_cities(code))

    for language in languages or [""]:
        for city in cities or [""]:
            parts = ["type:user"]
            if language:
                parts.append(f"language:{language}")
            if city:
                parts.append(f'location:"{city}"')
            queries.append(" ".join(parts))
    return queries
