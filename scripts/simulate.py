"""Simulated week (P1 done-test) and measured bytes/row.

Runs the real pipeline (registry -> fetch -> normalize -> dedupe -> filter ->
score -> store -> nightly janitor) against a fake universe of N companies
for D days with daily posting churn, then reports DB size per table and
measured bytes per row. Application-side tables (ledger, cooldown,
applications, outreach...) are filled synthetically at the spec's volume
(1000 applications/day) so their sizes are measured too.

    DATABASE_URL=... python -m scripts.simulate --companies 1000 --days 7

Needs a freshly migrated, otherwise empty database.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

ATS_MIX = ["greenhouse", "greenhouse", "lever", "lever", "ashby", "smartrecruiters", "workday"]


def table_report(conn) -> list[dict]:
    rows = conn.execute(text("""
        WITH t AS (
          SELECT coalesce(p.relname, c.relname) AS tbl,
                 sum(pg_total_relation_size(c.oid)) AS bytes,
                 sum(c.reltuples)::bigint AS est_rows
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
          LEFT JOIN pg_inherits i ON i.inhrelid = c.oid
          LEFT JOIN pg_class p ON p.oid = i.inhparent
          WHERE c.relkind = 'r'
          GROUP BY 1)
        SELECT tbl, bytes, est_rows FROM t ORDER BY bytes DESC""")).all()
    out = []
    for r in rows:
        exact = conn.execute(text(f'SELECT count(*) FROM "{r.tbl}"')).scalar()
        out.append({"table": r.tbl, "bytes": int(r.bytes), "rows": exact,
                    "bytes_per_row": int(r.bytes / exact) if exact else None})
    return out


def synth_application_side(conn, user_ids, day: date, per_day: int, rnd: random.Random) -> None:
    """Spec-volume rows for the tables P3/P4 fill (ledger, cooldown,
    applications, outreach), so their bytes/row are measured, not guessed."""
    for i in range(per_day):
        uid = rnd.choice(user_ids)
        canon = rnd.getrandbits(63)
        ck = rnd.getrandbits(62)
        applied = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=i)
        conn.execute(text("""INSERT INTO applied_ledger (user_id, canonical_hash, fingerprint, company_key_hash, applied_at)
                             VALUES (:u, :c, :f, :k, :t) ON CONFLICT DO NOTHING"""),
                     {"u": uid, "c": canon, "f": rnd.getrandbits(63), "k": ck, "t": applied})
        conn.execute(text("""INSERT INTO company_cooldown (user_id, company_key_hash, applied_ats, last_applied_at)
                             VALUES (:u, :k, ARRAY[CAST(:t AS timestamptz)], :t) ON CONFLICT DO NOTHING"""),
                     {"u": uid, "k": ck, "t": applied})
        conn.execute(text("""
            INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title,
                apply_url, score, bucket, salary_max, status, mode, applied_at, components, recipe, ats, req_id, location)
            VALUES (:u, :c, :f, :k, :cn, :t, :url, 0.74, 2, 1400000, 'applied', 'auto', :at, :comp,
                    CAST(:recipe AS jsonb), 'greenhouse', :req, 'Bengaluru, India')
            ON CONFLICT DO NOTHING"""),
            {"u": uid, "c": canon, "f": rnd.getrandbits(63), "k": ck, "cn": f"Company {ck % 10000}",
             "t": "Software Development Engineer I", "url": f"https://boards.greenhouse.io/c{ck % 10000}/jobs/{canon % 10**8}",
             "at": applied, "comp": [800, 1000, 900, 900, 300, 500], "req": f"R{canon % 10**6}",
             # recipes are kept 7 days after applying (nightly trims older ones)
             "recipe": json.dumps(SAMPLE_RECIPE) if (date.today() - day).days < 7 else None})
        if i % 3 == 0:
            conn.execute(text("""INSERT INTO mail_actions (user_id, gmail_id, priority, action, acted_at)
                                 VALUES (:u, :g, 4, 'trash', :t)"""), {"u": uid, "g": f"18c{canon:x}"[:16], "t": applied})
        if i % 100 == 0:   # outreach: <= 30 emails/day per user (C14)
            conn.execute(text("""INSERT INTO outreach_log (user_id, recipient_hash, company_key_hash, sent_at, gmail_message_id)
                                 VALUES (:u, :r, :k, :t, :g)"""),
                         {"u": uid, "r": rnd.getrandbits(63), "k": ck, "t": applied, "g": f"18d{canon:x}"[:16]})


SAMPLE_RECIPE = None


def run(companies: int, days: int, jobs_per_company: int, users: int, churn: float,
        apps_per_day: int, seed: int = 5) -> dict:
    global SAMPLE_RECIPE
    from app import db, janitor
    from app.ingest import registry
    from app.ingest.http import make_fetcher
    from app.ingest.runner import run_round
    from app.ratelimit import RateLimiter, UnlimitedBuckets
    from tests.fakes.ats import FakeUniverse

    root = Path(__file__).resolve().parents[1]
    vault = json.loads((root / "examples/vault.example.json").read_text())
    SAMPLE_RECIPE = {"v": 1, "vault_revision": 1, "title": "Software Development Engineer I", "company": "X",
                     "experience": [{"id": "exp-acme-intern", "bullets": [{"id": "acme-1", "rank": 1.2}, {"id": "acme-2", "rank": 1.1}]}],
                     "projects": [{"id": p["id"], "bullets": [{"id": b["id"], "rank": 0.7} for b in p["bullets"][:3]]}
                                  for p in vault["projects"][:3]],
                     "skills": [{"category": g["category"], "items": g["items"]} for g in vault["skills"]],
                     "keywords": ["python", "fastapi", "postgresql", "redis", "docker"],
                     "cover_letter": "x" * 700, "answers": {"Expected CTC": "12 LPA", "Notice period": "Immediate"}}
    rnd = random.Random(seed)
    start = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    uni = FakeUniverse(seed=seed, now=start)
    for i in range(companies):
        ats = ATS_MIX[i % len(ATS_MIX)]
        slug = f"co{i:05d}"
        uni.add_company(f"Company {i:05d}", ats, slug, n_jobs=jobs_per_company)
    with db.worker_session() as s:
        for (ats, _key), c in uni.companies.items():
            registry.admit(s, registry.CompanyRef(name=c.name, ats=ats, slug=c.slug, host=c.host))
    user_ids = []
    with db.engine.begin() as c:
        for _ in range(users):
            uid = c.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"),
                            {"e": f"sim{rnd.getrandbits(32)}@test.dev"}).scalar_one()
            c.execute(text("INSERT INTO vaults (user_id, data) VALUES (:u, CAST(:d AS jsonb))"),
                      {"u": uid, "d": json.dumps(vault)})
            user_ids.append(uid)

    f = make_fetcher(uni.transport(), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)
    days_out = []
    for d in range(days):
        now = start + timedelta(days=d)
        today = now.date()
        uni.now = now
        if d:   # churn: a share of every board is replaced by fresh postings
            for c in uni.companies.values():
                k = max(1, int(len(c.jobs) * churn))
                for idx in rnd.sample(range(len(c.jobs)), k):
                    c.jobs[idx] = uni.make_job(age_hours=rnd.uniform(0, 24))
        with db.engine.begin() as c:
            c.execute(text("UPDATE companies SET last_polled_at = NULL"))
        t0 = time.monotonic()
        res = run_round(f, tier_b=False, today=today, now=now)
        with db.engine.begin() as c:
            synth_application_side(c, user_ids, today, apps_per_day, rnd)
        rep = janitor.nightly(today=today, vacuum=False)
        stored = sum(sum(u["stored"].values()) for u in res.per_user.values())
        days_out.append({"day": d + 1, "polled": res.companies_polled, "fetched": res.fetched,
                         "pre_skipped": res.pre_skipped, "normalized": res.unique, "stored": stored,
                         "errors": len(res.errors), "seconds": round(time.monotonic() - t0, 1),
                         "db_mb": round(rep.db_mb, 1), "state": rep.state})
        print(json.dumps(days_out[-1]), flush=True)
    with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("VACUUM ANALYZE"))
        tables = table_report(c)
        total_mb = c.execute(text("SELECT pg_database_size(current_database())")).scalar() / 2**20
    return {"days": days_out, "tables": tables, "db_mb": round(total_mb, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--companies", type=int, default=1000)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--jobs-per-company", type=int, default=20)
    ap.add_argument("--users", type=int, default=1)
    ap.add_argument("--churn", type=float, default=0.15)
    ap.add_argument("--apps-per-day", type=int, default=1000)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    out = run(args.companies, args.days, args.jobs_per_company, args.users, args.churn, args.apps_per_day)
    print(f"\nDB total: {out['db_mb']} MB")
    print(f"{'table':<20}{'rows':>10}{'size':>12}{'B/row':>8}")
    for t in out["tables"]:
        if t["rows"]:
            print(f"{t['table']:<20}{t['rows']:>10}{t['bytes'] / 1024:>10.0f}KB{t['bytes_per_row']:>8}")
    if args.json:
        args.json.write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
