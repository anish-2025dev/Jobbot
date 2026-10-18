from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

engine = create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=5)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def worker_session() -> Iterator[Session]:
    """Owner connection used by background workers (bypasses RLS)."""
    with SessionLocal.begin() as s:
        yield s


@contextmanager
def user_session(user_id: UUID) -> Iterator[Session]:
    """Scopes RLS to one user for this transaction. Effective once the API
    connects as a non-owner role (P5); harmless before that."""
    with SessionLocal.begin() as s:
        s.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id)})
        yield s


def db_size_mb() -> float:
    with engine.connect() as c:
        return c.execute(text("SELECT pg_database_size(current_database())")).scalar_one() / 2**20
