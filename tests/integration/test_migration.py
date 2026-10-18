from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

TABLES = {
    "users", "vaults", "sources", "companies", "jobs", "seen_jobs", "jd_signatures",
    "applied_ledger", "company_cooldown", "applications", "waiting_list",
    "responses", "referral_contacts", "referral_requests", "outcomes_daily",
    "form_answers", "credentials", "recipe_cache", "adapter_health", "mail_actions",
}
RLS_TABLES = {"users", "vaults", "jobs", "seen_jobs", "applied_ledger", "company_cooldown",
              "applications", "waiting_list", "responses", "referral_contacts",
              "referral_requests", "form_answers", "credentials", "recipe_cache", "mail_actions"}


def _parent_tables(c):
    return set(c.execute(text("""
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
    """)).scalars())


def test_all_tables_exist(conn):
    assert TABLES <= _parent_tables(conn)


def test_pgvector_and_helpers(conn):
    assert conn.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar()
    fns = set(conn.execute(text("SELECT proname FROM pg_proc WHERE proname LIKE 'jobbot%'")).scalars())
    assert fns == {"jobbot_ensure_daily_partitions", "jobbot_drop_daily_partitions"}


def test_sources_seeded(conn):
    rows = dict(conn.execute(text("SELECT key, tier FROM sources")).all())
    assert len(rows) == 14
    assert rows["greenhouse"] == "A" and rows["jsearch"] == "B"
    assert rows["alert_linkedin"] == "C" and rows["career_jsonld"] == "D"


def test_week_of_partitions_created(conn):
    today = conn.execute(text("SELECT current_date")).scalar_one()
    for parent in ("jobs", "seen_jobs", "jd_signatures"):
        for d in range(-1, 8):
            name = f"{parent}_p{(today + timedelta(days=d)):%Y%m%d}"
            assert conn.execute(text("SELECT to_regclass(:n)"), {"n": name}).scalar(), name


def test_rls_enabled(conn):
    on = set(conn.execute(text("""
        SELECT relname FROM pg_class WHERE relrowsecurity AND relnamespace = 'public'::regnamespace
        AND NOT relispartition""")).scalars())
    assert RLS_TABLES == on


def test_migration_is_committed(engine):
    # Regression: env.py once ran SET before begin_transaction, which left the
    # whole migration in an uncommitted transaction. A brand-new connection
    # must see the tables.
    with engine.connect() as c:
        assert c.execute(text("SELECT to_regclass('public.applied_ledger')")).scalar()


def test_downgrade_upgrade_roundtrip(fresh_db_url, alembic_cfg):
    from alembic import command
    cfg, env = alembic_cfg
    with env(fresh_db_url):
        command.upgrade(cfg(), "head")
        command.downgrade(cfg(), "base")
        eng = create_engine(fresh_db_url)
        with eng.connect() as c:
            assert _parent_tables(c) <= {"alembic_version"}
            assert not c.execute(text(
                "SELECT count(*) FROM pg_type WHERE typname IN ('application_status','ats_type')")).scalar()
            assert not c.execute(text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'jobbot%'")).scalar()
        command.upgrade(cfg(), "head")
        with eng.connect() as c:
            assert TABLES <= _parent_tables(c)
        eng.dispose()
