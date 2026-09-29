import uuid

import psycopg
import pytest


def _make_candidate(conn) -> uuid.UUID:
    candidate_id = uuid.uuid4()
    conn.execute(
        "insert into candidate (id, display_name) values (%s, %s)",
        (candidate_id, "Test Candidate"),
    )
    return candidate_id


def _insert_evidence(conn, candidate_id, snippet: str) -> None:
    conn.execute(
        "insert into evidence "
        "(candidate_id, claim_type, tier, source_url, snippet, observed_at, extractor, extractor_version) "
        "values (%s, 'skill', 'self_reported', 'https://example.com', %s, now(), 'test', 'test@1')",
        (candidate_id, snippet),
    )


def test_deleting_candidate_cascades_evidence(db_conn):
    candidate_id = _make_candidate(db_conn)
    _insert_evidence(db_conn, candidate_id, "knows python")

    db_conn.execute("delete from candidate where id = %s", (candidate_id,))

    remaining = db_conn.execute(
        "select count(*) from evidence where candidate_id = %s", (candidate_id,)
    ).fetchone()[0]
    assert remaining == 0


def test_evidence_rejects_empty_snippet(db_conn):
    candidate_id = _make_candidate(db_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_evidence(db_conn, candidate_id, "")


def test_identity_rejects_duplicate_kind_value(db_conn):
    candidate_a = _make_candidate(db_conn)
    candidate_b = _make_candidate(db_conn)
    db_conn.execute(
        "insert into identity (candidate_id, kind, value, first_seen, last_seen) "
        "values (%s, 'github_login', 'octocat', now(), now())",
        (candidate_a,),
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        db_conn.execute(
            "insert into identity (candidate_id, kind, value, first_seen, last_seen) "
            "values (%s, 'github_login', 'octocat', now(), now())",
            (candidate_b,),
        )


def test_candidate_ref_rejects_failed_gate_without_reason(db_conn):
    role_id = uuid.uuid4()
    db_conn.execute(
        "insert into role (id, spec_json) values (%s, '{}'::jsonb)", (role_id,)
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "insert into candidate_ref "
            "(role_id, adapter, ref_kind, ref_value, snippet_raw, source_url, gate_state) "
            "values (%s, 'linkedin_serp', 'linkedin_url', 'linkedin.com/in/x', 'raw', 'https://x', 'failed')",
            (role_id,),
        )
