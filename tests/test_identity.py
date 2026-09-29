import uuid

import pytest

from hi import identity
from hi.extract import EvidenceRow, write_evidence


def make_candidate(conn, name="Someone") -> uuid.UUID:
    cid = uuid.uuid4()
    conn.execute("insert into candidate (id, display_name) values (%s, %s)", (cid, name))
    return cid


def add_key(conn, candidate_id, kind, value):
    conn.execute(
        "insert into identity (candidate_id, kind, value, first_seen, last_seen) "
        "values (%s, %s, %s, now(), now())",
        (candidate_id, kind, value),
    )


def add_evidence(candidate_id, *, claim_key, source_url):
    write_evidence(
        candidate_id,
        [
            EvidenceRow(
                claim_type="skill",
                claim_key=claim_key,
                claim_value=claim_key,
                tier="artifact_backed",
                source_url=source_url,
                snippet=f"{claim_key}: 1,000 bytes",
                extractor="test",
                extractor_version="test@1",
            )
        ],
        source_text=None,
    )


# --- resolve -----------------------------------------------------------------


def test_zero_matches_creates_a_candidate(db_conn):
    result = identity.resolve({"github_login": "octocat"}, display_name="Octo Cat")

    assert result.created is True
    assert result.candidate_id is not None
    assert identity.strong_keys_for(result.candidate_id) == {"github_login": "octocat"}


def test_single_match_attaches_instead_of_creating(db_conn):
    first = identity.resolve({"github_login": "octocat"})
    second = identity.resolve({"github_login": "octocat"})

    assert second.created is False
    assert second.candidate_id == first.candidate_id
    count = db_conn.execute("select count(*) from candidate").fetchone()[0]
    assert count == 1


def test_new_key_is_attached_to_the_matched_candidate(db_conn):
    first = identity.resolve({"github_login": "octocat"})
    identity.resolve({"github_login": "octocat", "personal_domain": "octo.dev"})

    assert identity.strong_keys_for(first.candidate_id) == {
        "github_login": "octocat",
        "personal_domain": "octo.dev",
    }


def test_multi_match_merges_nothing_and_opens_a_review(db_conn):
    a = make_candidate(db_conn, "A")
    b = make_candidate(db_conn, "B")
    add_key(db_conn, a, "github_login", "octocat")
    add_key(db_conn, b, "personal_domain", "octo.dev")

    result = identity.resolve({"github_login": "octocat", "personal_domain": "octo.dev"})

    assert result.candidate_id is None
    assert result.ambiguous is True
    assert set(result.matched) == {a, b}
    assert result.review_id is not None

    # Both candidates survive untouched — a false merge is worse than a duplicate.
    assert db_conn.execute("select count(*) from candidate").fetchone()[0] == 2
    assert identity.strong_keys_for(a) == {"github_login": "octocat"}
    assert identity.strong_keys_for(b) == {"personal_domain": "octo.dev"}
    assert len(identity.open_reviews()) == 1


def test_weak_signals_never_match(db_conn):
    identity.resolve({"github_login": "octocat"}, display_name="Rohit Sharma")
    # Same name, same city, no strong key: must not resolve to the existing person.
    result = identity.resolve(
        {"display_name": "Rohit Sharma", "city": "Bengaluru"}, display_name="Rohit Sharma"
    )
    assert result.candidate_id is None
    assert result.created is False


def test_unknown_key_kinds_are_ignored(db_conn):
    result = identity.resolve({"favourite_colour": "blue"})
    assert result.candidate_id is None


# --- merge requires a decider ------------------------------------------------


def test_merge_without_decided_by_is_refused(db_conn):
    a, b = make_candidate(db_conn, "A"), make_candidate(db_conn, "B")
    with pytest.raises(ValueError, match="decided_by"):
        identity.merge(a, b, decided_by="")


def test_merge_into_self_is_refused(db_conn):
    a = make_candidate(db_conn, "A")
    with pytest.raises(ValueError, match="itself"):
        identity.merge(a, a, decided_by="human")


def test_propose_merge_changes_nothing(db_conn):
    a, b = make_candidate(db_conn, "A"), make_candidate(db_conn, "B")
    add_key(db_conn, a, "github_login", "octocat")

    identity.propose_merge(a, b, "same avatar hash", {"avatar": "abc"})

    assert identity.strong_keys_for(a) == {"github_login": "octocat"}
    assert identity.strong_keys_for(b) == {}
    assert db_conn.execute("select count(*) from identity_merge").fetchone()[0] == 0


# --- merge / unmerge round trip ----------------------------------------------


def test_merge_moves_rows_and_archives_the_source(db_conn):
    a, b = make_candidate(db_conn, "A"), make_candidate(db_conn, "B")
    add_key(db_conn, a, "github_login", "octocat")
    add_evidence(a, claim_key="Python", source_url="https://github.com/octocat/x")

    identity.merge(a, b, decided_by="ayushmaan", reason="same person")

    assert identity.strong_keys_for(b) == {"github_login": "octocat"}
    assert identity.strong_keys_for(a) == {}
    assert db_conn.execute(
        "select count(*) from evidence where candidate_id = %s", (b,)
    ).fetchone()[0] == 1
    assert db_conn.execute("select status from candidate where id = %s", (a,)).fetchone()[0] == (
        "archived"
    )


def test_merge_then_unmerge_restores_the_prior_state(db_conn):
    a, b = make_candidate(db_conn, "A"), make_candidate(db_conn, "B")
    add_key(db_conn, a, "github_login", "octocat")
    add_evidence(a, claim_key="Python", source_url="https://github.com/octocat/x")
    add_key(db_conn, b, "personal_domain", "octo.dev")

    before_a = identity.strong_keys_for(a)
    before_b = identity.strong_keys_for(b)

    merge_id = identity.merge(a, b, decided_by="ayushmaan")
    identity.unmerge(merge_id, reversed_by="ayushmaan")

    assert identity.strong_keys_for(a) == before_a
    assert identity.strong_keys_for(b) == before_b
    assert db_conn.execute(
        "select count(*) from evidence where candidate_id = %s", (a,)
    ).fetchone()[0] == 1
    assert db_conn.execute("select status from candidate where id = %s", (a,)).fetchone()[0] == (
        "active"
    )


def test_colliding_evidence_stays_behind_so_unmerge_still_works(db_conn):
    a, b = make_candidate(db_conn, "A"), make_candidate(db_conn, "B")
    # Same claim, same source, on both candidates: moving it would violate the
    # uniqueness index, so it must stay put rather than be deleted.
    add_evidence(a, claim_key="Python", source_url="https://github.com/octocat/x")
    add_evidence(b, claim_key="Python", source_url="https://github.com/octocat/x")

    merge_id = identity.merge(a, b, decided_by="ayushmaan")
    assert db_conn.execute(
        "select count(*) from evidence where candidate_id = %s", (a,)
    ).fetchone()[0] == 1

    identity.unmerge(merge_id, reversed_by="ayushmaan")
    assert db_conn.execute(
        "select count(*) from evidence where candidate_id = %s", (a,)
    ).fetchone()[0] == 1


def test_unmerge_twice_is_refused(db_conn):
    a, b = make_candidate(db_conn, "A"), make_candidate(db_conn, "B")
    merge_id = identity.merge(a, b, decided_by="ayushmaan")
    identity.unmerge(merge_id, reversed_by="ayushmaan")
    with pytest.raises(ValueError, match="already reversed"):
        identity.unmerge(merge_id, reversed_by="ayushmaan")


def test_every_merge_records_a_decider(db_conn):
    a, b = make_candidate(db_conn, "A"), make_candidate(db_conn, "B")
    identity.merge(a, b, decided_by="ayushmaan")
    rows = db_conn.execute("select decided_by from identity_merge").fetchall()
    assert rows and all(r[0] for r in rows)
