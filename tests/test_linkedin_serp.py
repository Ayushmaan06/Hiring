import json
from pathlib import Path

import pytest

from hi.adapters import linkedin_serp as ls
from hi.models import RoleSpec, Seniority

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin_serp"


@pytest.fixture()
def payload() -> dict:
    return json.loads((FIXTURES / "bangalore_backend.json").read_text(encoding="utf-8"))


# --- URL normalisation -------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://in.linkedin.com/in/rohit-sharma-b1234567", "linkedin.com/in/rohit-sharma-b1234567"),
        ("https://www.linkedin.com/in/rohit-sharma-b1234567", "linkedin.com/in/rohit-sharma-b1234567"),
        ("https://linkedin.com/in/rohit-sharma-b1234567/", "linkedin.com/in/rohit-sharma-b1234567"),
        ("https://linkedin.com/pub/rohit-sharma-b1234567/1/2a/3b", "linkedin.com/in/rohit-sharma-b1234567"),
        ("https://www.linkedin.com/in/Rohit-Sharma-B1234567", "linkedin.com/in/rohit-sharma-b1234567"),
        ("https://in.linkedin.com/in/foo?trk=public_profile", "linkedin.com/in/foo"),
    ],
)
def test_normalise_collapses_url_forms(url, expected):
    assert ls.normalise_linkedin_url(url) == expected


# --- structured extensions ---------------------------------------------------


def test_parse_extensions_splits_location_title_employer():
    location, title, employer = ls.parse_extensions(
        [
            "Bengaluru, Karnataka, India",
            "Senior Backend Engineer / Technical Architect",
            "Prestine Technologies Pvt. Ltd.",
        ]
    )
    assert location == "Bengaluru, Karnataka, India"
    assert title == "Senior Backend Engineer / Technical Architect"
    assert employer == "Prestine Technologies Pvt. Ltd."


def test_parse_extensions_handles_unresolvable_location():
    # "India" is too coarse for the region canon to resolve, but the title and
    # employer beside it are still good and must not be thrown away.
    location, title, employer = ls.parse_extensions(
        ["India", "Engineering Lead", "Persistent Systems"]
    )
    assert title == "Engineering Lead"
    assert employer == "Persistent Systems"


def test_parse_extensions_empty():
    assert ls.parse_extensions([]) == (None, None, None)


def test_refs_carry_structured_title_and_employer(payload):
    refs = ls.refs_from_serpapi(payload, query_text="q")
    ref = next(r for r in refs if r.ref_value.endswith("priya-nair-99887766"))
    assert ref.snippet_title == "Backend Engineer"
    assert ref.snippet_employer == "Swiggy"
    assert ref.snippet_location == "Bengaluru, Karnataka, India"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/razorpay",
        "https://in.linkedin.com/jobs/backend-engineer-jobs-bengaluru",
        "https://www.linkedin.com/school/nit-trichy",
        "https://www.linkedin.com/pulse/some-article",
        "https://github.com/octocat",
        "https://in.linkedin.com/in/",
    ],
)
def test_normalise_rejects_non_person_pages(url):
    assert ls.normalise_linkedin_url(url) is None


def test_url_forms_dedup_to_one_ref(payload):
    refs = ls.refs_from_serpapi(payload, query_text="q")
    values = [r.ref_value for r in refs]

    assert len(values) == len(set(values)), "refs must be deduped"
    assert values.count("linkedin.com/in/chenthil-kumar-4178b144") == 1


def test_company_and_jobs_pages_are_dropped(payload):
    refs = ls.refs_from_serpapi(payload, query_text="q")
    assert not any(r.ref_value.endswith("razorpay") for r in refs)
    assert all("/in/" in r.ref_value for r in refs)
    assert len(refs) == 6  # 9 results - 1 dup - 1 company - 1 jobs


# --- snippet parsing ---------------------------------------------------------


def test_parse_title_splits_name_and_headline():
    name, headline = ls.parse_title("Rohit Sharma - Senior Backend Engineer - Razorpay | LinkedIn")
    assert name == "Rohit Sharma"
    assert headline == "Senior Backend Engineer - Razorpay"


def test_parse_title_handles_name_only():
    name, headline = ls.parse_title("Vikram Iyer | LinkedIn")
    assert name == "Vikram Iyer"
    assert headline is None


def test_snippet_raw_is_preserved_verbatim(payload):
    refs = ls.refs_from_serpapi(payload, query_text="q")
    ref = next(r for r in refs if r.ref_value.endswith("priya-nair-99887766"))
    raw = json.loads(ref.snippet_raw)
    assert raw["snippet"] == payload["organic_results"][1]["snippet"]
    assert raw["title"] == payload["organic_results"][1]["title"]
    assert raw["extensions"] == payload["organic_results"][1]["rich_snippet"]["top"]["extensions"]


# --- query planning ----------------------------------------------------------


def test_plan_expands_titles_regions_and_skills():
    spec = RoleSpec(
        titles=["Backend Engineer"],
        must_have_skills=["Python"],
        locations=["IN-KA-BLR"],
        seniority=Seniority(min_years=4, max_years=8),
    )
    queries = ls.plan(spec)
    texts = [q.query_text for q in queries]

    assert all(q.adapter == "linkedin_serp" for q in queries)
    assert all("site:linkedin.com/in/" in t for t in texts)
    assert any("Bengaluru" in t and "Bangalore" in t for t in texts)
    assert any("Python" in t for t in texts)
    # India is ORed in because many profiles list only the country.
    assert all("India" in t for t in texts)


@pytest.mark.parametrize(
    "title,expected_extra",
    [
        ("Backend Engineer", "Backend Developer"),
        ("Python Developer", "Python Engineer"),
        ("Data Engineer", "Data SDE"),
    ],
)
def test_title_variants_swap_role_words(title, expected_extra):
    variants = ls.title_variants(title)
    assert title in variants
    assert expected_extra in variants


def test_title_variants_leaves_titles_without_a_role_word_alone():
    assert ls.title_variants("Architect") == ["Architect"]


def test_plan_role_word_expansion_reaches_the_query():
    spec = RoleSpec(titles=["Backend Engineer"], locations=["IN-KA-BLR"])
    text = ls.plan(spec)[0].query_text
    for word in ("Backend Engineer", "Backend Developer", "Backend SDE"):
        assert word in text


def test_plan_skill_queries_pair_skill_with_role_words():
    spec = RoleSpec(must_have_skills=["Python"], locations=["IN-KA-BLR"])
    text = ls.plan(spec)[0].query_text
    assert "Python" in text
    assert "Developer" in text and "Engineer" in text


def test_plan_omits_seniority_phrases():
    # Matching "5+ years" lexically finds only profiles that wrote that exact
    # string; seniority belongs in scoring, not in the query.
    spec = RoleSpec(
        titles=["Backend Engineer"],
        locations=["IN-KA-BLR"],
        seniority=Seniority(min_years=5, max_years=9),
    )
    text = ls.plan(spec)[0].query_text
    assert "5" not in text and "years" not in text.lower()


def test_plan_applies_extra_keywords():
    spec = RoleSpec(titles=["Backend Engineer"], locations=["IN-KA-BLR"], extra_keywords=["fintech"])
    assert all("fintech" in q.query_text for q in ls.plan(spec))


def test_plan_respects_the_query_cap():
    spec = RoleSpec(
        titles=[f"Title {i}" for i in range(6)],
        must_have_skills=[f"Skill {i}" for i in range(6)],
        locations=["IN-KA-BLR", "IN-DL-NCR"],
    )
    assert len(ls.plan(spec)) <= ls.MAX_QUERIES_PER_ROLE


def test_missing_api_key_raises_rather_than_calling_out(monkeypatch):
    monkeypatch.setattr(ls.settings, "serpapi_key", "")
    with pytest.raises(ls.SerpApiKeyMissing):
        ls._serpapi_url("anything")
