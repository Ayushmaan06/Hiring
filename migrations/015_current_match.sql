-- The score a recruiter sees: the latest `match` row per (role, candidate).
--
-- `match` keeps one row per (scorer_version, weights_version) so an old ranking stays
-- explainable. But the pages read `match` directly, so the first time a role was
-- re-scored under a new version (scoring@2, 2026-10-02) every candidate would have
-- been listed twice. Readers go through this view; history stays in the table.
create or replace view current_match as
select distinct on (role_id, candidate_id) *
from match
order by role_id, candidate_id, scored_at desc nulls last, id desc;
