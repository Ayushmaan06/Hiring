import uuid
from datetime import datetime, timedelta, timezone

import pytest

from hi import discovery, matching, scoring
from hi.extract import EvidenceRow, write_evidence
from hi.scoring import Evidence
from hi.models import RoleSpec, Seniority

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)

SPEC = RoleSpec(
    titles=["Backend Engineer"],
    must_have_skills=["Python"],
    locations=["IN-KA-BLR"],
    seniority=Seniority(min_years=4, max_years=8),
    remote="hybrid",
)


@pytest.fixture()
def role_id(db_conn):
    return discovery.create_role("Backend Engineer", SPEC)


def attach_candidate(db_conn, role_id, *, name, ref_value, region="IN-KA-BLR", skills=("Python",)):
    candidate_id = uuid.uuid4()
    db_conn.execute(
        "insert into candidate (id, display_name, location_region) values (%s, %s, %s)",
        (candidate_id, name, region),
    )
    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_raw, source_url, gate_state, candidate_id) "
        "values (%s, 'linkedin_serp', 'linkedin_url', %s, '{}', %s, 'passed', %s)",
        (role_id, ref_value, f"https://{ref_value}", candidate_id),
    )
    rows = [
        EvidenceRow(
            claim_type="skill",
            claim_key=s,
            claim_value=s,
            tier="artifact_backed",
            value_num=100_000.0,
            observed_at=NOW - timedelta(days=10),
            source_url=f"https://github.com/{name}/{s}",
            snippet=f"{s}: 100,000 bytes",
            extractor="test",
            extractor_version="test@1",
        )
        for s in skills
    ]
    rows.append(
        EvidenceRow(
            claim_type="location",
            claim_key=region,
            claim_value=region,
            tier="self_reported",
            source_url=f"https://github.com/{name}",
            snippet=f"Location: {region}",
            extractor="test",
            extractor_version="test@1",
        )
    )
    write_evidence(candidate_id, rows, source_text=None)
    return candidate_id


def test_active_weights_come_from_the_seeded_row(db_conn):
    weights = matching.active_weights()
    assert weights.version == "v1"
    assert set(weights.values) == set(scoring.DEFAULT_WEIGHTS.values)


def test_score_role_writes_a_match_per_candidate(db_conn, role_id):
    attach_candidate(db_conn, role_id, name="alice", ref_value="linkedin.com/in/alice")
    attach_candidate(db_conn, role_id, name="bob", ref_value="linkedin.com/in/bob")

    summary = matching.score_role(role_id, now=NOW)
    assert summary["scored"] == 2
    assert summary["weights_version"] == "v1"

    count = db_conn.execute("select count(*) from match where role_id = %s", (role_id,)).fetchone()[0]
    assert count == 2


def test_rescoring_updates_rather_than_duplicating(db_conn, role_id):
    attach_candidate(db_conn, role_id, name="alice", ref_value="linkedin.com/in/alice")

    matching.score_role(role_id, now=NOW)
    matching.score_role(role_id, now=NOW)

    # unique (role_id, candidate_id, scorer_version, weights_version)
    count = db_conn.execute("select count(*) from match where role_id = %s", (role_id,)).fetchone()[0]
    assert count == 1


def test_components_json_is_stored_sorted_and_complete(db_conn, role_id):
    attach_candidate(db_conn, role_id, name="alice", ref_value="linkedin.com/in/alice")
    matching.score_role(role_id, now=NOW)

    components = db_conn.execute(
        "select components_json from match where role_id = %s", (role_id,)
    ).fetchone()[0]
    assert set(components) == set(scoring.FEATURE_ALLOWLIST)


def test_gates_json_records_the_seniority_basis(db_conn, role_id):
    attach_candidate(db_conn, role_id, name="alice", ref_value="linkedin.com/in/alice")
    matching.score_role(role_id, now=NOW)

    gates = db_conn.execute(
        "select gates_json from match where role_id = %s", (role_id,)
    ).fetchone()[0]
    assert gates["seniority_basis"] in {"computed", "estimated", "unknown"}
    assert "must_have_skills" in gates


def test_shortlist_is_ranked_and_hides_excluded_by_default(db_conn, role_id):
    # alice has the must-have; carol does not, so carol fails a gate.
    attach_candidate(db_conn, role_id, name="alice", ref_value="linkedin.com/in/alice")
    attach_candidate(
        db_conn, role_id, name="carol", ref_value="linkedin.com/in/carol", skills=("Go",)
    )
    matching.score_role(role_id, now=NOW)

    visible = matching.shortlist(role_id)
    assert [r["display_name"] for r in visible] == ["alice"]

    everyone = matching.shortlist(role_id, include_excluded=True)
    assert {r["display_name"] for r in everyone} == {"alice", "carol"}
    # Excluded candidates are available, never silently dropped.
    carol = next(r for r in everyone if r["display_name"] == "carol")
    assert carol["passed_gates"] is False
    assert "fail" in carol["gates_json"]["must_have_skills"]


def test_shortlist_orders_by_score_descending(db_conn, role_id):
    attach_candidate(
        db_conn, role_id, name="deep", ref_value="linkedin.com/in/deep", skills=("Python",)
    )
    shallow = attach_candidate(
        db_conn, role_id, name="shallow", ref_value="linkedin.com/in/shallow", skills=("Python",)
    )
    # Give shallow much less volume so it must rank lower.
    db_conn.execute(
        "update evidence set value_num = 100 where candidate_id = %s and claim_type = 'skill'",
        (shallow,),
    )
    matching.score_role(role_id, now=NOW)

    scores = [float(r["score"]) for r in matching.shortlist(role_id)]
    assert scores == sorted(scores, reverse=True)


def test_archived_candidates_are_excluded_from_scoring(db_conn, role_id):
    absorbed = attach_candidate(db_conn, role_id, name="dup", ref_value="linkedin.com/in/dup")
    db_conn.execute("update candidate set status = 'archived' where id = %s", (absorbed,))

    assert matching.score_role(role_id, now=NOW)["scored"] == 0


def test_score_role_on_a_missing_role_raises(db_conn):
    with pytest.raises(ValueError, match="no such role"):
        matching.score_role(uuid.uuid4(), now=NOW)


def test_role_with_no_candidates_scores_nothing_without_erroring(db_conn, role_id):
    assert matching.score_role(role_id, now=NOW) == {
        "scored": 0,
        "passed_gates": 0,
        "weights_version": "v1",
    }


def test_the_estimated_years_banner_reflects_the_data_not_a_flag(db_conn, role_id):
    """The shortlist banner must stop claiming enrichment is unavailable once it is not.

    It was hard-coded True while mode B was blocked; a stale reassurance that survives
    the thing it describes is exactly the kind of quiet lie this project avoids.
    """
    candidate_id = attach_candidate(db_conn, role_id, name="asha", ref_value="in/asha")
    matching.score_role(role_id, now=NOW)
    assert matching.enrichment_degraded(role_id) is True

    write_evidence(
        candidate_id,
        [
            EvidenceRow(
                claim_type="experience_years",
                claim_key="linkedin_experience_total",
                claim_value="8.1 years",
                value_num=8.1,
                tier="self_reported",
                source_url="https://www.linkedin.com/in/asha",
                snippet="8.1 years of dated employment across 2 roles listed on LinkedIn",
                extractor="linkedin_profile",
                extractor_version="linkedin_mode_c@1",
            )
        ],
        source_text=None,
    )
    assert matching.enrichment_degraded(role_id) is False


# --- weight profiles for roles with no public artefacts (ARCHITECTURE.md §2.3) ---


def test_an_engineering_role_keeps_the_v1_weights(db_conn):
    spec = RoleSpec(must_have_skills=["Python"])
    assert matching.weights_profile_for(spec) == matching.ENGINEERING_PROFILE
    assert matching.weights_for_role(spec).version == "v1"


def test_a_business_role_gets_the_business_weights(db_conn):
    spec = RoleSpec(must_have_skills=["Financial Modeling", "Excel"])
    assert matching.weights_profile_for(spec) == matching.BUSINESS_PROFILE
    weights = matching.weights_for_role(spec)
    assert weights.version == "v1-business"
    assert weights.values["skill_depth"] == 0.0
    assert weights.values["activity_recency"] == 0.0


def test_a_framework_still_counts_as_an_engineering_role(db_conn):
    """Django names no language but is unambiguously code."""
    assert matching.weights_profile_for(RoleSpec(must_have_skills=["Django"])) == (
        matching.ENGINEERING_PROFILE
    )


def test_a_mixed_role_is_treated_as_engineering(db_conn):
    """If they want Python, artefacts are a fair expectation whatever else is asked."""
    spec = RoleSpec(must_have_skills=["Financial Modeling", "Python"])
    assert matching.weights_profile_for(spec) == matching.ENGINEERING_PROFILE


def test_a_role_with_no_must_haves_does_not_switch_profile(db_conn):
    """Scoring must not change on an absence of signal."""
    assert matching.weights_profile_for(RoleSpec()) == matching.ENGINEERING_PROFILE


def test_every_profile_sums_to_one(db_conn):
    """Otherwise scores from two profiles are not on the same scale as each other."""
    for profile in (matching.ENGINEERING_PROFILE, matching.BUSINESS_PROFILE):
        total = sum(matching.active_weights(profile).values.values())
        assert total == pytest.approx(1.0), f"{profile} weights sum to {total}"


def test_a_business_candidate_can_reach_a_strong_match(db_conn):
    """The point of the change. Under v1, 40% of the score was unreachable without
    artefacts, so the ceiling was ~0.47 against a Strong threshold of 0.55 — no MBA
    candidate could ever be a strong match however good they were.
    """
    from hi.config import settings

    spec = RoleSpec(must_have_skills=["Financial Modeling"], seniority=Seniority(min_years=4))
    evidence = [
        Evidence(
            claim_type="skill", claim_key="Financial Modeling",
            claim_value="Financial Modeling", tier="self_reported", observed_at=NOW,
        ),
        Evidence(
            claim_type="experience_years", claim_key="linkedin_experience_total",
            value_num=8.0, tier="self_reported", observed_at=NOW,
        ),
        Evidence(claim_type="availability", claim_value="open_to_work",
                 tier="self_reported", observed_at=NOW),
    ]

    under_v1 = scoring.evaluate(spec, evidence, matching.active_weights("engineering"), now=NOW)
    under_business = scoring.evaluate(spec, evidence, matching.weights_for_role(spec), now=NOW)

    assert under_v1.score < settings.match_strong_min, "the ceiling this change exists to lift"
    assert under_business.score > under_v1.score
    assert under_business.score >= settings.match_good_min


def test_the_tier_discount_is_not_relaxed_by_the_business_profile(db_conn):
    """A listed skill must still count less than a proven one — third_party_stated is
    reachable for non-technical people, so the incentive to verify has to survive.
    """
    spec = RoleSpec(must_have_skills=["Financial Modeling"])
    weights = matching.weights_for_role(spec)

    def score_with(tier):
        return scoring.evaluate(
            spec,
            [Evidence(claim_type="skill", claim_key="Financial Modeling",
                      claim_value="Financial Modeling", tier=tier, observed_at=NOW)],
            weights, now=NOW,
        ).score

    assert score_with("self_reported") < score_with("third_party_stated")
    assert score_with("third_party_stated") < score_with("artifact_backed")


def test_the_score_records_which_profile_produced_it(db_conn):
    """`match.weights_version` is what keeps an old score interpretable."""
    role_id = discovery.create_role("Finance Manager", RoleSpec(must_have_skills=["Excel"]))
    attach_candidate(db_conn, role_id, name="mba", ref_value="in/mba", skills=("Excel",))
    summary = matching.score_role(role_id, now=NOW)
    assert summary["weights_version"] == "v1-business"


def test_a_zero_weight_component_is_not_shown_as_an_empty_bar(db_conn):
    """A 0% bar labelled "Depth of proven work" reads as a deficiency. Under the
    business profile that component simply does not apply.
    """
    role_id = discovery.create_role("Finance Manager", RoleSpec(must_have_skills=["Excel"]))
    candidate_id = attach_candidate(db_conn, role_id, name="mba2", ref_value="in/mba2",
                                    skills=("Excel",))
    matching.score_role(role_id, now=NOW)
    detail = matching.candidate_detail(role_id, candidate_id)
    assert "skill_match" in detail["components"]
    assert "skill_depth" not in detail["components"]
    assert "activity_recency" not in detail["components"]


def test_an_engineering_role_still_shows_every_component(db_conn):
    role_id = discovery.create_role("Backend", RoleSpec(must_have_skills=["Python"]))
    candidate_id = attach_candidate(db_conn, role_id, name="dev", ref_value="in/dev",
                                    skills=("Python",))
    matching.score_role(role_id, now=NOW)
    detail = matching.candidate_detail(role_id, candidate_id)
    assert {"skill_match", "skill_depth", "seniority_fit", "activity_recency",
            "availability"} <= set(detail["components"])
