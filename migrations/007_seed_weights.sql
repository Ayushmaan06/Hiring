-- v1 scoring weights. All five components weighted equally, because there is no
-- evidence yet to justify anything else (IMPLEMENTATION.md 1.9). Changing these means
-- inserting a NEW row, never updating this one: every `match` stores its
-- `weights_version`, so old scores stay interpretable.

insert into scoring_weights (version, weights_json, note, approved_by, approved_at, active)
values (
    'v1',
    '{"skill_match": 0.2, "skill_depth": 0.2, "seniority_fit": 0.2, "activity_recency": 0.2, "availability": 0.2}'::jsonb,
    'Initial weights. Equal by default — no recruiter feedback exists yet to tune them.',
    'ayushmaan',
    now(),
    true
)
on conflict (version) do nothing;
