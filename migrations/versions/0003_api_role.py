"""Non-owner API role so row-level security is actually enforced (P6).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18

The table owner bypasses RLS. Workers keep using the owner connection; the
user-facing API runs each request inside `SET LOCAL ROLE jobbot_api` plus
`app.user_id` (app/db.py), so every policy from 0001/0002 applies.

If the migrating user lacks CREATEROLE, the role is skipped with a NOTICE
and the API falls back to application-level ownership checks.
"""
from alembic import op

from app.util.sqlsplit import split_sql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

API_ROLE = "jobbot_api"
CRUD_TABLES = [
    "users", "vaults", "jobs", "seen_jobs", "applied_ledger", "company_cooldown",
    "applications", "waiting_list", "responses", "form_answers", "credentials",
    "recipe_cache", "mail_actions", "outreach_contacts", "referral_queue",
    "do_not_contact", "provider_usage", "outreach_log", "company_stars",
    "training_examples",
]
READ_TABLES = ["sources", "companies", "adapter_health"]

UPGRADE_SQL = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{API_ROLE}') THEN
        CREATE ROLE {API_ROLE} NOLOGIN;
    END IF;
    EXECUTE 'GRANT {API_ROLE} TO ' || quote_ident(current_user);
    EXECUTE 'GRANT USAGE ON SCHEMA public TO {API_ROLE}';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON {", ".join(CRUD_TABLES)} TO {API_ROLE}';
    EXECUTE 'GRANT SELECT ON {", ".join(READ_TABLES)} TO {API_ROLE}';
    EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {API_ROLE}';
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'jobbot: cannot create role {API_ROLE}; API will use app-level checks only';
END $$;
"""

DOWNGRADE_SQL = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{API_ROLE}') THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {API_ROLE}';
        EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {API_ROLE}';
        EXECUTE 'REVOKE USAGE ON SCHEMA public FROM {API_ROLE}';
    END IF;
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'jobbot: cannot revoke from {API_ROLE}';
END $$;
"""
# The role itself is cluster-wide and may be shared by other databases
# (e.g. test databases), so downgrade revokes privileges but keeps the role.


def upgrade() -> None:
    for stmt in split_sql(UPGRADE_SQL):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in split_sql(DOWNGRADE_SQL):
        op.execute(stmt)
