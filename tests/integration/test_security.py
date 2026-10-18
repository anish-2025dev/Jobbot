import pytest
from sqlalchemy import text

from tests.integration.conftest import fails, insert_application, make_user

pytestmark = pytest.mark.integration


def _as_probe(conn, user_id=None):
    conn.execute(text("GRANT SELECT, INSERT ON applications, users TO rls_probe"))
    conn.execute(text("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rls_probe"))
    conn.execute(text("SET LOCAL ROLE rls_probe"))
    if user_id:
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id)})


def test_rls_user_sees_only_own_rows(conn):
    a, b = make_user(conn), make_user(conn)
    insert_application(conn, a, 1)
    insert_application(conn, b, 2)
    _as_probe(conn, a)
    rows = conn.execute(text("SELECT user_id FROM applications")).scalars().all()
    assert rows == [a]
    assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 1


def test_rls_blocks_writing_for_another_user(conn):
    a, b = make_user(conn), make_user(conn)
    _as_probe(conn, a)
    with fails(conn, "row-level security"):
        insert_application(conn, b, 9)


def test_rls_no_user_set_sees_nothing(conn):
    insert_application(conn, make_user(conn), 1)
    _as_probe(conn)
    assert conn.execute(text("SELECT count(*) FROM applications")).scalar() == 0


def test_anon_has_no_table_access(conn):
    tables = conn.execute(text("""SELECT c.relname FROM pg_class c
        WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r','p')""")).scalars().all()
    leaked = [t for t in tables for r in ("anon", "authenticated")
              if conn.execute(text("SELECT has_table_privilege(:r, :t, 'SELECT')"), {"r": r, "t": f"public.{t}"}).scalar()]
    assert leaked == []


def test_future_partitions_not_granted_to_anon(conn):
    conn.execute(text("SELECT jobbot_ensure_daily_partitions('jobs', current_date + 20, current_date + 20)"))
    name = conn.execute(text("SELECT 'public.jobs_p' || to_char(current_date + 20, 'YYYYMMDD')")).scalar()
    assert not conn.execute(text("SELECT has_table_privilege('anon', :t, 'SELECT')"), {"t": name}).scalar()


def test_partition_functions_not_public(conn):
    for fn in ("jobbot_ensure_daily_partitions(text, date, date)", "jobbot_drop_daily_partitions(text, date)"):
        for role in ("anon", "authenticated", "rls_probe"):
            assert not conn.execute(text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                                    {"r": role, "f": fn}).scalar(), (role, fn)


def test_credentials_store_only_ciphertext(conn):
    col = conn.execute(text("""SELECT data_type FROM information_schema.columns
        WHERE table_name = 'credentials' AND column_name = 'secret_enc'""")).scalar()
    assert col == "bytea"
