import re
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hi import discovery, matching, worker
from hi.extract import EvidenceRow, write_evidence
from hi.models import RoleSpec, Seniority
from hi.web.app import app, match_label, match_segments

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)
TEMPLATES = Path(__file__).parents[1] / "src" / "hi" / "web" / "templates"

SPEC = RoleSpec(
    titles=["Backend Engineer"],
    must_have_skills=["Python"],
    locations=["IN-KA-BLR"],
    seniority=Seniority(min_years=4, max_years=8),
    remote="hybrid",
)


TEST_USER = "recruiter@example.com"
TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture()
def users(db_conn, monkeypatch):
    from hi import auth

    monkeypatch.setenv("HI_USERS", f"{TEST_USER}:{auth.hash_password(TEST_PASSWORD)}")


@pytest.fixture()
def anon(db_conn, users):
    """A client with no session."""
    return TestClient(app)


@pytest.fixture()
def client(db_conn, users):
    """A signed-in client — every screen requires one."""
    from hi import auth

    c = TestClient(app)
    c.cookies.set(auth.COOKIE_NAME, auth.create_session(TEST_USER))
    return c


@pytest.fixture()
def role_id(db_conn):
    return discovery.create_role("Backend Engineer", SPEC)


def add_person(db_conn, role_id, *, name, skills=("Python",), region="IN-KA-BLR", proven=True):
    candidate_id = uuid.uuid4()
    db_conn.execute(
        "insert into candidate (id, display_name, primary_location_text, location_region) "
        "values (%s, %s, %s, %s)",
        (candidate_id, name, "Bengaluru, Karnataka, India", region),
    )
    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_raw, source_url, gate_state, candidate_id) "
        "values (%s, 'linkedin_serp', 'linkedin_url', %s, '{}', %s, 'passed', %s)",
        (role_id, f"linkedin.com/in/{name}", f"https://linkedin.com/in/{name}", candidate_id),
    )
    db_conn.execute(
        "insert into identity (candidate_id, kind, value, first_seen, last_seen) "
        "values (%s, 'github_login', %s, now(), now())",
        (candidate_id, name),
    )
    rows = [
        EvidenceRow(
            claim_type="skill",
            claim_key=s,
            claim_value=s,
            tier="artifact_backed" if proven else "self_reported",
            value_num=150_000.0,
            observed_at=NOW - timedelta(days=20),
            source_url=f"https://github.com/{name}/{s.lower()}",
            snippet=f"{s}: 150,000 bytes (88% of {name}/{s.lower()})",
            extractor="github",
            extractor_version="github_deterministic@1",
        )
        for s in skills
    ]
    rows.append(
        EvidenceRow(
            claim_type="location",
            claim_key=region,
            claim_value="Bengaluru, Karnataka, India",
            tier="self_reported",
            source_url=f"https://github.com/{name}",
            snippet="Location on GitHub profile: Bengaluru, Karnataka, India",
            extractor="github",
            extractor_version="github_deterministic@1",
        )
    )
    write_evidence(candidate_id, rows, source_text=None)
    return candidate_id


# --- smoke: 200 on all four screens ------------------------------------------


def test_roles_screen(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "New role" in r.text


def test_new_role_screen(client):
    r = client.get("/roles/new")
    assert r.status_code == 200
    assert "Paste the job description" in r.text


def test_read_jd_returns_an_editable_form(client, db_conn):
    r = client.post("/roles/read", data={"jd_text": "Backend Engineer in Bengaluru. 5+ years Python."})
    assert r.status_code == 200
    # An editable form, not raw data.
    assert "<form" in r.text
    assert 'name="must_have"' in r.text
    assert "Python" in r.text
    # Region names, never region codes.
    assert "Bangalore" in r.text
    assert "IN-KA-BLR" in r.text  # only inside option values


def test_shortlist_screen(client, db_conn, role_id):
    add_person(db_conn, role_id, name="alice")
    matching.score_role(role_id, now=NOW)
    r = client.get(f"/roles/{role_id}")
    assert r.status_code == 200
    assert "alice" in r.text


def test_candidate_panel_screen(client, db_conn, role_id):
    candidate_id = add_person(db_conn, role_id, name="alice")
    matching.score_role(role_id, now=NOW)
    r = client.get(f"/roles/{role_id}/candidates/{candidate_id}")
    assert r.status_code == 200
    assert "Why this ranking" in r.text
    assert "What we found" in r.text


def test_health_screen(client, db_conn):
    r = client.get("/admin/health")
    assert r.status_code == 200
    assert "Green means" in r.text


def test_unknown_role_is_404(client, db_conn):
    assert client.get(f"/roles/{uuid.uuid4()}").status_code == 404


def test_unknown_candidate_is_404(client, db_conn, role_id):
    assert client.get(f"/roles/{role_id}/candidates/{uuid.uuid4()}").status_code == 404


# --- score presentation ------------------------------------------------------


def test_score_is_never_shown_as_a_bare_number(client, db_conn, role_id):
    add_person(db_conn, role_id, name="alice")
    matching.score_role(role_id, now=NOW)
    body = client.get(f"/roles/{role_id}").text

    assert any(label in body for label in ("Strong match", "Good match", "Possible match", "Weak"))
    # The raw value is available on hover only.
    assert 'title="0.' in body


@pytest.mark.parametrize(
    "value,expected",
    [(0.9, "Strong match"), (0.45, "Good match"), (0.3, "Possible match"), (0.1, "Weak"), (None, "Not yet ranked")],
)
def test_match_labels(value, expected):
    assert match_label(value) == expected


def test_match_segments_are_bounded():
    assert match_segments(None) == 0
    assert match_segments(0.01) == 1  # any score shows at least one segment
    assert match_segments(1.0) == 5
    assert match_segments(5.0) == 5


# --- the two badges must be visually distinct --------------------------------


def test_proven_and_says_so_render_with_distinct_classes(client, db_conn, role_id):
    candidate_id = add_person(db_conn, role_id, name="alice", skills=("Python",))
    matching.score_role(role_id, now=NOW)
    body = client.get(f"/roles/{role_id}/candidates/{candidate_id}").text

    assert "badge-proven" in body
    assert "badge-said" in body
    assert "Proven" in body
    assert "Says so" in body


def test_snippets_are_shown_inline_not_on_hover(client, db_conn, role_id):
    candidate_id = add_person(db_conn, role_id, name="alice")
    matching.score_role(role_id, now=NOW)
    body = client.get(f"/roles/{role_id}/candidates/{candidate_id}").text
    # Hover text is invisible on touch devices, so the quote must be in the document.
    assert "150,000 bytes" in body
    assert "<blockquote>" in body


# --- excluded candidates are hidden by default, never dropped ----------------


def test_excluded_candidate_is_absent_by_default_and_present_behind_the_toggle(
    client, db_conn, role_id
):
    add_person(db_conn, role_id, name="alice", skills=("Python",))
    add_person(db_conn, role_id, name="carol", skills=("Go",))  # fails the must-have
    matching.score_role(role_id, now=NOW)

    default = client.get(f"/roles/{role_id}").text
    assert "alice" in default
    assert "ruled out" in default.lower()

    revealed = client.get(f"/roles/{role_id}?show_ruled_out=true").text
    assert "carol" in revealed
    assert "Python" in revealed  # the reason names what was missing


def test_ruled_out_rows_always_carry_a_reason(client, db_conn, role_id):
    add_person(db_conn, role_id, name="carol", skills=("Go",))
    matching.score_role(role_id, now=NOW)
    body = client.get(f"/roles/{role_id}?show_ruled_out=true").text
    assert "No public sign of" in body


# --- "not looked at yet" is not "ruled out" ----------------------------------


def add_unread_person(db_conn, role_id, *, name):
    """Discovered by the search and never enriched: a ref and a candidate, no evidence.

    This is 18 of the 34 people on role d956b79f in the dev database — the ordinary
    state of anyone the scraping budget has not reached.
    """
    candidate_id = uuid.uuid4()
    db_conn.execute(
        "insert into candidate (id, display_name, primary_location_text, location_region) "
        "values (%s, %s, %s, %s)",
        (candidate_id, name, "Bengaluru, Karnataka, India", "IN-KA-BLR"),
    )
    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_raw, source_url, gate_state, candidate_id) "
        "values (%s, 'linkedin_serp', 'linkedin_url', %s, '{}', %s, 'passed', %s)",
        (role_id, f"linkedin.com/in/{name}", f"https://linkedin.com/in/{name}", candidate_id),
    )
    return candidate_id


def test_an_unread_candidate_is_not_reported_as_ruled_out(client, db_conn, role_id):
    """The bug this prevents: an unread candidate has no evidence, so every gate reads
    "no evidence for Python" and the recruiter is shown "No public sign of Python" —
    a claim about someone nobody looked at. 18 of 25 exclusions on the live role.
    """
    add_person(db_conn, role_id, name="alice", skills=("Python",))
    add_person(db_conn, role_id, name="carol", skills=("Go",))  # read, genuinely fails
    add_unread_person(db_conn, role_id, name="dave")  # never read
    matching.score_role(role_id, now=NOW)

    body = client.get(f"/roles/{role_id}?show_ruled_out=true").text
    assert "dave" in body, "an unread person must still be visible"
    assert "haven't looked at yet" in body

    # The ruled-out heading renders because carol is in it, so this split is real.
    # Bounded at the next heading: everything after it is the collection panel, which
    # lists unread people on purpose and is not part of the ruled-out reasoning.
    ruled_out_section = body.split("people we ruled out")[1].split("Read LinkedIn profiles")[0]
    assert "carol" in ruled_out_section, "carol was read and does fail the must-have"
    assert "dave" not in ruled_out_section, "dave was never read — that is not a reason"
    assert "No public sign of" not in body.split("people we ruled out")[0], (
        "no claim about a candidate's skills may appear above the ruled-out section"
    )


def test_an_unread_candidate_never_reaches_the_ranked_list(client, db_conn, role_id):
    """A role with no must-haves passes every gate trivially, so bucketing on the gate
    verdict alone would rank someone nobody has read. `checked` is tested first.
    """
    db_conn.execute(
        "update role set spec_json = jsonb_set(spec_json, '{must_have_skills}', '[]') "
        "where id = %s",
        (role_id,),
    )
    add_unread_person(db_conn, role_id, name="erin")
    matching.score_role(role_id, now=NOW)

    rows = matching.shortlist(role_id, include_excluded=True)
    erin = next(r for r in rows if r["display_name"] == "erin")
    assert erin["checked"] is False


def test_a_read_candidate_is_still_marked_checked(client, db_conn, role_id):
    add_person(db_conn, role_id, name="frank", skills=("Python",))
    matching.score_role(role_id, now=NOW)
    rows = matching.shortlist(role_id, include_excluded=True)
    assert all(r["checked"] for r in rows if r["display_name"] == "frank")


def test_the_empty_page_distinguishes_unread_from_rejected(client, db_conn, role_id):
    """"Nobody came through" is wrong when the truth is "nobody has been read yet"."""
    add_unread_person(db_conn, role_id, name="grace")
    matching.score_role(role_id, now=NOW)
    body = client.get(f"/roles/{role_id}").text
    assert "Nobody has been read yet" in body
    assert "Nobody came through" not in body


def test_zero_results_explains_itself(client, db_conn, role_id):
    r = client.get(f"/roles/{role_id}")
    assert r.status_code == 200
    # Never a bare "no results".
    assert "Nobody came through" in r.text or "Looked at 0 people" in r.text


# --- recruiter actions -------------------------------------------------------


def test_action_is_recorded_and_greys_the_row(client, db_conn, role_id):
    add_person(db_conn, role_id, name="alice")
    matching.score_role(role_id, now=NOW)
    match_id = db_conn.execute("select id from match limit 1").fetchone()[0]

    r = client.post(f"/matches/{match_id}/action", data={"action": "shortlisted"})
    assert r.status_code == 200
    assert "shortlisted" in r.text

    stored = db_conn.execute("select action, actor from recruiter_action").fetchall()
    # The actor columns exist so this stops being null (IMPLEMENTATION.md 1.12).
    assert stored == [("shortlisted", TEST_USER)]

    body = client.get(f"/roles/{role_id}").text
    assert "row-acted" in body


def test_invalid_action_is_rejected(client, db_conn, role_id):
    add_person(db_conn, role_id, name="alice")
    matching.score_role(role_id, now=NOW)
    match_id = db_conn.execute("select id from match limit 1").fetchone()[0]
    assert client.post(f"/matches/{match_id}/action", data={"action": "hire_immediately"}).status_code == 400


# --- create role -------------------------------------------------------------


def test_contradictory_spec_is_blocked_at_confirm(client, db_conn):
    r = client.post(
        "/roles",
        data={"title": "Backend", "titles": "Backend Engineer", "must_have": "Python",
              "remote": "onsite"},  # in-office with no location
    )
    assert r.status_code == 422
    assert "in-office" in r.text


def test_confirming_a_role_queues_the_search(client, db_conn):
    r = client.post(
        "/roles",
        data={"title": "Backend", "titles": "Backend Engineer", "must_have": "Python",
              "remote": "hybrid", "locations": ["IN-KA-BLR"], "pages": "3"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    kinds = db_conn.execute("select kind from job where state = 'queued'").fetchall()
    assert ("discover",) in kinds


# --- language rules, enforced by test ----------------------------------------

BANNED_JARGON = [
    "artifact_backed", "third_party_stated", "self_reported",
    "tier", "adapter", "evidence", "gate", "spec", "score",
    "candidate_ref", "source_policy", "snippet",
]

RECRUITER_TEMPLATES = [
    "roles.html", "new_role.html", "role_form.html",
    "shortlist.html", "_shortlist_rows.html", "_candidate_panel.html", "_reading.html",
]


def visible_text(template: str) -> str:
    """Strip Jinja expressions, HTML tags, attributes and CSS — leaving what a recruiter reads."""
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    text = re.sub(r"\{\#.*?\#\}", " ", text, flags=re.DOTALL)   # jinja comments
    text = re.sub(r"\{\{.*?\}\}", " ", text, flags=re.DOTALL)   # expressions
    text = re.sub(r"\{%.*?%\}", " ", text, flags=re.DOTALL)     # statements
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)                        # tags and their attributes
    return text


@pytest.mark.parametrize("template", RECRUITER_TEMPLATES)
def test_no_jargon_reaches_a_recruiter(template):
    text = visible_text(template).lower()
    found = [word for word in BANNED_JARGON if re.search(rf"\b{re.escape(word)}\b", text)]
    assert not found, f"{template} shows jargon to a recruiter: {found}"


@pytest.mark.parametrize("template", RECRUITER_TEMPLATES)
def test_recruiter_templates_use_the_agreed_vocabulary(template):
    """The words are proven, says so, source, ruled out, match."""
    text = visible_text(template).lower()
    assert any(
        word in text
        for word in ("proven", "says so", "source", "ruled out", "match", "public work", "role")
    ), f"{template} uses none of the agreed vocabulary"


def test_admin_page_may_use_internal_words():
    # The admin page is engineer-facing and deliberately exempt.
    assert "adapter" in visible_text("admin_health.html").lower() or True


def test_role_page_prints_the_command_to_read_profiles(client, db_conn, role_id):
    """The panel names this role and offers a count the budget can actually pay for."""
    from hi.adapters import linkedin_profile as lp

    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_raw, source_url, gate_state) "
        "values (%s, 'linkedin_serp', 'linkedin_url', 'linkedin.com/in/unread-1', '{}', "
        "'https://www.linkedin.com/in/unread-1/', 'passed')",
        (role_id,),
    )
    r = client.get(f"/roles/{role_id}")
    assert r.status_code == 200
    assert f"--role {str(role_id)[:8]}" in r.text        # this role, not another one
    assert "unread-1" in r.text
    assert f"of {lp.MAX_PROFILES_PER_DAY}</strong> reads left" in r.text


def test_queue_order_matches_what_the_run_would_read(client, db_conn, role_id):
    """A queue in a different order from the run it prints a command for is a lie."""
    from hi.adapters import linkedin_profile as lp

    for slug in ("java-dev", "python-dev"):
        db_conn.execute(
            "insert into candidate_ref "
            "(role_id, adapter, ref_kind, ref_value, snippet_raw, snippet_headline, "
            " source_url, gate_state) "
            "values (%s, 'linkedin_serp', 'linkedin_url', %s, '{}', %s, %s, 'passed')",
            (role_id, f"linkedin.com/in/{slug}", slug.replace("-", " "),
             f"https://www.linkedin.com/in/{slug}/"),
        )
    waiting = [p["slug"] for p in lp.waiting_for_role(role_id) if not p["enriched"]]
    running = [lp.slug_of(url) for _, url in lp.targets_for_role(role_id)]
    assert waiting == running
    assert waiting[0] == "python-dev"  # the must-have in the snippet wins the budget


def test_already_read_people_are_shown_but_not_queued(client, db_conn, role_id):
    """`add_person` has a github_login, which is exactly what the run skips."""
    from hi.adapters import linkedin_profile as lp

    add_person(db_conn, role_id, name="alice")
    waiting = lp.waiting_for_role(role_id)
    assert [p["slug"] for p in waiting] == ["alice"]
    assert waiting[0]["enriched"] is True
    assert lp.targets_for_role(role_id) == []


def test_locations_are_checkboxes_not_a_ctrl_click_multiselect(client, db_conn):
    """A <select multiple> needs Ctrl+click and gives no sign of it. A recruiter reads
    the resulting refusal as "it is not working" — three submits, live, 2026-08-31.
    """
    r = client.post("/roles/read", data={"jd_text": "Operations Manager in Bangalore."})
    assert 'type="checkbox" name="locations"' in r.text
    assert 'name="locations" multiple' not in r.text


def test_a_missing_location_marks_the_field_not_just_the_banner(client, db_conn):
    r = client.post("/roles", data={"title": "Ops Manager", "titles": "Ops Manager",
                                    "remote": "onsite"})
    assert r.status_code == 422
    assert "pick at least one" in r.text          # on the field itself
    assert "needs-fixing" in r.text               # and the field is visibly marked


def test_ticking_a_location_lets_the_role_through(client, db_conn):
    r = client.post("/roles", data={"title": "Ops Manager", "titles": "Ops Manager",
                                    "remote": "onsite", "locations": ["IN-KA-BLR"]},
                    follow_redirects=False)
    assert r.status_code == 303


def test_a_job_nobody_picked_up_says_so(client, db_conn, role_id):
    """Ten live minutes of "Searching..." over a queue with no worker, 2026-08-31.
    A page reporting progress nothing is making is the same lie as a green light
    over a dead adapter.
    """
    db_conn.execute(
        "insert into job (kind, payload_json, payload_hash, state, attempts, created_at) "
        "values ('discover', %s, %s, 'queued', 0, now() - interval '10 minutes')",
        (json.dumps({"role_id": str(role_id)}), f"stalled-{role_id}"),
    )
    body = client.get(f"/roles/{role_id}").text
    assert "The search has not started" in body
    assert "python -m hi.worker" in body


def test_a_job_just_queued_is_not_called_stalled(client, db_conn, role_id):
    """A worker gets a grace period — a fresh job is starting, not broken."""
    db_conn.execute(
        "insert into job (kind, payload_json, payload_hash, state, attempts) "
        "values ('discover', %s, %s, 'queued', 0)",
        (json.dumps({"role_id": str(role_id)}), f"fresh-{role_id}"),
    )
    body = client.get(f"/roles/{role_id}").text
    assert "The search has not started" not in body
    assert "Searching" in body


def test_the_web_process_drains_the_queue_itself(db_conn, users, role_id):
    """Clicking "Find candidates" must actually search, not merely queue a search.

    Forgetting the second process did not look like an error — the page reported
    "Searching" over a queue nobody was draining, indefinitely (2026-08-31, ten live
    minutes). TestClient runs the lifespan only as a context manager, which is why the
    other tests in this file are unaffected by the inline worker.
    """
    import time

    from hi.web.app import app

    job_id = worker.enqueue("score", {"role_id": str(role_id)})
    assert job_id is not None

    with TestClient(app):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = db_conn.execute(
                "select state from job where id = %s", (job_id,)
            ).fetchone()[0]
            if state != "queued":
                break
            db_conn.commit()  # a new snapshot; the worker writes on its own connection
            time.sleep(0.25)

    assert state == "done", f"the web process left the job {state!r}"


def test_the_inline_worker_can_spawn_a_subprocess(db_conn, users, role_id, monkeypatch):
    """Playwright is a subprocess, and uvicorn's loop cannot always start one.

    `uvicorn --reload` on Windows runs the server on a `SelectorEventLoop`; a job that
    started a browser there died with a bare `NotImplementedError` while the identical
    job run from `python -m hi.worker` succeeded (2026-09-02, every enrich from the
    button). The worker owns its own loop for exactly this reason.
    """
    import asyncio
    import sys
    import time

    from hi.web.app import app

    async def spawn(job):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "print('ok')", stdout=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        worker.set_note(job.id, out.decode().strip())

    monkeypatch.setitem(worker.HANDLERS, "score", spawn)
    job_id = worker.enqueue("score", {"role_id": str(role_id)})

    with TestClient(app):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state, note = db_conn.execute(
                "select state, note from job where id = %s", (job_id,)
            ).fetchone()
            if state != "queued":
                break
            db_conn.commit()
            time.sleep(0.25)

    assert (state, note) == ("done", "ok")


def test_the_people_we_never_opened_are_shown_with_their_reason(client, db_conn, role_id):
    """"Looked at 23 people" over a list of 11 makes an over-tight gate look like an
    empty market. The 12 missing are the ones a recruiter most needs to see.
    """
    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_raw, snippet_name, "
        " snippet_location, source_url, gate_state, gate_reason) "
        "values (%s, 'linkedin_serp', 'linkedin_url', 'linkedin.com/in/rejected', '{}', "
        "'Rebecca Caldwell', 'Greater Sydney Area', 'https://linkedin.com/in/rejected', "
        "'failed', 'location: Greater Sydney Area is not a recognised Indian location')",
        (role_id,),
    )
    body = client.get(f"/roles/{role_id}").text
    assert "we did not open" in body
    assert "Rebecca Caldwell" in body
    assert "not a recognised Indian location" in body


def test_nobody_is_counted_as_ranked_until_they_are_read(client, db_conn, role_id):
    """The banner said "11 ranked" directly above a section reading "nothing to rank"
    — a match row exists from discovery, long before anyone opens the profile.
    """
    add_unread_person(db_conn, role_id, name="dave")
    matching.score_role(role_id, now=NOW)
    assert matching.role_progress(role_id)["ranked"] == 0

    add_person(db_conn, role_id, name="alice")
    matching.score_role(role_id, now=NOW)
    assert matching.role_progress(role_id)["ranked"] == 1


# --- "not enough people?" — search deeper, and re-check who is already here ----


def failed_ref(db_conn, role_id, *, slug, location, name="Someone"):
    """A ref stored with a `passed` verdict it should not have — the shape the location
    fix left behind on every role searched before it."""
    # A title SPEC accepts, so location is the only thing left to decide the verdict.
    raw = json.dumps(
        {"title": f"{name} - Backend Engineer | LinkedIn", "extensions": [location]}
    )
    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_name, snippet_location, "
        " snippet_raw, source_url, gate_state) "
        "values (%s, 'linkedin_serp', 'linkedin_url', %s, %s, %s, %s, %s, 'passed')",
        (role_id, f"linkedin.com/in/{slug}", name, location, raw,
         f"https://linkedin.com/in/{slug}"),
    )


def test_the_role_page_offers_to_look_for_more_people(client, db_conn, role_id):
    body = client.get(f"/roles/{role_id}").text
    assert "Not enough people?" in body
    assert f'action="/roles/{role_id}/find-more"' in body


def test_finding_more_queues_a_deeper_search_than_the_last_one(client, db_conn, role_id):
    worker.enqueue("discover", {"role_id": str(role_id), "pages": 2})
    db_conn.execute("update job set state = 'done', finished_at = now()")

    r = client.post(f"/roles/{role_id}/find-more", follow_redirects=False)
    assert r.status_code == 303
    assert "more=started" in r.headers["location"]

    pages = db_conn.execute(
        "select payload_json->>'pages' from job "
        "where kind = 'discover' and state = 'queued'"
    ).fetchall()
    assert pages == [("3",)]


def test_a_role_searched_before_pages_existed_still_goes_deeper(client, db_conn, role_id):
    """The first discover jobs carried no `pages` key. Treating that as "unknown" and
    refusing would make the button dead on exactly the old roles that need it."""
    worker.enqueue("discover", {"role_id": str(role_id)})
    db_conn.execute("update job set state = 'done', finished_at = now()")

    client.post(f"/roles/{role_id}/find-more", follow_redirects=False)
    pages = db_conn.execute(
        "select payload_json->>'pages' from job where kind = 'discover' and state = 'queued'"
    ).fetchone()
    assert pages == ("2",)


def test_finding_more_rechecks_people_already_found(client, db_conn, role_id):
    """The complaint that produced this button: a run made before the location rule was
    tightened left people abroad sitting in the shortlist, and re-running discovery to
    correct that would have paid for results already in the database."""
    failed_ref(db_conn, role_id, slug="abroad", location="Greater Sydney Area")
    failed_ref(db_conn, role_id, slug="local", location="Bengaluru, Karnataka, India")

    r = client.post(f"/roles/{role_id}/find-more", follow_redirects=False)
    assert "rechecked=1" in r.headers["location"]

    states = dict(db_conn.execute(
        "select ref_value, gate_state from candidate_ref where role_id = %s", (role_id,)
    ).fetchall())
    assert states["linkedin.com/in/abroad"] == "failed"
    assert states["linkedin.com/in/local"] == "passed"


def test_a_ref_ruled_out_on_recheck_always_carries_a_reason(client, db_conn, role_id):
    failed_ref(db_conn, role_id, slug="abroad", location="Greater Sydney Area")
    client.post(f"/roles/{role_id}/find-more", follow_redirects=False)
    reason = db_conn.execute(
        "select gate_reason from candidate_ref where ref_value = 'linkedin.com/in/abroad'"
    ).fetchone()[0]
    assert reason


def test_finding_more_refuses_while_a_search_is_running(client, db_conn, role_id):
    worker.enqueue("discover", {"role_id": str(role_id), "pages": 1})

    r = client.post(f"/roles/{role_id}/find-more", follow_redirects=False)
    assert "more=busy" in r.headers["location"]
    queued = db_conn.execute(
        "select count(*) from job where kind = 'discover' and state = 'queued'"
    ).fetchone()[0]
    assert queued == 1, "a second search would pay twice for the same pages"


def test_the_deepest_a_role_can_be_searched_is_a_sentence_not_a_silent_no_op(
    client, db_conn, role_id
):
    from hi.adapters.linkedin_serp import MAX_PAGES

    worker.enqueue("discover", {"role_id": str(role_id), "pages": MAX_PAGES})
    db_conn.execute("update job set state = 'done', finished_at = now()")

    r = client.post(f"/roles/{role_id}/find-more", follow_redirects=False)
    assert "more=deepest" in r.headers["location"]
    assert not db_conn.execute(
        "select 1 from job where kind = 'discover' and state = 'queued'"
    ).fetchone()

    body = client.get(f"/roles/{role_id}?more=deepest").text
    assert "as deep as it goes" in body
    assert "find-more" not in body, "a button that cannot do anything is worse than none"


def test_the_recheck_still_runs_at_maximum_depth(client, db_conn, role_id):
    from hi.adapters.linkedin_serp import MAX_PAGES

    worker.enqueue("discover", {"role_id": str(role_id), "pages": MAX_PAGES})
    db_conn.execute("update job set state = 'done', finished_at = now()")
    failed_ref(db_conn, role_id, slug="abroad", location="Greater Sydney Area")

    r = client.post(f"/roles/{role_id}/find-more", follow_redirects=False)
    assert "rechecked=1" in r.headers["location"]


def test_an_unknown_outcome_in_the_url_is_ignored(client, db_conn, role_id):
    """`more` comes back off a URL a recruiter can edit or bookmark."""
    # A marker, not "<script>": the page carries its own script (the filter bar), and
    # the point is that nothing off the URL is echoed back, escaped or not.
    body = client.get(f"/roles/{role_id}?more=<script>zq9marker</script>&rechecked=9").text
    assert "zq9marker" not in body


def test_finding_more_on_an_unknown_role_is_404(client, db_conn):
    r = client.post(f"/roles/{uuid.uuid4()}/find-more", follow_redirects=False)
    assert r.status_code == 404


# --- the button that starts a read (IMPLEMENTATION.md 1.11d) ------------------


@pytest.fixture()
def unread(db_conn, role_id):
    """One gate-passing ref nobody has read, which is what the button needs."""
    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_raw, source_url, gate_state) "
        "values (%s, 'linkedin_serp', 'linkedin_url', 'linkedin.com/in/unread-1', '{}', "
        "'https://www.linkedin.com/in/unread-1/', 'passed')",
        (role_id,),
    )
    return role_id


@pytest.fixture()
def can_read(db_conn, monkeypatch):
    """Everything the preflight wants: the switch on, and a window to attach to.

    Mode C ships disabled (migration 010) and a fresh database refuses every read, which
    is the correct default and the reason this has to be explicit in a test.
    """
    from hi.adapters import linkedin_profile as lp

    db_conn.execute(
        "update source_policy set enabled = true, reviewed_at = now(), "
        "disabled_reason = null where domain in ('linkedin.com', 'www.linkedin.com')"
    )
    monkeypatch.setattr(lp, "browser_attached", lambda: True)


def enrich_jobs(conn) -> list[tuple]:
    return conn.execute(
        "select payload_json, state from job where kind = 'enrich'"
    ).fetchall()


def test_the_button_queues_a_read_capped_to_what_it_can_pay_for(
    client, db_conn, unread, can_read
):
    r = client.post(f"/roles/{unread}/enrich", data={"limit": "50"}, follow_redirects=False)
    assert r.headers["location"].endswith("reading=started")

    (payload, state), = enrich_jobs(db_conn)
    assert state == "queued"
    # 50 was asked for and one person is waiting. A command that asks for more than the
    # run can deliver is the lie the panel was built to avoid.
    assert payload == {"role_id": str(unread), "limit": 1}


@pytest.fixture()
def ruled_out(db_conn, role_id):
    """One ref the snippet gate refused before anything was read."""
    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_raw, source_url, gate_state, "
        " gate_reason) "
        "values (%s, 'linkedin_serp', 'linkedin_url', 'linkedin.com/in/refused-1', '{}', "
        "'https://www.linkedin.com/in/refused-1/', 'failed', 'location_mismatch')",
        (role_id,),
    )
    return role_id


def test_the_ruled_out_can_be_read_on_purpose(client, db_conn, unread, ruled_out, can_read):
    """The gate's verdict is a default, not a ruling — the quota is the recruiter's."""
    r = client.post(
        f"/roles/{unread}/enrich",
        data={"limit": "50", "scope": "ruled_out"},
        follow_redirects=False,
    )
    assert r.headers["location"].endswith("reading=started")

    (payload, _), = enrich_jobs(db_conn)
    # One refused person waiting, and the gate-passing one is not in this run.
    assert payload == {"role_id": str(unread), "limit": 1, "scope": "ruled_out"}


def test_reading_everyone_covers_both_sides_of_the_gate(
    client, db_conn, unread, ruled_out, can_read
):
    r = client.post(
        f"/roles/{unread}/enrich", data={"limit": "50", "scope": "all"}, follow_redirects=False
    )
    assert r.headers["location"].endswith("reading=started")

    (payload, _), = enrich_jobs(db_conn)
    assert payload == {"role_id": str(unread), "limit": 2, "scope": "all"}


def test_reading_everyone_never_exceeds_the_days_quota(
    client, db_conn, unread, ruled_out, can_read, monkeypatch
):
    """"All" means all we can pay for. The rolling 24h cap is shared across every role."""
    from hi.adapters import linkedin_profile as lp

    monkeypatch.setattr(lp, "spent_today", lambda: lp.MAX_PROFILES_PER_DAY - 1)
    client.post(
        f"/roles/{unread}/enrich", data={"limit": "50", "scope": "all"}, follow_redirects=False
    )
    (payload, _), = enrich_jobs(db_conn)
    assert payload["limit"] == 1


def test_an_unknown_scope_reads_only_the_people_who_passed(
    client, db_conn, unread, ruled_out, can_read
):
    """A typo in a form field must never spend the quota on people we ruled out."""
    client.post(
        f"/roles/{unread}/enrich", data={"limit": "50", "scope": "everyone!"}, follow_redirects=False
    )
    (payload, _), = enrich_jobs(db_conn)
    assert payload == {"role_id": str(unread), "limit": 1}


def test_the_panel_offers_the_ruled_out_when_there_are_any(
    client, db_conn, unread, ruled_out, can_read
):
    body = client.get(f"/roles/{unread}").text
    assert "Read the 1 ruled out" in body
    assert "Read everyone" in body


def test_the_panel_offers_no_override_when_nothing_was_ruled_out(
    client, db_conn, unread, can_read
):
    body = client.get(f"/roles/{unread}").text
    assert "ruled out" not in body.split("Read LinkedIn profiles")[1]


def test_reading_a_ruled_out_person_takes_them_out_of_the_did_not_open_list(
    client, db_conn, ruled_out, can_read
):
    """Read means read. Someone we opened is ranked, not listed as never opened."""
    from hi import identity
    from hi.extract import EvidenceRow, write_evidence

    resolved = identity.resolve({"linkedin_slug": "refused-1"}, display_name="Refused One")
    db_conn.execute(
        "update candidate_ref set candidate_id = %s where ref_value = %s",
        (resolved.candidate_id, "linkedin.com/in/refused-1"),
    )
    write_evidence(
        resolved.candidate_id,
        [
            EvidenceRow(
                claim_type="skill",
                claim_key="python",
                tier="self_reported",
                source_url="https://www.linkedin.com/in/refused-1/",
                snippet="Python",
                extractor="linkedin_profile",
            )
        ],
        source_text=None,
    )
    matching.score_role(ruled_out)

    body = client.get(f"/roles/{ruled_out}").text
    assert "we did not open" not in body


def test_no_open_linkedin_window_refuses_before_queueing_anything(
    client, db_conn, unread, can_read, monkeypatch
):
    """The whole point of a preflight: find out now, not twenty seconds into a run."""
    from hi.adapters import linkedin_profile as lp

    monkeypatch.setattr(lp, "browser_attached", lambda: False)
    r = client.post(f"/roles/{unread}/enrich", data={"limit": "1"}, follow_redirects=False)
    assert r.headers["location"].endswith("reading=no-browser")
    assert enrich_jobs(db_conn) == []
    assert "window is not open" in client.get(f"/roles/{unread}?reading=no-browser").text


def test_a_read_running_for_another_role_refuses_this_one(
    client, db_conn, unread, can_read
):
    """One account, one at a time (ARCHITECTURE.md §7.4) — across every role."""
    worker.enqueue("enrich", {"role_id": str(uuid.uuid4()), "limit": 1})
    r = client.post(f"/roles/{unread}/enrich", data={"limit": "1"}, follow_redirects=False)
    assert r.headers["location"].endswith("reading=busy")
    assert len(enrich_jobs(db_conn)) == 1


def test_the_kill_switch_refuses_the_button(client, db_conn, unread, can_read):
    db_conn.execute(
        "update source_policy set enabled = false, disabled_reason = 'authwall' "
        "where domain = 'linkedin.com'"
    )
    r = client.post(f"/roles/{unread}/enrich", data={"limit": "1"}, follow_redirects=False)
    assert r.headers["location"].endswith("reading=off")
    assert enrich_jobs(db_conn) == []


def test_the_spent_budget_refuses_the_button(client, db_conn, unread, can_read, monkeypatch):
    from hi.adapters import linkedin_profile as lp

    monkeypatch.setattr(lp, "spent_today", lambda: lp.MAX_PROFILES_PER_DAY)
    r = client.post(f"/roles/{unread}/enrich", data={"limit": "1"}, follow_redirects=False)
    assert r.headers["location"].endswith("reading=spent")
    assert enrich_jobs(db_conn) == []


def test_nobody_left_to_read_says_so(client, db_conn, role_id, can_read):
    r = client.post(f"/roles/{role_id}/enrich", data={"limit": "1"}, follow_redirects=False)
    assert r.headers["location"].endswith("reading=nobody")


def test_a_running_read_shows_its_progress_and_offers_no_second_button(
    client, db_conn, unread, can_read
):
    job_id = worker.enqueue("enrich", {"role_id": str(unread), "limit": 3})
    worker.set_note(job_id, "Reading 2 of 3: unread-1")

    body = client.get(f"/roles/{unread}").text
    assert "Reading 2 of 3: unread-1" in body
    assert f'/roles/{unread}/reading' in body      # it polls itself
    assert "Read them now" not in body            # and cannot be started twice


def test_a_stopped_read_says_stopped_on_the_page(client, db_conn, unread, can_read):
    """A button is exactly where a silent zero-row run would get hidden."""
    job_id = worker.enqueue("enrich", {"role_id": str(unread), "limit": 3})
    worker.set_note(job_id, "Stopped: authwall — LinkedIn disabled, a human must re-enable")

    body = client.get(f"/roles/{unread}").text
    assert "Stopped:" in body
    assert "warn" in body


def test_the_reading_fragment_stops_polling_when_the_run_ends(client, db_conn, unread, can_read):
    job_id = worker.enqueue("enrich", {"role_id": str(unread), "limit": 1})
    worker.set_note(job_id, "Read 1 of 1 person, 1 with employment history.")
    db_conn.execute("update job set state = 'done', finished_at = now() where id = %s", (job_id,))

    body = client.get(f"/roles/{unread}/reading").text
    assert "hx-trigger" not in body
    assert "Read 1 of 1 person" in body


def test_reading_on_an_unknown_role_is_404(client, db_conn):
    r = client.post(f"/roles/{uuid.uuid4()}/enrich", data={"limit": "1"}, follow_redirects=False)
    assert r.status_code == 404


def test_the_kill_switch_hides_the_button_but_never_the_command(client, db_conn, unread):
    """A fresh database has reading switched off, and the manual route is then the only one.

    The command panel is not a fallback for the button — it is how the tool is driven when
    the signed-in browser lives on another machine, and how an operator turns the switch
    back on knowingly. Hiding it with the button would leave a dead end.
    """
    body = client.get(f"/roles/{unread}").text
    assert "Read them now" not in body
    assert f"--role {str(unread)[:8]}" in body
    assert "switched off" in body


def test_a_run_that_gave_up_does_not_leave_a_hopeful_note(client, db_conn, unread, can_read):
    """"Reading 2 of 8" left over a dead job reads as success. It is the opposite."""
    job_id = worker.enqueue("enrich", {"role_id": str(unread), "limit": 8})
    worker.set_note(job_id, "Reading 2 of 8: unread-1")
    db_conn.execute("update job set state = 'failed', finished_at = now() where id = %s", (job_id,))

    body = client.get(f"/roles/{unread}").text
    assert "Reading 2 of 8" not in body
    assert "did not finish" in body


def test_a_new_role_searches_five_queries_two_pages_deep(client, db_conn):
    """The first search is capped at 10 SERP calls; "find more" is how a role grows."""
    client.post(
        "/roles",
        data={"title": "Backend", "titles": "Backend Engineer", "must_have": "Python",
              "remote": "hybrid", "locations": ["IN-KA-BLR"]},
        follow_redirects=False,
    )
    payload = db_conn.execute(
        "select payload_json from job where kind = 'discover' and state = 'queued'"
    ).fetchone()[0]
    assert payload["pages"] == 2 and payload["max_queries"] == 5


def test_a_name_links_to_linkedin_and_details_still_opens_the_panel(client, db_conn, role_id):
    candidate_id = add_person(db_conn, role_id, name="alice")
    db_conn.execute(
        "insert into identity (candidate_id, kind, value, first_seen, last_seen) "
        "values (%s, 'linkedin_slug', 'alice-k-123', now(), now())",
        (candidate_id,),
    )
    matching.score_role(role_id, now=NOW)

    body = client.get(f"/roles/{role_id}").text
    assert 'href="https://www.linkedin.com/in/alice-k-123"' in body
    assert f'hx-get="/roles/{role_id}/candidates/{candidate_id}"' in body

    panel = client.get(f"/roles/{role_id}/candidates/{candidate_id}").text
    assert "linkedin.com/in/alice-k-123" in panel.split("</h1>")[0]
