"""Initial schema: every section-4 table plus jd_signatures.

Revision ID: 0001
Revises:
Create Date: 2026-10-18

Conventions
- All hashes (canonical_hash, fingerprint, company_key_hash, simhash,
  question_hash) are signed 64-bit xxhash/simhash values -> bigint (8 B).
  See app/util/hashing.py.
- Byte estimates in comments = heap tuple (24 B header + data + padding)
  + index entries. Real numbers vary with text length; verify with
  scripts/size_report.sql after a simulated week (P1).
- DB runs in UTC; partition keys use current_date in UTC.
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Tables that carry user_id and get a row-level-security policy.
USER_TABLES = [
    "vaults", "jobs", "seen_jobs", "applied_ledger", "company_cooldown",
    "applications", "waiting_list", "responses", "referral_contacts",
    "referral_requests", "form_answers", "credentials", "recipe_cache",
    "mail_actions",
]

ENUMS = {
    "application_status": ["queued", "applying", "waiting", "applied",
                           "applied_pending_referral", "positive",
                           "rejected", "closed", "failed"],
    "apply_mode": ["auto", "assisted", "manual"],
    "response_class": ["offer", "interview", "test", "recruiter",
                       "info_request", "unclear"],
    "referral_status": ["drafted", "asked", "referred", "applied",
                        "closed", "expired"],
    "mail_action": ["star_label", "trash", "restore"],
    "health_status": ["active", "paused"],
    "ats_type": ["greenhouse", "lever", "ashby", "smartrecruiters",
                 "workable", "workday", "jsonld", "generic", "unknown"],
    "remote_type": ["onsite", "hybrid", "remote", "unknown"],
}

SCHEMA_SQL = r"""
CREATE EXTENSION IF NOT EXISTS vector;   -- pgvector >= 0.7 (halfvec)

-- ~250 B
CREATE TABLE users (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email            text NOT NULL UNIQUE,
    display_name     text,
    telegram_chat_id bigint,
    settings         jsonb NOT NULL DEFAULT '{}',   -- per-user overrides of MIN_SCORE etc.
    created_at       timestamptz NOT NULL DEFAULT now(),
    deleted_at       timestamptz
);

-- ~50 KB raw, TOAST-compressed to roughly 15-20 KB
CREATE TABLE vaults (
    user_id        uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data           jsonb NOT NULL,
    schema_version smallint NOT NULL DEFAULT 1,
    revision       integer NOT NULL DEFAULT 1,      -- bumps on every save; recipes record it
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- ~200 B. A source is a SITE (rate limits apply here, C9).
CREATE TABLE sources (
    id                   smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key                  text NOT NULL UNIQUE,
    tier                 char(1) NOT NULL CHECK (tier IN ('A','B','C','D')),
    quality              real NOT NULL DEFAULT 0.5, -- feeds the 0.10 source-quality weight
    status               health_status NOT NULL DEFAULT 'active',
    consecutive_failures smallint NOT NULL DEFAULT 0,
    paused_until         timestamptz,
    last_ok_at           timestamptz,
    last_error           text,
    config               jsonb NOT NULL DEFAULT '{}'
);

-- ~350 B heap + ~100 B idx. A company is an EMPLOYER (caps apply here, C9).
-- Global registry shared by all users; eviction never touches company_cooldown.
CREATE TABLE companies (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_key        text NOT NULL UNIQUE,        -- normalized employer name
    company_key_hash   bigint NOT NULL UNIQUE,
    name               text NOT NULL,
    ats_type           ats_type NOT NULL DEFAULT 'unknown',
    ats_slug           text,
    ats_host           text,                        -- Workday tenant host etc.
    careers_url        text,
    poll_interval_min  smallint NOT NULL DEFAULT 120,
    last_polled_at     timestamptz,
    last_probed_at     timestamptz,
    open_matching_jobs integer NOT NULL DEFAULT 0,  -- admission test (section 5, rule 3)
    eviction_score     real NOT NULL DEFAULT 0,     -- S = S*exp(-dt/30d) + 1
    score_updated_at   timestamptz NOT NULL DEFAULT now(),
    last_applied_at    timestamptz,
    pinned             boolean NOT NULL DEFAULT false,
    added_at           timestamptz NOT NULL DEFAULT now(),
    meta               jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX companies_eviction_idx ON companies (eviction_score, last_applied_at);

-- ~1.0-1.4 KB heap + ~120 B idx. Per-user (scores are per user). 7 days.
CREATE TABLE jobs (
    id               bigserial,
    ingested_on      date NOT NULL DEFAULT current_date,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    canonical_hash   bigint NOT NULL,
    fingerprint      bigint NOT NULL,
    company_id       bigint REFERENCES companies(id) ON DELETE SET NULL, -- null = aggregator-only
    company_key_hash bigint NOT NULL,
    company_name     text NOT NULL,
    source_id        smallint NOT NULL REFERENCES sources(id),
    title            text NOT NULL,
    location         text,
    remote_type      remote_type NOT NULL DEFAULT 'unknown',
    exp_min          smallint,
    exp_max          smallint,
    grad_years       smallint[],                     -- eligible batches; null = unrestricted
    salary_min       integer,                        -- annual, in salary_currency
    salary_max       integer,
    salary_currency  char(3),
    skills           text[] NOT NULL DEFAULT '{}',
    summary          text CHECK (char_length(summary) <= 600),  -- C3: no full JD
    url              text NOT NULL,
    posted_at        timestamptz,
    score            real NOT NULL,
    bucket           smallint NOT NULL,              -- 3 high, 2 medium, 1 low, 0 stretch
    PRIMARY KEY (ingested_on, id)
) PARTITION BY RANGE (ingested_on);
CREATE INDEX jobs_rank_idx  ON jobs (user_id, bucket DESC, salary_max DESC NULLS LAST, posted_at DESC);
CREATE INDEX jobs_canon_idx ON jobs (user_id, canonical_hash);

-- ~64 B heap + ~80 B idx. Every processed job incl. rejected. 7 days.
CREATE TABLE seen_jobs (
    seen_on        date NOT NULL DEFAULT current_date,
    user_id        uuid NOT NULL,
    fingerprint    bigint NOT NULL,
    canonical_hash bigint NOT NULL,
    PRIMARY KEY (user_id, fingerprint, seen_on)
) PARTITION BY RANGE (seen_on);
CREATE INDEX seen_jobs_canon_idx ON seen_jobs (user_id, canonical_hash);

-- ~48 B heap + ~30 B idx. Global near-duplicate JD signatures for repost
-- detection (60 days). Written only for jobs that reach Stretch or above.
CREATE TABLE jd_signatures (
    seen_on          date NOT NULL DEFAULT current_date,
    company_key_hash bigint NOT NULL,
    simhash          bigint NOT NULL
) PARTITION BY RANGE (seen_on);
CREATE INDEX jd_signatures_company_idx ON jd_signatures (company_key_hash);

-- ~80 B heap + ~90 B idx. C4 source of truth. Kept past 90 days until the
-- posting is verified closed (janitor, P1).
CREATE TABLE applied_ledger (
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    canonical_hash   bigint NOT NULL,
    fingerprint      bigint NOT NULL,
    company_key_hash bigint NOT NULL,
    applied_at       timestamptz NOT NULL DEFAULT now(),
    via              smallint NOT NULL DEFAULT 0,    -- 0 normal, 1 referral, 2 manual
    PRIMARY KEY (user_id, canonical_hash)
);
CREATE INDEX applied_ledger_fp_idx ON applied_ledger (user_id, fingerprint);

-- ~110 B heap + ~45 B idx. C5/C6. applied_ats holds at most the last 3
-- timestamps -> exact rolling 30-day window, not a bucketed counter.
CREATE TABLE company_cooldown (
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_key_hash bigint NOT NULL,
    applied_ats      timestamptz[] NOT NULL DEFAULT '{}' CHECK (cardinality(applied_ats) <= 3),
    reserved_slots   smallint NOT NULL DEFAULT 0 CHECK (reserved_slots >= 0),
    last_applied_at  timestamptz,
    PRIMARY KEY (user_id, company_key_hash),
    CHECK (cardinality(applied_ats) + reserved_slots <= 3)
);

-- ~500 B heap (+ ~2 KB recipe for 7 days; kept only if positive) + ~150 B idx.
CREATE TABLE applications (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    canonical_hash   bigint NOT NULL,
    fingerprint      bigint NOT NULL,
    company_key_hash bigint NOT NULL,
    company_name     text NOT NULL,                  -- snapshot: jobs expire after 7 days
    title            text NOT NULL,
    apply_url        text NOT NULL,
    source_id        smallint REFERENCES sources(id),
    score            real,
    bucket           smallint,
    salary_max       integer,
    posted_at        timestamptz,
    status           application_status NOT NULL DEFAULT 'queued',
    mode             apply_mode,
    not_before       timestamptz,                    -- C6 requeue time
    recipe           jsonb,                          -- nulled 7d after apply unless positive
    vault_revision   integer,
    failure_step     text,
    failure_reason   text,
    gmail_thread_id  text,
    queued_at        timestamptz NOT NULL DEFAULT now(),
    applied_at       timestamptz,
    last_status_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at       timestamptz,                    -- soft delete for rejections
    UNIQUE (user_id, canonical_hash)
);
CREATE INDEX applications_queue_idx   ON applications (user_id, status, not_before);
CREATE INDEX applications_company_idx ON applications (user_id, company_key_hash);

-- ~10 KB (Ready Pack). Max 30 per user, enforced by the service under a user lock.
CREATE TABLE waiting_list (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id bigint NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
    entered_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    pin_count      smallint NOT NULL DEFAULT 0,
    ready_pack     jsonb NOT NULL
);
CREATE INDEX waiting_list_expiry_idx ON waiting_list (user_id, expires_at);

-- ~0.8-2 KB. Positive / unclear mail only.
CREATE TABLE responses (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id  bigint REFERENCES applications(id) ON DELETE SET NULL,
    class           response_class NOT NULL,
    priority        smallint NOT NULL,               -- 0 offer, 1 interview/test/etc, 2 unclear
    deadline        timestamptz,
    snippet         text CHECK (char_length(snippet) <= 500),
    gmail_id        text NOT NULL,
    gmail_thread_id text,
    received_at     timestamptz NOT NULL,
    keep_until      timestamptz NOT NULL,            -- >= received_at + 7 days
    user_done_at    timestamptz,
    deleted_at      timestamptz,
    UNIQUE (user_id, gmail_id)
);

-- ~200 B
CREATE TABLE referral_contacts (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name             text NOT NULL,
    company_key_hash bigint NOT NULL,
    company_name     text NOT NULL,
    relationship     text,
    channel          text,                           -- whatsapp / linkedin / email
    notes            text,
    last_asked_at    timestamptz,
    active           boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX referral_contacts_company_idx ON referral_contacts (user_id, company_key_hash);

-- ~300 B + ~250 B per extra job appended. One ask per contact per company per
-- 30 days: later jobs at that company are appended to `jobs`, not re-asked.
CREATE TABLE referral_requests (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    contact_id       bigint NOT NULL REFERENCES referral_contacts(id) ON DELETE CASCADE,
    company_key_hash bigint NOT NULL,
    jobs             jsonb NOT NULL DEFAULT '[]',    -- frozen [{title, req_id, canonical_hash, apply_url, added_at}]
    status           referral_status NOT NULL DEFAULT 'drafted',
    asked_at         timestamptz,
    hold_until       timestamptz,
    slot_reserved    boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    closed_at        timestamptz
);
CREATE INDEX referral_requests_hold_idx    ON referral_requests (user_id, status, hold_until);
CREATE INDEX referral_requests_contact_idx ON referral_requests (contact_id, created_at);

-- ~70 B. Anonymous: no user_id by design.
CREATE TABLE outcomes_daily (
    day          date NOT NULL,
    score_bucket smallint NOT NULL,
    source_id    smallint NOT NULL,
    outcome      smallint NOT NULL,                  -- 0 no_reply, 1 rejected, 2 positive, 3 interview, 4 offer
    count        integer NOT NULL DEFAULT 0,
    PRIMARY KEY (day, score_bucket, source_id, outcome)
);

-- ~300 B
CREATE TABLE form_answers (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    question_hash bigint NOT NULL,                   -- hash of normalized question text
    question_text text NOT NULL,
    answer        text NOT NULL,
    confidence    real NOT NULL DEFAULT 1.0,
    answered_by   smallint NOT NULL DEFAULT 0,       -- 0 user, 1 llm
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, question_hash)
);

-- ~250 B. secret_enc = AES-GCM ciphertext; key lives only in env.
CREATE TABLE credentials (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    host       text NOT NULL,
    username   text NOT NULL,
    secret_enc bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    rotated_at timestamptz,
    UNIQUE (user_id, host)
);

-- ~2.6 KB heap + ~1 KB HNSW. 7 days.
CREATE TABLE recipe_cache (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jd_embedding   halfvec(256) NOT NULL,
    recipe         jsonb NOT NULL,
    vault_revision integer NOT NULL,                 -- stale if vault changed
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX recipe_cache_emb_idx     ON recipe_cache USING hnsw (jd_embedding halfvec_cosine_ops);
CREATE INDEX recipe_cache_created_idx ON recipe_cache (created_at);

-- ~70 B. recent_bits: bit i = 1 if attempt i succeeded (last 20).
CREATE TABLE adapter_health (
    adapter      text PRIMARY KEY,
    recent_bits  integer NOT NULL DEFAULT 0,
    recent_count smallint NOT NULL DEFAULT 0 CHECK (recent_count BETWEEN 0 AND 20),
    status       health_status NOT NULL DEFAULT 'active',
    paused_until timestamptz,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- ~100 B heap + ~60 B idx. 30 days (undo window).
CREATE TABLE mail_actions (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gmail_id  text NOT NULL,
    priority  smallint NOT NULL,                     -- P0..P4
    action    mail_action NOT NULL,
    acted_at  timestamptz NOT NULL DEFAULT now(),
    undone_at timestamptz
);
CREATE INDEX mail_actions_user_idx ON mail_actions (user_id, acted_at);

-- ---------------------------------------------------------------------------
-- Partition helpers. Ingestion calls ensure(...) for today before inserting;
-- the nightly janitor (P1) ensures 7 days ahead and drops expired days.
-- No DEFAULT partition on purpose: rows there could never be dropped cheaply.
-- ---------------------------------------------------------------------------
CREATE FUNCTION jobbot_ensure_daily_partitions(parent text, from_day date, to_day date)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    d date := from_day;
    part text;
    created integer := 0;
BEGIN
    WHILE d <= to_day LOOP
        part := parent || '_p' || to_char(d, 'YYYYMMDD');
        IF to_regclass('public.' || quote_ident(part)) IS NULL THEN
            EXECUTE 'CREATE TABLE public.' || quote_ident(part)
                 || ' PARTITION OF public.' || quote_ident(parent)
                 || ' FOR VALUES FROM (' || quote_literal(d)
                 || ') TO (' || quote_literal(d + 1) || ')';
            created := created + 1;
        END IF;
        d := d + 1;
    END LOOP;
    RETURN created;
END $$;

CREATE FUNCTION jobbot_drop_daily_partitions(parent text, keep_from date)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    r record;
    dropped integer := 0;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = ('public.' || quote_ident(parent))::regclass
    LOOP
        IF r.relname ~ '_p\d{8}$'
           AND to_date(right(r.relname, 8), 'YYYYMMDD') < keep_from THEN
            EXECUTE 'DROP TABLE public.' || quote_ident(r.relname);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END $$;

-- Postgres grants EXECUTE on new functions to PUBLIC; keep these owner-only.
REVOKE EXECUTE ON FUNCTION jobbot_ensure_daily_partitions(text, date, date) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION jobbot_drop_daily_partitions(text, date) FROM PUBLIC;

SELECT jobbot_ensure_daily_partitions('jobs',          current_date - 1, current_date + 7);
SELECT jobbot_ensure_daily_partitions('seen_jobs',     current_date - 1, current_date + 7);
SELECT jobbot_ensure_daily_partitions('jd_signatures', current_date - 1, current_date + 7);

-- Seed sources (section 5).
INSERT INTO sources (key, tier, quality) VALUES
    ('greenhouse','A',0.9), ('lever','A',0.9), ('ashby','A',0.9),
    ('smartrecruiters','A',0.85), ('workday','A',0.85),
    ('jsearch','B',0.5), ('adzuna','B',0.5), ('hn_whos_hiring','B',0.6),
    ('alert_linkedin','C',0.5), ('alert_naukri','C',0.45),
    ('alert_wellfound','C',0.55), ('alert_instahyre','C',0.5),
    ('career_jsonld','D',0.8), ('career_generic','D',0.7);
"""


def _rls_sql() -> str:
    """Owner-only policies. Workers connect as the table owner (bypasses RLS);
    the user-facing API connects as a non-owner role that sets
    `SET LOCAL app.user_id = '<uuid>'` per transaction (app/db.py)."""
    me = "nullif(current_setting('app.user_id', true), '')::uuid"
    parts = [
        "ALTER TABLE users ENABLE ROW LEVEL SECURITY;",
        f"CREATE POLICY users_self ON users USING (id = {me}) WITH CHECK (id = {me});",
    ]
    for t in USER_TABLES:
        parts.append(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;")
        parts.append(
            f"CREATE POLICY {t}_owner ON {t} USING (user_id = {me}) WITH CHECK (user_id = {me});"
        )
    # Supabase exposes `public` through PostgREST to anon/authenticated.
    # This app never uses PostgREST, so lock those roles out entirely.
    parts.append("""
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ' || quote_ident(r);
            EXECUTE 'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ' || quote_ident(r);
            EXECUTE 'REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM ' || quote_ident(r);
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM ' || quote_ident(r);
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM ' || quote_ident(r);
        END IF;
    END LOOP;
END $$;""")
    return "\n".join(parts)


def split_sql(sql: str) -> list[str]:
    """Split a script into single statements, keeping $$ bodies intact.
    One statement per execute: psycopg 3's extended protocol rejects
    multi-statement strings whenever a parameter object is passed."""
    stmts, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        code = line if in_dollar else line.split("--", 1)[0]
        buf.append(line)
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        if not in_dollar and code.rstrip().endswith(";"):
            while buf and (not buf[0].strip() or buf[0].strip().startswith("--")):
                buf.pop(0)  # drop leading comment lines
            if buf:
                stmts.append("\n".join(buf).strip())
            buf = []
    leftover = [l for l in buf if l.strip() and not l.strip().startswith("--")]
    if in_dollar or leftover:
        raise ValueError(f"unterminated SQL near: {leftover[:3]}")
    return stmts


def all_statements() -> list[str]:
    enums = [
        f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)});"
        for name, values in ENUMS.items()
    ]
    return enums + split_sql(SCHEMA_SQL) + split_sql(_rls_sql())


def upgrade() -> None:
    for stmt in all_statements():
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS jobbot_drop_daily_partitions(text, date);")
    op.execute("DROP FUNCTION IF EXISTS jobbot_ensure_daily_partitions(text, date, date);")
    for t in [
        "mail_actions", "adapter_health", "recipe_cache", "credentials",
        "form_answers", "outcomes_daily", "referral_requests",
        "referral_contacts", "responses", "waiting_list", "applications",
        "company_cooldown", "applied_ledger", "jd_signatures", "seen_jobs",
        "jobs", "companies", "sources", "vaults", "users",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE;")
    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name};")
