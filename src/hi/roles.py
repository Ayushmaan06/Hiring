"""JD intake and Human Gate 1 (IMPLEMENTATION.md 1.8, ARCHITECTURE.md §3).

The flow is: JD text -> `draft_from_jd()` -> a recruiter edits the form -> `confirm()`.
The draft is never used directly, which is what makes a bad parse cost two minutes
instead of a whole wrong search.

Division of labour, per AGENTS.md §2: the LLM reads prose and reports names; canon
maps those names to canonical skills and region codes. Anything canon cannot map is
surfaced to the recruiter rather than dropped or guessed.
"""

from __future__ import annotations

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from hi import canon, llm
from hi.models import RoleSpec, Seniority

MAX_SENSIBLE_MUST_HAVES = 6
VALID_REMOTE = {"onsite", "hybrid", "remote", "remote_relocate"}

_YEARS_RANGE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*years?", re.I)
_YEARS_MIN = re.compile(r"(?:at least|minimum(?: of)?|min\.?)\s*(\d{1,2})\s*\+?\s*years?", re.I)
_YEARS_PLUS = re.compile(r"(\d{1,2})\s*\+\s*years?", re.I)


class RoleDraft(BaseModel):
    """A proposal for the recruiter to edit. Not a spec until `confirm()` accepts it."""

    spec: RoleSpec
    source: Literal["llm", "deterministic", "empty"]
    warnings: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)
    unmapped_skills: list[str] = Field(default_factory=list)
    unmapped_locations: list[str] = Field(default_factory=list)
    raw_model_output: str | None = None


# --------------------------------------------------------------------------
# Deterministic pieces — used both to map LLM output and as the no-key fallback
# --------------------------------------------------------------------------


def _map_skills(names: list[str]) -> tuple[list[str], list[str]]:
    """Names -> (canonical, unmapped). Unknowns are proposed for human review."""
    mapped, unmapped = {}, {}
    for name in names:
        canonical = canon.canonical_skill(name)
        (mapped if canonical else unmapped).setdefault(canonical or name, None)
    return list(mapped), list(unmapped)


def _map_locations(names: list[str]) -> tuple[list[str], list[str]]:
    mapped, unmapped = {}, {}
    for name in names:
        code = canon.region_of(name)
        (mapped if code else unmapped).setdefault(code or name, None)
    return list(mapped), list(unmapped)


def years_from_text(text: str) -> tuple[int | None, int | None]:
    """Explicit experience statements only — never inferred from seniority words."""
    if match := _YEARS_RANGE.search(text):
        lo, hi = int(match.group(1)), int(match.group(2))
        return (lo, hi) if lo <= hi else (hi, lo)
    if match := _YEARS_MIN.search(text) or _YEARS_PLUS.search(text):
        return int(match.group(1)), None
    return None, None


def deterministic_draft(jd_text: str) -> RoleDraft:
    """Canon-only extraction. Works with no API key and invents nothing.

    Everything it finds is a literal match against the checked-in canon, so it cannot
    hallucinate a requirement. It cannot tell must-have from nice-to-have either, so
    it puts every skill in must_have and says so — the recruiter demotes from there.
    """
    skills = canon.skills_in_text(jd_text)
    regions = canon.regions_in_text(jd_text)
    min_years, max_years = years_from_text(jd_text)

    warnings = [
        "Read without the language model, so requirements were matched literally. "
        "Check the skills below and move any that are only nice-to-have."
    ]
    if not skills:
        warnings.append("No known skills were recognised in this text.")

    return RoleDraft(
        spec=RoleSpec(
            must_have_skills=skills,
            locations=regions,
            seniority=Seniority(min_years=min_years, max_years=max_years),
        ),
        source="deterministic",
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Intake
# --------------------------------------------------------------------------


def draft_from_jd(jd_text: str) -> RoleDraft:
    """Parse a JD into an editable draft. Never raises, never fabricates.

    On any failure the recruiter gets an empty (or canon-only) form plus the raw model
    output, per AGENTS.md §2.1 — a plausible wrong spec is worse than a blank one.
    """
    if not jd_text or not jd_text.strip():
        return RoleDraft(spec=RoleSpec(), source="empty", blocking=["The job description is empty."])

    try:
        extraction, raw = llm.parse_jd(jd_text)
    except llm.LlmUnavailable:
        return deterministic_draft(jd_text)
    except Exception as exc:  # SDK error, schema failure, malformed response
        draft = deterministic_draft(jd_text)
        draft.warnings.insert(0, f"Could not read the job description automatically: {exc}")
        draft.raw_model_output = None
        return draft

    must_have, unmapped_must = _map_skills(extraction.must_have_skills)
    nice, unmapped_nice = _map_skills(extraction.nice_to_have_skills)
    locations, unmapped_locations = _map_locations(extraction.location_names)

    remote = extraction.remote if extraction.remote in VALID_REMOTE else "onsite"

    # Trust the JD text over the model for years: the regex only fires on an explicit
    # statement, so when it disagrees the model has inferred something.
    min_years, max_years = years_from_text(jd_text)
    if min_years is None and max_years is None:
        min_years, max_years = extraction.min_years, extraction.max_years

    spec = RoleSpec(
        titles=extraction.titles,
        must_have_skills=must_have,
        nice_to_have_skills=[s for s in nice if s not in must_have],
        seniority=Seniority(min_years=min_years, max_years=max_years),
        locations=locations,
        remote=remote,
        domains=extraction.domains,
    )

    draft = RoleDraft(
        spec=spec,
        source="llm",
        unmapped_skills=unmapped_must + unmapped_nice,
        unmapped_locations=unmapped_locations,
        raw_model_output=raw,
    )
    draft.warnings, draft.blocking = review(spec, draft)
    return draft


# Named so the form can highlight the field this blocker is about without re-deriving
# the rule or matching on prose. A recruiter who cannot see *which* box to tick reads a
# refusal as "it is not working".
BLOCK_NO_LOCATION = (
    "This role is in-office but has no location. Add a location, or change it to fully remote."
)


def review(spec: RoleSpec, draft: RoleDraft | None = None) -> tuple[list[str], list[str]]:
    """(warnings, blocking) for a spec. Blocking items must be fixed before confirm."""
    warnings: list[str] = []
    blocking: list[str] = []

    if len(spec.must_have_skills) > MAX_SENSIBLE_MUST_HAVES:
        warnings.append(
            f"{len(spec.must_have_skills)} must-have skills is a lot and usually returns "
            f"nobody. Consider moving some to nice-to-have."
        )
    if not spec.titles and not spec.must_have_skills:
        blocking.append("Add at least one job title or one must-have skill to search for.")
    if not spec.locations and spec.remote == "onsite":
        blocking.append(BLOCK_NO_LOCATION)

    lo, hi = spec.seniority.min_years, spec.seniority.max_years
    if lo is not None and hi is not None and lo > hi:
        blocking.append(f"Minimum experience ({lo}) is above the maximum ({hi}).")

    if draft is not None:
        if draft.unmapped_skills:
            warnings.append(
                "These skills aren't in our list yet, so they won't be searched: "
                + ", ".join(draft.unmapped_skills)
            )
        if draft.unmapped_locations:
            warnings.append(
                "These locations aren't recognised: " + ", ".join(draft.unmapped_locations)
            )
    return warnings, blocking


def confirm(title: str, spec: RoleSpec, *, jd_text: str | None = None, actor: str | None = None) -> uuid.UUID:
    """Human Gate 1. Creates the role only if the spec is coherent."""
    from hi.discovery import create_role

    _, blocking = review(spec)
    if blocking:
        raise ValueError("; ".join(blocking))
    return create_role(title, spec, jd_text=jd_text, created_by=actor)
