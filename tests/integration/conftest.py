"""Integration fixtures. Each session migrates a throwaway database created
on the server named by TEST_DATABASE_URL (any DB on it, e.g. /postgres; the
user needs CREATEDB and CREATEROLE). Every test runs in a transaction that
is rolled back, so tests never see each other's rows."""
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parents[2]
ADMIN_URL = os.environ.get("TEST_DATABASE_URL")

if not ADMIN_URL:
    collect_ignore_glob = ["test_*.py"]  # unit tests still run without a DB


def _cfg():
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


@contextmanager
def _database_url_env(url: str):
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


def _create_db(supabase_like: bool = True) -> str:
    name = f"jobbot_it_{uuid.uuid4().hex[:10]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
        for role in ("anon", "authenticated", "rls_probe"):
            c.execute(text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
                + role + "') THEN CREATE ROLE " + role + " NOLOGIN; END IF; END $$"))
    admin.dispose()
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    if supabase_like:  # Supabase grants new public tables to anon/authenticated
        eng = create_engine(url, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text("GRANT USAGE ON SCHEMA public TO anon, authenticated, rls_probe"))
            c.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated"))
        eng.dispose()
    return url


def _drop_db(url: str) -> None:
    name = make_url(url).database
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="session")
def migrated_url():
    from alembic import command
    url = _create_db()
    with _database_url_env(url):
        command.upgrade(_cfg(), "head")
    yield url
    _drop_db(url)


@pytest.fixture(scope="session")
def engine(migrated_url):
    eng = create_engine(migrated_url, connect_args={"options": "-c timezone=UTC"})
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        tx = c.begin()
        yield c
        tx.rollback()


@pytest.fixture
def fresh_db_url():
    """Unmigrated database for round-trip tests."""
    url = _create_db()
    yield url
    _drop_db(url)


@pytest.fixture
def alembic_cfg():
    return _cfg, _database_url_env


# ---------------------------------------------------------------- helpers
def make_user(conn, email=None) -> uuid.UUID:
    email = email or f"u{uuid.uuid4().hex[:8]}@test.dev"
    return conn.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()


def source_id(conn, key="greenhouse") -> int:
    return conn.execute(text("SELECT id FROM sources WHERE key = :k"), {"k": key}).scalar_one()


def insert_job(conn, user_id, **kw) -> int:
    row = dict(user_id=user_id, canonical_hash=1, fingerprint=1, company_id=None,
               company_key_hash=42, company_name="Acme", source_id=source_id(conn),
               title="SDE", url="https://acme.dev/j/1", score=0.7, bucket=2, summary="s")
    row.update(kw)
    cols = ", ".join(row)
    vals = ", ".join(f":{k}" for k in row)
    return conn.execute(text(f"INSERT INTO jobs ({cols}) VALUES ({vals}) RETURNING id"), row).scalar_one()


def insert_application(conn, user_id, canonical_hash=1, **kw) -> int:
    row = dict(user_id=user_id, canonical_hash=canonical_hash, fingerprint=canonical_hash,
               company_key_hash=42, company_name="Acme", title="SDE", apply_url="https://a/1")
    row.update(kw)
    cols = ", ".join(row)
    vals = ", ".join(f":{k}" for k in row)
    return conn.execute(text(f"INSERT INTO applications ({cols}) VALUES ({vals}) RETURNING id"), row).scalar_one()


@contextmanager
def fails(conn, match: str | None = None):
    """Assert the block raises a DB error; roll back only that savepoint."""
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError) as exc:
        yield
    sp.rollback()
    if match:
        assert match in str(exc.value).lower(), str(exc.value)
