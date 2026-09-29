-- Milestone 1 schema. See docs/ARCHITECTURE.md §4.

-- 4.1 Sources and fetching -------------------------------------------------

create table source_policy (
    domain          text primary key,
    adapter         text not null,
    enabled         bool not null default false,
    requires_login  bool not null default false,
    respect_robots  bool not null default true,
    rate_limit_rps  numeric not null default 0.5,
    daily_fetch_cap int,
    tos_note        text,
    reviewed_at     timestamptz,
    disabled_reason text
);

create table "fetch" (
    id           bigint generated always as identity primary key,
    url          text not null,
    url_hash     text not null,
    domain       text not null,
    adapter      text not null,
    http_status  int,
    content_hash text,
    body_path    text,
    bytes        int,
    fetched_at   timestamptz not null,
    from_cache   bool not null default false,
    error        text
);
create index on "fetch" (url_hash, fetched_at desc);

-- 4.5 Roles (candidate references and evidence hang off role/candidate) ----

create table role (
    id         uuid primary key default gen_random_uuid(),
    title      text,
    jd_text    text,
    spec_json  jsonb not null,
    status     text not null default 'draft',
    created_by text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table role_query (
    id            bigint generated always as identity primary key,
    role_id       uuid references role on delete cascade,
    adapter       text,
    query_text    text,
    run_at        timestamptz,
    results_count int
);

-- 4.2 People and identity ---------------------------------------------------

create table candidate (
    id                    uuid primary key default gen_random_uuid(),
    display_name          text,
    primary_location_text text,
    location_region       text,
    status                text not null default 'active',
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now()
);

create table identity (
    id           bigint generated always as identity primary key,
    candidate_id uuid not null references candidate on delete cascade,
    kind         text not null,
    value        text not null,
    first_seen   timestamptz,
    last_seen    timestamptz,
    unique (kind, value)
);

create table identity_review (
    id           bigint generated always as identity primary key,
    candidate_a  uuid,
    candidate_b  uuid,
    reason       text,
    evidence_json jsonb,
    status       text not null default 'open',
    decided_by   text,
    decided_at   timestamptz
);

-- 4.3 Evidence — the spine ---------------------------------------------------

create table evidence (
    id                bigint generated always as identity primary key,
    candidate_id      uuid not null references candidate on delete cascade,
    claim_type        text not null check (claim_type in (
                          'skill', 'title', 'employer', 'location', 'experience_years',
                          'education', 'activity', 'availability', 'project', 'link'
                      )),
    claim_key         text,
    claim_value       text,
    value_num         numeric,
    tier              text not null check (tier in (
                          'self_reported', 'third_party_stated', 'artifact_backed'
                      )),
    source_url        text not null,
    fetch_id          bigint references "fetch",
    snippet           text not null check (snippet <> ''),
    observed_at       timestamptz not null,
    extractor         text not null,
    extractor_version text not null
);
create index on evidence (candidate_id, claim_type);

-- 4.3a Candidate refs and the snippet gate -----------------------------------

create table candidate_ref (
    id               bigint generated always as identity primary key,
    role_id          uuid not null references role on delete cascade,
    adapter          text not null,
    ref_kind         text not null,
    ref_value        text not null,
    snippet_name     text,
    snippet_headline text,
    snippet_location text,
    snippet_raw      text not null,
    source_url       text not null,
    gate_state       text not null default 'pending' check (gate_state in ('pending', 'passed', 'failed')),
    gate_reason      text,
    candidate_id     uuid references candidate on delete cascade,
    discovered_at    timestamptz not null default now(),
    unique (role_id, ref_kind, ref_value),
    check (gate_state <> 'failed' or gate_reason is not null)
);

-- 4.4 Skills canon ------------------------------------------------------------

create table skill (
    id             bigint generated always as identity primary key,
    canonical_name text unique,
    kind           text
);

create table skill_alias (
    alias    text primary key,
    skill_id bigint not null references skill
);

-- 4.6 Matching and feedback ---------------------------------------------------

create table match (
    id              bigint generated always as identity primary key,
    role_id         uuid not null,
    candidate_id    uuid not null,
    score           numeric not null,
    components_json jsonb not null,
    gates_json      jsonb not null,
    rationale_text  text,
    cited_evidence  bigint[],
    scorer_version  text not null,
    weights_version text not null,
    scored_at       timestamptz,
    unique (role_id, candidate_id, scorer_version, weights_version)
);

create table recruiter_action (
    id         bigint generated always as identity primary key,
    match_id   bigint references match,
    action     text,
    note       text,
    actor      text,
    created_at timestamptz not null default now()
);

create table scoring_weights (
    id           bigint generated always as identity primary key,
    version      text unique,
    weights_json jsonb,
    note         text,
    approved_by  text,
    approved_at  timestamptz,
    active       bool
);

-- 4.7 Jobs and privacy ---------------------------------------------------------

create table job (
    id           bigint generated always as identity primary key,
    kind         text,
    payload_json jsonb,
    state        text not null default 'queued',
    attempts     int not null default 0,
    locked_at    timestamptz,
    locked_by    text,
    last_error   text,
    created_at   timestamptz not null default now(),
    finished_at  timestamptz
);

create table erasure_request (
    id            bigint generated always as identity primary key,
    candidate_id  uuid,
    requested_at  timestamptz not null default now(),
    source        text,
    completed_at  timestamptz
);
