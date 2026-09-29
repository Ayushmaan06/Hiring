"""The snippet gate — IMPLEMENTATION.md 1.4b, ARCHITECTURE.md §3.

Runs BEFORE any page is fetched, using only what discovery already saw. Pure: no
I/O, no clock, no DB, so it is cheap to test exhaustively (same reason scoring.py
is pure).

Two rules shape every decision here:

1. **Never reject on absent evidence.** A snippet with no location is not a
   candidate outside the region — it is a candidate whose location we do not know.
   Rejecting those would silently discard good people and the recruiter could not
   tell the difference. Absent evidence is absent, not inferred.
2. **Every failure carries a reason.** The excluded list renders it verbatim, and a
   DB constraint refuses a failed ref with a null reason.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from hi.models import CandidateRef, GateVerdict, RoleSpec

# Words that carry no signal when matching a title, so "Sr. Backend Engineer at Acme"
# still matches a spec title of "Backend Engineer".
_TITLE_NOISE = {
    "a", "an", "the", "at", "of", "and", "or", "in", "for", "to", "with",
    "sr", "snr", "senior", "jr", "junior", "lead", "principal", "staff",
    "i", "ii", "iii", "iv", "l1", "l2", "l3", "l4", "l5",
}

# Interchangeable role words. The query planner searches all of them, so the gate
# must accept all of them too — otherwise discovery pays to find "Backend
# Developer" and the gate immediately throws them away.
ROLE_WORDS = ["Engineer", "Developer", "Programmer", "SDE"]
_ROLE_WORD_RE = re.compile(r"\b(engineer|developer|programmer|sde)s?\b", re.IGNORECASE)

# Location strings naming one of these are positively somewhere else. Needed
# because "India" as a search term also matches "Indonesia".
# ponytail: a short list of the countries that actually show up, not a full ISO set.
NON_INDIA_COUNTRIES = {
    "indonesia", "pakistan", "bangladesh", "srilanka", "nepal", "singapore",
    "malaysia", "philippines", "vietnam", "thailand", "unitedstates", "usa",
    "unitedkingdom", "uk", "canada", "australia", "germany", "netherlands",
    "ireland", "uae", "unitedarabemirates", "dubai", "qatar", "saudiarabia",
    "poland", "brazil", "mexico", "nigeria", "kenya", "egypt", "turkey", "china",
    "japan", "southkorea", "france", "spain", "italy", "sweden", "switzerland",
}


# The word boundaries matter twice over: "Indiana" must not read as India, and the word must not be
# found inside "Indonesia" either — both traps this project has already hit once.
_INDIA = re.compile(r"\bindia\b", re.I)


def _mentions_india(text: str) -> bool:
    """True when a location string names India, however unparseable the rest of it is.

    The safety valve on the rule above: "Karnataka, India" resolves to no city, and
    rejecting it would throw away exactly the people the role wants.
    """
    return bool(_INDIA.search(text))


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9+#]+", text.lower()) if t}


def _significant(text: str) -> set[str]:
    return _tokens(text) - _TITLE_NOISE


def _normalise_country(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def title_variants(title: str) -> list[str]:
    """Expand a title across interchangeable role words.

    "Backend Engineer" -> Backend Engineer / Developer / Programmer / SDE. A title
    with no role word is returned unchanged.
    """
    if not title:
        return []
    if not _ROLE_WORD_RE.search(title):
        return [title]
    return list(dict.fromkeys(_ROLE_WORD_RE.sub(w, title, count=1) for w in ROLE_WORDS))


def _title_matches(headline: str, titles: list[str]) -> bool:
    """True when any spec title (or role-word variant of it) is covered by the headline."""
    headline_tokens = _tokens(headline)
    for title in titles:
        for variant in title_variants(title):
            wanted = _significant(variant)
            if wanted and wanted <= headline_tokens:
                return True
    return False


def acceptable_titles(spec: RoleSpec) -> list[str]:
    """The titles that satisfy this spec, which is wider than `spec.titles`.

    A role of "Backend Engineer, must have Python" is also satisfied by someone
    titled "Python Developer" — and the planner's skill queries go looking for
    exactly those people, so the gate has to accept them or that half of discovery
    is wasted. Role-word expansion happens later in `_title_matches`, so one entry
    per skill covers Developer/Programmer/SDE too.
    """
    return list(spec.titles) + [f"{skill} Engineer" for skill in spec.must_have_skills]


def _region_compatible(resolved: str, wanted: list[str]) -> bool:
    """Hierarchical region match.

    Codes are hierarchical strings (`IN` > `IN-KA` > `IN-KA-BLR`), so a prefix
    relationship in either direction means "not ruled out": a profile listing only
    `Karnataka` (IN-KA) is compatible with a Bangalore role, and one listing only
    `India` is compatible with anything. `IN-PB` against `IN-KA-BLR` shares no
    prefix and is a genuine miss.
    """
    # "Anywhere in India" is offered as a location in the role form, and it is a
    # country-wide ask, not a place: as a literal code it shares no prefix with any
    # real region, so it rejected everyone in India — including people in the very
    # city the role named (measured 2026-09-18). Read it as plain `IN`.
    wanted = ["IN" if w == "IN-REMOTE" else w for w in wanted]
    return any(r.startswith(w) or w.startswith(r) for w in wanted for r in [resolved])


def _employer_excluded(ref: CandidateRef, excluded: list[str]) -> str | None:
    """Return the matched excluded employer, or None.

    Tested against the whole snippet rather than a parsed employer field: headlines
    are aspirational free text ("Ex-Google | Building the future") and a parser that
    guesses which company someone actually works at gets it wrong often enough to
    matter. Substring-over-the-whole-snippet is blunt but it never invents an
    employer that was not written down.
    """
    haystack = " ".join(
        part
        for part in (ref.snippet_employer, ref.snippet_headline, ref.snippet_raw)
        if part
    ).lower()
    for employer in excluded:
        if employer.lower() in haystack:
            return employer
    return None


def snippet_gate(
    ref: CandidateRef,
    spec: RoleSpec,
    *,
    region_resolver: Callable[[str], str | None] | None = None,
) -> GateVerdict:
    """Test title, employer and region against the spec using the snippet alone."""
    if region_resolver is None:
        from hi.canon import region_of  # lazy: keeps this module importable without a DB

        region_resolver = region_of

    # --- title -------------------------------------------------------------
    # Tested against both the structured current title and the self-written
    # headline. Either is a legitimate statement of what this person does, and at
    # the gate stage recall matters more than precision: a rejected ref is never
    # looked at again, whereas a false pass costs one enrichment. Deliberately
    # NOT tested against the snippet prose — the query text is echoed there, so
    # everything would match and the gate would stop filtering anything.
    wanted_titles = acceptable_titles(spec)
    if wanted_titles:
        testable = [t for t in (ref.snippet_title, ref.snippet_headline) if t]
        if not testable:
            return GateVerdict(passed=False, reason="title: no title or headline in snippet to test")
        if not any(_title_matches(t, wanted_titles) for t in testable):
            wanted = " | ".join(wanted_titles)
            saw = " / ".join(testable)
            return GateVerdict(passed=False, reason=f"title: {saw!r} does not match {wanted}")

    # --- employer ----------------------------------------------------------
    matched = _employer_excluded(ref, spec.excluded_employers)
    if matched is not None:
        return GateVerdict(passed=False, reason=f"employer: excluded ({matched})")

    # --- region ------------------------------------------------------------
    # A fully-remote role does not filter on location at all.
    note: str | None = None
    if spec.locations and spec.remote != "remote":
        location_text = ref.snippet_location
        if location_text:
            clean = location_text.strip()
            wanted = ", ".join(spec.locations)

            # A named foreign country is positive disproof, and region resolution
            # cannot supply it because the canon only knows Indian places.
            if any(_normalise_country(p) in NON_INDIA_COUNTRIES for p in clean.split(",")):
                return GateVerdict(passed=False, reason=f"location: {clean} is outside India")

            region = region_resolver(clean)
            if region is None:
                # An unresolvable location used to pass, on the reasoning that we
                # cannot disprove it. That made an *unknown* place safer than a known
                # one: "Hyderabad, Telangana, India" was rejected as the wrong Indian
                # city while "Greater Sydney Area" and "Austin, Texas Metropolitan
                # Area" sailed through — neither names a country, so NON_INDIA_COUNTRIES
                # never fired, and neither resolves to a region. Live on the Bangalore
                # ops role, 2026-08-31: 2 of 11 people shown to the recruiter were
                # abroad. A role naming Indian locations is asking for India, so a
                # location naming no Indian place at all fails.
                if not _mentions_india(clean):
                    return GateVerdict(
                        passed=False,
                        reason=f"location: {clean} is not a recognised Indian location",
                    )
            elif not _region_compatible(region, spec.locations):
                # Another Indian city is NOT a rejection. Measured 2026-09-18: Kolkata,
                # Delhi, Gurgaon, Chennai, Mumbai and Noida were most of every location
                # refusal on city-based roles — people relocate and roles go hybrid, so
                # this is a fact the recruiter should weigh, not one we decide for them.
                # Only "outside India" still refuses, which is what the 2026-08-31
                # incident was actually about.
                note = f"location: {clean} ({region}) is outside {wanted}"

    return GateVerdict(passed=True, reason=note)
