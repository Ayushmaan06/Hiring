"""Deterministic scoring (IMPLEMENTATION.md 1.9, ARCHITECTURE.md §5).

**Pure by design: no I/O, no clock, no DB.** `now` is a parameter, not
`datetime.now()`, which is the single thing that makes two runs over identical inputs
produce byte-identical output — and it is why the tests here are cheap.

An LLM never touches any number in this file. It may later write prose *about* the
result (AGENTS.md §2.4); if the prose and the number disagree, the prose is wrong.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from hi.models import RoleSpec
from hi.signals import BANNED_SIGNALS, FEATURE_ALLOWLIST, is_banned

# @2 (2026-10-02): scraped claims count in full, over-experience is not a penalty, and
# the must-have gate needs one must-have on record rather than all of them.
SCORER_VERSION = "scoring@2"

# ARCHITECTURE.md §2.4. What we scrape is taken as true, so every tier counts in full
# toward the score. The tiers still stay separate everywhere else — in the evidence
# table and on the candidate card ("proven" vs "says so") — so a recruiter can still see
# which claims are backed by code. Scoring them at 0.35 capped every non-technical
# candidate, who have no code to prove anything with, at "Weak".
TIER_MULTIPLIER = {"artifact_backed": 1.0, "third_party_stated": 1.0, "self_reported": 1.0}

# A nice-to-have skill contributes, but less than a must-have.
NICE_TO_HAVE_WEIGHT = 0.4

# Someone with none of the role's must-haves on record is listed, not ruled out, but
# their score is cut to this fraction so they read Weak. Without it, experience alone
# earned 0.40-0.55 ("Good"/"Strong") with no matching skill at all.
NO_MUST_HAVE_FACTOR = 0.25

# skill_depth is log-scaled so a prolific committer cannot dominate every shortlist:
# a 200-commit repo is not 200x a 1-commit repo (ARCHITECTURE.md §5.2).
DEPTH_VOLUME_CAP = 500_000.0  # bytes of a language that counts as "deep"

ACTIVITY_HALF_LIFE_DAYS = 365.0
SENIORITY_TOLERANCE_YEARS = 4.0

# Components and the score are rounded so `components_json` is byte-identical
# across runs and across machines.
PRECISION = 6


@dataclass(frozen=True)
class Evidence:
    """Scoring's view of an evidence row. Deliberately not the DB model."""

    claim_type: str
    tier: str
    claim_key: str | None = None
    claim_value: str | None = None
    value_num: float | None = None
    observed_at: datetime | None = None
    source_url: str = ""
    id: int | None = None


@dataclass(frozen=True)
class Weights:
    version: str
    values: dict[str, float]

    def get(self, name: str) -> float:
        return float(self.values.get(name, 0.0))


DEFAULT_WEIGHTS = Weights(
    version="v1",
    # All equal until there is evidence to change them (IMPLEMENTATION.md 1.9).
    values={name: 0.2 for name in sorted(FEATURE_ALLOWLIST)},
)


@dataclass(frozen=True)
class MatchResult:
    score: float
    components: dict[str, float]
    gates: dict[str, str]
    seniority_basis: str  # computed | estimated | unknown
    passed_gates: bool = field(default=True)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _usable(evidence: list[Evidence]) -> list[Evidence]:
    """Drop anything naming a banned signal.

    Belt and braces: `extract.writer` already refuses these at collection time, so a
    row reaching here means something bypassed the chokepoint. Ignoring it keeps the
    score unable to depend on a protected attribute even then.
    """
    return [
        e
        for e in evidence
        if not is_banned(e.claim_type, e.claim_key)
        and e.claim_type not in BANNED_SIGNALS
    ]


def _days_between(later: datetime, earlier: datetime) -> float:
    return max(0.0, (later - earlier).total_seconds() / 86400.0)


def _decay(days: float, half_life: float = ACTIVITY_HALF_LIFE_DAYS) -> float:
    return 0.5 ** (days / half_life)


def _skill_evidence(evidence: list[Evidence]) -> dict[str, list[Evidence]]:
    out: dict[str, list[Evidence]] = {}
    for e in evidence:
        if e.claim_type == "skill" and e.claim_key:
            out.setdefault(e.claim_key, []).append(e)
    return out


def _foreign_country(location_rows: list[Evidence]) -> str | None:
    """The named non-Indian country in an unmappable location string, if any.

    Shares `gating.NON_INDIA_COUNTRIES` so the snippet gate and the score gate cannot
    disagree about where someone is.
    """
    from hi.gating import NON_INDIA_COUNTRIES, _normalise_country

    for row in location_rows:
        for part in (row.claim_value or "").split(","):
            if _normalise_country(part) in NON_INDIA_COUNTRIES:
                return (row.claim_value or "").strip()
    return None


def _latest_artifact(evidence: list[Evidence]) -> datetime | None:
    dates = [
        e.observed_at
        for e in evidence
        if e.tier == "artifact_backed" and e.observed_at is not None
    ]
    return max(dates) if dates else None


# --------------------------------------------------------------------------
# gates (ARCHITECTURE.md §5.1)
# --------------------------------------------------------------------------


def evaluate_gates(spec: RoleSpec, evidence: list[Evidence], *, now: datetime) -> dict[str, str]:
    """Gate verdicts. A failure excludes but is always recorded, never dropped."""
    gates: dict[str, str] = {}
    by_skill = _skill_evidence(evidence)

    # Never rules anyone out (2026-10-02). Everyone we read is listed, and missing
    # must-haves are paid for in the score instead — `skill_match`, and the
    # NO_MUST_HAVE_FACTOR in `evaluate`. Requiring every must-have ruled out all 21
    # people read for a 10-must-have business role; nobody lists ten skills on LinkedIn.
    # The missing ones stay in the verdict so the card still shows them.
    must = spec.must_have_skills
    missing = [s for s in must if s not in by_skill]
    gates["must_have_skills"] = (
        "pass" if not missing
        else f"pass: has {len(must) - len(missing)} of {len(must)}; no evidence for {', '.join(missing)}"
    )

    if spec.remote in {"remote", "remote_relocate"}:
        gates["location"] = "pass: role is remote"
    elif not spec.locations:
        gates["location"] = "pass: role has no location requirement"
    else:
        location_rows = [e for e in evidence if e.claim_type == "location"]
        regions = [e.claim_key for e in location_rows if e.claim_key]
        if regions:
            # Borrowed rather than re-written: two copies of "is this the right place"
            # is exactly how the snippet gate and the score gate came to disagree.
            from hi.gating import _region_compatible

            if any(_region_compatible(r, spec.locations) for r in regions):
                gates["location"] = "pass"
            else:
                # Mirrors the snippet gate: a resolved region is an Indian one (the canon
                # knows no other), and another Indian city is a fact to weigh, not a
                # disqualification. Recorded as a pass so the card still shows it.
                gates["location"] = (
                    f"pass: {', '.join(regions)} is outside "
                    f"{', '.join(spec.locations)} — would need to relocate"
                )
        elif foreign := _foreign_country(location_rows):
            # We know where they are, we just could not map it to a region code. That
            # is positive disproof for an India-primary role, not missing information.
            gates["location"] = f"fail: {foreign} is outside {', '.join(spec.locations)}"
        else:
            # Absent evidence is absent, not disqualifying — but say so, so the card
            # can show that we simply do not know where this person is.
            gates["location"] = "pass: location unknown"

    latest = _latest_artifact(evidence)
    if latest is None:
        # Skipped rather than failed. Failing here would empty the shortlist whenever
        # artefact evidence is unavailable, which is exactly the degraded mode that
        # IMPLEMENTATION.md 1.11b requires to keep working. Recorded so it is visible.
        gates["activity"] = "skip: no artefact evidence"
    elif _days_between(now, latest) <= spec.max_staleness_days:
        gates["activity"] = "pass"
    else:
        gates["activity"] = (
            f"fail: most recent public work {int(_days_between(now, latest))} days old, "
            f"limit {spec.max_staleness_days}"
        )
    return gates


# --------------------------------------------------------------------------
# components (ARCHITECTURE.md §5.2) — each returns [0, 1]
# --------------------------------------------------------------------------


def skill_match(spec: RoleSpec, evidence: list[Evidence]) -> float:
    by_skill = _skill_evidence(evidence)
    wanted = [(s, 1.0) for s in spec.must_have_skills]
    wanted += [(s, NICE_TO_HAVE_WEIGHT) for s in spec.nice_to_have_skills if s not in spec.must_have_skills]
    if not wanted:
        return 0.0

    total = sum(w for _, w in wanted)
    earned = 0.0
    for skill, weight in wanted:
        rows = by_skill.get(skill)
        if not rows:
            continue
        best = max(TIER_MULTIPLIER.get(e.tier, 0.0) for e in rows)
        earned += weight * best
    return earned / total


def skill_depth(spec: RoleSpec, evidence: list[Evidence], *, now: datetime) -> float:
    """Volume x recency of artefact evidence per must-have skill, log-scaled."""
    if not spec.must_have_skills:
        return 0.0
    by_skill = _skill_evidence(evidence)

    per_skill = []
    for skill in spec.must_have_skills:
        artifacts = [e for e in by_skill.get(skill, []) if e.tier == "artifact_backed"]
        if not artifacts:
            per_skill.append(0.0)
            continue

        volume = sum(e.value_num or 0.0 for e in artifacts)
        # log1p keeps the curve flat at the top: 10x the bytes is not 10x the skill.
        depth = math.log1p(volume) / math.log1p(DEPTH_VOLUME_CAP) if volume > 0 else 0.0
        depth = min(1.0, depth)

        dates = [e.observed_at for e in artifacts if e.observed_at]
        recency = _decay(_days_between(now, max(dates))) if dates else 0.5
        per_skill.append(depth * recency)

    return sum(per_skill) / len(per_skill)


def computed_years(evidence: list[Evidence]) -> tuple[float | None, str]:
    """(years, basis). Never parsed from a title string (B5.4)."""
    stated = [
        e.value_num
        for e in evidence
        if e.claim_type == "experience_years" and e.value_num is not None
    ]
    if stated:
        return max(stated), "computed"
    return None, "unknown"


def estimated_years(evidence: list[Evidence], *, now: datetime) -> float | None:
    """Fallback: years since the earliest artefact. A floor, not a measurement."""
    dates = [
        e.observed_at
        for e in evidence
        if e.tier == "artifact_backed" and e.observed_at is not None
    ]
    if not dates:
        return None
    return _days_between(now, min(dates)) / 365.25


def seniority_fit(
    spec: RoleSpec, evidence: list[Evidence], *, now: datetime
) -> tuple[float, str]:
    years, basis = computed_years(evidence)
    if years is None:
        years = estimated_years(evidence, now=now)
        basis = "estimated" if years is not None else "unknown"
    if years is None:
        return 0.0, "unknown"

    # Only the minimum is scored. More experience than the role asks for is not a
    # penalty (2026-10-02): a 3-6 year role scored a 25-year operations manager 0, and
    # whether someone is "too senior" is the recruiter's call, not the ranking's.
    # `max_years` is still shown on the role; it just never lowers a score.
    lo = spec.seniority.min_years
    if lo is None or years >= lo:
        return 1.0, basis
    return max(0.0, 1.0 - (lo - years) / SENIORITY_TOLERANCE_YEARS), basis


def activity_recency(evidence: list[Evidence], *, now: datetime) -> float:
    latest = _latest_artifact(evidence)
    return _decay(_days_between(now, latest)) if latest else 0.0


def availability(evidence: list[Evidence]) -> float:
    """Unknown is 0, never negative (B5.8) — absence of a signal is not a penalty."""
    return 1.0 if any(e.claim_type == "availability" for e in evidence) else 0.0


# --------------------------------------------------------------------------
# evaluate
# --------------------------------------------------------------------------


def evaluate(
    spec: RoleSpec,
    evidence: list[Evidence],
    weights: Weights = DEFAULT_WEIGHTS,
    *,
    now: datetime,
) -> MatchResult:
    """Gates, then the five allowlisted components. Pure — `now` is injected."""
    usable = _usable(evidence)

    gates = evaluate_gates(spec, usable, now=now)
    fit, basis = seniority_fit(spec, usable, now=now)

    components = {
        "skill_match": skill_match(spec, usable),
        "skill_depth": skill_depth(spec, usable, now=now),
        "seniority_fit": fit,
        "activity_recency": activity_recency(usable, now=now),
        "availability": availability(usable),
    }

    # The allowlist is the fairness mechanism, so assert it rather than trust it.
    assert set(components) == set(FEATURE_ALLOWLIST), (
        f"components {sorted(components)} != allowlist {sorted(FEATURE_ALLOWLIST)}"
    )

    components = {k: round(min(1.0, max(0.0, v)), PRECISION) for k, v in sorted(components.items())}
    score = sum(weights.get(k) * v for k, v in components.items())
    if spec.must_have_skills and not any(s in _skill_evidence(usable) for s in spec.must_have_skills):
        score *= NO_MUST_HAVE_FACTOR
    score = round(score, PRECISION)

    return MatchResult(
        score=score,
        components=components,
        gates=gates,
        seniority_basis=basis,
        passed_gates=not any(v.startswith("fail") for v in gates.values()),
    )
