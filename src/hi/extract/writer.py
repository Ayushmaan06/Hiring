"""The single chokepoint for writing evidence (IMPLEMENTATION.md 1.6).

Every claim the product makes about a person passes through `write_evidence`, which
is what keeps "every claim is quotable" true. It is enforced here rather than in each
adapter because a rule spread across eight adapters is a rule that erodes.

Three refusals, all silent-by-design at the row level and counted for the caller:

1. A snippet that is not actually present in the source document is **dropped**, not
   corrected. This is the anti-fabrication rule.
2. A claim naming a `BANNED_SIGNALS` term is **rejected**, even if an adapter asks
   nicely. We do not collect those, not merely decline to rank them.
3. An empty snippet is rejected (the database also refuses it).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from hi.db import pool
from hi.signals import REFUSED_CLAIM_TYPES, is_banned

TIERS = {"self_reported", "third_party_stated", "artifact_backed"}


@dataclass
class EvidenceRow:
    claim_type: str
    tier: str
    source_url: str
    snippet: str
    claim_key: str | None = None
    claim_value: str | None = None
    value_num: float | None = None
    fetch_id: int | None = None
    observed_at: datetime | None = None
    extractor: str = "unknown"
    extractor_version: str = "0"


@dataclass
class WriteResult:
    written: int = 0
    dropped_unquotable: int = 0
    rejected_banned: int = 0
    rejected_invalid: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def refused(self) -> int:
        return self.dropped_unquotable + self.rejected_banned + self.rejected_invalid


def _normalise_ws(text: str) -> str:
    """Collapse whitespace for the substring check only.

    Extractors and HTML-to-text reflow whitespace, so a byte-exact check would reject
    honest snippets. Nothing else about the snippet is altered — the stored text stays
    verbatim, because it is what the recruiter reads.
    """
    return re.sub(r"\s+", " ", text).strip()


def _is_quotable(snippet: str, source_text: str | None) -> bool:
    if source_text is None:
        return True  # nothing to check against; the adapter vouches for it
    return _normalise_ws(snippet).lower() in _normalise_ws(source_text).lower()


def write_evidence(
    candidate_id: uuid.UUID,
    rows: list[EvidenceRow],
    *,
    source_text: str | None,
) -> WriteResult:
    """Write evidence rows, refusing any that cannot be justified.

    `source_text` is the document the claims came from. When provided, every snippet
    must appear in it. Pass None only for structured API responses where the snippet
    is synthesised from JSON fields rather than quoted from prose.
    """
    result = WriteResult()
    accepted: list[EvidenceRow] = []

    for row in rows:
        if not row.snippet or not row.snippet.strip():
            result.rejected_invalid += 1
            result.reasons.append(f"{row.claim_type}: empty snippet")
            continue
        if row.tier not in TIERS:
            result.rejected_invalid += 1
            result.reasons.append(f"{row.claim_type}: unknown tier {row.tier!r}")
            continue
        if row.claim_type in REFUSED_CLAIM_TYPES:
            result.rejected_banned += 1
            result.reasons.append(f"{row.claim_type}: refused claim type")
            continue
        # `claim_type` is NOT passed to `is_banned`. It is a closed enum, already
        # checked exactly against REFUSED_CLAIM_TYPES above and by a DDL constraint,
        # whereas `is_banned` matches substrings in both directions so that a key of
        # "college" catches "college_name". Those two rules collide on exactly one
        # value: claim_type "employer" is a substring of "current_employer_prestige",
        # so every employment row LinkedIn enrichment produced was silently refused
        # on 2026-08-27. Keys and values still go through the fuzzy check, which is
        # what it was written for.
        if banned := is_banned(row.claim_key, row.claim_value):
            result.rejected_banned += 1
            result.reasons.append(f"{row.claim_type}: names banned signal {banned!r}")
            continue
        if not _is_quotable(row.snippet, source_text):
            result.dropped_unquotable += 1
            result.reasons.append(f"{row.claim_type}: snippet not found in source")
            continue
        accepted.append(row)

    if not accepted:
        return result

    now = datetime.now(timezone.utc)
    with pool.connection() as conn:
        for row in accepted:
            conn.execute(
                "insert into evidence "
                "(candidate_id, claim_type, claim_key, claim_value, value_num, tier, "
                " source_url, fetch_id, snippet, observed_at, extractor, extractor_version) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (candidate_id, claim_type, claim_key, source_url) do update set "
                "  observed_at = excluded.observed_at,"
                "  claim_value = excluded.claim_value,"
                "  value_num = excluded.value_num,"
                "  snippet = excluded.snippet,"
                "  extractor_version = excluded.extractor_version",
                (
                    candidate_id, row.claim_type, row.claim_key, row.claim_value,
                    row.value_num, row.tier, row.source_url, row.fetch_id, row.snippet,
                    row.observed_at or now, row.extractor, row.extractor_version,
                ),
            )
    result.written = len(accepted)
    return result


def quarantine(
    *,
    extractor: str,
    extractor_version: str,
    reason: str,
    fetch_id: int | None = None,
    candidate_id: uuid.UUID | None = None,
    source_url: str | None = None,
    raw_response: str | None = None,
) -> None:
    """Record a whole-document extraction failure. Never write partial rows instead."""
    with pool.connection() as conn:
        conn.execute(
            "insert into extraction_failure "
            "(fetch_id, candidate_id, source_url, extractor, extractor_version, reason, raw_response) "
            "values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
            (fetch_id, candidate_id, source_url, extractor, extractor_version, reason, raw_response),
        )
