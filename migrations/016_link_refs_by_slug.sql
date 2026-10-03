-- Link every role's ref to a person already known by LinkedIn slug.
--
-- `candidate_ref.candidate_id` used to be set only when *that* role ran enrichment, so a
-- person read for one role stayed unlinked — "next run", unscored — on every other role
-- that found them (305 refs on 2026-10-03). Code now links on both sides; this catches up
-- the rows written before it. Re-score affected roles afterwards.
update candidate_ref r
set candidate_id = i.candidate_id
from identity i
where r.candidate_id is null
  and r.ref_kind = 'linkedin_url'
  and i.kind = 'linkedin_slug'
  and i.value = split_part(r.ref_value, '/', 3);
