# JobBot

A multi-user job discovery and auto-apply bot that runs on free tiers. It finds
jobs that match your skills and ranks them by chance of success, then salary. For
each job it builds a truthful one-page resume and applies wherever that's safe. For
your best jobs it first asks people at the company for a referral. Your own
time goes only to the jobs that need a human and to the replies that worked.

Everything in the spec (P0–P6) is implemented and covered by 476 tests against a
real Postgres 16 + pgvector, Redis and headless Chromium.

- [Quickstart](#quickstart)
- [First run](#first-run)
- [Deploying on free tiers](#deploying-on-free-tiers)
- [How it works](#how-it-works)
- [Hard constraints and where they're enforced](#hard-constraints-and-where-theyre-enforced)
- [Storage: measured bytes per row](#storage-measured-bytes-per-row)
- [Tests](#tests)
- [Decisions that differ from the spec](#decisions-that-differ-from-the-spec)
- [Known limits](#known-limits)

## Quickstart

Needs Docker with Compose.

```bash
cp .env.example .env
# fill ENCRYPTION_KEY, SESSION_SECRET and ADMIN_API_KEY (commands are in the file)
docker compose up -d --build
```

This starts Postgres + pgvector, Redis, the migrations (one-shot), the API and
dashboard, the scheduler and one apply worker. Open **http://localhost:8000**.

Telegram (once `TELEGRAM_BOT_TOKEN` is set): `docker compose --profile telegram up -d`.

Without Docker (Python 3.12, Postgres 16 with pgvector, Redis):

```bash
pip install -r requirements.txt && python -m playwright install chromium
alembic upgrade head
python -m app.cli api            # dashboard + API on :8000
python -m app.cli scheduler      # ingestion, mail, outreach, janitor
python -m app.cli apply-worker   # browser workers
python -m app.cli telegram       # optional
```

Resumes need one of: Tectonic (used in Docker), XeLaTeX, pdfLaTeX or Typst.

## First run

1. **Create your account** in the dashboard. The first account becomes the admin.
2. **Fill your profile** (Profile tab), one section at a time. Required: Basics, Target
   roles, Education, Projects, Skills, Job preferences. You can import your existing resume
   (Import / JSON) for a first draft, then review every section. Use real numbers in your
   bullets: a metric that isn't in your profile can never appear on a resume. See
   [Profile and resumes](#profile-and-resumes).
3. **Connect mail** (Settings). The bot needs it to confirm applications, catch replies,
   clean up and send referral emails.
   - *Gmail (recommended):* in Google Cloud, create a project, enable the Gmail API, and
     create an OAuth client of type *Web application* with the redirect URI
     `{PUBLIC_BASE_URL}/oauth/google/callback`. On the consent screen add the scopes
     `gmail.modify` and `gmail.send`, then **publish the app "In production"**. Unverified
     is fine for personal use, and it stops refresh tokens from expiring after 7 days.
     Paste the client ID and secret, then click *Connect Gmail*.
   - *IMAP fallback:* your address plus a Google app password.
4. **Link Telegram** (Settings, then *Get link code*) for instant alerts and one-tap approvals.
5. **Add companies to poll.** `data/companies.csv` is a verified list of employers with
   open tech roles, India and fresher hiring first (see [Job sources](#job-sources)):
   ```bash
   docker compose exec api python -m app.cli seed-companies data/companies.csv
   ```
   The registry also grows by itself whenever a feed or alert links to a company board.
   Optionally add free keys for JSearch, Adzuna, Jooble and Findwork in `.env` for more Tier B.
6. **Use the ranked list for a week** (the lean-first mode). Auto-apply is off by default:
   open *Ranked jobs*, download the tailored resume, apply yourself, then press *I applied*.
   The same gate (C4–C6) and bookkeeping apply as for the bot.
7. **Turn on auto-apply**, and optionally **referral outreach** after adding Hunter, Apollo
   or Snov.io keys. Outreach drafts need your approval for the first 7 days.

Useful commands: `python -m app.cli run <job>` runs one scheduler job now (`ingest`, `mail`,
`outreach`, `hourly`, `digest`, `nightly`, `weekly`). `python -m app.cli resume --vault v.json
--jd jd.txt --title "SDE I" --company Acme --out r.pdf --docx` builds a resume with no database.

## Profile and resumes

One profile, many resumes: every job gets a resume built for it at apply time.

- **Fixed facts** (name, contact, education, links) are the same on every resume.
- **Target roles** (up to 10, e.g. *Software Development Engineer*, *Data Analyst*): each has
  the job titles it covers, its own 2-line summary, the skills to list first and the projects
  to prefer. A job's title picks the role (title keywords, then role family, then text
  similarity).
- **Pools** the resume picks from: up to 10 projects, up to 15 internships/jobs, and a skill
  pool of up to 200. Each resume lists about 28 skills: the job's must-haves, then its
  nice-to-haves, the role's priority skills, then the tech of the chosen projects.
- **Layout** (single column, ATS-safe, one page): name and linked contact line, Summary,
  Experience, Technical Skills, Projects (3–4), Education, Certifications, Achievements.
  Class X/XII appear only if you tick *show on resume*.
- **Application profile**: everything forms ask that never goes on a resume: name parts,
  date of birth, addresses with PIN, parents' names, languages, Class X/XII board and score,
  backlogs, gap years, bond / night shifts / laptop answers, and self-identification
  (declined unless you choose to share it). Missing answers send that form to you
  (Assisted mode) instead of the bot guessing.
- **ID documents** (PAN, Aadhaar, passport, driving licence, voter ID, UAN) are stored
  encrypted (AES-GCM, per user), shown back only masked, never put in a resume, a Ready Pack
  or an LLM prompt, and typed only into forms that ask for them.

The same resume PDF is used for the application upload and attached to referral emails.

## Job sources

40 sources in four tiers (`sources` table; each has its own rate limit and health status):

| Tier | Sources | How |
|---|---|---|
| A: company boards (10) | Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Recruitee, Rippling, Breezy, Oracle Recruiting Cloud, Workable (links) | Public keyless APIs, polled per company every 2 h |
| B: search and board APIs (12) | JSearch (LinkedIn/Indeed/Glassdoor listings), Adzuna India, Jooble, Findwork, HN Who's Hiring, Remotive, RemoteOK, Arbeitnow, Jobicy, Himalayas, The Muse, Working Nomads | Keyed ones per (role, city); keyless boards every 4–6 h as they ask |
| C: alert emails (16) | LinkedIn, Naukri, Indeed, Glassdoor, foundit, Wellfound, Instahyre, Internshala, Cutshort, Hirist, Unstop, Shine, TimesJobs, iimjobs, apna, YC Work at a Startup | Parsed from the alerts you already get; manual apply only (C12) |
| D: career pages (2) | schema.org JSON-LD, LLM extraction | Any other careers URL |

Turn on job alerts on the Tier C sites with the mailbox you connect; the bot reads them,
ranks the jobs with everything else, and trashes the alert mail after parsing. Mail from the
same portals about a shortlist, test or interview is never treated as an alert.

**`data/companies.csv`** lists employers whose boards were checked against the live APIs on
the date in its `verified` column, with their open, tech, India and entry-level counts.
`segment` orders them: `india-fresher` (entry-level tech roles in India), `india`,
`global-fresher` (intern/new-grad roles, often remote), `global`. Seeded companies are pinned
in the registry; boards that disappear are re-detected weekly. Regenerate or extend it with
the scripts in `scripts/companies/`.

## Deploying on free tiers

| Piece | Where | Notes |
|---|---|---|
| Postgres | Supabase free (500 MB) | Use the **direct/session** string (port 5432). pgvector is preinstalled; migration 0001 enables it. |
| Workers, Redis, Chromium | One always-on VM (Oracle Cloud free Ampere ARM recommended: up to 4 cores / 24 GB; the free AMD VM has 1 GB, enough for `APPLY_WORKERS=1`) or your own PC | `docker compose up -d` without the `db` service. The image builds on x86_64 and ARM64. |
| Telegram | Bot API | Long polling, so no public URL is needed. |
| Dashboard | Same VM | Put it behind HTTPS (Caddy or Cloudflare Tunnel) before inviting others, and set `PUBLIC_BASE_URL`. |

On Supabase, run `alembic upgrade head` once from your machine, then remove `db`
and `migrate` from the compose file on the VM.

## How it works

```
 Tier A ATS APIs ─┐                  ┌─ dedupe: canonical id + fingerprint + SimHash repost check
 Tier B JSearch,  ├─ fetch ─ normalize ─ hard filters ─ score (6 weighted parts) ─ jobs (7 days)
 Adzuna, HN       │   (shared)            (per user)                                 │
 Tier C alerts ───┤                                                                  ▼
 Tier D JSON-LD ──┘                                     ranked list ── router ─┬─ referral queue (email 2–3 people, hold 24 h)
                                                                               ├─ apply queue ─ gate ─ adapter ─ submit
                                                                               └─ waiting list + Ready Pack (you, ~1 min)
 Gmail every 15 min ─ match to own activity ─ classify ─ P0–P4 actions (undo 30 d) ─ Telegram alerts
```

| Area | Code |
|---|---|
| Vault (single source of resume facts) | `app/vault/` |
| Resume engine: selection, LLM tailoring, truth validator, LaTeX/Typst, ATS check, DOCX | `app/resume/` |
| Fetchers (10 ATS boards, 12 search/board APIs, JSON-LD, LLM fallback) | `app/ingest/sources/` |
| Normalize, score, registry + eviction, pipeline | `app/ingest/` |
| Gate, filler, adapters (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Workday, generic), waiting list | `app/apply/` |
| Mail: Gmail/IMAP, matching, classification, cleanup, OTP, Tier C parsing | `app/mail/` |
| Referral outreach: providers, contact choice, compose, schedule, queue | `app/outreach/` |
| Telegram, digest | `app/notify/` |
| Auth, dashboard API, onboarding, webhooks | `app/auth.py`, `app/api/` |
| Dashboard UI (Preact + htm, no build step) | `app/web/` |
| Scheduler, CLI, janitor, weight tuning | `app/scheduler.py`, `app/cli.py`, `app/janitor.py`, `app/tuning.py` |

An LLM is optional everywhere. With `LLM_PROVIDER=none` you get a deterministic
resume selection, template cover letters and emails, and rule-based mail
classification. With an LLM, its output goes through the same truth checks.

## Hard constraints and where they're enforced

| | Rule | Enforcement |
|---|---|---|
| C1 | 300 MB budget | Hourly size check: 70% shortens jobs to 3 days, 90% pauses ingestion and alerts admins (`janitor.py`, `ingest/runner.py`) |
| C2 | No files in DB | Recipes only; PDFs are rendered on demand in temp dirs that are deleted; screenshots are skipped |
| C3 | No full JDs | JD text lives in memory during scoring; the stored summary is capped at 600 chars by a CHECK constraint |
| C4 | One application per opening, forever | `applied_ledger` (canonical id + fingerprint), re-checked under an advisory lock in `apply/gate.py`. A submit with no confirmation keeps the claim, so an application is never sent twice. |
| C5 | 3 per company per 30 days | `company_cooldown` holds the last ≤3 timestamps plus referral reservations; a DB CHECK enforces `applied + reserved ≤ 3` |
| C6 | 3-hour gap | Gate requeues with `not_before = last + 3 h`; the waiting list shows "ready at" |
| C7 | 1000-company registry | Decayed score `S·e^(−Δt/30d)+1`; protected companies are never evicted; eviction never touches `company_cooldown` |
| C8 | Waiting list 30 / 24 h | Rank-based displacement under a per-user lock; pin adds 24 h; the referral queue is separate |
| C9 | Sources vs companies | Rate limits keyed by source/domain; caps keyed by `company_key` (legal suffixes and country subsidiaries folded) |
| C10 | Quality gate | Auto-apply only at score ≥ MIN_SCORE (per-user override); Stretch jobs appear in the list and digest only |
| C11 | Truthful resumes | `resume/validator.py`: a rephrased bullet may only use numbers from its own source bullet, and only skills from that bullet or its project's tech; cover letters, answers and emails lose any untraceable sentence |
| C12 | Platform safety | LinkedIn, Naukri, Indeed etc. are never automated (waiting list only); CAPTCHAs go to Assisted mode; no evasion |
| C13 | Mail priority | Offers are never auto-trashed; P1/P2 kept ≥ 7 days and until done; only mail matched to the bot's own activity is touched |
| C14 | Outreach caps | 10 jobs/day, 3 contacts/job, 30 emails/day, 90-day per-person cooldown, 3 contacts/company/30 days, hashed do-not-contact list; re-checked right before each send |
| C15 | 24 h hold | `apply_at = last send + 24 h`; a referral before then applies immediately via the link or referrer field; a late referral triggers a ready-to-send reply |

Section 13 extras: the bounce rate (>3%) or a spam complaint pauses outreach for 7 days.
Secrets are AES-GCM encrypted, bound to user and purpose. API requests run under a
non-owner Postgres role with row-level security. The SMS webhook needs a per-user token.

## Storage: measured bytes per row

From `python -m scripts.simulate --companies 1000 --days 7`: 1000 polled companies,
about 11,700 postings a day with 15% daily churn, and 1000 applications a day
filled at spec volume. Sizes include indexes. **Total after 7 days: 43 MB (14%
of budget).**

| Table | Rows after 7 days | Bytes/row (measured) | Retention |
|---|---|---|---|
| jobs | 10,543 | 1,148 | 7 days (3 when tight), daily partitions |
| applications | 7,000 | 1,504 (recipe kept 7 days) | rejected 7 days, no reply 45 days |
| seen_jobs | 19,027 | 212 | 7 days, daily partitions |
| jd_signatures | 17,623 | 146 | 60 days, daily partitions |
| applied_ledger | 7,000 | 190 | ≥ 90 days, then only once the posting is gone |
| company_cooldown | 7,000 | 146 | 30 days after last application |
| mail_actions | 2,338 | 185 | 30 days |
| companies | 1,000 | ~500 heap (2 KB incl. fixed index overhead at this size) | eviction at MAX_COMPANIES |

Estimates for tables the simulation doesn't fill at volume: `waiting_list` ~10 KB (30 per user),
`responses` 1–2 KB, `outreach_contacts` ~360 B, `referral_queue` ~600 B + 400 B per contact,
`recipe_cache` ~3.6 KB, `training_examples` ~70 B, `do_not_contact` ~80 B, `provider_usage` ~70 B.
Linear extrapolation to the 90-day ledger and 45-day applications gives roughly
110–130 MB peak at 1000 applications a day.

Check a live database with `psql "$DATABASE_URL" -f scripts/size_report.sql`.

## Tests

```bash
docker compose --profile test run --rm test
# or locally:
TEST_DATABASE_URL=postgresql+psycopg://user@localhost:5432/postgres REDIS_URL=redis://localhost:6379/15 pytest -q
```

Integration tests create a throwaway database per module (the user needs CREATEDB and
CREATEROLE). Without `TEST_DATABASE_URL`, only unit tests run. What the suites prove:

- **P0:** a vault plus a JD gives a one-page ATS-clean PDF on every installed engine. The trim loop converges. LaTeX injection is inert.
- **P1:** a simulated week with ranked jobs stays under budget. Dedupe works across portals and reposts.
- **P2:** alerts arrive, rejections and low-value mail clean themselves up, positives stay 7+ days, unmatched mail is untouched, and undo works.
- **P3:** end-to-end auto-apply in Chromium on local Greenhouse and Lever forms, with C4/C5/C6, CAPTCHA, unknown questions, DOCX fallback and adapter pausing.
- **P4:** the full outreach cycle (contacts, approval, send at 9–11 am, reply, apply at T+24h), plus early and late referrals, caps, exhausted providers and the bounce guard.
- **P5:** Ashby, SmartRecruiters (shadow DOM), Workable, Workday (account creation, email verification, multi-step, custom listboxes) and the generic adapter.
- **P6:** signup/login, token revocation, cross-user isolation enforced by RLS, the dashboard API and UI in a browser, onboarding import, and weight tuning.

## Decisions that differ from the spec

1. **Referral outreach replaces the older manual-ask referral tables** (migration 0002), following
   the updated spec. `outreach_log` and `company_stars` were added: the 30/day email cap and the bounce
   rate need a send log, and the referral flag needs per-user starred companies.
2. **Repost detection uses a 64-bit SimHash** instead of a stored JD embedding: about 146 B/row
   instead of ~600 B over 60 days. Signatures are global but keyed by canonical id, so two users can
   share an opening without it looking like a repost.
3. **The ledger is kept past 90 days while the posting is still open**, so C4 ("forever") holds without unbounded growth.
4. **Lean mode builds resumes from the stored summary and skills**, because the full JD was discarded (C3).
   Jobs routed for auto-apply get their recipe built during ingestion, while the full JD is still in memory.
5. **Weight tuning uses `training_examples`** (per user, 180 days, ~70 B/row), because `outcomes_daily` is
   anonymous by design. Learned weights are shrunk toward the defaults until about 60 outcomes.
6. **The dashboard uses Preact + htm** (React-compatible API, no build step) instead of React, so the VM needs no Node toolchain.
7. **Screenshots are skipped** (C2 allows "or skipped").
8. **Embeddings default to deterministic feature hashing** (no model download, reproducible cache hits).
   Set `EMBEDDING_PROVIDER=ollama` for semantic embeddings.

## Known limits

- **Live third-party APIs were not reachable from the build environment.** Every ATS board,
  JSearch, Adzuna, Gmail, Telegram, Apollo, Hunter, Snov.io and the LLM providers were tested
  against fixtures built from their documented formats. Watch the first live run with
  `python -m app.cli run ingest` and `run mail`; a field name may differ.
- **Real ATS forms vary by company** (custom questions, multi-page Greenhouse variants, Workday tenant
  differences). Anything the filler can't do confidently goes to Assisted mode instead of guessing.
  Adapter health pauses an adapter that starts failing.
- **Email-finder free tiers change often.** Set a monthly credit limit per provider in Settings. When
  every provider is out of credits, outreach skips straight to applying.
- **Tectonic downloads its TeX bundle once, at image build.** If that build step fails, set
  `RESUME_ENGINE=typst`; the Typst template is tested too.
