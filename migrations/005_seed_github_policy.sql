-- GitHub is the verifier (ARCHITECTURE.md §7.2). Official REST/GraphQL API, called
-- with our own token — robots.txt governs crawlers, not API clients.
--
-- rate_limit_rps 0.4 keeps us under the tightest GitHub ceiling that matters: the
-- SEARCH endpoints allow 30 requests/minute (0.5/s), not the 5,000/hour headline. The
-- adapter also reads X-RateLimit-Remaining and backs off before exhaustion.
--
-- Authorisation of record: approved by Priyesh (senior), relayed by ayushmaan
-- (ayushmaan.singh@habuild.in) on 2026-08-26. Public data only, no unsolicited
-- email — GitHub's AUP forbids using its data for that, and we do not do outreach.

insert into source_policy
    (domain, adapter, enabled, requires_login, respect_robots, rate_limit_rps,
     daily_fetch_cap, tos_note, reviewed_at)
values
    ('api.github.com', 'github', true, false, false, 0.4, 4000,
     'Official API, public data only. Sized on the 30/min search limit. No outreach (GitHub AUP). Approved by Priyesh via ayushmaan 2026-08-26.',
     now())
on conflict (domain) do nothing;
