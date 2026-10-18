import pytest
from sqlalchemy import text

from tests.integration.conftest import fails, insert_job, make_user

pytestmark = pytest.mark.integration


def test_row_lands_in_todays_partition(conn):
    uid = make_user(conn)
    insert_job(conn, uid)
    part = conn.execute(text("SELECT tableoid::regclass::text FROM jobs WHERE user_id = :u"), {"u": uid}).scalar()
    today = conn.execute(text("SELECT to_char(current_date, 'YYYYMMDD')")).scalar()
    assert part == f"jobs_p{today}"


def test_insert_without_partition_fails(conn):
    uid = make_user(conn)
    with fails(conn, "no partition"):
        insert_job(conn, uid, ingested_on=conn.execute(text("SELECT current_date + 60")).scalar())


def test_ensure_is_idempotent(conn):
    q = text("SELECT jobbot_ensure_daily_partitions('jobs', current_date, current_date + 10)")
    assert conn.execute(q).scalar() == 3      # days 8, 9, 10 are new
    assert conn.execute(q).scalar() == 0


def test_drop_removes_old_days_and_their_rows(conn):
    uid = make_user(conn)
    yesterday = conn.execute(text("SELECT current_date - 1")).scalar()
    insert_job(conn, uid, ingested_on=yesterday)
    insert_job(conn, uid)
    dropped = conn.execute(text("SELECT jobbot_drop_daily_partitions('jobs', current_date)")).scalar()
    assert dropped == 1
    assert conn.execute(text("SELECT count(*) FROM jobs WHERE user_id = :u"), {"u": uid}).scalar() == 1


def test_drop_ignores_non_daily_children(conn):
    conn.execute(text("CREATE TABLE jobs_archive PARTITION OF jobs FOR VALUES FROM ('2000-01-01') TO ('2000-02-01')"))
    conn.execute(text("SELECT jobbot_drop_daily_partitions('jobs', current_date)"))
    assert conn.execute(text("SELECT to_regclass('jobs_archive')")).scalar()


def test_seen_jobs_dedupe_lookup(conn):
    uid = make_user(conn)
    conn.execute(text("INSERT INTO seen_jobs (user_id, fingerprint, canonical_hash) VALUES (:u, 7, 70)"), {"u": uid})
    with fails(conn, "duplicate"):
        conn.execute(text("INSERT INTO seen_jobs (user_id, fingerprint, canonical_hash) VALUES (:u, 7, 71)"), {"u": uid})
    assert conn.execute(text("SELECT 1 FROM seen_jobs WHERE user_id=:u AND canonical_hash=70"), {"u": uid}).scalar()


def test_jd_signature_hamming_query(conn):
    conn.execute(text("INSERT INTO jd_signatures (company_key_hash, simhash) VALUES (9, :a)"), {"a": 0b1011})
    near = conn.execute(text("""
        SELECT count(*) FROM jd_signatures
        WHERE company_key_hash = 9 AND bit_count((simhash # :q)::bit(64)) <= 6"""), {"q": 0b1000}).scalar()
    assert near == 1
