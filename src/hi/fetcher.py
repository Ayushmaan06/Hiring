from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from hi.db import pool

USER_AGENT = "HiringIntelligenceBot/0.1 (+mailto:ayushmaan.singh@habuild.in)"
CACHE_DIR = Path(__file__).resolve().parents[2] / "var" / "cache"
MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_ATTEMPTS = 4  # first try + 3 retries, per IMPLEMENTATION.md 1.3
MAX_REDIRECTS = 5
MAX_CROSS_DOMAIN_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ROBOTS_CACHE_SECONDS = 24 * 3600

# Vendor APIs (SerpAPI) take their credential as a query parameter. The request has
# to carry it, but the `fetch` table must not: these are stripped before a URL is
# hashed or stored, so a DB dump never leaks a key. Redacting before hashing also
# means the cache key is the *query*, so rotating a key does not cold-start the cache.
SECRET_QUERY_PARAMS = {"api_key", "apikey", "key", "token", "access_token", "secret"}


class PolicyDenied(Exception):
    pass


class RobotsDenied(Exception):
    pass


class FetchFailed(Exception):
    pass


@dataclass(frozen=True)
class Fetched:
    fetch_id: int
    url: str
    status: int
    text: str | None
    from_cache: bool
    body_path: str | None


# ponytail: process-global state is fine for the single worker process this runs in (IMPLEMENTATION.md 1.10).
_robots_cache: dict[str, tuple[RobotFileParser, float]] = {}
# ponytail: leaky-bucket-of-one per host — good enough at ~30 role searches/month.
# Upgrade to a real multi-token bucket if per-host burst concurrency is ever needed.
_next_allowed: dict[str, float] = {}
_rate_lock = asyncio.Lock()


def _domain_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def redact_url(url: str) -> str:
    """Strip credential query params. Used for everything persisted or hashed."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (k, "REDACTED" if k.lower() in SECRET_QUERY_PARAMS else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _url_hash(url: str) -> str:
    return hashlib.sha256(redact_url(url).encode()).hexdigest()


def _get_policy(domain: str, adapter: str) -> dict | None:
    """The policy governing this fetch, or None if nothing authorises it.

    An exact-domain row always wins. Failing that, a wildcard row (`domain = '*'`)
    applies **only to the adapter named on that row** — see migration 012. That
    scoping is the whole safety property: `serp_web` needs to reach domains no one
    can enumerate in advance, and without the adapter check the same row would hand
    every other adapter an unlimited route to the internet.
    """
    if not domain:
        # A hostless URL used to be refused for free: "" matched no row. Under a
        # wildcard it would match one, so the emptiness is checked explicitly.
        return None
    with pool.connection() as conn:
        row = conn.execute(
            "select enabled, respect_robots, rate_limit_rps, daily_fetch_cap "
            "from source_policy where domain = %s",
            (domain,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "select enabled, respect_robots, rate_limit_rps, daily_fetch_cap "
                "from source_policy where domain = '*' and adapter = %s",
                (adapter,),
            ).fetchone()
    if row is None:
        return None
    return {
        "enabled": row[0],
        "respect_robots": row[1],
        "rate_limit_rps": row[2],
        "daily_fetch_cap": row[3],
    }


def _check_daily_cap(domain: str, cap: int | None) -> None:
    if cap is None:
        return
    with pool.connection() as conn:
        count = conn.execute(
            "select count(*) from \"fetch\" "
            "where domain = %s and fetched_at > now() - interval '24 hours'",
            (domain,),
        ).fetchone()[0]
    if count >= cap:
        raise PolicyDenied(f"{domain}: daily fetch cap ({cap}) reached")


def _cache_lookup(url_hash: str, max_age_hours: int) -> tuple[int, int, str] | None:
    with pool.connection() as conn:
        return conn.execute(
            "select id, http_status, body_path from \"fetch\" "
            "where url_hash = %s and http_status between 200 and 299 and body_path is not null "
            "and fetched_at > now() - make_interval(hours => %s) "
            "order by fetched_at desc limit 1",
            (url_hash, max_age_hours),
        ).fetchone()


def _read_cached_text(body_path: str) -> str | None:
    data = Path(body_path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def _robots_allowed(url: str) -> bool:
    parts = urlsplit(url)
    domain = parts.netloc.lower()
    cached = _robots_cache.get(domain)
    now = time.monotonic()
    if cached is None or now - cached[1] > ROBOTS_CACHE_SECONDS:
        parser = RobotFileParser()
        robots_url = f"{parts.scheme}://{domain}/robots.txt"
        try:
            async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": USER_AGENT}) as client:
                resp = await client.get(robots_url)
            parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except httpx.HTTPError:
            parser.parse([])
        cached = (parser, now)
        _robots_cache[domain] = cached
    return cached[0].can_fetch(USER_AGENT, url)


async def _throttle(domain: str, rate_limit_rps: float) -> None:
    if rate_limit_rps <= 0:
        return
    interval = 1.0 / rate_limit_rps
    async with _rate_lock:
        now = time.monotonic()
        next_allowed = _next_allowed.get(domain, 0.0)
        wait = max(0.0, next_allowed - now)
        _next_allowed[domain] = max(now, next_allowed) + interval
    if wait:
        await asyncio.sleep(wait)
    await asyncio.sleep(random.uniform(0, 0.25))


def _store(
    content: bytes,
    truncated: bool,
    *,
    status: int,
    content_type: str,
    encoding: str | None,
    url: str,
    domain: str,
    adapter: str,
) -> Fetched:
    content_hash = hashlib.sha256(content).hexdigest()
    body_path = CACHE_DIR / content_hash[:2] / content_hash
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_bytes(content)

    text = None
    if any(marker in content_type for marker in ("text", "html", "json", "xml")):
        text = content.decode(encoding or "utf-8", errors="replace")

    error = "truncated: body exceeded MAX_BODY_BYTES" if truncated else None
    safe_url = redact_url(url)

    with pool.connection() as conn:
        fetch_id = conn.execute(
            "insert into \"fetch\" "
            "(url, url_hash, domain, adapter, http_status, content_hash, body_path, bytes, "
            " fetched_at, from_cache, error) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, now(), false, %s) returning id",
            (
                safe_url, _url_hash(url), domain, adapter, status, content_hash,
                str(body_path), len(content), error,
            ),
        ).fetchone()[0]

    return Fetched(fetch_id, safe_url, status, text, False, str(body_path))


async def _do_fetch(
    url: str,
    *,
    domain: str,
    adapter: str,
    redirects_left: int,
    max_age_hours: int,
    cross_domain_hops: int,
    headers: dict[str, str] | None = None,
) -> Fetched:
    last_exc: Exception | None = None
    # Headers are never persisted — only the (redacted) URL is — so a bearer token
    # passed here does not reach the database.
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=20.0, headers=request_headers
    ) as client:
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with client.stream("GET", url) as resp:
                    if resp.status_code in REDIRECT_STATUSES and "location" in resp.headers:
                        next_url = urljoin(url, resp.headers["location"])
                        await resp.aclose()
                        if redirects_left <= 0:
                            raise FetchFailed(f"{url}: too many redirects")
                        if _domain_of(next_url) != domain:
                            # Cross-domain redirect: re-enter fetch() so the target gets its own
                            # full policy/cache/robots/rate-limit check (IMPLEMENTATION.md 1.3
                            # edge cases) rather than inheriting this domain's clearance.
                            # Auth headers are deliberately NOT forwarded across a
                            # domain boundary: following a redirect with someone
                            # else's bearer token attached is how credentials leak.
                            return await fetch(
                                next_url,
                                adapter=adapter,
                                max_age_hours=max_age_hours,
                                _cross_domain_hops=cross_domain_hops + 1,
                            )
                        return await _do_fetch(
                            next_url,
                            domain=domain,
                            adapter=adapter,
                            redirects_left=redirects_left - 1,
                            max_age_hours=max_age_hours,
                            cross_domain_hops=cross_domain_hops,
                            headers=headers,
                        )

                    if 500 <= resp.status_code < 600:
                        last_exc = FetchFailed(f"{url}: {resp.status_code}")
                        if attempt < MAX_ATTEMPTS - 1:
                            await asyncio.sleep(2**attempt + random.uniform(0, 0.25))
                        continue

                    content = bytearray()
                    truncated = False
                    async for chunk in resp.aiter_bytes():
                        content.extend(chunk)
                        if len(content) >= MAX_BODY_BYTES:
                            truncated = True
                            break
                    return _store(
                        bytes(content[:MAX_BODY_BYTES]),
                        truncated,
                        status=resp.status_code,
                        content_type=resp.headers.get("content-type", ""),
                        encoding=resp.encoding,
                        url=url,
                        domain=domain,
                        adapter=adapter,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(2**attempt + random.uniform(0, 0.25))
                continue

    raise FetchFailed(str(last_exc) if last_exc else f"{url}: failed after retries")


async def fetch(
    url: str,
    *,
    adapter: str,
    max_age_hours: int = 24,
    headers: dict[str, str] | None = None,
    _cross_domain_hops: int = 0,
) -> Fetched:
    """Single chokepoint for all outbound scraping. See IMPLEMENTATION.md 1.3.

    `headers` adds request headers (e.g. an API bearer token). They are sent but never
    stored, and never forwarded across a domain boundary.
    """
    if _cross_domain_hops > MAX_CROSS_DOMAIN_REDIRECTS:
        raise FetchFailed(f"{url}: too many cross-domain redirects")

    domain = _domain_of(url)
    policy = _get_policy(domain, adapter)
    if policy is None or not policy["enabled"]:
        raise PolicyDenied(f"{domain}: no enabled source_policy row")

    cached = _cache_lookup(_url_hash(url), max_age_hours)
    if cached is not None:
        fetch_id, http_status, body_path = cached
        return Fetched(
            fetch_id, redact_url(url), http_status, _read_cached_text(body_path), True, body_path
        )

    if policy["respect_robots"] and not await _robots_allowed(url):
        raise RobotsDenied(f"{domain}: disallowed by robots.txt")

    _check_daily_cap(domain, policy["daily_fetch_cap"])
    await _throttle(domain, float(policy["rate_limit_rps"]))

    return await _do_fetch(
        url,
        domain=domain,
        adapter=adapter,
        redirects_left=MAX_REDIRECTS,
        max_age_hours=max_age_hours,
        cross_domain_hops=_cross_domain_hops,
        headers=headers,
    )


# --------------------------------------------------------------------------
# Browser-driven fetches — mode C (ARCHITECTURE.md §7.4)
# --------------------------------------------------------------------------
#
# Mode C navigates in the operator's own browser, so no HTTP request is issued from
# here. That must not turn it into a second, unpoliced route to the internet: the
# policy row, the daily cap, the pacing and the `fetch` audit trail all still apply,
# which is what these two functions are for. An adapter calls `browser_gate` before
# navigating and `record_browser_fetch` after.
#
# The one rule mode C does NOT apply is robots.txt, deliberately and narrowly.
# robots.txt governs crawlers; mode C is a person's own logged-in browser reading
# pages that account is entitled to read, at human pace, capped. `respect_robots`
# stays true on the LinkedIn rows and keeps the httpx crawler (mode B) out, which is
# still the correct answer for mode B. See migration 010 for the same reasoning.


async def browser_gate(url: str, *, adapter: str) -> None:
    """Policy gate for a fetch the browser will perform. Raises, or returns silently.

    Same checks as `fetch` minus robots and the response cache: an enabled policy row,
    the domain's daily cap, and the domain's rate limit (which sleeps).
    """
    domain = _domain_of(url)
    if not domain:
        # Otherwise this surfaces as ": no enabled source_policy row", which reads like
        # a policy problem and sends you to the database instead of to the caller.
        raise PolicyDenied(f"{url!r} has no host — not a fetchable absolute URL")
    policy = _get_policy(domain, adapter)
    if policy is None or not policy["enabled"]:
        raise PolicyDenied(f"{domain}: no enabled source_policy row")
    _check_daily_cap(domain, policy["daily_fetch_cap"])
    await _throttle(domain, float(policy["rate_limit_rps"]))


def record_browser_fetch(url: str, *, adapter: str, status: int, html: str) -> Fetched:
    """Record a page the browser retrieved, so it is auditable and counts against caps.

    Returns a `Fetched` with a real `fetch_id`, so evidence rows written from this page
    can point at the document they came from exactly as an httpx-sourced row does.
    """
    return _store(
        html.encode("utf-8"),
        False,
        status=status,
        content_type="text/html; charset=utf-8",
        encoding="utf-8",
        url=url,
        domain=_domain_of(url),
        adapter=adapter,
    )


def disable_source(domain: str, reason: str) -> None:
    """Trip the kill switch on a domain. Mode C's auto-disable, ARCHITECTURE.md §7.4.

    Deliberately not an adapter-local flag: the next run must be refused by the same
    gate every other fetch goes through, and a human has to clear it.
    """
    with pool.connection() as conn:
        conn.execute(
            "update source_policy set enabled = false, disabled_reason = %s where domain = %s",
            (reason, domain),
        )
