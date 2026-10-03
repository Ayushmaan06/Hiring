"""Mode C enrichment (IMPLEMENTATION.md 1.4c).

Everything here runs off a fixture Person and a fixture page text. Nothing opens a
browser and nothing touches LinkedIn — a test that needs the internet is a broken test.
"""

import contextlib
import uuid
from datetime import date
from pathlib import Path

import pytest

from hi.adapters import linkedin_profile as lp
from hi.db import pool
from hi.extract import write_evidence
from linkedin_scraper.models import Contact, Education, Experience, Person

PROFILE_URL = "https://www.linkedin.com/in/asha-example"

# The visible text of the profile. `_present` checks every field against this, so a
# field that is not in here must not survive into evidence.
PAGE_TEXT = """
Asha Example
Senior Backend Engineer at Acme Payments
Bengaluru, Karnataka, India
Open to work

About
Backend engineer. Code at https://github.com/ashaexample and mail asha@example.com

Experience
Senior Backend Engineer
Acme Payments
Mar 2021 - Present
Bengaluru, Karnataka, India

Backend Engineer
Rupee Systems
Jul 2018 - Feb 2021

Intern
Tiny Startup

Education
Indian Institute of Technology Bombay
B.Tech, Computer Science
2014 - 2018
"""


def make_person(**overrides) -> Person:
    defaults = dict(
        linkedin_url=PROFILE_URL,
        name="Asha Example",
        location="Bengaluru, Karnataka, India",
        about="Backend engineer. Code at https://github.com/ashaexample and mail asha@example.com",
        open_to_work=True,
        experiences=[
            Experience(
                position_title="Senior Backend Engineer",
                institution_name="Acme Payments",
                from_date="Mar 2021",
                to_date="Present",
            ),
            Experience(
                position_title="Backend Engineer",
                institution_name="Rupee Systems",
                from_date="Jul 2018",
                to_date="Feb 2021",
            ),
            # Real profiles carry undated entries. They must be counted and excluded,
            # never guessed at.
            Experience(position_title="Intern", institution_name="Tiny Startup"),
        ],
        educations=[
            Education(
                institution_name="Indian Institute of Technology Bombay",
                degree="B.Tech, Computer Science",
                from_date="2014",
                to_date="2018",
            )
        ],
        contacts=[Contact(type="email", value="asha@example.com")],
    )
    return Person(**{**defaults, **overrides})


def rows_for(person, page_text=PAGE_TEXT, today=date(2026, 8, 27)):
    return lp.rows_from_person(person, page_text=page_text, source_url=PROFILE_URL, today=today)


# --- date parsing -------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Mar 2021", date(2021, 3, 1)),
        ("March 2021", date(2021, 3, 1)),
        ("Sept. 2019", date(2019, 9, 1)),
        ("2020", date(2020, 1, 1)),  # year-only is common and must not be dropped
        ("Present", None),
        ("", None),
        (None, None),
        ("sometime last year", None),  # never guessed at
    ],
)
def test_parse_date(text, expected):
    assert lp.parse_date(text) == expected


def test_open_ended_span_closes_today_not_in_the_future():
    got = lp.spans([Experience(from_date="Mar 2021", to_date="Present")], today=date(2026, 8, 27))
    assert got == [(date(2021, 3, 1), date(2026, 8, 27))]


def test_undated_experience_is_dropped_not_guessed():
    assert lp.spans([Experience(position_title="Intern")], today=date(2026, 8, 27)) == []


def test_future_start_date_is_refused():
    assert lp.spans([Experience(from_date="Jan 2030")], today=date(2026, 8, 27)) == []


def test_overlapping_jobs_are_not_double_counted():
    """Two concurrent roles over the same three years are three years, not six."""
    overlapping = [
        (date(2020, 1, 1), date(2023, 1, 1)),
        (date(2021, 1, 1), date(2023, 1, 1)),
    ]
    assert lp.total_years(overlapping) == pytest.approx(3.0, abs=0.1)


def test_a_gap_between_jobs_is_not_counted_as_worked():
    gapped = [(date(2015, 1, 1), date(2016, 1, 1)), (date(2020, 1, 1), date(2021, 1, 1))]
    assert lp.total_years(gapped) == pytest.approx(2.0, abs=0.1)


def test_touching_spans_merge_without_gaining_a_day():
    touching = [(date(2020, 1, 1), date(2021, 1, 1)), (date(2021, 1, 1), date(2022, 1, 1))]
    assert lp.total_years(touching) == pytest.approx(2.0, abs=0.1)


def test_no_intervals_is_zero_not_a_crash():
    assert lp.total_years([]) == 0.0


# --- education: stored and shown, never scored (ARCHITECTURE.md §2.2) ---------


def test_education_is_recorded_with_the_institution_visible():
    """ARCHITECTURE.md §2.2 — a JD asking for IIT/IIM needs the institution on the card."""
    rows, _ = rows_for(make_person())
    edu = [r for r in rows if r.claim_type == "education"]
    assert len(edu) == 1
    assert edu[0].claim_key == "Indian Institute of Technology Bombay"
    assert "Indian Institute of Technology Bombay" in edu[0].snippet


@pytest.mark.parametrize(
    "institution",
    [
        "Government College of Engineering",
        "St. Joseph's College of Commerce",
        "Shri Ram College of Commerce",
        "College of Engineering, Pune",
    ],
)
def test_an_institution_named_college_is_not_refused(institution):
    """ARCHITECTURE.md §2.2 stores and shows the institution, so the word "college" in
    its *name* cannot be a banned signal. Found live 2026-08-28: five of twelve profiles
    reported "refused 1 (education: names banned signal 'college')", silently losing the
    one field the §2.2 decision exists to display. A large share of Indian institutions
    are named this way.
    """
    from hi.signals import is_banned

    assert is_banned(institution, institution) is None


@pytest.mark.parametrize("key", ["college_tier", "college_name", "graduation_year", "age"])
def test_a_feature_keyed_on_college_is_still_refused(key):
    """What §2.2 refuses is *ranking* on institution tier — a caste proxy in this market.
    Dropping the bare word must not drop that.
    """
    from hi.signals import is_banned

    assert is_banned(key) == key


def test_no_graduation_date_survives_anywhere_in_the_output():
    """Age is banned independently of the education decision, and a graduation year
    dates a person. Includes `observed_at`: a year in a timestamp is still the year.
    """
    rows, _ = rows_for(make_person())
    for r in rows:
        if r.claim_type != "education":
            continue
        for term in ("2014", "2018"):
            assert term not in (r.claim_value or ""), f"{term!r} leaked into claim_value"
            assert term not in r.snippet, f"{term!r} leaked into a snippet"
        assert r.observed_at is None, "observed_at would re-encode the graduation date"


def test_an_education_row_cannot_move_the_score_by_a_byte():
    """Stored and shown, never scored. `FEATURE_ALLOWLIST` is the enforcement; this is
    the test that proves nothing routes around it.
    """
    from datetime import datetime, timezone

    from hi.models import RoleSpec, Seniority
    from hi.scoring import Evidence, evaluate

    spec = RoleSpec(must_have_skills=["Python"], seniority=Seniority(min_years=3))
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    base = [
        Evidence(
            claim_type="skill", claim_key="Python", claim_value="Python",
            value_num=100_000.0, tier="artifact_backed", observed_at=now,
        )
    ]
    education = Evidence(
        claim_type="education",
        claim_key="Indian Institute of Technology Bombay",
        claim_value="B.Tech, Computer Science",
        tier="self_reported",
        observed_at=now,
    )
    without = evaluate(spec, base, now=now)
    with_iit = evaluate(spec, base + [education], now=now)
    assert without.score == with_iit.score
    assert without.components == with_iit.components
    assert without.gates == with_iit.gates


# --- strong keys: the actual gap this closes ----------------------------------


def test_github_link_on_the_profile_becomes_a_strong_key():
    """53 refs are stuck with only a linkedin_url. This is how they reach GitHub."""
    keys = lp.strong_keys_from(make_person(), PAGE_TEXT)
    assert keys["github_login"] == "ashaexample"


def test_email_is_hashed_never_stored_plain():
    keys = lp.strong_keys_from(make_person(), PAGE_TEXT)
    assert "asha@example.com" not in str(keys)
    assert len(keys["email_sha256"]) == 64


def test_a_linkedin_url_is_not_mistaken_for_a_github_login():
    person = make_person(about="Find me at https://www.linkedin.com/in/someone-else")
    keys = lp.strong_keys_from(person, "https://www.linkedin.com/in/someone-else")
    assert "github_login" not in keys


def test_github_login_also_lands_as_readable_evidence():
    rows, _ = rows_for(make_person())
    link = next(r for r in rows if r.claim_type == "link")
    assert link.claim_value == "ashaexample"
    assert "ashaexample" in link.snippet


# --- blocking: a wall must never read as an empty profile ---------------------


@pytest.mark.parametrize(
    "text",
    [
        "Join LinkedIn to see Asha's full profile",
        "Sign in to see who you already know",
        "authwall",
        "Please verify you are a human",
    ],
)
def test_walls_are_detected(text):
    assert lp.blocked_by(text) is not None


def test_a_real_profile_is_not_mistaken_for_a_wall():
    assert lp.blocked_by(PAGE_TEXT) is None


def test_kill_switch_disables_both_linkedin_hosts(db_conn):
    """One wall must close the canonical host AND its www redirect target."""
    from hi.fetcher import disable_source

    db_conn.execute("update source_policy set enabled = true where domain like '%linkedin.com'")
    disable_source("linkedin.com", "served a wall")
    disable_source("www.linkedin.com", "served a wall")
    rows = db_conn.execute(
        "select enabled, disabled_reason from source_policy where domain like '%linkedin.com'"
    ).fetchall()
    assert len(rows) == 2
    for enabled, reason in rows:
        assert enabled is False
        assert reason == "served a wall"


# --- the gate: mode C is policed by the same chokepoint as everything else ----


async def test_browser_gate_refuses_a_disabled_source(db_conn):
    from hi.fetcher import PolicyDenied, browser_gate

    db_conn.execute("update source_policy set enabled = false where domain like '%linkedin.com'")
    with pytest.raises(PolicyDenied):
        await browser_gate(PROFILE_URL, adapter=lp.ADAPTER)


async def test_browser_gate_allows_an_enabled_source(db_conn):
    from hi.fetcher import browser_gate

    db_conn.execute(
        "update source_policy set enabled = true, rate_limit_rps = 100 "
        "where domain like '%linkedin.com'"
    )
    await browser_gate(PROFILE_URL, adapter=lp.ADAPTER)  # must not raise


async def test_mode_b_stays_blocked_by_robots(db_conn):
    """Enabling mode C must not quietly re-open the logged-out crawler.

    `respect_robots` is what keeps httpx out of LinkedIn, and mode C is authorised
    *without* changing it. If this ever flips, it is a decision, not a side effect.
    """
    row = db_conn.execute(
        "select respect_robots from source_policy where domain = 'linkedin.com'"
    ).fetchone()
    assert row[0] is True


def test_the_cap_lives_in_code_not_config():
    """ARCHITECTURE.md §7.4 — a limit that can be edited in a row is not a limit."""
    import inspect

    from hi.config import Settings

    assert lp.MAX_PROFILES_PER_WINDOW == 30
    assert lp.WINDOW_HOURS == 12
    assert lp.MIN_DELAY_SECONDS >= 20
    assert lp.CONCURRENCY == 1
    for name in Settings.model_fields:
        assert "linkedin" not in name, f"{name} makes a mode C limit configurable"
    # And the run must actually consult it.
    assert "MAX_PROFILES_PER_WINDOW" in inspect.getsource(lp._enrich)


def test_no_credential_path_exists_in_this_adapter():
    """CLAUDE.md: `linkedin_scraper/core/auth.py` must never be ported or imported."""
    import pathlib

    source = pathlib.Path(lp.__file__).read_text(encoding="utf-8")
    for banned in ("LINKEDIN_EMAIL", "LINKEDIN_PASSWORD", "login_with_credentials", "core.auth"):
        assert banned not in source, f"{banned} appeared in the mode C adapter"


def test_it_attaches_and_never_launches_a_browser():
    """`launch` would create a session this process controls — that is not mode C."""
    import inspect

    source = inspect.getsource(lp.attached_page)
    assert "connect_over_cdp" in source
    assert ".launch(" not in source


async def test_cap_is_enforced_before_any_browser_is_opened(db_conn, monkeypatch):
    """At the cap, `enrich` returns without so much as looking for a browser."""
    monkeypatch.setattr(lp, "spent_today", lambda: lp.MAX_PROFILES_PER_WINDOW)

    def explode():
        raise AssertionError("attached to a browser despite being at the cap")

    monkeypatch.setattr(lp, "attached_page", explode)
    result = await lp.enrich([(None, PROFILE_URL)])
    assert result.attempted == 0
    assert "cap reached" in result.stopped_reason


# --- CLI ergonomics -----------------------------------------------------------


def test_role_can_be_given_as_a_prefix(db_conn):
    """A 36-character id gets retyped by hand at the worst moment."""
    from hi import discovery
    from hi.models import RoleSpec

    role_id = discovery.create_role("Backend Engineer", RoleSpec(titles=["Backend Engineer"]))
    assert lp._resolve_role(str(role_id)[:8]) == role_id
    # The literal paste from the runbook, ellipsis and all.
    assert lp._resolve_role(f"{str(role_id)[:8]}-...") == role_id


def test_an_unmatched_role_exits_with_the_list_not_a_stack_trace(db_conn):
    with pytest.raises(SystemExit):
        lp._resolve_role("zzzzzzzz")
    with pytest.raises(SystemExit):
        lp._resolve_role(None)


def test_an_ambiguous_prefix_refuses_rather_than_picking_one(db_conn, monkeypatch):
    from hi import discovery
    from hi.models import RoleSpec

    discovery.create_role("A", RoleSpec())
    discovery.create_role("B", RoleSpec())
    with pytest.raises(SystemExit):
        lp._resolve_role("")  # matches everything


def test_targets_are_fetchable_urls_not_dedup_keys(db_conn):
    """`ref_value` is 'linkedin.com/in/x' — no scheme, no host, cannot be navigated to.

    Handing that to the gate produced ': no enabled source_policy row', which reads like
    a policy problem and sends you to the database instead of to this function.
    """
    from urllib.parse import urlparse

    from hi import discovery
    from hi.models import RoleSpec

    role_id = discovery.create_role("Backend Engineer", RoleSpec())
    db_conn.execute(
        "insert into candidate_ref "
        "(role_id, adapter, ref_kind, ref_value, snippet_raw, source_url, gate_state) "
        "values (%s, 'linkedin_serp', 'linkedin_url', %s, '{}', %s, 'passed')",
        (role_id, "linkedin.com/in/ajith-b", "https://linkedin.com/in/ajith-b"),
    )
    targets = lp.targets_for_role(role_id)
    assert targets, "a gate-passing ref should be a target"
    for _, url in targets:
        parsed = urlparse(url)
        assert parsed.scheme and parsed.netloc, f"{url!r} is not fetchable"


async def test_the_gate_names_the_real_problem_for_a_hostless_url(db_conn):
    from hi.fetcher import PolicyDenied, browser_gate

    with pytest.raises(PolicyDenied, match="no host"):
        await browser_gate("linkedin.com/in/ajith-b", adapter=lp.ADAPTER)


# --- wall detection is ours, not the vendored heuristic -----------------------


class FakePage:
    def __init__(self, url, text=""):
        self.url = url
        self._text = text

    async def inner_text(self, _selector):
        return self._text


@pytest.mark.parametrize("phrase", ["try again later", "slow down", "rate limit"])
def test_the_vendored_loose_phrases_do_not_count_as_walls(phrase):
    """These fail healthy profiles. Verified live 2026-08-27: three good profiles in a
    row were rejected by `linkedin_scraper.detect_rate_limit` on a phrase that was not
    even on the profile page. A false wall trips the kill switch, so it is not benign.
    """
    assert lp.blocked_by(f"Ajith B\nBackend Developer\nPlease {phrase}.") is None


async def test_refuse_walls_trips_on_a_challenge_url():
    with pytest.raises(lp.ModeCBlocked, match="challenge or authwall"):
        await lp.refuse_walls(FakePage("https://www.linkedin.com/checkpoint/challenge/x"))
    with pytest.raises(lp.ModeCBlocked):
        await lp.refuse_walls(FakePage("https://www.linkedin.com/authwall?x=1"))


async def test_refuse_walls_trips_on_a_real_wall_marker():
    page = FakePage("https://www.linkedin.com/in/x", "Join LinkedIn to see this profile")
    with pytest.raises(lp.ModeCBlocked, match="served a wall"):
        await lp.refuse_walls(page)


async def test_refuse_walls_passes_a_real_profile():
    await lp.refuse_walls(FakePage("https://www.linkedin.com/in/x", PAGE_TEXT))  # no raise


def test_the_banned_sub_pages_are_never_fetched():
    """What this adapter loads, and what it must never load."""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(lp.scrape_profile))
    # Actual calls only — a docstring or comment naming a getter is documentation, not
    # a fetch, and this check has to be about what the code does.
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    # No vendored parser is used at all any more: they return empty against
    # LinkedIn's obfuscated markup, and a parser that silently returns nothing is
    # worse than no parser. We navigate and read text ourselves.
    for dead in ("_get_experiences", "_get_educations", "_get_accomplishments",
                 "_get_interests", "_get_contacts", "_get_name_and_location", "scrape"):
        assert dead not in called, f"{dead} returns empty against current markup"

    # What it must and must not load, by URL.
    urls = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "details/experience/" in urls, "employment history is the point of the adapter"
    assert "details/education/" in urls, "authorised 2026-08-27, ARCHITECTURE.md §2.2"
    for banned in ("details/interests/", "contact-info", "overlay/contact-info/"):
        assert banned not in urls, f"{banned} fetches data nothing asked for"


def test_our_wall_check_replaces_the_vendored_one():
    import inspect

    source = inspect.getsource(lp.scrape_profile)
    assert "check_rate_limit" in source, "the vendored detector must be substituted out"


# --- sub-page URLs and pacing -------------------------------------------------


def test_profile_base_keeps_the_person_in_sub_page_urls():
    """The 2026-08-27 bug: `urljoin` drops the last segment without a trailing slash, so
    every details page was requested for a nonexistent profile — and those error pages
    say "try again later", which is what looked like a rate limit.
    """
    from urllib.parse import urljoin

    stored = "https://linkedin.com/in/ajith-b-0b9b70156"  # exactly what discovery saves
    base = lp.profile_base(stored)
    for section in ("details/experience/", "details/education/", "details/patents/"):
        built = urljoin(base, section)
        assert "ajith-b-0b9b70156" in built, f"{built} lost the person"
        assert built.startswith(stored)


def test_profile_base_does_not_double_a_slash():
    assert lp.profile_base("https://linkedin.com/in/x/") == "https://linkedin.com/in/x/"


async def test_every_navigation_is_paced_not_just_every_profile(monkeypatch):
    """A profile costs several page loads now. Firing them back to back is the burst
    signature mode C exists to avoid, so the pacing hangs off each navigation.
    """
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(lp.asyncio, "sleep", fake_sleep)
    await lp.after_navigation(FakePage("https://www.linkedin.com/in/x", PAGE_TEXT))
    assert len(slept) == 1
    assert lp.SUBPAGE_DELAY_SECONDS <= slept[0] <= (
        lp.SUBPAGE_DELAY_SECONDS + lp.SUBPAGE_JITTER_SECONDS
    )


async def test_a_wall_stops_the_run_before_the_pacing_sleep(monkeypatch):
    """Order matters: on a wall we abort, and must not sit waiting first."""
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(lp.asyncio, "sleep", fake_sleep)
    with pytest.raises(lp.ModeCBlocked):
        await lp.after_navigation(FakePage("https://www.linkedin.com/authwall"))
    assert slept == []


def test_accomplishments_do_not_walk_eight_sections():
    """Eight sections is eight page loads per candidate. The vendored list was trimmed
    to the three that bear on an engineer; see the LOCAL MODIFICATION comment there.
    """
    import inspect

    from linkedin_scraper.scrapers.person import PersonScraper

    source = inspect.getsource(PersonScraper._get_accomplishments)
    for wanted in ("certifications", "publications", "patents"):
        assert f'("{wanted}"' in source
    for dropped in ("honors", "courses", "languages", "organizations"):
        assert f'("{dropped}"' not in source, f"{dropped} costs a page load for nothing"


# --- text parsing of the details pages, against LIVE-captured fixtures --------
#
# Recorded 2026-08-27 from a real profile. LinkedIn ships obfuscated class names, so
# these are text, not HTML: that is the whole point of parsing text here.

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin_profile"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_experience_page_yields_every_dated_role():
    exps = lp.parse_experience_text(fixture("experience_details.txt"))
    assert [(e.position_title, e.institution_name) for e in exps] == [
        ("Associate System Analyst", "NuSummit"),
        ("Back End Developer", "ReelUp"),
        ("Back End Developer", "Mool"),
        ("Software Engineer", "Happiest Minds Technologies"),
        ("Senior Compliance Associate", "Amazon"),
    ]


def test_employment_type_is_stripped_from_the_employer():
    """The line reads 'NuSummit · Full-time'. The employer is not called that."""
    exps = lp.parse_experience_text(fixture("experience_details.txt"))
    assert all("Full-time" not in (e.institution_name or "") for e in exps)


def test_a_current_role_has_no_end_date():
    exps = lp.parse_experience_text(fixture("experience_details.txt"))
    current = exps[0]
    assert current.from_date == "Aug 2025"
    assert current.to_date is None, "'Present' must become open-ended, not a literal"


def test_the_sidebar_of_other_people_is_not_read_as_employment():
    """'More profiles for you' lists strangers with job titles. Reading past it would
    attribute their jobs to this candidate — the worst bug this system can produce.
    """
    exps = lp.parse_experience_text(fixture("experience_details.txt"))
    names = {e.institution_name for e in exps}
    assert not any(n and "3rd" in n for n in names)
    assert len(exps) == 5, "stopped at the section boundary"


def test_real_profile_years_are_computed_with_gaps_excluded():
    exps = lp.parse_experience_text(fixture("experience_details.txt"))
    years = lp.total_years(lp.spans(exps, today=date(2026, 8, 27)))
    # May 2020->Jan 2022, Feb 2022->Dec 2023, Apr 2024->Jan 2025, Apr->Aug 2025,
    # Aug 2025->today. The two gaps are real and must not be counted as worked.
    assert years == pytest.approx(5.6, abs=0.2)


# --- IMPLEMENTATION.md §2.1a: the three experience layouts --------------------
#
# The fixture reproduces the layouts found in four cached profile pages on 2026-08-28,
# with synthetic names. Before the fix, four `employer` claims in the dev database were
# "Full-time", "Internship", "Senior Software Engineer" and "Python Developer".


def promotion_group():
    return lp.parse_experience_text(fixture("experience_promotion_group.txt"))


def test_all_three_layouts_parse_to_the_right_employer():
    assert [(e.position_title, e.institution_name) for e in promotion_group()] == [
        # one role, one company, type appended to the company line
        ("Lead Solution Engineer", "Naviq Labs"),
        # promotion group: employer named once, inner roles carry only a title
        ("Senior Software Engineer", "Hyreo Systems"),
        ("Software Engineer", "Hyreo Systems"),
        # promotion group where the type sits on its own line
        ("Associate Software Engineer", "Data N Stats"),
        ("Intern", "Data N Stats"),
        # the person entered "Freelance" as the organisation, so that is what it says
        ("Physics Tutor", "Freelance"),
    ]


@pytest.mark.parametrize(
    "value", ["Full-time", "Part-time", "Internship", "Contract", "Temporary"]
)
def test_an_employment_type_is_never_an_employer(value):
    """A recruiter reading "Employer: Full-time" stops trusting the card."""
    employers = {e.institution_name for e in promotion_group()}
    assert value not in employers


def test_a_job_title_is_never_read_as_an_employer():
    """The group layout has no company line, so the old fixed two-line offset took the
    title as the employer and the previous entry's location as the title."""
    employers = {e.institution_name for e in promotion_group()}
    assert "Senior Software Engineer" not in employers
    assert "Software Engineer" not in employers


def test_a_bullet_or_skills_line_is_never_read_as_a_title():
    titles = {e.position_title for e in promotion_group()}
    assert not any(t.startswith(("●", "•", "Skills:")) for t in titles)
    assert not any("Kerala" in t or "Delhi" in t for t in titles), "a location is not a title"


def test_the_group_closes_when_a_standalone_entry_follows():
    """Otherwise an employer named once bleeds forward onto every later role."""
    exps = promotion_group()
    assert exps[-1].institution_name == "Freelance"  # not "Data N Stats"


def test_the_common_layout_still_parses_exactly_as_before():
    """This widens the parser; it must not change what already worked."""
    exps = lp.parse_experience_text(fixture("experience_details.txt"))
    assert [(e.position_title, e.institution_name) for e in exps] == [
        ("Associate System Analyst", "NuSummit"),
        ("Back End Developer", "ReelUp"),
        ("Back End Developer", "Mool"),
        ("Software Engineer", "Happiest Minds Technologies"),
        ("Senior Compliance Associate", "Amazon"),
    ]


def test_the_sidebar_is_still_not_read_as_employment_in_a_group_layout():
    exps = promotion_group()
    assert not any("3rd" in (e.institution_name or "") for e in exps)
    assert len(exps) == 6, "stopped at the section boundary"


def test_a_duration_line_is_not_mistaken_for_a_dated_entry():
    assert lp._is_duration_header("1 yr 7 mos")
    assert lp._is_duration_header("Full-time · 2 yrs 3 mos")
    # A real entry's date line also ends in a duration and must not match.
    assert not lp._is_duration_header("Jun 2021 - Jul 2022 · 1 yr 2 mos")


def test_a_location_line_is_not_mistaken_for_a_company_line():
    assert lp._is_company_line("Naviq Labs · Full-time")
    # Same shape, but it ends in a work mode rather than an employment type.
    assert not lp._is_company_line("Bengaluru, Karnataka, India · Hybrid")
    assert not lp._is_company_line("New Delhi, Delhi, India · Remote")


def test_education_page_yields_institution_and_degree_but_no_dates():
    edus = lp.parse_education_text(fixture("education_details.txt"))
    assert len(edus) == 1
    assert edus[0].institution_name == "Don Bosco Institute of Technology, BANGALORE"
    assert "Bachelor of Technology" in edus[0].degree
    assert edus[0].from_date is None and edus[0].to_date is None


def test_education_dates_are_used_to_find_the_entry_then_discarded():
    """'2016 – 2020' is how the entry is located. It must not survive into the model."""
    assert "2016" not in str(lp.parse_education_text(fixture("education_details.txt")))


def test_a_page_with_no_dated_entries_yields_nothing_rather_than_guesses():
    assert lp.parse_experience_text("Experience\n\nSome Title\n\nSome Company\n") == []
    assert lp.parse_education_text("Education\n\nNothing to see for now\n") == []


def test_year_only_and_en_dash_ranges_are_both_understood():
    exps = lp.parse_experience_text("Experience\nEngineer\nAcme\n2019 – 2021\n")
    assert len(exps) == 1
    assert (exps[0].from_date, exps[0].to_date) == ("2019", "2021")


# --- skills: what makes the product work with no GitHub at all ----------------


def test_skills_page_yields_candidate_skills_without_context_lines():
    lines = lp.parse_skills_text(fixture("skills_details.txt"))
    assert "ClickHouse" in lines
    assert "React.js" in lines
    # "Amazon SQS / Back End Developer at ReelUp" — the second line is context, and an
    # unknown skill is recorded as a review proposal, so junk here poisons that queue.
    assert not any(" at " in line for line in lines)
    for tab in ("All", "Skills", "Industry Knowledge", "Tools & Technologies"):
        assert tab not in lines


def test_only_canon_known_skills_become_evidence():
    """Same rule the GitHub adapter uses for languages: canon decides, never a guess."""
    lines = lp.parse_skills_text(fixture("skills_details.txt"))
    rows, _ = lp.rows_from_person(
        make_person(experiences=[], educations=[]),
        page_text=fixture("skills_details.txt"),
        source_url=PROFILE_URL,
        skill_lines=lines,
        today=date(2026, 8, 27),
    )
    skills = {r.claim_key for r in rows if r.claim_type == "skill"}
    assert "ClickHouse" in skills
    assert "React" in skills, "React.js must be canonicalised, not stored raw"
    assert "Server Side Programming" not in skills, "unknown stays a proposal"


def test_a_listed_skill_is_self_reported_never_proven():
    """ARCHITECTURE.md §2: the three tiers never merge. 'Lists Python' is not 'has 40
    Python repos', and only GitHub can produce the second.
    """
    rows, _ = lp.rows_from_person(
        make_person(experiences=[], educations=[]),
        page_text=fixture("skills_details.txt"),
        source_url=PROFILE_URL,
        skill_lines=lp.parse_skills_text(fixture("skills_details.txt")),
        today=date(2026, 8, 27),
    )
    for r in (r for r in rows if r.claim_type == "skill"):
        assert r.tier == "self_reported"
        assert r.value_num is None, "no volume without an artefact to measure"


def test_listed_skills_match_in_full_and_code_still_adds_depth():
    """Scraped data is taken as true, so a listed skill matches as fully as a proven one.
    On an engineering role, public code still earns `skill_depth` on top of that."""
    from datetime import datetime, timezone

    from hi.models import RoleSpec
    from hi.scoring import Evidence, evaluate

    spec = RoleSpec(must_have_skills=["Python"])
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)

    def ev(tier, value_num=None):
        return [
            Evidence(
                claim_type="skill", claim_key="Python", claim_value="Python",
                value_num=value_num, tier=tier, observed_at=now,
            )
        ]

    listed = evaluate(spec, ev("self_reported"), now=now)
    proven = evaluate(spec, ev("artifact_backed", 100_000.0), now=now)
    nothing = evaluate(spec, [], now=now)

    assert nothing.components["skill_match"] == 0.0
    assert listed.components["skill_match"] == proven.components["skill_match"] == 1.0
    assert listed.score < proven.score, "public code is still extra evidence of depth"


# --- the employer regression --------------------------------------------------


def test_employer_rows_survive_the_banned_signal_check(db_conn):
    """'employer' is a substring of 'current_employer_prestige', so the fuzzy check
    silently refused every employment row on 2026-08-27. claim_type is a closed enum
    and is checked exactly; only keys and values go through the fuzzy match.
    """
    candidate_id = uuid.uuid4()
    db_conn.execute("insert into candidate (id) values (%s)", (candidate_id,))
    rows, _ = rows_for(make_person())
    employers = [r for r in rows if r.claim_type == "employer"]
    assert employers, "the fixture profile has employers"

    outcome = write_evidence(candidate_id, employers, source_text=None)
    assert outcome.rejected_banned == 0, outcome.reasons
    assert outcome.written == len(employers)


def test_a_real_prestige_signal_is_still_refused(db_conn):
    """The ban itself must stay intact — only the claim_type collision was wrong."""
    from hi.extract import EvidenceRow

    candidate_id = uuid.uuid4()
    db_conn.execute("insert into candidate (id) values (%s)", (candidate_id,))
    outcome = write_evidence(
        candidate_id,
        [
            EvidenceRow(
                claim_type="employer",
                claim_key="current_employer_prestige",
                claim_value="tier 1",
                tier="self_reported",
                source_url=PROFILE_URL,
                snippet="works at a top-tier company",
            )
        ],
        source_text=None,
    )
    assert outcome.written == 0
    assert outcome.rejected_banned == 1


def test_enrichment_budget_goes_to_refs_that_can_actually_pass(db_conn):
    """30 profiles a day, so the order matters more than it looks.

    Discovery runs a broad title query first, so discovery order front-loads people of
    every stack. On 2026-08-27 that spent all three profiles of a run on Java and Node
    developers for a Python role and produced `passed_gates 0`, while the refs from the
    Python query waited. Refs whose snippet already names a must-have go first.
    """
    from hi import discovery
    from hi.models import RoleSpec

    role_id = discovery.create_role("Backend Engineer", RoleSpec(must_have_skills=["Python"]))
    for slug, headline in [
        ("java-dev", "Backend Engineer at Acme | Java, Spring Boot"),
        ("python-dev", "Python Developer at Globex"),
    ]:
        db_conn.execute(
            "insert into candidate_ref (role_id, adapter, ref_kind, ref_value, snippet_raw, "
            " snippet_headline, source_url, gate_state) "
            "values (%s, 'linkedin_serp', 'linkedin_url', %s, %s, %s, %s, 'passed')",
            (role_id, f"linkedin.com/in/{slug}", headline, headline,
             f"https://linkedin.com/in/{slug}"),
        )

    targets = lp.targets_for_role(role_id)
    assert [url.rsplit("/", 1)[-1] for _, url in targets] == ["python-dev", "java-dev"]


def test_prioritising_does_not_drop_anyone(db_conn):
    """It reorders the queue; it must never shorten it."""
    from hi import discovery
    from hi.models import RoleSpec

    role_id = discovery.create_role("Backend Engineer", RoleSpec(must_have_skills=["Rust"]))
    for slug in ("a", "b", "c"):
        db_conn.execute(
            "insert into candidate_ref (role_id, adapter, ref_kind, ref_value, snippet_raw, "
            " source_url, gate_state) "
            "values (%s, 'linkedin_serp', 'linkedin_url', %s, 'nothing relevant', %s, 'passed')",
            (role_id, f"linkedin.com/in/{slug}", f"https://linkedin.com/in/{slug}"),
        )
    assert len(lp.targets_for_role(role_id)) == 3


# --- a dropped connection is not a thin profile -------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Page.goto: net::ERR_CONNECTION_CLOSED at https://linkedin.com/in/x/",
        "Timeout 60000ms exceeded",
        "net::ERR_NAME_NOT_RESOLVED",
        "Target closed",
    ],
)
def test_network_errors_are_recognised_as_transient(message):
    assert lp.is_transient(RuntimeError(message))


def test_a_parse_failure_is_not_transient():
    """Only the network gets a second chance. A real bug must surface, not retry."""
    assert not lp.is_transient(ValueError("could not parse the experience section"))


async def test_a_transient_error_is_retried_once_then_reported(monkeypatch):
    calls = []

    async def flaky(page, url):
        calls.append(url)
        raise RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED")

    monkeypatch.setattr(lp, "scrape_profile", flaky)
    monkeypatch.setattr(lp.asyncio, "sleep", lambda _s: asyncio_noop())

    async def asyncio_noop():
        return None

    with pytest.raises(lp.TransientFetchError):
        await lp.scrape_with_retry(object(), PROFILE_URL)
    assert len(calls) == 2, "one retry, not four — a human is watching this browser"


async def test_a_wall_is_never_retried(monkeypatch):
    calls = []

    async def walled(page, url):
        calls.append(url)
        raise lp.ModeCBlocked("served a wall")

    monkeypatch.setattr(lp, "scrape_profile", walled)
    with pytest.raises(lp.ModeCBlocked):
        await lp.scrape_with_retry(object(), PROFILE_URL)
    assert len(calls) == 1, "retrying a block turns a soft block into a hard one"


async def test_a_network_drop_leaves_the_yield_denominator(db_conn, monkeypatch):
    """The 1.4a number is a ratio, so a profile we never saw must not be in it."""
    async def dropped(page, url):
        raise lp.TransientFetchError("net::ERR_CONNECTION_CLOSED")

    monkeypatch.setattr(lp, "scrape_with_retry", dropped)
    monkeypatch.setattr(lp, "spent_today", lambda: 0)

    import contextlib as _ctx

    @_ctx.asynccontextmanager
    async def fake_page():
        yield object()

    monkeypatch.setattr(lp, "attached_page", fake_page)
    monkeypatch.setattr(lp.asyncio, "sleep", lambda _s: _noop())

    async def _noop():
        return None

    result = await lp.enrich([(None, PROFILE_URL)])
    assert result.attempted == 0
    assert result.transient_failures == 1
    assert result.failed == 0, "a network drop is not an extraction failure"
    assert result.dated_yield == 0.0  # 0/0, not 0/1


def test_a_django_developer_earns_python_evidence():
    """The fix for 34-of-34-ruled-out. The snippet must show the basis, so a recruiter
    can see Python was inferred from Django rather than claimed on the profile.
    """
    page = PAGE_TEXT + "\nDjango\nSpring Boot\n"
    rows, _ = lp.rows_from_person(
        make_person(experiences=[], educations=[]),
        page_text=page,
        source_url=PROFILE_URL,
        skill_lines=["Django", "Spring Boot"],
        today=date(2026, 8, 27),
    )
    skills = {r.claim_key: r for r in rows if r.claim_type == "skill"}
    assert {"Django", "Python", "Spring Boot", "Java"} <= set(skills)
    assert "Django is written in Python" in skills["Python"].snippet
    assert skills["Python"].tier == "self_reported", "an inference is never promoted"


def test_an_implied_skill_closes_the_gate_that_ruled_everyone_out():
    from datetime import datetime, timezone

    from hi.models import RoleSpec
    from hi.scoring import Evidence, evaluate

    spec = RoleSpec(must_have_skills=["Python"])
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    rows, _ = lp.rows_from_person(
        make_person(experiences=[], educations=[]),
        page_text=PAGE_TEXT + "\nDjango\n",
        source_url=PROFILE_URL,
        skill_lines=["Django"],
        today=date(2026, 8, 27),
    )
    as_scored = [
        Evidence(
            claim_type=r.claim_type, claim_key=r.claim_key, claim_value=r.claim_value,
            value_num=r.value_num, tier=r.tier, observed_at=r.observed_at,
        )
        for r in rows
    ]
    result = evaluate(spec, as_scored, now=now)
    assert "fail" not in result.gates.get("must_have_skills", "")


def test_the_daily_budget_buys_new_people_not_repeats(db_conn):
    """"25 unique profiles a day" is only true if a run skips people already read.

    `only_missing_keys` did not deliver that: it looks for a GitHub strong key, almost
    nobody has one, so every candidate looked equally un-enriched and a second run
    re-read the same profiles at full cost.
    """
    from hi import discovery, identity
    from hi.extract import EvidenceRow
    from hi.models import RoleSpec

    role_id = discovery.create_role("Backend Engineer", RoleSpec(must_have_skills=["Python"]))
    for slug in ("already-read", "never-read"):
        db_conn.execute(
            "insert into candidate_ref (role_id, adapter, ref_kind, ref_value, snippet_raw, "
            " source_url, gate_state) "
            "values (%s, 'linkedin_serp', 'linkedin_url', %s, 'Backend Engineer', %s, 'passed')",
            (role_id, f"linkedin.com/in/{slug}", f"https://linkedin.com/in/{slug}"),
        )

    # Enrich one of them, the way a previous day's run would have.
    resolved = identity.resolve({"linkedin_slug": "already-read"})
    write_evidence(
        resolved.candidate_id,
        [
            EvidenceRow(
                claim_type="title", claim_key="engineer", claim_value="Engineer",
                tier="self_reported", source_url="https://linkedin.com/in/already-read",
                snippet="Engineer — listed on LinkedIn profile",
                extractor=lp.ADAPTER, extractor_version=lp.EXTRACTOR_VERSION,
            )
        ],
        source_text=None,
    )

    fresh = [url for _, url in lp.targets_for_role(role_id)]
    assert any("never-read" in u for u in fresh)
    assert not any("already-read" in u for u in fresh), "budget spent re-reading a profile"

    # A refresh is a deliberate opt-in, because a profile does gain a new job.
    refresh = [url for _, url in lp.targets_for_role(role_id, skip_enriched=False)]
    assert len(refresh) == 2


def test_only_narrows_targets_to_named_slugs():
    targets = [
        (1, "https://www.linkedin.com/in/alice-1234/"),
        (2, "https://www.linkedin.com/in/bob-5678/"),
        (3, "https://www.linkedin.com/in/carol-9012/"),
    ]
    kept = lp.filter_to_slugs(targets, ["carol-9012", "alice-1234"])
    assert [c for c, _ in kept] == [1, 3]  # target order is preserved, not the argument order


def test_only_refuses_a_slug_it_cannot_find():
    """A typo must fail loudly. Scraping 1 of 2 named people looks like success."""
    targets = [(1, "https://www.linkedin.com/in/alice-1234/")]
    try:
        lp.filter_to_slugs(targets, ["alice-1234", "alice-1243"])
    except ValueError as exc:
        assert "alice-1243" in str(exc)
    else:
        raise AssertionError("expected a refusal for the unmatched slug")


async def test_a_second_read_refuses_itself_while_one_is_running(db_conn):
    """The lock is inside `enrich`, so the button, the worker and the CLI all get it.

    Guarding at the call-sites instead would mean the next call-site added is the one that
    puts two runs on one LinkedIn account (ARCHITECTURE.md §7.4).
    """
    from hi import lock

    with lock.exclusive():
        result = await lp.enrich([(None, PROFILE_URL)])

    assert result.attempted == 0, "it must not reach LinkedIn at all"
    assert "already running" in result.stopped_reason


async def test_a_read_takes_the_lock_and_gives_it_back(db_conn, monkeypatch):
    from hi import lock

    seen = []

    @contextlib.asynccontextmanager
    async def no_browser():
        seen.append(lock.held())
        yield None

    monkeypatch.setattr(lp, "attached_page", no_browser)
    await lp.enrich([])

    assert seen == [True]
    assert lock.held() is False


def test_the_cdp_check_is_a_socket_probe_not_a_page_load(db_conn):
    """It runs on every page render, so it has to be cheap and it must never launch one."""
    assert lp.browser_attached() in (True, False)


def test_the_kill_switch_is_visible_as_a_reason(db_conn):
    # Mode C ships disabled (the delivered product uses no LinkedIn account), so a fresh
    # database already refuses — and says why once a human has recorded a reason.
    assert lp.policy_blocked() is not None
    with pool.connection() as conn:
        conn.execute(
            "update source_policy set enabled = false, disabled_reason = 'HTTP 999' "
            "where domain = 'linkedin.com'"
        )
    assert lp.policy_blocked() == "HTTP 999"
