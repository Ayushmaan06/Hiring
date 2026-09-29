-- serp_web: open-web verification (IMPLEMENTATION.md 2.1, ARCHITECTURE.md 2.4).
--
-- Every other adapter targets a known host, so a per-domain policy row is both the
-- authorisation and the rate limit. serp_web is different in kind: the domains are
-- whatever a search returns -- a company team page, a conference programme, a
-- published interview. There is no list to seed, because the list is the result.
--
-- Rather than weaken the chokepoint for everyone, this is one wildcard row scoped to
-- one adapter. `_get_policy` falls back to it only when the *calling adapter* matches
-- the adapter named here, so linkedin_profile cannot reach an unlisted domain through
-- it. What the row gives up is the per-host review; what it keeps is every mechanism
-- that review was protecting:
--
--   * respect_robots stays TRUE, which makes robots.txt the per-site gate. That is
--     the correct instrument for the open web -- it is the site owner's own answer,
--     read fresh per host, and no seeded row could be better informed than that.
--   * rate_limit_rps and daily_fetch_cap apply per host, as they do for any row.
--   * the `fetch` table still records every domain touched, so the audit trail of
--     which sites were read is complete after the fact rather than before it.
--   * the adapter carries a code-level domain blocklist (aggregators, job boards and
--     people-data brokers) and its own daily total, neither of which is config.
--
-- Authorisation of record: same scope as migration 003 -- approved by Priyesh
-- (senior), relayed by ayushmaan (ayushmaan.singh@habuild.in). Publicly published
-- pages, fetched at crawler pace, with robots.txt honoured.

insert into source_policy
    (domain, adapter, enabled, requires_login, respect_robots, rate_limit_rps,
     daily_fetch_cap, tos_note, reviewed_at)
values
    ('*', 'serp_web', true, false, true, 0.2, 40,
     'Wildcard, scoped to the serp_web adapter only. Public pages a search returned; robots.txt honoured per host; aggregators and people-data brokers blocklisted in code. Approved by Priyesh via ayushmaan 2026-08-28.',
     now())
on conflict (domain) do nothing;
