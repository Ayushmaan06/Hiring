import sys
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

# `python -m hi.db` executes this file as `__main__`. Anything that later does
# `from hi.db import ...` (canon.py, at migrate() time) would otherwise trigger
# a second, independent execution of this module under the name `hi.db` — a
# second pool, a second set of background threads, and a slow exit. Alias the
# module now so that second import reuses this one instead.
if __name__ == "__main__":
    sys.modules.setdefault("hi.db", sys.modules["__main__"])

from hi.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

pool = ConnectionPool(settings.database_url, open=False)
pool.open()


def _applied_migrations(conn: psycopg.Connection) -> set[str]:
    conn.execute(
        "create table if not exists schema_migrations ("
        "  filename text primary key,"
        "  applied_at timestamptz not null default now()"
        ")"
    )
    conn.commit()
    return {row[0] for row in conn.execute("select filename from schema_migrations")}


def migrate() -> None:
    with psycopg.connect(settings.database_url) as conn:
        applied = _applied_migrations(conn)

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            # One transaction per file: a failure rolls back the whole file, never half-applies it.
            try:
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "insert into schema_migrations (filename) values (%s)", (path.name,)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            print(f"applied {path.name}")

    # Canon data lives in data/*.yaml, not in a migration file, so every
    # migrate() call re-syncs it — safe because sync_to_db() upserts.
    from hi.canon import sync_to_db

    sync_to_db()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "migrate":
        print("usage: python -m hi.db migrate")
        raise SystemExit(1)
    migrate()


if __name__ == "__main__":
    try:
        main()
    finally:
        pool.close()
