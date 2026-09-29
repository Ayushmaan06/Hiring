-- Evidence de-duplication and extraction quarantine (IMPLEMENTATION.md 1.6).

-- One row per (candidate, claim, source). A second observation of the same claim
-- from the same page updates `observed_at` instead of appending a duplicate — without
-- this, re-running a role inflates skill_depth just by looking twice.
-- `nulls not distinct` is required because claim_key is nullable, and Postgres would
-- otherwise treat every null-keyed row as unique and defeat the constraint entirely.
create unique index evidence_claim_source_uniq
    on evidence (candidate_id, claim_type, claim_key, source_url)
    nulls not distinct;

-- A document whose extraction failed is quarantined whole, never partially written.
create table extraction_failure (
    id            bigint generated always as identity primary key,
    fetch_id      bigint references "fetch",
    candidate_id  uuid references candidate on delete cascade,
    source_url    text,
    extractor     text not null,
    extractor_version text not null,
    reason        text not null,
    raw_response  text,
    failed_at     timestamptz not null default now()
);

-- Re-extraction over a cached body is what a prompt version bump buys us, so the
-- same extractor version must not process the same fetch twice (AGENTS.md §4).
create unique index extraction_failure_fetch_version_uniq
    on extraction_failure (fetch_id, extractor_version)
    where fetch_id is not null;
