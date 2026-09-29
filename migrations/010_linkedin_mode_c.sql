-- Mode C (`attached_browser`) authorisation, ARCHITECTURE.md §7.4.
--
-- Mode B never ran: linkedin.com/robots.txt is `Disallow: /` for every user agent and
-- fetcher.py correctly refuses it (docs/PRD.md §13.1). Mode C is the sanctioned answer
-- and it is a different kind of access, which is why it is not blocked by the same rule:
-- it is not a crawler. It is the operator driving their own already-logged-in browser,
-- at human pace, over pages that account is entitled to see. `respect_robots` therefore
-- stays TRUE on these rows on purpose -- it keeps the httpx crawler (mode B) out, which
-- is correct and unchanged. Mode C does not route through httpx and does not consult
-- robots; see fetcher.browser_gate, which documents the same distinction at the code.
--
-- `enabled` is the single kill switch for LinkedIn, and it starts FALSE here: mode C
-- puts a named individual's personal account at risk, so it must be switched on
-- deliberately by that person and not inherited from a migration.
--
--     python -m hi.adapters.linkedin_profile enable --by "<name>"
--
-- The auto-disable trip (first authwall, challenge, or HTTP 999) writes false back to
-- this column with a disabled_reason, and a human has to re-enable it.

update source_policy
   set adapter = 'linkedin_profile',
       enabled = false,
       reviewed_at = null,
       disabled_reason = 'Mode C not yet enabled by a named operator. See migration 010.',
       tos_note = 'LinkedIn enrichment. Mode B (logged out) is blocked by robots.txt and stays '
                  'blocked -- respect_robots is true and must remain true. Mode C '
                  '(attached_browser, ARCHITECTURE.md §7.4) is authorised on this domain: the '
                  'operator''s own logged-in browser, never headless, concurrency 1, >=20s '
                  'between profiles, 15 profiles/day, self-disabling on the first challenge. '
                  'Those limits live in code (hi.adapters.linkedin_profile), not in this row. '
                  'No burner, fabricated, shared, or stored-credential account, ever.'
 where domain in ('linkedin.com', 'www.linkedin.com');
