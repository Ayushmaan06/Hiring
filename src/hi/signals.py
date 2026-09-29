"""Fairness constants — ARCHITECTURE.md §5.3.

These live in their own module because two very different layers need them and
neither should import the other: the evidence writer rejects banned claims at the
point of collection, and the scorer asserts its components against the allowlist.
"""

import re

FEATURE_ALLOWLIST = frozenset(
    {
        "skill_match",
        "skill_depth",
        "seniority_fit",
        "activity_recency",
        "availability",
    }
)

BANNED_SIGNALS = frozenset(
    {
        # NOT the bare word "college". It was here before ARCHITECTURE.md §2.2 made the
        # institution storable and shown, and after that decision it contradicted it:
        # `is_banned` fires when every word of a signal appears, so an education row for
        # "Government College of Engineering" or "St. Joseph's College of Commerce" was
        # refused for naming the institution §2.2 exists to display. In India that is a
        # large share of institutions, and the refusal was silent — found 2026-08-28 in a
        # live enrichment run that reported "refused 1 (education: names banned signal
        # 'college')" for five of twelve profiles.
        #
        # The two compound keys stay, and they are the ones that mattered: a *feature*
        # keyed on college is the caste proxy §2.2 refuses to rank on. Neither matches an
        # institution's name, because "tier" and "name" do not appear in one.
        "college_name",
        "college_tier",
        "graduation_year",
        "age",
        "name_tokens",
        "photo",
        "photograph",
        "date_of_birth",
        "dob",
        "gender",
        "caste",
        "religion",
        "marital_status",
        "nationality",
        "current_employer_prestige",
    }
)

# `claim_type` values no adapter may write, whatever it thinks it needs.
#
# `education` was here until 2026-08-27, when the tool owner decided that a JD
# specifying IIT/IIM candidates needs the institution visible on the card
# (ARCHITECTURE.md §2.2). It is now **stored and shown, never scored**:
#
#   - `FEATURE_ALLOWLIST` is unchanged, so no scoring component can read it, and
#     `scoring.py` ignores the claim_type entirely. There is a test asserting an
#     education row changes no score by a single byte.
#   - Graduation dates are still refused below, because a graduation year dates a
#     person and age is banned independently of this decision.
#
# The fairness argument against ranking on institution tier has not changed and is
# recorded in ARCHITECTURE.md §2.2: in India it correlates with caste, so a score
# that reads it is a caste proxy. Storing it for a human to read is a different act
# from ranking on it, and only the first was authorised.
REFUSED_CLAIM_TYPES: frozenset[str] = frozenset()


def _tokens(value: str) -> frozenset[str]:
    return frozenset(t for t in re.split(r"[^a-z0-9]+", value.strip().lower()) if t)


def is_banned(*values: str | None) -> str | None:
    """Return the offending term if any value names a banned signal, else None.

    Matches on **whole words**, not raw substrings: a banned signal is present when
    all of its words appear in the value. So "graduation_year_2019" and "candidate age"
    are caught, and "Product Management" is not.

    It used to compare substrings in both directions, which was far too eager because
    several banned signals are short words that live inside ordinary ones. Found
    2026-08-27 when a non-technical skill vocabulary was added, but it was already
    live: every skill containing "Management" was refused as `age` (man-AGE-ment),
    along with "Natural Language Processing" (langu-AGE), "REST APIs"
    (p-REST-ige), "CA" (CAste) and the single-letter languages "C" and "R". The
    refusals were silent, so the evidence simply never appeared.
    """
    for value in values:
        if not value:
            continue
        words = _tokens(value)
        for banned in BANNED_SIGNALS:
            if _tokens(banned) <= words:
                return banned
    return None
