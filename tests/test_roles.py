from pathlib import Path

import pytest

from hi import llm, roles
from hi.models import RoleSpec, Seniority

JD = (Path(__file__).parent / "fixtures" / "jds" / "backend_bangalore.txt").read_text(encoding="utf-8")


@pytest.fixture()
def no_llm(monkeypatch):
    """Force the deterministic path — no network in tests.

    Both keys, not just the Anthropic one: `parse_jd` falls back to Groq, so clearing
    one key alone let these tests reach the real API whenever a developer had a Groq
    key in their `.env`.
    """
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "")
    monkeypatch.setattr(llm.settings, "groq_api_key", "")


def fake_extraction(**kw) -> llm.JDExtraction:
    base = dict(
        titles=["Senior Backend Engineer"],
        must_have_skills=["Python", "PostgreSQL", "Docker", "REST APIs"],
        nice_to_have_skills=["Kubernetes", "Kafka"],
        min_years=5,
        max_years=None,
        location_names=["Bengaluru"],
        remote="hybrid",
        domains=["fintech", "payments"],
    )
    base.update(kw)
    return llm.JDExtraction(**base)


# --- prompt discipline -------------------------------------------------------


def test_prompt_is_versioned():
    version, body = llm.load_prompt("parse_jd")
    assert version == "2"
    assert "Never invent skills" in body
    assert llm.extractor_version("parse_jd") == "parse_jd@2"


def test_no_key_raises_rather_than_calling_out(no_llm):
    with pytest.raises(llm.LlmUnavailable):
        llm.parse_jd("anything")


# --- explicit years only -----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5+ years of experience", (5, None)),
        ("4-8 years of experience", (4, 8)),
        ("4 to 8 years", (4, 8)),
        ("at least 6 years", (6, None)),
        ("minimum of 3 years", (3, None)),
        # Seniority words are not an experience statement.
        ("Senior Backend Engineer", (None, None)),
        ("We want a principal engineer", (None, None)),
    ],
)
def test_years_from_text(text, expected):
    assert roles.years_from_text(text) == expected


# --- LLM path ----------------------------------------------------------------


def test_draft_maps_names_to_canonical_forms(db_conn, monkeypatch):
    monkeypatch.setattr(llm, "parse_jd", lambda t: (fake_extraction(), "{}"))
    draft = roles.draft_from_jd(JD)

    assert draft.source == "llm"
    assert "Python" in draft.spec.must_have_skills
    assert "PostgreSQL" in draft.spec.must_have_skills   # from "PostgreSQL"
    assert "REST APIs" in draft.spec.must_have_skills
    # Names became region CODES, which the LLM was never asked to produce.
    assert draft.spec.locations == ["IN-KA-BLR"]
    assert draft.spec.remote == "hybrid"
    assert draft.spec.seniority.min_years == 5
    assert draft.spec.seniority.max_years is None


def test_unknown_skill_is_surfaced_not_dropped(db_conn, monkeypatch):
    monkeypatch.setattr(
        llm, "parse_jd", lambda t: (fake_extraction(must_have_skills=["Python", "Blorptech"]), "{}")
    )
    draft = roles.draft_from_jd(JD)

    assert "Blorptech" in draft.unmapped_skills
    assert "Blorptech" not in draft.spec.must_have_skills
    assert any("Blorptech" in w for w in draft.warnings)


def test_nice_to_have_never_duplicates_must_have(db_conn, monkeypatch):
    monkeypatch.setattr(
        llm,
        "parse_jd",
        lambda t: (fake_extraction(nice_to_have_skills=["Python", "Kubernetes"]), "{}"),
    )
    draft = roles.draft_from_jd(JD)
    assert "Python" not in draft.spec.nice_to_have_skills


def test_jd_text_wins_over_an_inferred_year_count(db_conn, monkeypatch):
    # The JD says "5+ years"; a model claiming 10-15 has inferred, so the text wins.
    monkeypatch.setattr(
        llm, "parse_jd", lambda t: (fake_extraction(min_years=10, max_years=15), "{}")
    )
    draft = roles.draft_from_jd(JD)
    assert (draft.spec.seniority.min_years, draft.spec.seniority.max_years) == (5, None)


def test_invalid_remote_value_falls_back(db_conn, monkeypatch):
    monkeypatch.setattr(llm, "parse_jd", lambda t: (fake_extraction(remote="whenever"), "{}"))
    assert roles.draft_from_jd(JD).spec.remote == "onsite"


# --- failure behaviour: never a fabricated spec ------------------------------


def test_llm_failure_degrades_to_deterministic_not_a_guess(db_conn, monkeypatch):
    def boom(_):
        raise RuntimeError("503 upstream")

    monkeypatch.setattr(llm, "parse_jd", boom)
    draft = roles.draft_from_jd(JD)

    assert draft.source == "deterministic"
    assert any("Could not read" in w for w in draft.warnings)
    # Still useful: canon matched real skills out of the text without inventing any.
    assert "Python" in draft.spec.must_have_skills


def test_deterministic_draft_finds_only_canon_skills(db_conn, no_llm):
    draft = roles.draft_from_jd(JD)
    assert draft.source == "deterministic"
    assert "Python" in draft.spec.must_have_skills
    assert "PostgreSQL" in draft.spec.must_have_skills
    assert draft.spec.locations == ["IN-KA-BLR"]
    assert draft.spec.seniority.min_years == 5
    assert any("without the language model" in w for w in draft.warnings)


def test_empty_jd_blocks(db_conn):
    draft = roles.draft_from_jd("   ")
    assert draft.source == "empty"
    assert draft.blocking


# --- Human Gate 1 ------------------------------------------------------------


def test_too_many_must_haves_warns(db_conn):
    spec = RoleSpec(
        titles=["Backend Engineer"],
        must_have_skills=["Python", "Go", "Rust", "Java", "Kafka", "Redis", "Docker"],
        locations=["IN-KA-BLR"],
    )
    warnings, blocking = roles.review(spec)
    assert any("must-have" in w for w in warnings)
    assert not blocking


def test_onsite_without_location_is_blocking(db_conn):
    spec = RoleSpec(titles=["Backend Engineer"], remote="onsite")
    _, blocking = roles.review(spec)
    assert any("in-office" in b for b in blocking)

    with pytest.raises(ValueError, match="in-office"):
        roles.confirm("Backend Engineer", spec)


def test_remote_without_location_is_fine(db_conn):
    spec = RoleSpec(titles=["Backend Engineer"], remote="remote")
    _, blocking = roles.review(spec)
    assert not blocking


def test_inverted_experience_range_is_blocking(db_conn):
    spec = RoleSpec(
        titles=["Backend Engineer"], locations=["IN-KA-BLR"], seniority=Seniority(min_years=9, max_years=4)
    )
    _, blocking = roles.review(spec)
    assert any("Minimum experience" in b for b in blocking)


def test_nothing_to_search_for_is_blocking(db_conn):
    _, blocking = roles.review(RoleSpec(locations=["IN-KA-BLR"], remote="hybrid"))
    assert any("title" in b for b in blocking)


def test_confirm_creates_a_searchable_role(db_conn):
    from hi import discovery
    from hi.adapters import linkedin_serp

    spec = RoleSpec(
        titles=["Backend Engineer"], must_have_skills=["Python"], locations=["IN-KA-BLR"], remote="hybrid"
    )
    role_id = roles.confirm("Backend Engineer", spec, jd_text=JD, actor="tester")

    stored_title, stored_spec = discovery.get_role(role_id)
    assert stored_title == "Backend Engineer"
    # The whole point: the confirmed spec drives the queries.
    queries = linkedin_serp.plan(stored_spec)
    assert queries
    assert any("Bengaluru" in q.query_text for q in queries)
    assert any("Python" in q.query_text for q in queries)
