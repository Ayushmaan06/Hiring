import uuid

import pytest

from hi.extract import EvidenceRow, quarantine, write_evidence

SOURCE = (
    "Rohit has been writing Python since 2016 and maintains three PostgreSQL "
    "extensions. He graduated from IIT Bombay in 2014."
)


@pytest.fixture()
def candidate_id(db_conn):
    cid = uuid.uuid4()
    db_conn.execute("insert into candidate (id, display_name) values (%s, %s)", (cid, "Rohit"))
    return cid


def row(**kw) -> EvidenceRow:
    base = dict(
        claim_type="skill",
        claim_key="Python",
        claim_value="Python",
        tier="artifact_backed",
        source_url="https://example.com/rohit",
        snippet="writing Python since 2016",
        extractor="test",
        extractor_version="test@1",
    )
    base.update(kw)
    return EvidenceRow(**base)


def stored(conn, candidate_id) -> list[tuple]:
    return conn.execute(
        "select claim_type, claim_key, tier, snippet from evidence where candidate_id = %s",
        (candidate_id,),
    ).fetchall()


# --- the anti-fabrication rule ----------------------------------------------


def test_quotable_snippet_is_written(db_conn, candidate_id):
    result = write_evidence(candidate_id, [row()], source_text=SOURCE)
    assert result.written == 1
    assert len(stored(db_conn, candidate_id)) == 1


def test_fabricated_snippet_is_dropped(db_conn, candidate_id):
    # The claim may even be true; if we cannot quote the source, there is no evidence.
    result = write_evidence(
        candidate_id, [row(snippet="ten years of Rust at Google")], source_text=SOURCE
    )
    assert result.written == 0
    assert result.dropped_unquotable == 1
    assert stored(db_conn, candidate_id) == []


def test_whitespace_reflowed_snippet_is_accepted(db_conn, candidate_id):
    # Models and HTML-to-text reflow whitespace; that is not fabrication.
    result = write_evidence(
        candidate_id, [row(snippet="writing   Python\n  since 2016")], source_text=SOURCE
    )
    assert result.written == 1
    # Stored verbatim, not normalised — the recruiter reads this string.
    assert stored(db_conn, candidate_id)[0][3] == "writing   Python\n  since 2016"


def test_one_bad_row_does_not_block_the_good_ones(db_conn, candidate_id):
    result = write_evidence(
        candidate_id,
        [row(), row(claim_key="Rust", snippet="invented claim about Rust")],
        source_text=SOURCE,
    )
    assert result.written == 1
    assert result.dropped_unquotable == 1


def test_no_source_text_skips_the_check(db_conn, candidate_id):
    # Structured API responses synthesise snippets from JSON rather than quoting prose.
    result = write_evidence(
        candidate_id, [row(snippet="Python — 14 repositories, 2.1k commits")], source_text=None
    )
    assert result.written == 1


# --- banned signals ----------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"claim_key": "graduation_year", "snippet": "graduated from IIT Bombay in 2014"},
        {"claim_key": "college_name", "snippet": "graduated from IIT Bombay in 2014"},
        {"claim_key": "age", "snippet": "graduated from IIT Bombay in 2014"},
        {"claim_key": "gender", "snippet": "graduated from IIT Bombay in 2014"},
        {"claim_key": "photo", "snippet": "graduated from IIT Bombay in 2014"},
        {"claim_key": "caste", "snippet": "graduated from IIT Bombay in 2014"},
    ],
)
def test_banned_signals_are_refused_even_when_quotable(db_conn, candidate_id, kw):
    # Every snippet here IS present in the source, so only the ban stops them. This is
    # the easiest rule in the system to violate by accident.
    result = write_evidence(candidate_id, [row(**kw)], source_text=SOURCE)
    assert result.written == 0
    assert result.rejected_banned == 1
    assert stored(db_conn, candidate_id) == []


def test_banned_rejection_survives_spacing_and_case(db_conn, candidate_id):
    result = write_evidence(
        candidate_id,
        [row(claim_key="Graduation Year", snippet="graduated from IIT Bombay in 2014")],
        source_text=SOURCE,
    )
    assert result.rejected_banned == 1


# --- validity ----------------------------------------------------------------


def test_empty_snippet_is_rejected(db_conn, candidate_id):
    assert write_evidence(candidate_id, [row(snippet="  ")], source_text=SOURCE).rejected_invalid == 1


def test_unknown_tier_is_rejected(db_conn, candidate_id):
    result = write_evidence(candidate_id, [row(tier="probably_true")], source_text=SOURCE)
    assert result.rejected_invalid == 1


# --- idempotency -------------------------------------------------------------


def test_same_claim_from_same_source_upserts(db_conn, candidate_id):
    write_evidence(candidate_id, [row()], source_text=SOURCE)
    first = db_conn.execute(
        "select observed_at from evidence where candidate_id = %s", (candidate_id,)
    ).fetchone()[0]

    write_evidence(candidate_id, [row()], source_text=SOURCE)
    rows = db_conn.execute(
        "select observed_at from evidence where candidate_id = %s", (candidate_id,)
    ).fetchall()

    # Re-observing must not inflate the row count — skill_depth reads these.
    assert len(rows) == 1
    assert rows[0][0] >= first


def test_same_claim_from_a_different_source_is_a_second_row(db_conn, candidate_id):
    write_evidence(candidate_id, [row()], source_text=SOURCE)
    write_evidence(
        candidate_id, [row(source_url="https://other.example/rohit")], source_text=SOURCE
    )
    assert len(stored(db_conn, candidate_id)) == 2


def test_null_claim_key_still_dedups(db_conn, candidate_id):
    # Postgres treats nulls as distinct by default, which would defeat the constraint.
    r = row(claim_type="availability", claim_key=None, snippet="Rohit has been writing Python")
    write_evidence(candidate_id, [r], source_text=SOURCE)
    write_evidence(candidate_id, [r], source_text=SOURCE)
    assert len(stored(db_conn, candidate_id)) == 1


# --- erasure and quarantine --------------------------------------------------


def test_evidence_is_erased_with_the_candidate(db_conn, candidate_id):
    write_evidence(candidate_id, [row()], source_text=SOURCE)
    db_conn.execute("delete from candidate where id = %s", (candidate_id,))
    assert stored(db_conn, candidate_id) == []


def test_quarantine_records_a_whole_document_failure(db_conn, candidate_id):
    quarantine(
        extractor="test",
        extractor_version="test@1",
        reason="malformed response",
        candidate_id=candidate_id,
        source_url="https://example.com/rohit",
        raw_response="{not json",
    )
    rows = db_conn.execute(
        "select reason, raw_response from extraction_failure where candidate_id = %s",
        (candidate_id,),
    ).fetchall()
    assert rows == [("malformed response", "{not json")]


def test_education_is_stored_now_but_the_age_proxy_still_is_not(db_conn, candidate_id):
    """ARCHITECTURE.md §2.2, 2026-08-27: institution is storable, a graduation year is not.

    The decision was about identifying IIT/IIM candidates, not about dating people, and
    age is banned independently of it.
    """
    stored_ok = write_evidence(
        candidate_id,
        [row(claim_type="education", claim_key="IIT Bombay", snippet="graduated from IIT Bombay")],
        source_text=SOURCE,
    )
    assert stored_ok.written == 1

    refused = write_evidence(
        candidate_id,
        [row(claim_type="education", claim_key="graduation_year", claim_value="2014",
             snippet="graduated from IIT Bombay in 2014")],
        source_text=SOURCE,
    )
    assert refused.written == 0
    assert refused.rejected_banned == 1
