import pytest

from hi import canon

SKILL_CASES = [
    ("reactjs", "React"),
    ("React.js", "React"),
    ("REACT", "React"),
    ("Node.js", "Node.js"),
    ("nodejs", "Node.js"),
    ("K8s", "Kubernetes"),
]


@pytest.mark.parametrize("raw,expected", SKILL_CASES)
def test_canonical_skill_normalises(db_conn, raw, expected):
    assert canon.canonical_skill(raw) == expected


def test_canonical_skill_unknown_returns_none_and_proposes(db_conn):
    assert canon.canonical_skill("Quantum Basket Weaving") is None

    row = db_conn.execute(
        "select raw_text, status from skill_alias_proposal where raw_text = %s",
        ("Quantum Basket Weaving",),
    ).fetchone()
    assert row == ("Quantum Basket Weaving", "open")


def test_region_of_resolves_member_cities():
    assert canon.region_of("Gurgaon") == "IN-DL-NCR"
    assert canon.region_of("Bengaluru") == "IN-KA-BLR"
    assert canon.region_of("Nowhereville") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The form SERP actually returns — an exact-match-only lookup resolves
        # none of these, which silently disables the location gate.
        ("Bengaluru, Karnataka, India", "IN-KA-BLR"),
        ("Bangalore, Karnataka, India", "IN-KA-BLR"),
        ("Pune, Maharashtra, India", "IN-MH-PUN"),
        ("Greater Noida, Uttar Pradesh, India", "IN-DL-NCR"),
        ("Electronic City, Bengaluru", "IN-KA-BLR"),
        # Coarser tiers resolve to coarser codes; the gate matches hierarchically,
        # so these are "not ruled out" rather than "unreadable".
        ("India", "IN"),
        ("Karnataka, India", "IN-KA"),
        ("Bangalore Urban, Karnataka, India", "IN-KA"),
        # A real place in the wrong state must resolve, or the gate cannot reject it.
        ("Abohar, Punjab, India", "IN-PB"),
        ("Kochi, Kerala, India", "IN-KL-KOC"),
        ("", None),
        ("Nowhereville", None),
    ],
)
def test_region_of_handles_multipart_locations(raw, expected):
    assert canon.region_of(raw) == expected


def test_migrate_seeds_skill_table(db_conn):
    count = db_conn.execute("select count(*) from skill").fetchone()[0]
    assert count > 100


# --- framework implies language (2026-08-27) ----------------------------------


def test_a_framework_implies_the_language_it_is_written_in():
    """Found on live data: LinkedIn Skills sections name frameworks where GitHub reports
    languages, so a `must_have: Python` gate ruled out every Django developer.
    """
    assert canon.implied_skills("Django") == ["Python"]
    assert canon.implied_skills("Spring Boot") == ["Java"]
    assert canon.implied_skills("React") == ["JavaScript"]


def test_a_language_implies_nothing_and_neither_does_a_neutral_tool():
    assert canon.implied_skills("Python") == []
    assert canon.implied_skills("Selenium") == [], "usable from many languages"
    assert canon.implied_skills("Kubernetes") == []


def test_implications_are_not_transitive():
    """"React implies JavaScript" is a fact; a chain of implications is a guess."""
    for skill in ("Django", "React", "Spring Boot"):
        for implied in canon.implied_skills(skill):
            assert canon.implied_skills(implied) == []


def test_every_implied_skill_is_itself_canonical():
    """An implication pointing at a name the canon does not know is a silent dead end."""
    import yaml
    from pathlib import Path

    data = yaml.safe_load(
        (Path(__file__).parents[1] / "data" / "skills.yaml").read_text(encoding="utf-8")
    )
    for name, entry in data.items():
        for implied in (entry.get("implies") or []):
            assert canon.canonical_skill(implied) == implied, (
                f"{name} implies {implied!r}, which is not a canonical skill name"
            )


def test_an_unknown_skill_still_implies_nothing_rather_than_raising():
    assert canon.implied_skills("Whatever The Recruiter Typed") == []


# --- non-technical vocabulary (2026-08-27) ------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Financial Modelling", "Financial Modeling"),  # both spellings, one skill
        ("Financial Modeling", "Financial Modeling"),
        ("P&L", "Profit and Loss Management"),
        ("FP&A", "Financial Planning and Analysis"),
        ("M&A", "Mergers and Acquisitions"),
        ("GTM", "Go-to-Market Strategy"),
        ("Supply Chain", "Supply Chain Management"),
        ("MS Excel", "Excel"),
        ("SFDC", "Salesforce"),
        ("CFA", "Chartered Financial Analyst"),
        ("Search Engine Optimisation", "Search Engine Optimization"),
    ],
)
def test_business_skills_and_their_abbreviations_resolve(text, expected):
    """Before this the canon was engineering-only and every MBA candidate failed the
    must-have gate: of 23 common business skills exactly one ("SQL") resolved.
    """
    assert canon.canonical_skill(text) == expected


def test_the_engineering_canon_still_resolves():
    """Adding 110 business entries must not disturb the ones the tech roles use."""
    for text, expected in [
        ("reactjs", "React"), ("Golang", "Go"), ("K8s", "Kubernetes"),
        ("Python", "Python"), ("Node.js", "Node.js"),
    ]:
        assert canon.canonical_skill(text) == expected


def test_a_qualification_is_a_skill_and_not_education():
    """A CFA charter is a skill claim. `BANNED_SIGNALS` still refuses college and
    graduation year, and nothing here reintroduces them.
    """
    from hi.signals import is_banned

    for cert in ("CFA", "CA", "PMP"):
        canonical = canon.canonical_skill(cert)
        assert canonical is not None
        assert is_banned(canonical) is None, f"{canonical} must not read as a banned signal"


def test_no_business_skill_collides_with_a_banned_signal():
    """`is_banned` matches substrings both ways — the bug that silently refused every
    `employer` row. A new vocabulary is exactly where that recurs.
    """
    import yaml
    from pathlib import Path

    from hi.signals import is_banned

    data = yaml.safe_load(
        (Path(__file__).parents[1] / "data" / "skills.yaml").read_text(encoding="utf-8")
    )
    offenders = {}
    for name, entry in data.items():
        for candidate in (name, *(entry.get("aliases") or [])):
            if banned := is_banned(candidate):
                offenders[candidate] = banned
    assert not offenders, f"these skills would be refused by the writer: {offenders}"
