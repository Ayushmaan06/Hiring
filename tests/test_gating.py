import pytest

from hi.gating import snippet_gate
from hi.models import CandidateRef, RoleSpec, Seniority

# The gate is pure, so these tests need no DB. region_resolver is injected to keep
# it that way (and to make the region cases explicit rather than canon-dependent).
REGIONS = {
    "Bengaluru, Karnataka, India": "IN-KA-BLR",
    "Bangalore, Karnataka, India": "IN-KA-BLR",
    "Pune, Maharashtra, India": "IN-MH-PUN",
    "Chennai, Tamil Nadu, India": "IN-TN-CHE",
    "Karnataka, India": "IN-KA",
    "India": "IN",
    "Abohar, Punjab, India": "IN-PB",
}


def resolver(text: str) -> str | None:
    return REGIONS.get(text.strip())


def make_ref(**kw) -> CandidateRef:
    base = dict(
        adapter="linkedin_serp",
        ref_kind="linkedin_url",
        ref_value="linkedin.com/in/someone",
        source_url="https://linkedin.com/in/someone",
        snippet_raw="raw",
        snippet_name="Someone",
        snippet_headline="Backend Engineer at Acme",
        snippet_location="Bengaluru, Karnataka, India",
        snippet_title="Backend Engineer",
        snippet_employer="Acme",
    )
    base.update(kw)
    return CandidateRef(**base)


SPEC = RoleSpec(
    titles=["Backend Engineer"],
    must_have_skills=["Python"],
    locations=["IN-KA-BLR"],
    seniority=Seniority(min_years=4, max_years=8),
)


def gate(ref, spec=SPEC):
    return snippet_gate(ref, spec, region_resolver=resolver)


def test_matching_ref_passes():
    assert gate(make_ref()).passed is True


def test_seniority_prefix_still_matches_title():
    assert gate(make_ref(snippet_title="Senior Backend Engineer")).passed is True
    assert gate(make_ref(snippet_title="Sr. Backend Engineer")).passed is True
    assert gate(make_ref(snippet_title="Lead Backend Engineer / Architect")).passed is True


def test_wrong_title_fails_with_reason():
    verdict = gate(make_ref(snippet_title="Product Designer", snippet_headline="Product Designer at Flipkart"))
    assert verdict.passed is False
    assert "title" in verdict.reason
    assert "Backend Engineer" in verdict.reason


def test_headline_rescues_a_mismatched_structured_title():
    # Structured title says something generic, but the person's own headline says
    # exactly what the role wants. Recall matters at the gate.
    verdict = gate(
        make_ref(snippet_title="Senior Software Engineer", snippet_headline="Backend Engineer | Go | Kafka")
    )
    assert verdict.passed is True


def test_out_of_region_passes_with_readable_reason():
    """Passes since 2026-09-18, but the mismatch is still recorded verbatim — a
    recruiter deciding on relocation needs to see where the person actually is."""
    verdict = gate(make_ref(snippet_location="Pune, Maharashtra, India"))
    assert verdict.passed is True
    assert "Pune" in verdict.reason
    assert "IN-KA-BLR" in verdict.reason


def test_absent_location_passes_rather_than_rejecting():
    # Absent evidence is absent, not inferred — no location at all must not silently
    # discard a candidate we know nothing about. Reversed 2026-08-31 for the case of a
    # location string that IS present and names nowhere in India: that is not absent
    # evidence, it is a place, and treating it as unknown let "Greater Sydney Area"
    # outrank "Hyderabad, Telangana, India" at the gate. See the tests below.
    assert gate(make_ref(snippet_location=None)).passed is True
    assert gate(make_ref(snippet_location="")).passed is True
    assert gate(make_ref(snippet_location="localhost")).passed is False


def test_role_words_are_interchangeable():
    # The planner searches Developer/Programmer/SDE, so the gate must accept them
    # or discovery pays to find people the gate then throws away.
    for headline in (
        "Backend Developer at LIT India",
        "Associate System Analyst / Backend Programmer",
        "Backend SDE",
    ):
        verdict = gate(make_ref(snippet_title=None, snippet_headline=headline))
        assert verdict.passed is True, f"{headline} should match Backend Engineer"


def test_must_have_skill_plus_role_word_counts_as_a_title():
    # The planner's skill queries go looking for these people, so rejecting them
    # would waste half of discovery.
    for headline in ("Senior Python Developer", "Python Engineer at Jorie AI", "Python SDE"):
        verdict = gate(make_ref(snippet_title=None, snippet_headline=headline))
        assert verdict.passed is True, f"{headline} should satisfy Backend Engineer + Python"


def test_unrelated_title_still_fails_after_widening():
    verdict = gate(make_ref(snippet_title="Senior Consultant", snippet_headline="Senior Technology Consultant @EY"))
    assert verdict.passed is False


def test_coarser_location_is_not_ruled_out():
    # "Karnataka" or bare "India" cannot disprove a Bangalore role.
    assert gate(make_ref(snippet_location="Karnataka, India")).passed is True
    assert gate(make_ref(snippet_location="India")).passed is True


def test_another_indian_city_passes_with_the_mismatch_noted():
    """Changed 2026-09-18. Another Indian city used to be a rejection, and on live
    roles that was most of every location refusal — Kolkata, Delhi, Gurgaon, Chennai,
    Mumbai. People relocate; the recruiter decides, we just show the fact.
    """
    verdict = gate(make_ref(snippet_location="Abohar, Punjab, India"))
    assert verdict.passed is True
    assert "Punjab" in verdict.reason and "IN-PB" in verdict.reason


def test_foreign_country_is_rejected():
    # "India" as a search term also matches "Indonesia", and the region canon only
    # knows Indian places, so this needs its own check.
    verdict = gate(make_ref(snippet_location="Jawa Barat, Indonesia"))
    assert verdict.passed is False
    assert "outside India" in verdict.reason


def test_excluded_employer_fails():
    spec = SPEC.model_copy(update={"excluded_employers": ["Acme"]})
    verdict = snippet_gate(make_ref(), spec, region_resolver=resolver)
    assert verdict.passed is False
    assert "Acme" in verdict.reason


def test_remote_role_skips_location_gate():
    spec = SPEC.model_copy(update={"remote": "remote"})
    verdict = snippet_gate(
        make_ref(snippet_location="Pune, Maharashtra, India"), spec, region_resolver=resolver
    )
    assert verdict.passed is True


def test_no_title_or_headline_fails_with_reason():
    verdict = gate(make_ref(snippet_headline=None, snippet_title=None))
    assert verdict.passed is False
    assert verdict.reason


def test_every_failure_carries_a_reason():
    # A failed ref with a null reason is refused by a DB constraint, so this is the
    # invariant that keeps the excluded list usable.
    cases = [
        make_ref(snippet_title="Product Designer", snippet_headline="Product Designer at Flipkart"),
        make_ref(snippet_location="Jawa Barat, Indonesia"),
        make_ref(snippet_headline=None, snippet_title=None),
    ]
    for ref in cases:
        verdict = gate(ref)
        assert verdict.passed is False
        assert verdict.reason and verdict.reason.strip()


def test_gate_is_deterministic():
    ref = make_ref(snippet_title="Product Designer", snippet_headline="Product Designer at Flipkart")
    verdicts = [gate(ref) for _ in range(5)]
    assert len({(v.passed, v.reason) for v in verdicts}) == 1


@pytest.mark.parametrize("location", [
    "Greater Sydney Area",
    "Austin, Texas Metropolitan Area",
    "San Francisco Bay Area",
])
def test_a_place_naming_nowhere_in_india_is_refused(location):
    """These used to pass. They name no country, so NON_INDIA_COUNTRIES never fired,
    and they resolve to no region, so the gate treated them as "unreadable, cannot
    disprove" — which made an unknown place safer than a known one. Live on the
    Bangalore ops role 2026-08-31: 2 of the 11 people shown were abroad.
    """
    verdict = snippet_gate(make_ref(snippet_location=location), SPEC, region_resolver=resolver)
    assert not verdict.passed
    assert "not a recognised Indian location" in verdict.reason


def test_an_indian_location_we_cannot_resolve_still_passes():
    """The safety valve. Rejecting these would throw away the people the role wants."""
    for location in ("Whitefield, Bengaluru, India", "India", "Some Village, India"):
        verdict = snippet_gate(make_ref(snippet_location=location), SPEC, region_resolver=lambda _: None)
        assert verdict.passed, location


def test_indiana_is_not_india():
    """The word-boundary trap, twice: Indiana must not read as India, and neither
    must Indonesia — which this project has already been bitten by once.
    """
    for location in ("Indianapolis, Indiana", "Jakarta, Indonesia"):
        verdict = snippet_gate(make_ref(snippet_location=location), SPEC, region_resolver=lambda _: None)
        assert not verdict.passed, location


def test_a_blank_location_still_passes():
    """Unknown is not foreign. Inferring a country from a name is exactly the kind of
    guess this project refuses to make.
    """
    verdict = snippet_gate(make_ref(snippet_location=""), SPEC, region_resolver=resolver)
    assert verdict.passed


def test_anywhere_in_india_accepts_any_indian_city():
    """`IN-REMOTE` is the "Anywhere in India" option on the role form. As a literal
    code it shares no prefix with a real region, so it used to reject every Indian
    candidate — including ones in the city the role itself named.
    """
    spec = SPEC.model_copy(update={"locations": ["IN-REMOTE"]})
    for place in ("Bengaluru, Karnataka, India", "Pune, Maharashtra, India",
                  "Abohar, Punjab, India"):
        verdict = snippet_gate(make_ref(snippet_location=place), spec, region_resolver=resolver)
        assert verdict.passed, place
        assert verdict.reason is None, place  # a real match, so nothing to warn about
    # ...and it is still India-only.
    assert not snippet_gate(
        make_ref(snippet_location="Jawa Barat, Indonesia"), spec, region_resolver=resolver
    ).passed
