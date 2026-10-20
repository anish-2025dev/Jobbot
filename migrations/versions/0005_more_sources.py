"""More job sources: 4 ATS boards, 9 job-board APIs, 12 more alert portals.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20

ats_type gains recruitee, rippling, breezy, oracle (keyless public boards).
sources gains one ~200 B row per new site (26 rows, ~5 KB total). Nothing else
changes size: the new boards feed the same jobs / seen_jobs tables.

Totals after this migration: 40 sources (10 ATS boards incl. Workable links,
12 search/board APIs, 16 alert portals, 2 career-page readers).
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

NEW_ATS = ("recruitee", "rippling", "breezy", "oracle")
NEW_SOURCES = [
    # Tier A: public ATS boards polled per company
    ("recruitee", "A", 0.9), ("rippling", "A", 0.9), ("breezy", "A", 0.85), ("oracle", "A", 0.85),
    ("workable", "A", 0.85),
    # Tier B: keyless job-board feeds and keyed search APIs
    ("remotive", "B", 0.55), ("remoteok", "B", 0.5), ("arbeitnow", "B", 0.45), ("jobicy", "B", 0.5),
    ("himalayas", "B", 0.55), ("themuse", "B", 0.55), ("workingnomads", "B", 0.45),
    ("jooble", "B", 0.5), ("findwork", "B", 0.5),
    # Tier C: job-alert emails (manual apply only, C12)
    ("alert_indeed", "C", 0.45), ("alert_glassdoor", "C", 0.45), ("alert_foundit", "C", 0.45),
    ("alert_internshala", "C", 0.5), ("alert_cutshort", "C", 0.55), ("alert_hirist", "C", 0.5),
    ("alert_unstop", "C", 0.5), ("alert_shine", "C", 0.4), ("alert_timesjobs", "C", 0.4),
    ("alert_iimjobs", "C", 0.45), ("alert_apna", "C", 0.4), ("alert_workatastartup", "C", 0.6),
]


def upgrade() -> None:
    for value in NEW_ATS:
        op.execute(f"ALTER TYPE ats_type ADD VALUE IF NOT EXISTS '{value}';")
    values = ", ".join(f"('{k}', '{t}', {q})" for k, t, q in NEW_SOURCES)
    op.execute(f"INSERT INTO sources (key, tier, quality) VALUES {values} ON CONFLICT (key) DO NOTHING;")


def downgrade() -> None:
    keys = ", ".join(f"'{k}'" for k, _, _ in NEW_SOURCES)
    op.execute(f"DELETE FROM sources WHERE key IN ({keys}) AND NOT EXISTS "
               f"(SELECT 1 FROM jobs WHERE jobs.source_id = sources.id);")
    # Postgres cannot drop enum values; companies on the new boards go back to 'unknown'.
    ats = ", ".join(f"'{a}'" for a in NEW_ATS)
    op.execute(f"UPDATE companies SET ats_type = 'unknown', ats_slug = NULL, ats_host = NULL "
               f"WHERE ats_type::text IN ({ats});")
