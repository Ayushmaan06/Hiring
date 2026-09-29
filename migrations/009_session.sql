-- Server-side sessions (IMPLEMENTATION.md 1.12).
--
-- Stored rather than signed-cookie-only because "logout must actually invalidate":
-- a signed cookie cannot be revoked before it expires, so a stolen one stays valid.
-- A row we can delete is the honest way to end a session.

create table session (
    id           text primary key,           -- opaque random token
    actor        text not null,              -- the email that logged in
    created_at   timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    expires_at   timestamptz not null
);

create index session_expiry_idx on session (expires_at);
