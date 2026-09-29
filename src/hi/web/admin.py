"""Health page data (IMPLEMENTATION.md 1.11a, ARCHITECTURE.md §9a.3.2).

**Green means it produced rows recently. Not "it did not throw."** That distinction is
the entire point of this page: an adapter that runs cleanly and returns zero rows is
the exact signature of a markup change, and a naive health check calls that healthy.
"""

from __future__ import annotations

from pathlib import Path

from hi.db import pool
from hi.fetcher import CACHE_DIR

GREEN, AMBER, RED = "green", "amber", "red"

CACHE_AMBER_THRESHOLD = 0.80
CACHE_BUDGET_BYTES = 20 * 1024 * 1024 * 1024  # 20 GB


def adapter_health() -> list[dict]:
    """One row per configured source, with a light a non-engineer can read."""
    with pool.connection() as conn:
        policies = conn.execute(
            "select domain, adapter, enabled, disabled_reason from source_policy order by adapter, domain"
        ).fetchall()

        rows = []
        for domain, adapter, enabled, disabled_reason in policies:
            last_success, fetches_24h, last_error = conn.execute(
                "select max(fetched_at) filter (where error is null and http_status < 400),"
                "       count(*) filter (where fetched_at > now() - interval '24 hours'),"
                "       (select error from \"fetch\" f2 where f2.domain = %s and f2.error is not null"
                "         order by fetched_at desc limit 1) "
                "from \"fetch\" where domain = %s",
                (domain, domain),
            ).fetchone()

            produced = conn.execute(
                "select count(*) from evidence where extractor = %s "
                "and observed_at > now() - interval '7 days'",
                (adapter,),
            ).fetchone()[0]

            if not enabled:
                light, note = RED, disabled_reason or "Switched off"
            elif last_success is None:
                light, note = AMBER, "Has never returned anything"
            elif produced == 0 and fetches_24h > 0:
                # Ran without error and produced nothing: the silent failure.
                light, note = AMBER, "Ran recently but produced no new information"
            elif produced == 0:
                light, note = AMBER, "No new information in the last 7 days"
            else:
                light, note = GREEN, f"{produced} new items in the last 7 days"

            rows.append(
                {
                    "adapter": adapter,
                    "domain": domain,
                    "light": light,
                    "note": note,
                    "last_success": last_success,
                    "requests_24h": fetches_24h,
                    "last_error": last_error,
                }
            )
    return rows


def cache_usage() -> dict:
    """Page-cache disk usage, amber at 80% (ARCHITECTURE.md §9a.3.6)."""
    total = 0
    if CACHE_DIR.exists():
        total = sum(f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file())
    share = total / CACHE_BUDGET_BYTES if CACHE_BUDGET_BYTES else 0.0
    return {
        "bytes": total,
        "human": _human(total),
        "share": share,
        "light": AMBER if share >= CACHE_AMBER_THRESHOLD else GREEN,
        "budget_human": _human(CACHE_BUDGET_BYTES),
    }


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def disable_source(domain: str, reason: str) -> None:
    """The kill switch. One row update takes a source out of the pipeline."""
    with pool.connection() as conn:
        conn.execute(
            "update source_policy set enabled = false, disabled_reason = %s where domain = %s",
            (reason, domain),
        )
