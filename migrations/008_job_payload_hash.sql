-- Idempotent enqueue (IMPLEMENTATION.md 1.10).
--
-- Without this, a double-clicked "Find candidates" button enqueues the same role
-- twice and doubles the SERP and API spend. The uniqueness applies only to unfinished
-- work, so the same role CAN be re-run later — it just cannot be queued twice at once.

alter table job add column payload_hash text;

create unique index job_unfinished_uniq
    on job (kind, payload_hash)
    where state in ('queued', 'running');

-- Claiming scans by state then age; the partial index keeps that cheap as done rows pile up.
create index job_queued_created_idx on job (created_at) where state = 'queued';
