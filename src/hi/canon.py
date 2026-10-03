from __future__ import annotations

import re
from pathlib import Path

import yaml

from hi.db import pool

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _load_skills_yaml() -> dict:
    return yaml.safe_load((DATA_DIR / "skills.yaml").read_text(encoding="utf-8"))


def _build_skill_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, entry in _load_skills_yaml().items():
        for name in (canonical, *entry.get("aliases", [])):
            lookup[_normalise(name)] = canonical
    return lookup


def _load_regions_yaml() -> dict[str, list[str]]:
    return yaml.safe_load((DATA_DIR / "regions.yaml").read_text(encoding="utf-8"))


def _build_region_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for region_code, cities in _load_regions_yaml().items():
        for city in cities:
            key = _normalise(city)
            existing = lookup.get(key)
            # A city listed under two regions resolves to the more specific one —
            # region codes get longer as they get more specific (IN-KA vs IN-KA-BLR).
            if existing is None or len(region_code) > len(existing):
                lookup[key] = region_code
    return lookup


def _build_implications() -> dict[str, list[str]]:
    """Canonical skill -> languages it is near-certain evidence of.

    A framework you can only use in one language is evidence of that language, and
    without this a Django developer fails a `must_have: Python` gate — which happened
    on live data 2026-08-27: LinkedIn Skills sections list frameworks where GitHub
    reports languages, so every Django and Spring developer was ruled out of a role
    they matched. Kept in `skills.yaml` next to the skill it belongs to, and kept
    conservative: only implications that hold essentially always.
    """
    out: dict[str, list[str]] = {}
    for canonical, entry in _load_skills_yaml().items():
        implied = entry.get("implies") or []
        if implied:
            out[canonical] = list(implied)
    return out


def _build_kinds() -> dict[str, str]:
    return {
        canonical: entry.get("kind", "")
        for canonical, entry in _load_skills_yaml().items()
    }


_SKILLS = _build_skill_lookup()
_IMPLIES = _build_implications()
_KINDS = _build_kinds()

# Kinds that only exist because someone writes code. A role naming one of these as a
# must-have is a role where public artefacts are a reasonable expectation; a role
# naming only tools, practices, domains or qualifications is not.
CODE_KINDS = frozenset({"language", "framework"})

# Languages that analysts, marketers and business people list as everyday tools. On
# their own they are no sign of public code: "Business & Strategy Associate" asked for
# SQL beside Excel and PowerPoint, and was scored and searched as an engineering role.
ANALYST_LANGUAGES = frozenset({"SQL", "R", "MATLAB", "HTML", "CSS"})
_REGION_CITIES = _load_regions_yaml()
_REGIONS = _build_region_lookup()


def skill_kind(canonical: str) -> str | None:
    """The `kind` of a canonical skill — language, framework, tool, practice, domain,
    certification — or None if the name is not canonical."""
    return _KINDS.get(canonical)


def expects_artifacts(skills: list[str], titles: list[str] = ()) -> bool:
    """True when the role asks for a programming language or framework.

    This is how a role is classified for weighting (ARCHITECTURE.md §2.3). Derived from
    the canon rather than asked of an LLM, so it is deterministic and a recruiter can
    see why by looking at the skill list. Not expecting artefacts is the neutral
    answer: it scores everyone only on what anyone can have.

    An analyst language (SQL, R...) counts only when a title says engineer or developer
    — that is how "Data Engineer" asking for SQL alone stays an engineering role.
    """
    from hi.gating import _ROLE_WORD_RE

    code = {
        canonical
        for skill in skills or []
        if (canonical := _SKILLS.get(_normalise(skill))) and _KINDS.get(canonical) in CODE_KINDS
    }
    if code - ANALYST_LANGUAGES:
        return True
    return bool(code) and any(_ROLE_WORD_RE.search(t or "") for t in titles or [])


def implied_skills(canonical: str) -> list[str]:
    """Languages implied by a canonical skill, or []. Never transitive — one hop only,
    because "React implies JavaScript" is a fact and a chain of them is a guess."""
    return list(_IMPLIES.get(canonical, []))


def region_cities(region_code: str) -> list[str]:
    """Member city/area names for a region code — used to build SERP queries."""
    return list(_REGION_CITIES.get(region_code, []))


def _windows(text: str, max_words: int = 3) -> list[str]:
    words = [w for w in re.split(r"[^A-Za-z0-9+#.]+", text) if w]
    out = []
    for size in range(max_words, 0, -1):
        out.extend(" ".join(words[i : i + size]) for i in range(len(words) - size + 1))
    return out


def skills_in_text(text: str) -> list[str]:
    """Canonical skills mentioned anywhere in free text, in canon order.

    A pure lookup — unlike `canonical_skill` it records no alias proposals, because
    scanning a whole JD would otherwise propose every unmatched word in it.
    """
    if not text:
        return []
    found = {}
    for window in _windows(text):
        canonical = _SKILLS.get(_normalise(window))
        if canonical:
            found.setdefault(canonical, None)
    return list(found)


def regions_in_text(text: str) -> list[str]:
    """Region codes named anywhere in free text, most specific first."""
    if not text:
        return []
    found = {}
    for window in _windows(text):
        code = _REGIONS.get(_normalise(window))
        if code:
            found.setdefault(code, None)
    # Drop codes that a more specific match already covers (IN when IN-KA-BLR hit).
    codes = sorted(found, key=len, reverse=True)
    return [c for c in codes if not any(o != c and o.startswith(c) for o in codes)]


def canonical_skill(text: str) -> str | None:
    canonical = _SKILLS.get(_normalise(text))
    if canonical is None:
        _propose_skill_alias(text)
    return canonical


def region_of(text: str) -> str | None:
    """Resolve a free-text location to a region code.

    Real location strings are multi-part ("Bengaluru, Karnataka, India"), so an
    exact-match-only lookup silently resolves nothing and the location gate never
    fires. Match on comma parts and then on short word windows, preferring the
    most specific region (longer code) when several hit.
    """
    if not text:
        return None

    exact = _REGIONS.get(_normalise(text))
    if exact is not None:
        return exact

    candidates: list[str] = []

    for part in text.split(","):
        hit = _REGIONS.get(_normalise(part))
        if hit:
            candidates.append(hit)

    if not candidates:
        # "Greater Noida" and "Electronic City" are multi-word, so single tokens
        # are not enough — slide a 1-3 word window over the whole string.
        words = [w for w in re.split(r"[^A-Za-z0-9]+", text) if w]
        for size in (3, 2, 1):
            for i in range(len(words) - size + 1):
                hit = _REGIONS.get(_normalise(" ".join(words[i : i + size])))
                if hit:
                    candidates.append(hit)
            if candidates:
                break

    if not candidates:
        return None
    return max(candidates, key=len)


def _propose_skill_alias(raw_text: str) -> None:
    with pool.connection() as conn:
        conn.execute("insert into skill_alias_proposal (raw_text) values (%s)", (raw_text,))


def sync_to_db() -> None:
    """Seed skill/skill_alias from data/skills.yaml. Called by `python -m hi.db migrate`."""
    with pool.connection() as conn:
        for canonical, entry in _load_skills_yaml().items():
            row = conn.execute(
                "select id from skill where canonical_name = %s", (canonical,)
            ).fetchone()
            if row is None:
                skill_id = conn.execute(
                    "insert into skill (canonical_name, kind) values (%s, %s) returning id",
                    (canonical, entry.get("kind")),
                ).fetchone()[0]
            else:
                skill_id = row[0]
            for alias in (canonical, *entry.get("aliases", [])):
                conn.execute(
                    "insert into skill_alias (alias, skill_id) values (%s, %s) "
                    "on conflict (alias) do nothing",
                    (alias, skill_id),
                )
