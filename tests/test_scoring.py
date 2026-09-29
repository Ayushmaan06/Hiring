"""Scoring is pure, so these tests are cheap — be thorough (IMPLEMENTATION.md 1.9)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from hi import scoring
from hi.models import RoleSpec, Seniority
from hi.scoring import DEFAULT_WEIGHTS, Evidence, evaluate
from hi.signals import FEATURE_ALLOWLIST

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)

SPEC = RoleSpec(
    titles=["Backend Engineer"],
    must_have_skills=["Python", "PostgreSQL"],
    nice_to_have_skills=["Kubernetes"],
    seniority=Seniority(min_years=4, max_years=8),
    locations=["IN-KA-BLR"],
    remote="hybrid",
    max_staleness_days=540,
)


def skill(key, tier="artifact_backed", *, bytes_=100_000, days_ago=30) -> Evidence:
    return Evidence(
        claim_type="skill",
        claim_key=key,
        claim_value=key,
        tier=tier,
        value_num=float(bytes_),
        observed_at=NOW - timedelta(days=days_ago),
        source_url=f"https://github.com/x/{key.lower()}",
    )


def location(region="IN-KA-BLR") -> Evidence:
    return Evidence(
        claim_type="location", claim_key=region, tier="self_reported", source_url="https://x"
    )


FULL = [skill("Python"), skill("PostgreSQL"), skill("Kubernetes"), location()]


# --- the allowlist invariant -------------------------------------------------


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        FULL,
        [location()],
        [skill("Python", "self_reported")],
        [Evidence(claim_type="availability", tier="self_reported", source_url="https://x")],
    ],
)
def test_components_always_equal_the_allowlist(evidence):
    result = evaluate(SPEC, evidence, now=NOW)
    assert set(result.components) == set(FEATURE_ALLOWLIST)


def test_every_component_is_within_zero_and_one():
    result = evaluate(SPEC, FULL, now=NOW)
    assert all(0.0 <= v <= 1.0 for v in result.components.values())


# --- reproducibility (the definition of done) --------------------------------


def test_two_runs_produce_byte_identical_components():
    a = evaluate(SPEC, FULL, now=NOW)
    b = evaluate(SPEC, FULL, now=NOW)
    assert json.dumps(a.components, sort_keys=True) == json.dumps(b.components, sort_keys=True)
    assert a.score == b.score


def test_evaluate_does_not_read_the_clock():
    # `now` is injected, so a different `now` must change the answer — proving the
    # function has no hidden clock dependency.
    recent = evaluate(SPEC, FULL, now=NOW)
    later = evaluate(SPEC, FULL, now=NOW + timedelta(days=800))
    assert later.components["activity_recency"] < recent.components["activity_recency"]


# --- monotonicity ------------------------------------------------------------


def test_adding_evidence_never_lowers_the_score():
    additions = [
        skill("Python"),
        skill("PostgreSQL"),
        skill("Kubernetes"),
        location(),
        Evidence(claim_type="availability", tier="self_reported", source_url="https://x"),
    ]
    evidence: list[Evidence] = []
    previous = evaluate(SPEC, evidence, now=NOW).score
    for item in additions:
        evidence.append(item)
        current = evaluate(SPEC, evidence, now=NOW).score
        assert current >= previous, f"score dropped after adding {item.claim_type}/{item.claim_key}"
        previous = current


def test_higher_tier_never_scores_lower():
    self_reported = evaluate(SPEC, [skill("Python", "self_reported")], now=NOW)
    third_party = evaluate(SPEC, [skill("Python", "third_party_stated")], now=NOW)
    artifact = evaluate(SPEC, [skill("Python", "artifact_backed")], now=NOW)
    assert (
        self_reported.components["skill_match"]
        < third_party.components["skill_match"]
        < artifact.components["skill_match"]
    )


# --- empty and degenerate input ----------------------------------------------


def test_no_evidence_scores_zero_without_dividing_by_zero():
    result = evaluate(SPEC, [], now=NOW)
    assert result.score == 0.0
    assert all(v == 0.0 for v in result.components.values())
    assert result.passed_gates is False


def test_spec_with_no_skills_does_not_divide_by_zero():
    spec = RoleSpec(titles=["Engineer"], remote="remote")
    result = evaluate(spec, [], now=NOW)
    assert result.components["skill_match"] == 0.0
    assert result.components["skill_depth"] == 0.0


def test_availability_unknown_is_zero_never_negative():
    result = evaluate(SPEC, FULL, now=NOW)
    assert result.components["availability"] == 0.0

    with_signal = evaluate(
        SPEC,
        FULL + [Evidence(claim_type="availability", tier="self_reported", source_url="https://x")],
        now=NOW,
    )
    assert with_signal.components["availability"] == 1.0


# --- banned signals cannot influence the score -------------------------------


@pytest.mark.parametrize("key", ["college_name", "graduation_year", "age", "gender", "caste"])
def test_banned_signal_changes_nothing(key):
    clean = evaluate(SPEC, FULL, now=NOW)
    polluted = evaluate(
        SPEC,
        FULL
        + [
            Evidence(
                claim_type="skill",
                claim_key=key,
                tier="artifact_backed",
                value_num=10_000_000.0,
                observed_at=NOW,
                source_url="https://x",
            )
        ],
        now=NOW,
    )
    assert polluted.score == clean.score
    assert polluted.components == clean.components


def test_education_claim_type_is_ignored():
    clean = evaluate(SPEC, FULL, now=NOW)
    polluted = evaluate(
        SPEC,
        FULL + [Evidence(claim_type="education", claim_key="IIT", tier="self_reported", source_url="https://x")],
        now=NOW,
    )
    assert polluted.score == clean.score


# --- gates are recorded, not silently dropped --------------------------------


def test_missing_must_have_fails_the_gate_with_a_reason():
    result = evaluate(SPEC, [skill("Python"), location()], now=NOW)
    assert result.gates["must_have_skills"].startswith("fail")
    assert "PostgreSQL" in result.gates["must_have_skills"]
    assert result.passed_gates is False


def test_another_indian_city_passes_the_gate_with_the_mismatch_shown():
    """Mirrors `gating.snippet_gate`: within India, a different city is a fact the
    recruiter weighs, not a disqualification (changed 2026-09-18). Outside India,
    below, still fails."""
    result = evaluate(SPEC, [skill("Python"), skill("PostgreSQL"), location("IN-MH-PUN")], now=NOW)
    assert result.gates["location"].startswith("pass")
    assert "IN-MH-PUN" in result.gates["location"]
    assert result.passed_gates


def test_unknown_location_passes_but_says_so():
    result = evaluate(SPEC, [skill("Python"), skill("PostgreSQL")], now=NOW)
    assert result.gates["location"] == "pass: location unknown"


def test_unmappable_foreign_location_fails_rather_than_reading_as_unknown():
    # A location we can read but cannot map to a region code is positive disproof for
    # an India-primary role — not missing information.
    berlin = Evidence(
        claim_type="location",
        claim_key=None,
        claim_value="Berlin, Germany",
        tier="self_reported",
        source_url="https://github.com/x",
    )
    result = evaluate(SPEC, [skill("Python"), skill("PostgreSQL"), berlin], now=NOW)
    assert result.gates["location"].startswith("fail")
    assert "Berlin" in result.gates["location"]


def test_unmappable_indian_location_still_reads_as_unknown():
    # An Indian town missing from the canon must not be rejected as foreign.
    town = Evidence(
        claim_type="location",
        claim_key=None,
        claim_value="Some Small Town, India",
        tier="self_reported",
        source_url="https://github.com/x",
    )
    result = evaluate(SPEC, [skill("Python"), skill("PostgreSQL"), town], now=NOW)
    assert result.gates["location"] == "pass: location unknown"


def test_remote_role_ignores_location():
    spec = SPEC.model_copy(update={"remote": "remote"})
    result = evaluate(spec, [skill("Python"), skill("PostgreSQL"), location("IN-MH-PUN")], now=NOW)
    assert result.gates["location"].startswith("pass")


def test_stale_activity_fails_the_gate():
    old = [skill("Python", days_ago=900), skill("PostgreSQL", days_ago=900), location()]
    result = evaluate(SPEC, old, now=NOW)
    assert result.gates["activity"].startswith("fail")


def test_no_artefact_evidence_skips_activity_rather_than_failing():
    # Degraded mode (IMPLEMENTATION.md 1.11b) must still produce a shortlist.
    profile_only = [
        skill("Python", "self_reported"),
        skill("PostgreSQL", "self_reported"),
        location(),
    ]
    result = evaluate(SPEC, profile_only, now=NOW)
    assert result.gates["activity"] == "skip: no artefact evidence"
    assert result.passed_gates is True
    assert result.score > 0


# --- skill_depth is log-scaled ------------------------------------------------


def test_skill_depth_is_log_scaled_not_linear():
    small = evaluate(SPEC, [skill("Python", bytes_=1_000), skill("PostgreSQL", bytes_=1_000)], now=NOW)
    big = evaluate(
        SPEC, [skill("Python", bytes_=200_000), skill("PostgreSQL", bytes_=200_000)], now=NOW
    )
    ratio = big.components["skill_depth"] / small.components["skill_depth"]
    # 200x the bytes must not be anything like 200x the depth.
    assert 1.0 < ratio < 3.0


def test_one_prolific_committer_cannot_max_out_depth_alone():
    # All the volume in one skill leaves the other must-have at zero depth.
    lopsided = evaluate(SPEC, [skill("Python", bytes_=50_000_000)], now=NOW)
    assert lopsided.components["skill_depth"] < 0.6


# --- seniority ---------------------------------------------------------------


def test_computed_years_beats_estimation_and_is_labelled():
    evidence = FULL + [
        Evidence(claim_type="experience_years", tier="self_reported", value_num=6.0, source_url="https://x")
    ]
    result = evaluate(SPEC, evidence, now=NOW)
    assert result.seniority_basis == "computed"
    assert result.components["seniority_fit"] == 1.0


def test_falls_back_to_estimated_and_says_so():
    result = evaluate(SPEC, FULL, now=NOW)
    assert result.seniority_basis == "estimated"


def test_unknown_seniority_is_zero_not_a_guess():
    result = evaluate(SPEC, [skill("Python", "self_reported", days_ago=0)], now=NOW)
    assert result.seniority_basis == "unknown"
    assert result.components["seniority_fit"] == 0.0


def test_years_outside_the_range_decay_rather_than_snap_to_zero():
    inside = Evidence(claim_type="experience_years", tier="self_reported", value_num=6.0, source_url="https://x")
    just_over = Evidence(claim_type="experience_years", tier="self_reported", value_num=9.0, source_url="https://x")
    far_over = Evidence(claim_type="experience_years", tier="self_reported", value_num=20.0, source_url="https://x")

    a = evaluate(SPEC, [inside], now=NOW).components["seniority_fit"]
    b = evaluate(SPEC, [just_over], now=NOW).components["seniority_fit"]
    c = evaluate(SPEC, [far_over], now=NOW).components["seniority_fit"]
    assert a == 1.0
    assert 0.0 < b < 1.0
    assert c == 0.0


# --- golden value (catches accidental weight drift) --------------------------


def test_golden_candidate_scores_exactly():
    """A hand-built candidate, checked against hand-computed component values.

    If this changes, either a weight or a formula moved — both are deliberate acts
    that should require editing this number.
    """
    evidence = [
        skill("Python", bytes_=100_000, days_ago=0),
        skill("PostgreSQL", bytes_=100_000, days_ago=0),
        location("IN-KA-BLR"),
        Evidence(claim_type="experience_years", tier="self_reported", value_num=6.0, source_url="https://x"),
        Evidence(claim_type="availability", tier="self_reported", source_url="https://x"),
    ]
    result = evaluate(SPEC, evidence, DEFAULT_WEIGHTS, now=NOW)

    # must-haves fully artefact-backed (2 x 1.0), nice-to-have absent:
    # (1.0 + 1.0) / (1.0 + 1.0 + 0.4) = 0.833333
    assert result.components["skill_match"] == pytest.approx(0.833333, abs=1e-6)
    # log1p(100000)/log1p(500000) with no decay (days_ago=0)
    assert result.components["skill_depth"] == pytest.approx(0.877352, abs=1e-6)
    assert result.components["seniority_fit"] == 1.0
    assert result.components["activity_recency"] == 1.0
    assert result.components["availability"] == 1.0
    assert result.passed_gates is True
    # 0.2 x (0.833333 + 0.877352 + 1 + 1 + 1)
    assert result.score == pytest.approx(0.942137, abs=1e-6)


def test_scorer_version_is_pinned():
    assert scoring.SCORER_VERSION == "scoring@1"
    assert DEFAULT_WEIGHTS.version == "v1"
    assert set(DEFAULT_WEIGHTS.values) == set(FEATURE_ALLOWLIST)
