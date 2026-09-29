"""Hand-off checklist guards (ARCHITECTURE.md §9a.5).

These are cheap invariants that catch documentation rotting away from the code — the
failure mode that turns a maintainable tool into an unmaintainable one six months after
the author leaves.
"""

import re
from pathlib import Path

import pytest

from hi.config import Settings

ROOT = Path(__file__).parents[1]
RUNBOOK = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

# The eight required entries, ARCHITECTURE.md §9a.3.8.
REQUIRED_RUNBOOK_TOPICS = {
    "no candidates found": ["found no candidates", "ruled out"],
    "nothing found at all": ["Nothing is found at all", "refresh_fixture"],
    "enrichment empty": ["employment history are missing", "estimated from"],
    "source shows red": ["shows red", "source_policy"],
    "it worked last month": ["worked last month", "changed its layout"],
    "rotate a key": ["Rotating a key", "SERPAPI_KEY", "ANTHROPIC_API_KEY"],
    "restore a backup": ["Restore a backup", "gunzip"],
    "turn linkedin off": ["Turn LinkedIn off", "keep working"],
}


@pytest.mark.parametrize("topic,markers", REQUIRED_RUNBOOK_TOPICS.items())
def test_runbook_covers_every_required_symptom(topic, markers):
    missing = [m for m in markers if m.lower() not in RUNBOOK.lower()]
    assert not missing, f"RUNBOOK.md is missing '{topic}' (no mention of {missing})"


def test_runbook_names_refresh_fixture_as_the_first_diagnostic():
    assert "scripts/refresh_fixture.py" in RUNBOOK


def test_every_secret_setting_is_documented_with_where_to_get_it():
    """A new secret must arrive in .env.example in the same change.

    Rotation has to be possible by someone who has never opened the codebase, which
    means every key needs a name AND a source URL.
    """
    secret_fields = [
        name
        for name in Settings.model_fields
        if any(word in name for word in ("key", "token", "password", "secret", "url"))
    ]
    assert secret_fields, "expected the settings model to contain secrets"

    for field in secret_fields:
        assert field.upper() in ENV_EXAMPLE, f"{field.upper()} is not listed in .env.example"


@pytest.mark.parametrize("key", ["SERPAPI_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "HI_USERS"])
def test_each_key_says_where_to_get_a_new_one(key):
    # Find the comment block immediately above the key and require a URL or a command.
    index = ENV_EXAMPLE.index(key)
    preceding = ENV_EXAMPLE[max(0, index - 600) : index]
    assert re.search(r"https://|python -m hi\.auth", preceding), (
        f"{key} has no 'where to get a new one' note in .env.example"
    )


def test_env_example_warns_that_no_users_means_locked_out():
    assert "HI_USERS" in ENV_EXAMPLE
    assert "fails closed" in ENV_EXAMPLE.lower() or "nobody can sign in" in ENV_EXAMPLE.lower()


def test_no_real_secret_leaked_into_env_example():
    # Placeholder values only — every key line must be empty or a non-secret default.
    for line in ENV_EXAMPLE.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        name, _, value = line.partition("=")
        if any(w in name.lower() for w in ("key", "token", "password", "secret")):
            assert not value.strip(), f"{name} has a value in .env.example — is that a real secret?"


def test_backup_script_exists_and_is_documented():
    assert (ROOT / "scripts" / "backup.sh").exists()
    assert "backup.sh" in RUNBOOK


def test_selectors_live_in_exactly_one_file():
    """ARCHITECTURE.md §9a.3.3 — a markup fix must be a one-file diff.

    Checks string *literals in code*, not docstrings: a docstring naming the field it
    parses is useful documentation, whereas `result.get("rich_snippet")` in a second
    file is the thing that turns a one-line fix into a hunt.
    """
    import ast

    selectors_file = ROOT / "src" / "hi" / "adapters" / "linkedin_selectors.py"
    assert selectors_file.exists()

    owned = ["rich_snippet", "organic_results"]
    for path in (ROOT / "src" / "hi" / "adapters").glob("*.py"):
        if path.name == "linkedin_selectors.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        docstrings = {
            doc
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and (doc := ast.get_docstring(node, clean=False))
        }
        offenders = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
            and any(term in node.value for term in owned)
        ]
        assert not offenders, (
            f"{path.name} hard-codes {offenders} — those belong in linkedin_selectors.py "
            "so a shape change stays a one-file diff"
        )


# --- candidate data is never deleted (ARCHITECTURE.md §2.5) -------------------

# The one module allowed to delete a person, when erasure is built. It fires on a
# recorded request from a named individual, never on a timer. Add nothing else here
# without a decision in ARCHITECTURE.md §2.5 — the point of the list is that a cleanup
# job cannot appear quietly somewhere else.
ERASURE_MODULES = {"erasure.py"}

PROTECTED_TABLES = ("candidate", "evidence", "candidate_ref", "match", "identity")


def test_nothing_in_the_product_deletes_candidate_data():
    """ARCHITECTURE.md §2.5 — the owner's decision is that the corpus is kept, always.

    Retention deletion was cancelled on 2026-08-28. This guard is what makes that
    durable: a nightly "tidy up old candidates" job is the kind of thing that gets
    added later by someone who has not read §2.5, and it would silently destroy the
    asset. Erasure on individual request is the sole exception, and it lives in one
    named module so it is reviewable.

    Session rows are not candidate data and are deleted freely (`auth.py`).
    """
    pattern = re.compile(
        r"""delete\s+from\s+"?(""" + "|".join(PROTECTED_TABLES) + r""")"?\b""",
        re.IGNORECASE,
    )

    offenders = []
    for path in (ROOT / "src" / "hi").rglob("*.py"):
        if path.name in ERASURE_MODULES:
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)!r}")

    assert not offenders, (
        "candidate data is kept indefinitely (ARCHITECTURE.md §2.5). These deletes must "
        "go, or move into the erasure module and be allowlisted here: " + "; ".join(offenders)
    )


def test_a_merge_archives_the_duplicate_rather_than_deleting_it():
    """Reversibility depends on the losing candidate still existing."""
    source = (ROOT / "src" / "hi" / "identity.py").read_text(encoding="utf-8")
    assert "archived" in source
    assert not re.search(r"delete\s+from\s+\"?candidate\"?\b", source, re.IGNORECASE)


def test_no_retention_setting_exists_to_drive_a_cleanup_job():
    """`RETENTION_DAYS` was the knob the cancelled nightly job would have read."""
    assert not hasattr(Settings(), "retention_days")
