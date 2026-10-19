"""Updated spec: email referral outreach, engine columns, training rows.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18

The updated spec replaces the manual "draft a WhatsApp ask" referral flow
(referral_contacts / referral_requests) with email outreach to 2-3 people
per job (sections 10, C14, C15). This migration:

- drops referral_contacts, referral_requests and the referral_status enum
- adds outreach_contacts, referral_queue, do_not_contact, provider_usage
- adds outreach_log (needed for C14's daily email cap and the >3% bounce
  guard in section 13; not in the section-4 table, see README)
- adds company_stars (per-user "starred company" for the referral flag)
- adds training_examples (per-user outcome rows for P6 weight tuning)
- adds the engine columns jobs/applications/companies/users need

Byte estimates follow the 0001 convention: heap tuple + index entries.
"""
from alembic import op

from app.util.sqlsplit import split_sql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NEW_USER_TABLES = [
    "outreach_contacts", "referral_queue", "do_not_contact", "provider_usage",
    "outreach_log", "company_stars", "training_examples",
]

UPGRADE_SQL = r"""
ALTER TYPE response_class ADD VALUE IF NOT EXISTS 'referral_given';
ALTER TYPE response_class ADD VALUE IF NOT EXISTS 'outreach_question';

CREATE TYPE contact_role AS ENUM ('recruiter', 'manager', 'peer');
CREATE TYPE referral_queue_status AS ENUM (
    'finding', 'awaiting_approval', 'scheduled', 'sent', 'referred',
    'applying', 'applied', 'closed', 'skipped'
);

DROP TABLE referral_requests;
DROP TABLE referral_contacts;
DROP TYPE referral_status;

-- users: auth (P6), time zone for digests and windows, Telegram linking.
ALTER TABLE users
    ADD COLUMN password_hash      text,
    ADD COLUMN timezone           text NOT NULL DEFAULT 'Asia/Kolkata',
    ADD COLUMN is_admin           boolean NOT NULL DEFAULT false,
    ADD COLUMN telegram_link_code text UNIQUE;

-- companies: email domain for outreach, size for the referral flag,
-- time zone for apply windows and 9-11 am sends.
ALTER TABLE companies
    ADD COLUMN email_domain    text,
    ADD COLUMN employee_count  integer,
    ADD COLUMN referral_driven boolean NOT NULL DEFAULT false,
    ADD COLUMN timezone        text;

-- jobs (+ ~40 B/row): what the engine needs after the JD is discarded (C3).
ALTER TABLE jobs
    ADD COLUMN ats            ats_type NOT NULL DEFAULT 'unknown',
    ADD COLUMN req_id         text,
    ADD COLUMN referral_worthy boolean NOT NULL DEFAULT false,
    ADD COLUMN components     smallint[],          -- 6 score parts x1000
    ADD COLUMN missing_skills text[] NOT NULL DEFAULT '{}';

-- applications (+ ~60 B/row)
ALTER TABLE applications
    ADD COLUMN ats                  ats_type NOT NULL DEFAULT 'unknown',
    ADD COLUMN req_id               text,
    ADD COLUMN location             text,
    ADD COLUMN company_tz           text,
    ADD COLUMN components           smallint[],
    ADD COLUMN missing_skills       text[] NOT NULL DEFAULT '{}',
    ADD COLUMN via_referral         boolean NOT NULL DEFAULT false,
    ADD COLUMN referrer             text,
    ADD COLUMN screenshot_url       text,
    ADD COLUMN attempts             smallint NOT NULL DEFAULT 0,
    ADD COLUMN claim_prev_last_at   timestamptz,       -- undo data for a failed submit
    ADD COLUMN claimed_at           timestamptz,
    ADD COLUMN uses_reserved_slot   boolean NOT NULL DEFAULT false,
    ADD COLUMN outcome_recorded     boolean NOT NULL DEFAULT false;

ALTER TABLE credentials
    ADD COLUMN kind text NOT NULL DEFAULT 'ats',     -- ats|gmail|imap|apollo|hunter|snov|totp
    ADD COLUMN meta jsonb NOT NULL DEFAULT '{}';     -- non-secret settings only

-- ~250 B heap + ~110 B idx. Found people at a company (section 10.5: only
-- name, title, company, verified email, provider, last contact).
CREATE TABLE outreach_contacts (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id           uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_key_hash  bigint NOT NULL,
    name              text NOT NULL,
    title             text NOT NULL,
    role_type         contact_role NOT NULL,
    location          text,
    email             text NOT NULL,
    provider          text NOT NULL,
    verified_at       timestamptz NOT NULL,
    last_contacted_at timestamptz,
    replied_positive  boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, email)
);
CREATE INDEX outreach_contacts_company_idx ON outreach_contacts (user_id, company_key_hash);

-- ~600 B heap (+ ~400 B per drafted contact) + ~120 B idx.
-- contacts: [{contact_id, role, status, subject, body, scheduled_at,
--             sent_at, gmail_message_id, gmail_thread_id}]
CREATE TABLE referral_queue (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id   bigint NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
    canonical_hash   bigint NOT NULL,
    company_key_hash bigint NOT NULL,
    job              jsonb NOT NULL,                 -- frozen {title, req_id, apply_url, ...}
    contacts         jsonb NOT NULL DEFAULT '[]',
    status           referral_queue_status NOT NULL DEFAULT 'finding',
    slot_reserved    boolean NOT NULL DEFAULT false,
    sent_at          timestamptz,                    -- last outreach email sent
    apply_at         timestamptz,                    -- sent_at + REFERRAL_HOLD_HOURS (C15)
    referral         jsonb,                          -- {by, link, at}
    created_at       timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz,
    UNIQUE (user_id, canonical_hash)
);
CREATE INDEX referral_queue_due_idx ON referral_queue (status, apply_at);
CREATE INDEX referral_queue_user_idx ON referral_queue (user_id, created_at);

-- ~40 B heap + ~40 B idx. HMAC of the lowercased address, never the address.
CREATE TABLE do_not_contact (
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email_hash bigint NOT NULL,
    added_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, email_hash)
);

-- ~70 B. One row per user per email-finder provider.
CREATE TABLE provider_usage (
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        text NOT NULL,
    period_start    date NOT NULL DEFAULT date_trunc('month', now())::date,
    credits_used    integer NOT NULL DEFAULT 0,
    credits_limit   integer,                         -- null = unknown, rely on 402/429
    exhausted_until timestamptz,
    PRIMARY KEY (user_id, provider)
);

-- ~90 B heap + ~50 B idx. 30 days: daily cap (C14) and bounce rate (section 13).
CREATE TABLE outreach_log (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient_hash   bigint NOT NULL,
    company_key_hash bigint NOT NULL,
    sent_at          timestamptz NOT NULL DEFAULT now(),
    status           smallint NOT NULL DEFAULT 0,    -- 0 sent, 1 bounced, 2 complaint
    gmail_message_id text
);
CREATE INDEX outreach_log_user_idx ON outreach_log (user_id, sent_at);

-- ~40 B. Per-user starred companies (referral-worthiness, section 6.5).
CREATE TABLE company_stars (
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_key_hash bigint NOT NULL,
    PRIMARY KEY (user_id, company_key_hash)
);

-- ~70 B. 180 days. One row per application with a known outcome; the six
-- score components let P6 fit per-user weights (outcomes_daily is
-- anonymous by design and can't carry components).
CREATE TABLE training_examples (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_on date NOT NULL DEFAULT current_date,
    components smallint[] NOT NULL,
    label      smallint NOT NULL CHECK (label IN (0, 1))
);
CREATE INDEX training_examples_user_idx ON training_examples (user_id, created_on);
"""

DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS training_examples;
DROP TABLE IF EXISTS company_stars;
DROP TABLE IF EXISTS outreach_log;
DROP TABLE IF EXISTS provider_usage;
DROP TABLE IF EXISTS do_not_contact;
DROP TABLE IF EXISTS referral_queue;
DROP TABLE IF EXISTS outreach_contacts;
DROP TYPE IF EXISTS referral_queue_status;
DROP TYPE IF EXISTS contact_role;

ALTER TABLE credentials DROP COLUMN IF EXISTS kind, DROP COLUMN IF EXISTS meta;
ALTER TABLE applications
    DROP COLUMN IF EXISTS ats, DROP COLUMN IF EXISTS req_id, DROP COLUMN IF EXISTS location,
    DROP COLUMN IF EXISTS company_tz, DROP COLUMN IF EXISTS components,
    DROP COLUMN IF EXISTS missing_skills, DROP COLUMN IF EXISTS via_referral,
    DROP COLUMN IF EXISTS referrer, DROP COLUMN IF EXISTS screenshot_url,
    DROP COLUMN IF EXISTS attempts, DROP COLUMN IF EXISTS claim_prev_last_at,
    DROP COLUMN IF EXISTS claimed_at, DROP COLUMN IF EXISTS uses_reserved_slot,
    DROP COLUMN IF EXISTS outcome_recorded;
ALTER TABLE jobs
    DROP COLUMN IF EXISTS ats, DROP COLUMN IF EXISTS req_id,
    DROP COLUMN IF EXISTS referral_worthy, DROP COLUMN IF EXISTS components,
    DROP COLUMN IF EXISTS missing_skills;
ALTER TABLE companies
    DROP COLUMN IF EXISTS email_domain, DROP COLUMN IF EXISTS employee_count,
    DROP COLUMN IF EXISTS referral_driven, DROP COLUMN IF EXISTS timezone;
ALTER TABLE users
    DROP COLUMN IF EXISTS password_hash, DROP COLUMN IF EXISTS timezone,
    DROP COLUMN IF EXISTS is_admin, DROP COLUMN IF EXISTS telegram_link_code;

CREATE TYPE referral_status AS ENUM ('drafted', 'asked', 'referred', 'applied', 'closed', 'expired');
CREATE TABLE referral_contacts (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name             text NOT NULL,
    company_key_hash bigint NOT NULL,
    company_name     text NOT NULL,
    relationship     text,
    channel          text,
    notes            text,
    last_asked_at    timestamptz,
    active           boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX referral_contacts_company_idx ON referral_contacts (user_id, company_key_hash);
CREATE TABLE referral_requests (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    contact_id       bigint NOT NULL REFERENCES referral_contacts(id) ON DELETE CASCADE,
    company_key_hash bigint NOT NULL,
    jobs             jsonb NOT NULL DEFAULT '[]',
    status           referral_status NOT NULL DEFAULT 'drafted',
    asked_at         timestamptz,
    hold_until       timestamptz,
    slot_reserved    boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz
);
CREATE INDEX referral_requests_hold_idx ON referral_requests (user_id, status, hold_until);
CREATE INDEX referral_requests_contact_idx ON referral_requests (contact_id, created_at);
"""
# Enum values added in upgrade (response_class) can't be removed in Postgres
# without rebuilding the type; downgrade leaves them, which is harmless.


def _rls(tables: list[str]) -> str:
    me = "nullif(current_setting('app.user_id', true), '')::uuid"
    out = []
    for t in tables:
        out.append(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;")
        out.append(f"CREATE POLICY {t}_owner ON {t} USING (user_id = {me}) WITH CHECK (user_id = {me});")
    out.append("""
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ' || quote_ident(r);
            EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ' || quote_ident(r);
        END IF;
    END LOOP;
END $$;""")
    return "\n".join(out)


def all_statements() -> list[str]:
    return split_sql(UPGRADE_SQL) + split_sql(_rls(NEW_USER_TABLES))


def downgrade_statements() -> list[str]:
    rls_old = _rls(["referral_contacts", "referral_requests"])
    return split_sql(DOWNGRADE_SQL) + split_sql(rls_old)


def upgrade() -> None:
    for stmt in all_statements():
        op.execute(stmt)


def downgrade() -> None:
    for stmt in downgrade_statements():
        op.execute(stmt)
