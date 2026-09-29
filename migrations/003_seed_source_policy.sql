-- Source policy rows. Per ARCHITECTURE.md §4.1 the fetch layer refuses any domain
-- without an enabled row here, so adding a source is a deliberate insert.
--
-- Authorisation of record: approved by Priyesh (senior), relayed by ayushmaan
-- (ayushmaan.singh@habuild.in) on 2026-08-26, for this internal, non-commercial
-- hiring tool. Scope as stated: SERP discovery, plus logged-out public profile
-- enrichment (mode B). No account, no credentials, no proxies — modes A and B only,
-- which is what CLAUDE.md already fixes as the delivered product.

insert into source_policy
    (domain, adapter, enabled, requires_login, respect_robots, rate_limit_rps,
     daily_fetch_cap, tos_note, reviewed_at)
values
    -- SerpAPI is a paid vendor API called with our own key. robots.txt governs
    -- crawlers, not authenticated API clients, so it does not apply here.
    ('serpapi.com', 'linkedin_serp', true, false, false, 2.0, 1000,
     'Paid SERP vendor. Discovery only; touches no LinkedIn surface. Approved by Priyesh via ayushmaan 2026-08-26.',
     now()),

    -- Mode B: logged-out public profile pages, human-paced (1 per 20s), capped.
    -- Both hosts are needed: canonical linkedin.com/in/<slug> 301s to www.
    ('linkedin.com', 'linkedin_profile', true, false, true, 0.05, 200,
     'Mode B logged-out enrichment only. No account, no stored credentials, no proxies. Approved by Priyesh via ayushmaan 2026-08-26.',
     now()),
    ('www.linkedin.com', 'linkedin_profile', true, false, true, 0.05, 200,
     'Mode B logged-out enrichment only. Redirect target of linkedin.com. Approved by Priyesh via ayushmaan 2026-08-26.',
     now())
on conflict (domain) do nothing;
