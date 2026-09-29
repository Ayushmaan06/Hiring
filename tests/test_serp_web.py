import asyncio
import json
import uuid
from pathlib import Path

import pytest

from hi.adapters import serp_web
from hi.extract import write_evidence
from hi.fetcher import PolicyDenied, _get_policy, fetch

FIXTURES = Path(__file__).parent / "fixtures" / "serp_web"

NAME = "Priya Sharma"
EMPLOYER = "Kestrel Foods Pvt Ltd"


def html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture()
def serp_payload():
    return json.loads((FIXTURES / "serp_priya.json").read_text(encoding="utf-8"))


# --- the blocklist ------------------------------------------------------------


@pytest.mark.parametrize(
    "url,blocked",
    [
        ("https://kestrelfoods.example.com/leadership", False),
        ("https://tradepress.example.org/kestrel-hosur", False),
        ("https://www.linkedin.com/in/priya-sharma", True),
        ("https://in.linkedin.com/in/priya-sharma", True),  # subdomain
        ("https://rocketreach.co/priya-sharma_1234", True),
        ("https://www.naukri.com/listing", True),
        ("https://jobs.naukri.com/listing", True),
        ("not-a-url", True),  # no host
    ],
)
def test_blocklist_covers_aggregators_brokers_and_subdomains(url, blocked):
    assert bool(serp_web.is_blocked(url)) is blocked


def test_linkedin_is_blocked_so_the_wildcard_row_cannot_route_around_its_caps():
    # LinkedIn has its own policy rows, its own daily caps and its own modes. Reaching
    # it through serp_web's wildcard row would bypass all three.
    assert serp_web.is_blocked("https://www.linkedin.com/in/anyone")


def test_urls_from_serpapi_filters_and_reports_what_it_dropped(serp_payload):
    urls, notes = serp_web.urls_from_serpapi(serp_payload)

    assert urls == [
        "https://kestrelfoods.example.com/leadership",
        "https://tradepress.example.org/kestrel-hosur",
    ]
    # Nothing is dropped silently — IMPLEMENTATION.md 2.1.
    assert any("linkedin.com" in note for note in notes)
    assert any("rocketreach.co" in note for note in notes)
    assert any("naukri.com" in note for note in notes)


def test_per_query_cap_is_enforced_and_logged(monkeypatch):
    monkeypatch.setattr(serp_web, "MAX_RESULTS_PER_QUERY", 2)
    payload = {
        "organic_results": [
            {"link": f"https://site{i}.example.com/page"} for i in range(5)
        ]
    }
    urls, notes = serp_web.urls_from_serpapi(payload)

    assert len(urls) == 2
    assert any("past the per-query cap" in note for note in notes)


# --- text extraction ----------------------------------------------------------


def test_page_text_drops_script_and_style_content():
    text = serp_web.page_text(html("team_page"))

    assert "Vice President of Supply Chain" in text
    assert "window.analytics" not in text  # a script mention is not a claim
    assert "display:none" not in text


def test_employer_keys_ignores_words_that_identify_nobody():
    assert serp_web.employer_keys("Kestrel Foods Pvt Ltd") == ["kestrel", "foods"]
    # A name made entirely of stopwords gives nothing to check against.
    assert serp_web.employer_keys("Tech Solutions Pvt Ltd") == []
    assert serp_web.employer_keys(None) == []


@pytest.mark.parametrize("employer", ["IBM", "TCS", "HCL", "SAP", "EY", "PwC", "Ola"])
def test_short_employer_names_are_kept(employer):
    # Found on live data 2026-08-28: a four-character floor dropped every one of these,
    # and the only shortlisted candidate in the dev database works at "IBM".
    assert serp_web.employer_keys(employer) == [employer.lower()]


def test_short_keys_match_whole_words_only():
    text = "Anita Rao\nTheyre not saying anything about an employer here at all."
    assert serp_web.rows_from_page(
        name="Anita Rao", employer="EY", text=text, source_url="https://x.example/y"
    ) == []


def test_short_key_still_corroborates_a_real_mention():
    text = "Anita Rao, Partner\nAnita is a Partner at EY in Mumbai."
    rows = serp_web.rows_from_page(
        name="Anita Rao", employer="EY", text=text, source_url="https://x.example/y"
    )
    assert [r.claim_type for r in rows] == ["employer", "title"]


@pytest.mark.parametrize(
    "value",
    ["Senior Software Engineer", "Python Developer", "Full-time", "Freelance", "Internship"],
)
def test_a_job_title_or_employment_type_is_not_an_employer(value):
    # All five are real `employer` claim_values in the dev database — the LinkedIn text
    # parser mis-assigns them. Searching for them as companies is money for nothing.
    assert serp_web.employer_keys(value) == []


def test_a_real_company_is_not_mistaken_for_a_title():
    assert serp_web.employer_keys("Kestrel Foods Pvt Ltd")
    assert serp_web.employer_keys("Nokia") == ["nokia"]


# --- the parse ----------------------------------------------------------------


def rows_for(fixture: str, *, name=NAME, employer=EMPLOYER):
    text = serp_web.page_text(html(fixture))
    return text, serp_web.rows_from_page(
        name=name, employer=employer, text=text, source_url="https://example.com/x"
    )


def test_team_page_corroborates_employer_as_third_party_stated():
    _, rows = rows_for("team_page")

    employer_rows = [r for r in rows if r.claim_type == "employer"]
    assert len(employer_rows) == 1
    assert employer_rows[0].tier == "third_party_stated"
    assert employer_rows[0].claim_value == EMPLOYER


def test_a_title_written_next_to_the_name_is_captured():
    _, rows = rows_for("team_page")

    titles = [r.claim_value for r in rows if r.claim_type == "title"]
    assert titles == ["Vice President of Supply Chain"]


def test_every_snippet_is_verbatim_in_the_page_text():
    text, rows = rows_for("team_page")
    assert rows
    for row in rows:
        assert row.snippet in text


def test_name_and_employer_far_apart_is_not_corroboration():
    # The page is about the company and separately mentions the person. Nobody has
    # said she works there, so there is no claim to make.
    _, rows = rows_for("unrelated_page")
    assert rows == []


def test_no_employer_to_check_means_no_claim():
    text = serp_web.page_text(html("team_page"))
    assert serp_web.rows_from_page(
        name=NAME, employer=None, text=text, source_url="https://example.com/x"
    ) == []


def test_a_namesake_is_not_matched_by_employer_alone():
    text = serp_web.page_text(html("team_page"))
    rows = serp_web.rows_from_page(
        name="Someone Else", employer=EMPLOYER, text=text, source_url="https://example.com/x"
    )
    assert rows == []


def test_one_page_corroborates_once():
    text = serp_web.page_text(html("team_page")) * 3  # same fact three times over
    rows = serp_web.rows_from_page(
        name=NAME, employer=EMPLOYER, text=text, source_url="https://example.com/x"
    )
    assert len([r for r in rows if r.claim_type == "employer"]) == 1


def test_title_near_requires_a_title_word():
    assert serp_web.title_near("Priya Sharma, Vice President", NAME) == "Vice President"
    # A sentence continuing after the name is not a job title.
    assert serp_web.title_near("Priya Sharma, who lives in Pune", NAME) is None


# --- the chokepoint -----------------------------------------------------------


def test_rows_survive_the_evidence_writer(db_conn):
    candidate_id = uuid.uuid4()
    db_conn.execute("insert into candidate (id) values (%s)", (candidate_id,))
    text, rows = rows_for("team_page")

    result = write_evidence(candidate_id, rows, source_text=text)

    assert result.written == len(rows)
    assert result.refused == 0
    tiers = db_conn.execute(
        "select distinct tier from evidence where candidate_id = %s", (candidate_id,)
    ).fetchall()
    assert tiers == [("third_party_stated",)]


def test_wildcard_policy_is_scoped_to_this_adapter_alone(db_conn):
    # The safety property of migration 012: the wildcard row is visible to serp_web and
    # to no other adapter, whether it is enabled or not.
    assert _get_policy("kestrelfoods.example.com", "serp_web") is not None
    assert _get_policy("kestrelfoods.example.com", "linkedin_profile") is None
    assert _get_policy("kestrelfoods.example.com", "github") is None


def test_serp_web_ships_dormant(db_conn):
    # Migration 013. The adapter is built but outside the delivered product, so the
    # fetch layer refuses it until a human turns the row on.
    policy = _get_policy("kestrelfoods.example.com", "serp_web")
    assert policy["enabled"] is False

    reason = db_conn.execute(
        "select disabled_reason from source_policy where domain = '*' and adapter = 'serp_web'"
    ).fetchone()[0]
    assert reason  # never disabled without saying why

    with pytest.raises(PolicyDenied):
        asyncio.run(fetch("https://kestrelfoods.example.com/leadership", adapter="serp_web"))


def test_wildcard_does_not_make_a_hostless_url_fetchable(db_conn):
    assert _get_policy("", "serp_web") is None


def test_exact_domain_row_still_wins_over_the_wildcard(db_conn):
    db_conn.execute(
        "insert into source_policy (domain, adapter, enabled, reviewed_at) "
        "values ('blocked.example.com', 'serp_web', false, now())"
    )
    policy = _get_policy("blocked.example.com", "serp_web")

    assert policy is not None and not policy["enabled"]  # refused, not wildcarded open


def test_fetch_refuses_an_unlisted_domain_for_another_adapter(db_conn):
    with pytest.raises(PolicyDenied):
        asyncio.run(fetch("https://kestrelfoods.example.com/leadership", adapter="github"))


def test_robots_is_respected_on_the_wildcard_row(db_conn):
    # The open web has no per-host review, so robots.txt is the per-host gate. If this
    # ever flips, every unlisted domain is fetched without the site owner's answer.
    assert _get_policy("kestrelfoods.example.com", "serp_web")["respect_robots"] is True


# --- spend --------------------------------------------------------------------


def test_daily_budget_is_in_code_not_config():
    import inspect

    source = inspect.getsource(serp_web)
    assert "DAILY_FETCH_BUDGET = " in source
    assert "settings.serp_web" not in source  # not overridable from .env


def test_plan_stops_costing_money_when_there_is_no_name():
    assert serp_web.plan("", "Kestrel Foods") == []


def test_plan_puts_the_cheapest_most_likely_query_first():
    queries = serp_web.plan(NAME, EMPLOYER)
    assert queries[0] == '"Priya Sharma" "Kestrel Foods Pvt Ltd"'
    assert len(queries) == 3


def test_plan_without_an_employer_still_tries_third_party_venues():
    queries = serp_web.plan(NAME, None)
    assert len(queries) == 1
    assert "speaker" in queries[0]
