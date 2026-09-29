-- Reversible merges (IMPLEMENTATION.md 1.7).
--
-- A merge MOVES identity and evidence rows and then archives the absorbed candidate
-- rather than deleting it. Deleting would cascade its evidence away and make the
-- merge irreversible; archiving keeps every row addressable so an unmerge restores
-- the exact prior state. `moved_*_ids` records precisely what moved, because rows
-- that would violate evidence's uniqueness constraint stay behind.

create table identity_merge (
    id                 bigint generated always as identity primary key,
    from_candidate_id  uuid not null references candidate on delete cascade,
    into_candidate_id  uuid not null references candidate on delete cascade,
    moved_identity_ids bigint[] not null default '{}',
    moved_evidence_ids bigint[] not null default '{}',
    review_id          bigint references identity_review,
    reason             text,
    -- No merge without a named decider. This is the 1.7 definition of done.
    decided_by         text not null,
    merged_at          timestamptz not null default now(),
    reversed_at        timestamptz,
    reversed_by        text,
    check (from_candidate_id <> into_candidate_id)
);

create index on identity_merge (into_candidate_id) where reversed_at is null;
