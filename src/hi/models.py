"""Pydantic domain types. See docs/ARCHITECTURE.md §4.5 for role_spec's stored shape."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal["self_reported", "third_party_stated", "artifact_backed"]
Remote = Literal["onsite", "hybrid", "remote", "remote_relocate"]


class Seniority(BaseModel):
    min_years: int | None = None
    max_years: int | None = None


class RoleSpec(BaseModel):
    """The recruiter-confirmed spec, not the LLM's raw output (ARCHITECTURE §4.5).

    `titles` is not in the §4.5 JSON sample but query planning needs it — the SERP
    query is built from titles x regions. It is populated from the role title plus
    any equivalents the recruiter adds on the confirm form.
    """

    titles: list[str] = Field(default_factory=list)
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    seniority: Seniority = Field(default_factory=Seniority)
    locations: list[str] = Field(default_factory=list)  # region codes, e.g. IN-KA-BLR
    remote: Remote = "onsite"
    domains: list[str] = Field(default_factory=list)
    max_staleness_days: int = 540
    excluded_employers: list[str] = Field(default_factory=list)

    # Free-text terms the recruiter wants ANDed into every discovery query
    # ("fintech", "payments"). Deliberately recruiter-controlled and empty by
    # default: each term narrows the result set, so this trades recall for focus.
    extra_keywords: list[str] = Field(default_factory=list)


class Query(BaseModel):
    adapter: str
    query_text: str


class CandidateRef(BaseModel):
    """What discovery found, before anything is fetched (ARCHITECTURE §4.3a)."""

    adapter: str
    ref_kind: str
    ref_value: str
    source_url: str
    snippet_raw: str
    snippet_name: str | None = None
    snippet_headline: str | None = None
    snippet_location: str | None = None

    # Structured current position, when the SERP provides it (Google exposes it for
    # LinkedIn results). More reliable than the self-written headline for gating, but
    # not always present. Persisted inside snippet_raw, not as their own columns.
    snippet_title: str | None = None
    snippet_employer: str | None = None


class GateVerdict(BaseModel):
    """Result of the snippet gate. `reason` is never empty when passed is False —
    the excluded list renders it, and a DB constraint refuses a failed ref without one."""

    passed: bool
    reason: str | None = None
