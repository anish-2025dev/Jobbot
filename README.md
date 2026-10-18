# JobBot

Multi-user job discovery and auto-apply bot on free tiers. The spec is the
source of truth. This README tracks what each phase delivers.

## Status: P0, step 1 of 3 (skeleton + schema)

| Step | Contents | State |
|---|---|---|
| 1 | Repo layout, Docker Compose, Alembic migration for all section-4 tables, hashing utils | **this drop** |
| 2 | Vault Pydantic schema + vault editor API | next |
| 3 | Resume engine (recipe, cache, truth validator, LaTeX, Tectonic, ATS loop, DOCX) | after |

P0 is done when a vault JSON + pasted JD produces a one-page, ATS-clean PDF.

## Layout

```
jobbot/
├── app/
│   ├── config.py          # every spec tunable (caps, retention, thresholds)
│   ├── db.py              # engine, worker_session, user_session (RLS scope)
│   ├── main.py            # FastAPI; /health reports DB size vs budget
│   ├── util/hashing.py    # canonical_hash, fingerprint, company_key, simhash
│   ├── vault/             # step 2
│   └── resume/            # step 3
├── migrations/versions/0001_initial_schema.py
├── scripts/size_report.sql  # real bytes/row per table
├── tests/test_hashing.py
├── docker-compose.yml   Dockerfile   alembic.ini   .env.example
```

Later phases add `app/ingest`, `app/scoring`, `app/apply`, `app/mail`,
`app/janitor` and `app/notify`.

## Quickstart

```bash
cp .env.example .env              # fill ENCRYPTION_KEY
docker compose up -d db redis
docker compose run --rm api alembic upgrade head
docker compose run --rm api pytest -q
docker compose up -d api && curl localhost:8000/health
```

To use Supabase, set `DATABASE_URL` to the direct/session string (port 5432)
and run `alembic upgrade head` from your machine. pgvector is preinstalled
on Supabase, and the migration enables it.

### Tests

```bash
docker compose up -d db
docker compose --profile test run --rm test      # unit + integration
pytest tests/unit -q                             # unit only, no DB needed
```

Integration tests create a throwaway database per session (named by
`TEST_DATABASE_URL`, whose user needs CREATEDB and CREATEROLE). They simulate
Supabase's `anon`/`authenticated` default grants, and roll back after every
test.

| Suite | Covers |
|---|---|
| unit/test_hashing | company keys, req-code stripping (not years), city aliases, cross-portal fingerprints, canonical IDs, SimHash repost detection |
| unit/test_budget | C1 thresholds (210 MB tight, 270 MB pause) |
| unit/test_config | every default matches C1, C5–C8, C10, C13, retention; env overrides |
| unit/test_migration_static | all section-4 tables, one statement per execute, no `%`/bind colons, RLS on every user table, C2/C3 shape, downgrade completeness |
| integration/test_migration | tables, pgvector, seeded sources, partitions, RLS flags, committed migration, downgrade→upgrade round trip |
| integration/test_partitions | routing, missing-partition failure, idempotent ensure, drop + row removal, SimHash Hamming SQL |
| integration/test_constraints | C4 ledger, C5 cooldown CHECKs, C3 summary cap, C7 eviction keeps cap, C8 uniqueness/24h, cascades, halfvec(256), rank order |
| integration/test_security | RLS read/write isolation, anon lockout incl. future partitions, functions not PUBLIC |
| integration/test_health_api | /health ok and pause states |

### Verify the schema by hand

```bash
psql "postgresql://jobbot:jobbot@localhost:5432/jobbot" <<'SQL'
\dt
SELECT jobbot_ensure_daily_partitions('jobs', current_date, current_date + 10);  -- 3 new
SELECT jobbot_drop_daily_partitions('jobs', current_date);                       -- 1 dropped
SQL
psql "postgresql://jobbot:jobbot@localhost:5432/jobbot" -f scripts/size_report.sql
```

## Data model: bytes per row

Each estimate is heap tuple (24 B header + data) plus index entries. The P1
simulated-week test will replace these with measured values from
`size_report.sql`.

| Table | Est. bytes/row | Rows at 1000 apps/day | Peak MB |
|---|---|---|---|
| jobs (7d, daily partitions) | ~1,300 | 3k/day × 7 = 21k | 27 |
| seen_jobs (7d, daily partitions) | ~145 | 20k/day × 7 = 140k | 20 |
| jd_signatures (60d, daily partitions) | ~80 | 3k/day × 60 = 180k | 14 |
| applied_ledger | ~170 | 1k/day, see below | 15 at 90d |
| company_cooldown | ~155 | ≤ distinct companies in 30d | 5 |
| applications (45d) | ~650 avg (recipe kept 7d) | 45k | 30 |
| waiting_list | ~10 KB | 30/user | 0.3 |
| responses | ~1–2 KB | hundreds | <1 |
| referral_contacts / requests | ~200 / ~300 | hundreds | <1 |
| outcomes_daily | ~70 | ~50/day | 1.3/yr |
| form_answers / credentials | ~300 / ~250 | hundreds | <1 |
| recipe_cache (7d) | ~3,600 | ~500/day × 7 | 13 |
| adapter_health / sources | ~70 / ~200 | tens | 0 |
| mail_actions (30d) | ~160 | 3k/day × 30 | 14 |
| companies | ~450 | 1,000 | 0.5 |
| users + vaults | ~20 KB compressed | per user | <1 |
| **Total** | | | **~145 MB** (48% of budget) |

This assumes about 3× as many ranked jobs as applications and roughly 20k
processed jobs/day for one user.

## Deviations from the spec

1. **jobs and seen_jobs are per-user.** Scores and thresholds depend on the
   user's vault, so a job row is really "this job for this user". Fetching
   stays shared; scoring writes per-user rows. This also makes RLS trivial.
2. **Repost detection uses SimHash instead of a stored embedding.** Keeping a
   256-dim embedding for 60 days costs about 600 B/row, which is about 108 MB
   at this volume. A 64-bit SimHash costs about 80 B/row. Reposts reuse the
   JD text almost verbatim, so `hamming <= 6` catches them. This corrects the
   36 MB I estimated earlier, which assumed far fewer processed jobs.
3. **Ledger retention.** C4 says "forever", but the table says 90 days. The
   resolution is to keep every row for at least 90 days. After that, the
   janitor (P1) deletes a row only once the ATS confirms the posting is
   closed, because a closed opening can't be applied to again. This satisfies
   C4 without unbounded growth (permanent rows would add ~60 MB/year at
   1000/day).
4. **company_cooldown stores the last ≤3 application timestamps** instead of
   a counter. That gives an exact rolling 30-day window for C5, at about
   155 B/row instead of 40. A CHECK constraint enforces
   `applied + reserved <= 3` at the database level.
5. **The resume recipe lives on the application row for 7 days.** After
   that it's kept only if the application turned positive, so the submitted
   resume can be re-rendered for interview prep.
6. **Supabase hardening.** The migration revokes all `public` privileges
   from `anon` and `authenticated`. PostgREST exposes that schema, and this
   app never uses it.

## Security notes

- RLS policies exist on every user table and scope rows via `app.user_id`.
  Workers connect as the table owner and bypass RLS. The user-facing API will
  connect as a non-owner role in P5, which is when multi-user auth lands.
- No DEFAULT partitions. Ingestion calls `jobbot_ensure_daily_partitions` for
  today before inserting, and the nightly janitor creates 7 days ahead.
