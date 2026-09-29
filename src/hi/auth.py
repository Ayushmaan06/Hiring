"""Access control (IMPLEMENTATION.md 1.12).

The app holds personal data about named individuals; "internal tool" is not a network
boundary. So: no anonymous read of anything, including the admin page.

Deliberately small. Every user is a recruiter — there are no roles and no permissions,
because there is no reason for them yet. What this does provide is an `actor` on every
mutation, which is why the `actor` columns exist.

Passwords are PBKDF2-SHA256 from the standard library rather than bcrypt/argon2: it is
the correct algorithm family for this, and it avoids a dependency for a handful of
internal users. Users come from `HI_USERS`, in `.env` or the environment.

    python -m hi.auth hash 'the password'      # -> paste the result into HI_USERS
    HI_USERS='a@b.com:pbkdf2_sha256$...,c@d.com:pbkdf2_sha256$...'
"""

from __future__ import annotations

import hashlib
import hmac

from hi.config import settings
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

from hi.db import pool

COOKIE_NAME = "hi_session"
SESSION_HOURS = 12
PBKDF2_ITERATIONS = 480_000


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # Constant-time: a fast reject leaks which prefix matched.
    return hmac.compare_digest(digest.hex(), expected)


def configured_users() -> dict[str, str]:
    """{email: password_hash} from HI_USERS. Empty means nobody can log in."""
    # A real environment variable wins, so a container or systemd unit can inject it
    # without a file; otherwise it comes from `.env` like every other secret. Reading
    # only `os.environ` was a bug: `.env.example`, RUNBOOK entry 6 and the sign-in
    # page all tell the operator to put it in `.env`, and until 2026-08-27 doing that
    # silently produced a 401 with no clue why.
    # Presence, not truthiness: `HI_USERS=""` in the environment means "no accounts"
    # deliberately, and must not fall through to `.env` and quietly re-enable them.
    raw = (os.environ["HI_USERS"] if "HI_USERS" in os.environ else settings.hi_users).strip()
    users: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        email, _, encoded = entry.partition(":")
        if email.strip() and encoded.strip():
            users[email.strip().lower()] = encoded.strip()
    return users


def authenticate(email: str, password: str) -> str | None:
    """Return the actor on success, else None.

    With no users configured this always fails — the app is bricked rather than open.
    That is the correct direction to fail for something holding personal data.
    """
    users = configured_users()
    encoded = users.get((email or "").strip().lower())
    if encoded is None:
        # Spend the same work on an unknown user so timing does not reveal who exists.
        hash_password(password or "")
        return None
    return email.strip().lower() if verify_password(password or "", encoded) else None


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


def create_session(actor: str, *, hours: int = SESSION_HOURS) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    with pool.connection() as conn:
        conn.execute(
            "insert into session (id, actor, expires_at) values (%s, %s, %s)",
            (token, actor, expires),
        )
    return token


def actor_for(token: str | None) -> str | None:
    """Resolve a session token to an actor, refreshing last_seen_at."""
    if not token:
        return None
    with pool.connection() as conn:
        row = conn.execute(
            "update session set last_seen_at = now() "
            "where id = %s and expires_at > now() returning actor",
            (token,),
        ).fetchone()
    return row[0] if row else None


def destroy_session(token: str | None) -> None:
    """Actually invalidate — the row is gone, not just the cookie."""
    if not token:
        return
    with pool.connection() as conn:
        conn.execute("delete from session where id = %s", (token,))


def purge_expired() -> int:
    with pool.connection() as conn:
        rows = conn.execute("delete from session where expires_at <= now() returning id").fetchall()
    return len(rows)


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "hash":
        print("usage: python -m hi.auth hash '<password>'")
        raise SystemExit(1)
    print(hash_password(sys.argv[2]))


if __name__ == "__main__":
    try:
        main()
    finally:
        pool.close()
