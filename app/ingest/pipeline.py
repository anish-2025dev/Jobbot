"""Per-user processing of normalized jobs (section 6).

dedupe -> hard filters -> score -> store -> route. Fetching and
normalization are shared across users; this part is per user because
scores depend on each user's vault.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingest.normalize import NormalizedJob
from app.ingest.scoring import (BUCKET_NAMES, Scored, UserProfile, hard_filter, referral_worthy,
                                score_job)

log = logging.getLogger(__name__)
Router = Callable[[Session, UserProfile, NormalizedJob, Scored, int], None]


@dataclass
class Stats:
    seen_before: int = 0
    applied_before: int = 0
    reposts: int = 0
    filtered: Counter = field(default_factory=Counter)
    below: int = 0
    stored: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {"seen_before": self.seen_before, "applied_before": self.applied_before,
                "reposts": self.reposts, "filtered": dict(self.filtered), "below_stretch": self.below,
                "stored": {BUCKET_NAMES[k]: v for k, v in self.stored.items()}}


@dataclass
class Context:
    """DB-derived inputs to scoring, loaded once per run."""
    source_ids: dict[str, int]
    source_quality: dict[str, float]
    learned: dict[tuple[str, int], float]           # (source, prelim bucket) -> score 0..1
    company_meta: dict[int, dict]                   # company_key_hash -> registry row subset

    @classmethod
    def load(cls, s: Session) -> "Context":
        src = s.execute(text("SELECT id, key, quality FROM sources")).all()
        rows = s.execute(text("""
            SELECT s.key, o.score_bucket,
                   sum(o.count) FILTER (WHERE o.outcome >= 2) AS pos, sum(o.count) AS n
            FROM outcomes_daily o JOIN sources s ON s.id = o.source_id
            WHERE o.day > current_date - 180 GROUP BY s.key, o.score_bucket""")).all()
        total_pos = sum(r.pos or 0 for r in rows)
        total_n = sum(r.n or 0 for r in rows)
        g = (total_pos + 1) / (total_n + 20) if total_n else 0.05
        learned = {}
        for r in rows:
            rate = ((r.pos or 0) + 10 * g) / ((r.n or 0) + 10)          # shrink toward global rate
            learned[(r.key, r.score_bucket)] = min(1.0, 0.5 * rate / g)  # 0.5 = average
        meta = {r.company_key_hash: dict(r._mapping) for r in s.execute(text(
            "SELECT id, company_key_hash, employee_count, referral_driven, open_matching_jobs FROM companies"))}
        return cls(source_ids={r.key: r.id for r in src}, source_quality={r.key: float(r.quality) for r in src},
                   learned=learned, company_meta=meta)


def ensure_partitions(s: Session, day: date) -> None:
    for parent in ("jobs", "seen_jobs", "jd_signatures"):
        s.execute(text("SELECT jobbot_ensure_daily_partitions(:p, CAST(:d AS date), CAST(:d AS date) + 7)"),
                  {"p": parent, "d": day})


def is_repost(s: Session, job: NormalizedJob, day: date) -> bool:
    cfg = get_settings()
    return s.execute(text("""
        SELECT 1 FROM jd_signatures
        WHERE company_key_hash = :c AND canonical_hash <> :canon
          AND seen_on >= CAST(:d AS date) - :win
          AND bit_count((simhash # :s)::bit(64)) <= :maxd
        LIMIT 1"""), {"c": job.company_key_hash, "canon": job.canonical_hash, "d": day,
                      "win": cfg.repost_window_days, "s": job.simhash, "maxd": cfg.repost_hamming_max}).first() is not None


def record_signature(s: Session, job: NormalizedJob, day: date) -> None:
    exists = s.execute(text("SELECT 1 FROM jd_signatures WHERE canonical_hash = :c LIMIT 1"),
                       {"c": job.canonical_hash}).first()
    if not exists:
        s.execute(text("""INSERT INTO jd_signatures (seen_on, company_key_hash, simhash, canonical_hash)
                          VALUES (:d, :co, :s, :c)"""),
                  {"d": day, "co": job.company_key_hash, "s": job.simhash, "c": job.canonical_hash})


def process_user(s: Session, p: UserProfile, jobs: list[NormalizedJob], ctx: Context, *,
                 day: date | None = None, now: datetime | None = None,
                 router: Router | None = None) -> Stats:
    day = day or date.today()
    now = now or datetime.now(timezone.utc)
    st = Stats()
    uid = p.user_id
    if not jobs:
        return st

    canons = list({j.canonical_hash for j in jobs})
    fps = list({j.fingerprint for j in jobs})
    seen = s.execute(text("""SELECT fingerprint, canonical_hash FROM seen_jobs WHERE user_id = :u
                             AND (canonical_hash = ANY(:c) OR fingerprint = ANY(:f))"""),
                     {"u": uid, "c": canons, "f": fps}).all()
    seen_c = {r.canonical_hash for r in seen}
    seen_f = {r.fingerprint for r in seen}
    applied = s.execute(text("""SELECT canonical_hash, fingerprint FROM applied_ledger WHERE user_id = :u
                                AND (canonical_hash = ANY(:c) OR fingerprint = ANY(:f))"""),
                        {"u": uid, "c": canons, "f": fps}).all()
    applied_c = {r.canonical_hash for r in applied}
    applied_f = {r.fingerprint for r in applied}
    velocity = dict(s.execute(text("""SELECT company_key_hash, count(*) FROM jobs WHERE user_id = :u
                                      GROUP BY company_key_hash"""), {"u": uid}).all())
    batch_counts = Counter(j.company_key_hash for j in jobs if j.families & p.families)

    def mark_seen(j: NormalizedJob) -> None:
        s.execute(text("""INSERT INTO seen_jobs (seen_on, user_id, fingerprint, canonical_hash)
                          VALUES (:d, :u, :f, :c) ON CONFLICT DO NOTHING"""),
                  {"d": day, "u": uid, "f": j.fingerprint, "c": j.canonical_hash})
        seen_c.add(j.canonical_hash)
        seen_f.add(j.fingerprint)

    for j in jobs:
        if j.canonical_hash in applied_c or j.fingerprint in applied_f:   # C4, across portals
            st.applied_before += 1
            continue
        if j.canonical_hash in seen_c or j.fingerprint in seen_f:
            st.seen_before += 1
            continue
        if is_repost(s, j, day):
            st.reposts += 1
            mark_seen(j)
            continue
        # Recorded as each job is processed (not batch-first), so two similar
        # new postings in one batch keep the first and skip only the second.
        record_signature(s, j, day)
        reason = hard_filter(p, j)
        if reason:
            st.filtered[reason] += 1
            mark_seen(j)
            continue
        source = j.raw.source
        prelim = score_job(p, j, source_quality=ctx.source_quality.get(source, 0.5),
                           company_jobs_7d=velocity.get(j.company_key_hash, 0) + batch_counts[j.company_key_hash],
                           learned_rate=None, now=now)
        sc = score_job(p, j, source_quality=ctx.source_quality.get(source, 0.5),
                       company_jobs_7d=velocity.get(j.company_key_hash, 0) + batch_counts[j.company_key_hash],
                       learned_rate=ctx.learned.get((source, max(prelim.bucket, 0))), now=now)
        mark_seen(j)
        if sc.bucket < 0:
            st.below += 1
            continue
        meta = ctx.company_meta.get(j.company_key_hash, {})
        sc.referral_worthy = referral_worthy(
            sc, employee_count=meta.get("employee_count"), starred=j.company_key_hash in p.starred,
            referral_driven=bool(meta.get("referral_driven")),
            open_jobs=max(meta.get("open_matching_jobs") or 0, batch_counts[j.company_key_hash]))
        job_id = s.execute(text("""
            INSERT INTO jobs (ingested_on, user_id, canonical_hash, fingerprint, company_id, company_key_hash,
                company_name, source_id, title, location, remote_type, exp_min, exp_max, grad_years,
                salary_min, salary_max, salary_currency, skills, summary, url, posted_at, score, bucket,
                ats, req_id, referral_worthy, components, missing_skills)
            VALUES (:d, :u, :c, :f, :cid, :ck, :cname, :src, :title, :loc, CAST(:rt AS remote_type), :emin, :emax,
                :gy, :smin, :smax, :cur, :skills, :summary, :url, :posted, :score, :bucket,
                CAST(:ats AS ats_type), :req, :rw, :comp, :missing)
            RETURNING id"""), {
            "d": day, "u": uid, "c": j.canonical_hash, "f": j.fingerprint, "cid": meta.get("id"),
            "ck": j.company_key_hash, "cname": j.company[:200], "src": ctx.source_ids.get(source),
            "title": j.title[:300], "loc": (j.location or "")[:300] or None, "rt": j.remote_type,
            "emin": j.exp_min, "emax": j.exp_max, "gy": j.grad_years or None,
            "smin": j.salary_min, "smax": j.salary_max, "cur": j.salary_currency,
            "skills": j.skills[:40], "summary": j.summary[:600], "url": j.raw.url[:1000],
            "posted": j.posted_at, "score": sc.score, "bucket": sc.bucket,
            "ats": j.raw.ats if j.raw.ats in _ATS_ENUM else "unknown", "req": j.raw.req_id,
            "rw": sc.referral_worthy, "comp": sc.component_ints(), "missing": sc.missing[:20],
        }).scalar_one()
        st.stored[sc.bucket] += 1
        if router is not None and sc.bucket >= 1:
            router(s, p, j, sc, job_id)
    return st


_ATS_ENUM = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "workday", "jsonld", "generic", "unknown"}
