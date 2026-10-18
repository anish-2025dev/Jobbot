"""Database-level guarantees behind the spec's hard constraints."""
import pytest
from sqlalchemy import text

from tests.integration.conftest import fails, insert_application, insert_job, make_user

pytestmark = pytest.mark.integration


def test_c4_ledger_one_row_per_opening(conn):
    uid = make_user(conn)
    q = text("INSERT INTO applied_ledger (user_id, canonical_hash, fingerprint, company_key_hash) VALUES (:u, 1, 11, 5)")
    conn.execute(q, {"u": uid})
    with fails(conn, "duplicate"):
        conn.execute(q, {"u": uid})
    # Same opening for a different user is fine (isolation).
    conn.execute(q, {"u": make_user(conn)})


def test_c4_ledger_fingerprint_lookup_uses_index(conn):
    conn.execute(text("SET LOCAL enable_seqscan = off"))   # tiny table would seq-scan
    plan = "\n".join(conn.execute(text(
        "EXPLAIN SELECT 1 FROM applied_ledger "
        "WHERE user_id = '00000000-0000-0000-0000-000000000001' AND fingerprint = 1")).scalars())
    assert "applied_ledger_fp_idx" in plan


@pytest.mark.parametrize("applied,reserved,ok", [(3, 0, True), (2, 1, True), (3, 1, False), (0, 4, False)])
def test_c5_cooldown_cap(conn, applied, reserved, ok):
    uid = make_user(conn)
    q = text("""INSERT INTO company_cooldown (user_id, company_key_hash, applied_ats, reserved_slots)
                VALUES (:u, 5, coalesce((SELECT array_agg(now()) FROM generate_series(1, :n)), '{}'), :r)""")
    params = {"u": uid, "n": applied, "r": reserved}
    if ok:
        conn.execute(q, params)
    else:
        with fails(conn, "check"):
            conn.execute(q, params)


def test_c5_four_timestamps_rejected(conn):
    uid = make_user(conn)
    with fails(conn, "check"):
        conn.execute(text("""INSERT INTO company_cooldown (user_id, company_key_hash, applied_ats)
                             VALUES (:u, 5, ARRAY[now(), now(), now(), now()])"""), {"u": uid})


def test_c3_summary_capped(conn):
    uid = make_user(conn)
    insert_job(conn, uid, summary="x" * 600)
    with fails(conn, "check"):
        insert_job(conn, uid, summary="x" * 601)


def test_applications_unique_per_opening(conn):
    uid = make_user(conn)
    insert_application(conn, uid, 77)
    with fails(conn, "duplicate"):
        insert_application(conn, uid, 77)


def test_c8_waiting_list_one_entry_per_application(conn):
    uid = make_user(conn)
    app_id = insert_application(conn, uid, 5, status="waiting")
    q = text("INSERT INTO waiting_list (user_id, application_id, ready_pack) VALUES (:u, :a, '{}')")
    conn.execute(q, {"u": uid, "a": app_id})
    with fails(conn, "duplicate"):
        conn.execute(q, {"u": uid, "a": app_id})
    exp = conn.execute(text("SELECT expires_at - entered_at FROM waiting_list WHERE application_id = :a"), {"a": app_id}).scalar()
    assert exp.total_seconds() == 24 * 3600


def test_responses_unique_gmail_id(conn):
    uid = make_user(conn)
    q = text("""INSERT INTO responses (user_id, class, priority, gmail_id, received_at, keep_until)
                VALUES (:u, 'interview', 1, 'g1', now(), now() + interval '7 days')""")
    conn.execute(q, {"u": uid})
    with fails(conn, "duplicate"):
        conn.execute(q, {"u": uid})


def test_c7_eviction_keeps_cap(conn):
    """Evicting a registry row never resets its cap (C5 vs C7)."""
    uid = make_user(conn)
    cid = conn.execute(text("""INSERT INTO companies (company_key, company_key_hash, name)
                               VALUES ('acme', 42, 'Acme') RETURNING id""")).scalar()
    insert_job(conn, uid, company_id=cid)
    conn.execute(text("""INSERT INTO company_cooldown (user_id, company_key_hash, applied_ats, last_applied_at)
                         VALUES (:u, 42, ARRAY[now(), now()], now())"""), {"u": uid})
    conn.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
    assert conn.execute(text("SELECT company_id FROM jobs WHERE user_id = :u"), {"u": uid}).scalar() is None
    n = conn.execute(text("SELECT cardinality(applied_ats) FROM company_cooldown WHERE user_id = :u"), {"u": uid}).scalar()
    assert n == 2


def test_user_delete_cascades(conn):
    uid = make_user(conn)
    conn.execute(text("INSERT INTO vaults (user_id, data) VALUES (:u, '{}')"), {"u": uid})
    insert_application(conn, uid, 3)
    conn.execute(text("INSERT INTO applied_ledger (user_id, canonical_hash, fingerprint, company_key_hash) VALUES (:u, 3, 3, 1)"), {"u": uid})
    conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    for t in ("vaults", "applications", "applied_ledger"):
        assert conn.execute(text(f"SELECT count(*) FROM {t} WHERE user_id = :u"), {"u": uid}).scalar() == 0


def test_referral_contact_delete_cascades_requests(conn):
    uid = make_user(conn)
    cid = conn.execute(text("""INSERT INTO referral_contacts (user_id, name, company_key_hash, company_name)
                               VALUES (:u, 'Priya', 42, 'Acme') RETURNING id"""), {"u": uid}).scalar()
    conn.execute(text("INSERT INTO referral_requests (user_id, contact_id, company_key_hash) VALUES (:u, :c, 42)"),
                 {"u": uid, "c": cid})
    conn.execute(text("DELETE FROM referral_contacts WHERE id = :c"), {"c": cid})
    assert conn.execute(text("SELECT count(*) FROM referral_requests WHERE user_id = :u"), {"u": uid}).scalar() == 0


def test_recipe_cache_halfvec(conn):
    uid = make_user(conn)
    q = text("INSERT INTO recipe_cache (user_id, jd_embedding, recipe, vault_revision) VALUES (:u, :e, '{}', 1)")
    vec = "[" + ",".join(["0.1"] * 256) + "]"
    conn.execute(q, {"u": uid, "e": vec})
    with fails(conn, "dimensions"):
        conn.execute(q, {"u": uid, "e": "[" + ",".join(["0.1"] * 255) + "]"})
    sim = conn.execute(text("SELECT 1 - (jd_embedding <=> CAST(:e AS halfvec(256))) FROM recipe_cache WHERE user_id = :u"),
                       {"u": uid, "e": vec}).scalar()
    assert sim == pytest.approx(1.0, abs=1e-3)   # >= 0.92 reuse threshold


def test_adapter_health_window(conn):
    conn.execute(text("INSERT INTO adapter_health (adapter, recent_count) VALUES ('greenhouse', 20)"))
    with fails(conn, "check"):
        conn.execute(text("INSERT INTO adapter_health (adapter, recent_count) VALUES ('lever', 21)"))


def test_outcomes_daily_has_no_user_column(conn):
    cols = set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'outcomes_daily'")).scalars())
    assert cols == {"day", "score_bucket", "source_id", "outcome", "count"}


def test_jobs_rank_sort_matches_spec(conn):
    """bucket desc, then salary desc (unknown last), then freshness."""
    uid = make_user(conn)
    insert_job(conn, uid, canonical_hash=1, bucket=2, salary_max=None, posted_at="2026-10-18")
    insert_job(conn, uid, canonical_hash=2, bucket=3, salary_max=800000, posted_at="2026-10-10")
    insert_job(conn, uid, canonical_hash=3, bucket=2, salary_max=1200000, posted_at="2026-10-01")
    insert_job(conn, uid, canonical_hash=4, bucket=2, salary_max=1200000, posted_at="2026-10-17")
    order = conn.execute(text("""SELECT canonical_hash FROM jobs WHERE user_id = :u
        ORDER BY bucket DESC, salary_max DESC NULLS LAST, posted_at DESC"""), {"u": uid}).scalars().all()
    assert order == [2, 4, 3, 1]
