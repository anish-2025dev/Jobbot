# Review guide

Notes for human and AI reviewers (Claude `/code-review`, Claude Security, CI scanners).
The README's "Hard constraints" table lists every rule C1–C15 and where it's enforced.
This file says where bugs would hurt most and which choices are deliberate.

## Highest-risk areas: review these first

1. **Never apply twice (C4), `app/apply/gate.py`.**
   `claim()` checks `applied_ledger` by canonical id and fingerprint, then writes the
   ledger and `company_cooldown` under an advisory lock keyed by user and company. `finalize`, `release`
   and `mark_unconfirmed` must keep this invariant: once a submit may have reached the
   employer, the claim is never released. Look for any race or exception path that
   releases a claim after `page.click(submit)`.
2. **Company caps (C5/C6).** The `applied + reserved ≤ 3` CHECK in migration 0001/0002.
   Referral reservations (`app/outreach/queue.py`) must convert or expire, never leak.
3. **Truth validator (C11), `app/resume/validator.py`.**
   - A rephrased bullet may contain only numbers from its own source bullet ("5 million",
     lakh and crore are canonicalized).
   - It may contain only skills from that bullet or its owner's tech.
   - Years-of-experience claims are checked against the vault.
   - Cover letters, form answers and outreach emails drop any untraceable sentence.

   Any way to get a fabricated number or skill past it is a critical bug.
4. **Tenant isolation, `app/db.py` `user_session` plus migration 0003.**
   - API requests run with `SET LOCAL ROLE jobbot_api` and `app.user_id`, so RLS applies.
   - Check every router in `app/api/`, `app/vault/api.py` and `app/resume/api.py` for queries
     that use `worker_session` (owner role, bypasses RLS) on behalf of a user. A few
     do so on purpose (gate checks, account bootstrap in `account.py` and `dashboard.py`)
     and must filter by the authenticated `user_id` explicitly.
5. **Auth, `app/auth.py`.**
   - Passwords are hashed with scrypt.
   - Tokens are HMAC-signed and carry a fingerprint of the password hash, so a password
     change revokes them.
   - OAuth state is signed. The SMS webhook (`app/api/webhooks.py`) needs a per-user token.
6. **Outreach caps (C14/C15), `app/outreach/`.**
   - Caps: 10 jobs/day, 3 contacts/job, 30 emails/day, 90-day per-person cooldown,
     3 contacts per company per 30 days, and a hashed do-not-contact list.
   - Caps are re-checked right before each send, not only when the send is scheduled.
   - A bounce rate above 3% or a spam complaint pauses outreach for 7 days.
   - The 24h hold is `apply_at = last send + 24h`.
7. **Mail actions (C13), `app/mail/processor.py` and `classify.py`.**
   - Offers are never trashed.
   - Weak rejection signals are classified "unclear" and left alone.
   - Only mail matched to the bot's own applications is touched. Undo must work.
8. **Secrets, `app/crypto.py`.** AES-GCM, with associated data bound to user and purpose.
   No secrets in the repo; `.env` is git-ignored.

## Platform safety (C12): deliberate non-features

- LinkedIn, Naukri, Indeed and similar sites are **never automated**. Their jobs go to the
  waiting list for manual applying. A change that automates them should be rejected.
- There is **no CAPTCHA solving and no anti-bot evasion** (no stealth plugins, no
  fingerprint spoofing, no proxy rotation). A CAPTCHA moves the application to Assisted mode.
- Caps and human-like delays in `app/apply/timing.py` are never relaxed for throughput.

## Intentional design choices (not bugs)

- Storage limits (C1–C3):
  - The full JD is never stored; the summary is capped at 600 characters by a CHECK.
  - PDFs are rendered on demand in temp directories.
  - Screenshots are skipped.
- `jobs`, `seen_jobs` and `jd_signatures` use daily partitions. They are created and
  dropped by `jobbot_ensure_daily_partitions` and `jobbot_drop_daily_partitions`.
- Repost detection uses a 64-bit SimHash, not embeddings, to save storage.
- Migrations are hand-written SQL so partitions, RLS and CHECKs are explicit.
- The dashboard is Preact + htm, with vendored ES modules and no build step.
  htm requires self-closed `<input />`.
- With `LLM_PROVIDER=none`, everything still runs on deterministic fallbacks.
- `UnlimitedBuckets` exists only for tests; production uses the Redis or memory rate limiter.

## Known limits

- Live third-party APIs (ATS boards, JSearch, Adzuna, Gmail, Telegram, email finders,
  LLM providers) were tested only against fixtures built from their documented formats.
  Field-name drift is the most likely real-world failure.
- Real ATS forms vary. Anything the filler can't answer confidently goes to Assisted
  mode instead of guessing.

## How to test

```bash
# unit + integration (needs Postgres 16 + pgvector, Redis)
TEST_DATABASE_URL=postgresql+psycopg://jobbot:jobbot@localhost:5432/postgres \
REDIS_URL=redis://localhost:6379/15 pytest -q

# live dry run against a real posting: fills the form in a visible browser, never submits
python -m app.cli try-apply <application_id>
python -m app.cli try-apply <application_id> --submit   # only when you mean it
```

CI runs on every PR. The pytest and ruff (real-bug rules) jobs block the merge. Bandit,
pip-audit, Semgrep, Gitleaks and Trivy report findings but don't block.
