-- Unknown skill strings land here for human review instead of being guessed.
-- See docs/ARCHITECTURE.md §4.4 and IMPLEMENTATION.md 1.4.

create table skill_alias_proposal (
    id               bigint generated always as identity primary key,
    raw_text         text not null,
    proposed_at      timestamptz not null default now(),
    status           text not null default 'open' check (status in ('open', 'approved', 'rejected')),
    resolved_skill_id bigint references skill
);
