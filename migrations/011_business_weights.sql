-- A weight profile for roles where public artefacts do not exist.
--
-- The problem this fixes, measured 2026-08-27: two of the five scoring components
-- (`skill_depth`, `activity_recency`) read `artifact_backed` evidence only, and the v1
-- weights give them 0.20 each. For anyone without public code that is **40% of the
-- score permanently unreachable**, so the ceiling is ~0.47 against a "Strong match"
-- threshold of 0.55. No MBA candidate could ever be a strong match, however good, and
-- LinkedIn-only engineers were capped the same way.
--
-- Reweighting is the honest fix rather than lowering the labels: a 0.45 that reads
-- "Strong" because the cut-off moved means the labels stop meaning the same thing from
-- one role to the next. Here the components that cannot earn are given no weight, and
-- the ones that can carry the score.
--
-- Chosen per **role**, never per candidate: two people on the same shortlist must be
-- scored on the same scale or the ranking is meaningless. `match.weights_version`
-- records which profile produced each number, so old scores stay interpretable.
--
-- The tier discount is deliberately NOT relaxed. A listed skill still counts 0.35
-- against a proven one's 1.0, because `third_party_stated` (0.6) IS reachable for
-- non-technical people — a company team page, a conference programme, a published
-- interview. Removing the discount would remove the reason to go and find those.

alter table scoring_weights add column if not exists profile text not null default 'engineering';

insert into scoring_weights (version, weights_json, profile, note, approved_by, approved_at, active)
values (
    'v1-business',
    '{"skill_match": 0.45, "skill_depth": 0.0, "seniority_fit": 0.40, '
    ' "activity_recency": 0.0, "availability": 0.15}'::jsonb,
    'business',
    'Roles with no expectation of public artefacts (non-technical, MBA). '
    'skill_depth and activity_recency read artifact_backed evidence only, so under v1 '
    'they were 40% of the score that such a candidate could never earn. Their weight '
    'moves to skill_match and seniority_fit, which are evidenced for these roles. '
    'Ceiling with self-reported skills only is ~0.71, so Strong is reachable on merit.',
    'ayushmaan',
    now(),
    true
)
on conflict (version) do nothing;
