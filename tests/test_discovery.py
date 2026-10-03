import json
from pathlib import Path

import pytest

from hi import discovery
from hi.adapters import linkedin_serp as ls
from hi.models import RoleSpec, Seniority

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin_serp"

SPEC = RoleSpec(
    titles=["Backend Engineer"],
    must_have_skills=["Python"],
    locations=["IN-KA-BLR"],
    seniority=Seniority(min_years=4, max_years=8),
)


@pytest.fixture()
def refs():
    payload = json.loads((FIXTURES / "bangalore_backend.json").read_text(encoding="utf-8"))
    return ls.refs_from_serpapi(payload, query_text="q")


@pytest.fixture()
def role_id(db_conn):
    return discovery.create_role("Backend Engineer", SPEC)


def test_create_and_read_role(db_conn, role_id):
    title, spec = discovery.get_role(role_id)
    assert title == "Backend Engineer"
    assert spec.titles == ["Backend Engineer"]
    assert spec.must_have_skills == ["Python"]


def test_persist_then_read_back_without_network(db_conn, role_id, refs):
    counts = discovery.persist_refs(role_id, refs, SPEC)
    assert counts["passed"] + counts["failed"] == len(refs)

    saved = discovery.saved_refs(role_id)
    assert len(saved) == len(refs)
    assert all(row["gate_state"] in {"passed", "failed"} for row in saved)


def test_persist_is_idempotent(db_conn, role_id, refs):
    # Re-running a role must not duplicate people — this is what makes a retry free.
    discovery.persist_refs(role_id, refs, SPEC)
    first = discovery.saved_refs(role_id)
    discovery.persist_refs(role_id, refs, SPEC)
    second = discovery.saved_refs(role_id)

    assert len(first) == len(second)


def test_every_failed_row_has_a_reason(db_conn, role_id, refs):
    discovery.persist_refs(role_id, refs, SPEC)
    failed = discovery.saved_refs(role_id, gate_state="failed")
    assert failed, "fixture should contain at least one gate failure"
    assert all(row["gate_reason"] for row in failed)


def test_filter_by_gate_state(db_conn, role_id, refs):
    discovery.persist_refs(role_id, refs, SPEC)
    passed = discovery.saved_refs(role_id, gate_state="passed")
    assert passed
    assert all(row["gate_state"] == "passed" for row in passed)
    assert discovery.role_counts(role_id)["passed"] == len(passed)


def test_structured_fields_survive_a_db_roundtrip(db_conn, role_id, refs):
    discovery.persist_refs(role_id, refs, SPEC)
    row = next(
        r for r in discovery.saved_refs(role_id) if r["ref_value"].endswith("priya-nair-99887766")
    )
    rebuilt = discovery.ref_from_row(row)

    assert rebuilt.snippet_title == "Backend Engineer"
    assert rebuilt.snippet_employer == "Swiggy"
    assert rebuilt.snippet_location == "Bengaluru, Karnataka, India"


def test_gate_verdicts_are_stable_across_a_db_roundtrip(db_conn, role_id, refs):
    """Re-gating stored refs must reproduce the same verdicts, not drift.

    A fresh SERP ref and a ref rebuilt from `snippet_raw` have to gate identically,
    or a role silently changes shape every time it is re-run.
    """
    discovery.persist_refs(role_id, refs, SPEC)
    first = discovery.role_counts(role_id)

    for _ in range(3):
        rebuilt = [discovery.ref_from_row(r) for r in discovery.saved_refs(role_id)]
        discovery.persist_refs(role_id, rebuilt, SPEC)
        assert discovery.role_counts(role_id) == first


def test_regating_from_db_needs_no_search(db_conn, role_id, refs):
    """A spec edit can be re-applied to stored refs without spending SERP budget."""
    discovery.persist_refs(role_id, refs, SPEC)

    widened = SPEC.model_copy(update={"titles": ["Product Designer"]})
    rebuilt = [discovery.ref_from_row(r) for r in discovery.saved_refs(role_id)]
    discovery.persist_refs(role_id, rebuilt, widened)

    designers = [
        r for r in discovery.saved_refs(role_id, gate_state="passed")
        if r["ref_value"].endswith("sneha-gupta-design")
    ]
    assert designers, "the designer should pass once the spec asks for designers"


def _ref(slug, headline, location="Bengaluru, Karnataka, India"):
    """A ref shaped like a real SERP hit. `snippet_raw` has to carry the headline and
    location, because `ref_from_row` rebuilds the gate-relevant fields from it and
    ignores the display columns — a re-gate sees only what is in here."""
    from hi.models import CandidateRef

    raw = json.dumps({
        "title": f"{slug} - {headline} | LinkedIn",
        "extensions": [location, headline],
    })
    return CandidateRef(
        adapter="linkedin_serp", ref_kind="linkedin_url", ref_value=f"linkedin.com/in/{slug}",
        source_url=f"https://linkedin.com/in/{slug}", snippet_raw=raw, snippet_name=slug,
        snippet_headline=headline, snippet_title=headline, snippet_location=location,
    )


def test_a_gate_that_refuses_most_of_the_role_stops_being_trusted(db_conn):
    """The title test threw away 39 of 40 on a live business role: the people who do the
    job write "BizOps", not the posted title. Under half kept, the refusals come back —
    but only the ones in India."""
    role = discovery.create_role("Ops Manager", SPEC)
    discovery.persist_refs(role, [
        _ref("match", "Backend Engineer"),
        _ref("bizops", "BizOps Lead"),
        _ref("cos", "Chief of Staff"),
        _ref("abroad", "Chief of Staff", location="Seattle, Washington, United States"),
    ], SPEC)
    assert discovery.role_counts(role).get("passed", 0) == 1  # only the literal title match

    discovery.regate(role, SPEC)

    states = {r["ref_value"].split("/")[-1]: r["gate_state"] for r in discovery.saved_refs(role)}
    assert states == {"match": "passed", "bizops": "passed", "cos": "passed", "abroad": "failed"}
    admitted = next(r for r in discovery.saved_refs(role) if r["ref_value"].endswith("bizops"))
    assert "not being trusted" in admitted["gate_reason"]


def test_the_rescue_settles_instead_of_flip_flopping(db_conn):
    """Re-running must not demote the people it just admitted and re-admit them next
    time, reporting the churn as people who moved."""
    role = discovery.create_role("Ops Manager", SPEC)
    discovery.persist_refs(role, [
        _ref("match", "Backend Engineer"), _ref("bizops", "BizOps Lead"),
        _ref("cos", "Chief of Staff"), _ref("ops", "Operations Lead"),
    ], SPEC)
    discovery.regate(role, SPEC)
    assert discovery.regate(role, SPEC) == []


def test_a_healthy_role_is_left_alone(db_conn):
    """The rescue is for a gate that is failing, not a licence to widen every role."""
    role = discovery.create_role("Backend Engineer", SPEC)
    discovery.persist_refs(role, [
        _ref("a", "Backend Engineer"), _ref("b", "Backend Developer"),
        _ref("c", "Backend SDE"), _ref("nope", "Product Designer"),
    ], SPEC)
    discovery.regate(role, SPEC)
    states = {r["ref_value"].split("/")[-1]: r["gate_state"] for r in discovery.saved_refs(role)}
    assert states["nope"] == "failed"


def test_manual_filing_survives_a_regate_and_known_people_feed_new_roles(db_conn):
    """A ref filed by hand keeps its verdict, and a later role picks up people other
    roles already found — any source — when their snippet passes its gate."""
    old = discovery.create_role("Earlier", SPEC)
    with discovery.pool.connection() as conn:
        for adapter, slug, title, state, reason in [
            ("manual", "dev", "Backend Engineer", "passed", None),
            ("linkedin_serp", "serp", "Backend Developer", "passed", None),
            ("manual", "pm", "Product Manager", "passed", None),
            ("manual", "kid", "Backend Engineer Intern", "failed", "seniority: junior"),
        ]:
            raw = json.dumps({"title": f"{slug} - {title} | LinkedIn", "extensions": []})
            conn.execute(
                "insert into candidate_ref (role_id, adapter, ref_kind, ref_value, snippet_name, "
                "snippet_raw, source_url, gate_state, gate_reason) "
                "values (%s, %s, 'linkedin_url', %s, %s, %s, %s, %s, %s)",
                (old, adapter, f"linkedin.com/in/{slug}", slug, raw,
                 f"https://linkedin.com/in/{slug}", state, reason),
            )
    kid = next(r for r in discovery.saved_refs(old) if r["ref_value"].endswith("kid"))
    discovery.regate(old, SPEC)
    assert next(r for r in discovery.saved_refs(old) if r["ref_value"].endswith("kid")) == kid

    new = discovery.create_role("Later", SPEC)
    found = discovery.known_candidates(SPEC, new)
    assert sorted(r.ref_value for r in found) == ["linkedin.com/in/dev", "linkedin.com/in/serp"]
    assert all(r.adapter == "local_db" for r in found)
    assert discovery.known_candidates(SPEC, old) == []  # already on that role


def test_a_person_read_for_one_role_is_linked_on_every_role(db_conn):
    """A ref is per role, the person is not. Read once, linked everywhere — before and after."""
    from hi import identity

    before = discovery.create_role("Ops Manager", SPEC)
    after = discovery.create_role("BD Manager", SPEC)
    discovery.persist_refs(before, [_ref("jayasimhabr", "Business Development")], SPEC)

    # Enrichment resolves the slug on whichever role it ran for...
    cid = identity.resolve({"linkedin_slug": "jayasimhabr"}).candidate_id
    assert [r["candidate_id"] for r in discovery.saved_refs(before)] == [cid]

    # ...and a role that finds them later gets them already linked.
    discovery.persist_refs(after, [_ref("jayasimhabr", "Business Development")], SPEC)
    assert [r["candidate_id"] for r in discovery.saved_refs(after)] == [cid]
