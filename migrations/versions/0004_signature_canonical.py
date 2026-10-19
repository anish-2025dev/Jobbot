"""jd_signatures.canonical_hash: repost = similar JD under a DIFFERENT opening.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18

Signatures are global (a repost is a property of the job, not the user).
Without the canonical id, the second user to process a job would match the
first user's signature of the very same opening and skip it as a "repost".
+8 B/row (~88 B incl. index).
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jd_signatures ADD COLUMN canonical_hash bigint NOT NULL DEFAULT 0;")
    op.execute("CREATE INDEX jd_signatures_canon_idx ON jd_signatures (canonical_hash);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS jd_signatures_canon_idx;")
    op.execute("ALTER TABLE jd_signatures DROP COLUMN IF EXISTS canonical_hash;")
