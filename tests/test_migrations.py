from hi.db import migrate


def test_migrate_twice_is_a_noop(db_conn):
    before = db_conn.execute("select count(*) from schema_migrations").fetchone()[0]

    migrate()

    after = db_conn.execute("select count(*) from schema_migrations").fetchone()[0]
    assert before == after
    tables = db_conn.execute(
        "select table_name from information_schema.tables where table_schema = 'public'"
    ).fetchall()
    assert ("evidence",) in tables
