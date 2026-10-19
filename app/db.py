"""Database access.

worker_session(): owner connection for background workers (bypasses RLS).
user_session(uid): user-facing requests. Runs under the non-owner role from
migration 0003 when available, so row-level security is enforced by
Postgres, not just by the application.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    connect_args={"options": "-c timezone=UTC"},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@lru_cache
def api_role_available() -> bool:
    role = get_settings().db_api_role
    if not role:
        return False
    with engine.connect() as c:
        return bool(c.execute(
            text("SELECT pg_has_role(current_user, :r, 'MEMBER') FROM pg_roles WHERE rolname = :r"),
            {"r": role},
        ).scalar())


@contextmanager
def worker_session() -> Iterator[Session]:
    """Owner connection used by background workers (bypasses RLS)."""
    with SessionLocal.begin() as s:
        yield s


@contextmanager
def user_session(user_id: UUID) -> Iterator[Session]:
    """One transaction scoped to one user. RLS applies when the API role exists."""
    with SessionLocal.begin() as s:
        if api_role_available():
            s.execute(text(f'SET LOCAL ROLE "{get_settings().db_api_role}"'))
        s.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id)})
        yield s


def db_size_mb() -> float:
    with engine.connect() as c:
        return c.execute(text("SELECT pg_database_size(current_database())")).scalar_one() / 2**20
