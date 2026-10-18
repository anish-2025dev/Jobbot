"""Migrations are hand-written SQL (partitions, pgvector, RLS), so there is no
autogenerate target. DATABASE_URL must be a direct/session connection, not
Supabase's transaction pooler (port 6543)."""
import os

from alembic import context
from sqlalchemy import create_engine, pool

url = os.environ["DATABASE_URL"]


def run_offline() -> None:
    context.configure(url=url, literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    # Timezone via connect options: running SET on the connection first would
    # autobegin a transaction that Alembic then never commits.
    engine = create_engine(url, poolclass=pool.NullPool,
                           connect_args={"options": "-c timezone=UTC"})
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_offline() if context.is_offline_mode() else run_online()
