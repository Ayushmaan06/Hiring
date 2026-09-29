import json
import uuid
from pathlib import Path

import pytest

from hi.adapters import github
from hi.extract import write_evidence
from hi.models import RoleSpec

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture()
def collected():
    return load("collected_user")


# --- resolve: strong keys only ------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/octocat", "octocat"),
        ("https://github.com/octocat/", "octocat"),
        ("http://www.github.com/octocat/some-repo", "octocat"),
        ("https://github.com/octocat?tab=repositories", "octocat"),
        ("https://github.com/orgs/acme", None),
        ("https://github.com/settings/profile", None),
        ("https://gitlab.com/octocat", None),
        ("", None),
    ],
)
def test_login_from_url(url, expected):
    assert github.login_from_url(url) == expected


def test_resolve_uses_strong_keys():
    assert github.resolve({"github_login": "octocat"}) == "octocat"
    assert github.resolve({"blog": "https://github.com/octocat"}) == "octocat"


def test_resolve_never_guesses_from_a_name():
    # A wrong resolve attributes someone else's work to a candidate. There must be no
    # code path from a display name to a login.
    assert github.resolve({"display_name": "Rohit Sharma", "name": "Rohit Sharma"}) is None
    assert github.resolve({}) is None


# --- repo filtering ----------------------------------------------------------


def test_org_accounts_produce_no_evidence():
    assert github.rows_from_collected(load("collected_org")) == []


def test_fork_without_commits_produces_no_skill_evidence(collected):
    rows = github.rows_from_collected(collected)
    # The forked linux repo has 900MB of C and would otherwise dominate everything.
    assert not any(r.claim_key == "C" for r in rows)
    assert not any("linux" in r.source_url for r in rows)


def test_markdown_only_repo_is_not_skill_evidence(collected):
    rows = github.rows_from_collected(collected)
    assert not any("pg-tuning-notes" in r.source_url for r in rows)
    assert not any((r.claim_key or "").lower() == "markdown" for r in rows)


def test_trivial_language_share_is_ignored(collected):
    # 900 bytes of Rust in a 200k Go repo is a vendored file, not a skill.
    rows = github.rows_from_collected(collected)
    assert not any(r.claim_key == "Rust" for r in rows)
    assert any(r.claim_key == "Go" for r in rows)


def test_unknown_language_is_not_invented_as_a_skill(db_conn, collected):
    rows = github.rows_from_collected(collected)
    assert not any((r.claim_key or "").lower() == "blorpscript" for r in rows)


# --- evidence shape ----------------------------------------------------------


def test_skill_evidence_is_artifact_backed_with_a_real_source(db_conn, collected):
    rows = github.rows_from_collected(collected)
    python = [r for r in rows if r.claim_key == "Python"]
    assert python
    for r in python:
        assert r.tier == "artifact_backed"
        assert r.source_url.startswith("https://github.com/rohitdev/")
        assert r.snippet and "Python" in r.snippet
        assert r.value_num and r.value_num > 0


def test_location_and_availability_are_self_reported(db_conn, collected):
    rows = github.rows_from_collected(collected)

    location = next(r for r in rows if r.claim_type == "location")
    assert location.tier == "self_reported"
    assert location.claim_key == "IN-KA-BLR"

    availability = next(r for r in rows if r.claim_type == "availability")
    assert availability.tier == "self_reported"


def test_no_availability_row_when_not_hireable(db_conn, collected):
    collected["user"]["hireable"] = None
    rows = github.rows_from_collected(collected)
    assert not any(r.claim_type == "availability" for r in rows)


def test_unmappable_location_is_kept_but_claims_no_region(db_conn, collected):
    # "localhost" is what someone actually wrote, so it is real evidence — but it must
    # not become a region claim. The gate decides what an unmappable string means;
    # dropping it here would make it indistinguishable from having no location.
    collected["user"]["location"] = "localhost"
    rows = github.rows_from_collected(collected)

    location = next(r for r in rows if r.claim_type == "location")
    assert location.claim_key is None
    assert location.claim_value == "localhost"


def test_missing_location_produces_no_location_row(db_conn, collected):
    collected["user"]["location"] = ""
    rows = github.rows_from_collected(collected)
    assert not any(r.claim_type == "location" for r in rows)


def test_rows_write_through_the_evidence_chokepoint(db_conn, collected):
    candidate_id = uuid.uuid4()
    db_conn.execute(
        "insert into candidate (id, display_name) values (%s, %s)", (candidate_id, "Rohit")
    )
    rows = github.rows_from_collected(collected)

    # source_text=None: these snippets are synthesised from API numbers, not quoted prose.
    result = write_evidence(candidate_id, rows, source_text=None)
    assert result.written == len(rows)
    assert result.rejected_banned == 0

    tiers = db_conn.execute(
        "select distinct tier from evidence where candidate_id = %s", (candidate_id,)
    ).fetchall()
    assert ("artifact_backed",) in tiers


def test_collect_is_idempotent_through_the_writer(db_conn, collected):
    candidate_id = uuid.uuid4()
    db_conn.execute("insert into candidate (id) values (%s)", (candidate_id,))
    rows = github.rows_from_collected(collected)

    write_evidence(candidate_id, rows, source_text=None)
    write_evidence(candidate_id, rows, source_text=None)

    count = db_conn.execute(
        "select count(*) from evidence where candidate_id = %s", (candidate_id,)
    ).fetchone()[0]
    assert count == len(rows)


# --- plan (supplementary, off by default) ------------------------------------


def test_plan_builds_user_search_queries(db_conn):
    spec = RoleSpec(must_have_skills=["Python"], locations=["IN-KA-BLR"])
    queries = github.plan(spec)
    assert all(q.startswith("type:user") for q in queries)
    assert any("language:Python" in q for q in queries)
    assert any('location:"Bengaluru"' in q for q in queries)
