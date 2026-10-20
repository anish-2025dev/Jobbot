"""Company registry (section 5, C7).

Up to MAX_COMPANIES employers polled directly. Eviction score decays with
tau = 30 days and gains +1 per application:  S = S_old * exp(-dt/tau) + 1.
Eviction removes registry metadata only; company_cooldown (the C5 cap)
lives in its own table and is never touched here.
"""
from __future__ import annotations

import csv
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingest.detect import detect
from app.ingest.http import Fetcher, NotFoundError, SourceError
from app.util.hashing import company_key, h64

log = logging.getLogger(__name__)
TAU_SECONDS = 30 * 86400
PROBE_ORDER = ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee", "rippling", "breezy")

# SQL predicate: companies that must never be evicted (section 5, rule 1).
PROTECTED_SQL = """(
    c.pinned
    OR c.added_at > now() - interval '14 days'
    OR EXISTS (SELECT 1 FROM applications a WHERE a.company_key_hash = c.company_key_hash
               AND a.deleted_at IS NULL
               AND a.status IN ('queued', 'applying', 'waiting', 'applied_pending_referral', 'positive'))
    OR EXISTS (SELECT 1 FROM responses r JOIN applications a ON a.id = r.application_id
               WHERE a.company_key_hash = c.company_key_hash AND r.deleted_at IS NULL)
    OR EXISTS (SELECT 1 FROM outreach_contacts oc WHERE oc.company_key_hash = c.company_key_hash)
    OR EXISTS (SELECT 1 FROM referral_queue q WHERE q.company_key_hash = c.company_key_hash
               AND q.status NOT IN ('applied', 'closed', 'skipped'))
)"""
CURRENT_SCORE_SQL = "c.eviction_score * exp(-extract(epoch FROM now() - c.score_updated_at) / 2592000.0)"


def decayed(score: float, updated_at: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return score * math.exp(-max(0.0, (now - updated_at).total_seconds()) / TAU_SECONDS)


@dataclass
class CompanyRef:
    name: str
    ats: str = "unknown"
    slug: str | None = None
    host: str | None = None
    careers_url: str | None = None
    email_domain: str | None = None
    open_jobs: int = 0


def find(s: Session, name: str) -> dict | None:
    row = s.execute(text("SELECT * FROM companies WHERE company_key_hash = :h"),
                    {"h": h64(company_key(name))}).mappings().first()
    return dict(row) if row else None


def count(s: Session) -> int:
    return s.execute(text("SELECT count(*) FROM companies")).scalar_one()


def weakest_evictable(s: Session) -> tuple[int, float] | None:
    row = s.execute(text(f"""
        SELECT c.id, {CURRENT_SCORE_SQL} AS cur FROM companies c
        WHERE NOT {PROTECTED_SQL}
        ORDER BY cur ASC, c.last_applied_at ASC NULLS FIRST, c.id ASC
        LIMIT 1""")).first()
    return (row.id, float(row.cur)) if row else None


def admit(s: Session, ref: CompanyRef, max_companies: int | None = None) -> int | None:
    """Insert or update a company. When the registry is full, a newcomer is
    admitted only if its matching open jobs beat the weakest evictable
    current score (section 5, rule 3); that company is evicted."""
    max_companies = max_companies or get_settings().max_companies
    key = company_key(ref.name)
    if not key:
        return None
    existing = find(s, ref.name)
    if existing:
        s.execute(text("""
            UPDATE companies SET
                ats_type = CASE WHEN CAST(:ats AS ats_type) <> 'unknown' THEN CAST(:ats AS ats_type) ELSE ats_type END,
                ats_slug = COALESCE(:slug, ats_slug), ats_host = COALESCE(:host, ats_host),
                careers_url = COALESCE(:url, careers_url), email_domain = COALESCE(:dom, email_domain),
                open_matching_jobs = GREATEST(open_matching_jobs, :open)
            WHERE id = :id"""), {"ats": ref.ats, "slug": ref.slug, "host": ref.host, "url": ref.careers_url,
                                 "dom": ref.email_domain, "open": ref.open_jobs, "id": existing["id"]})
        return existing["id"]
    # serialize admissions so two workers can't both take the last slot
    s.execute(text("SELECT pg_advisory_xact_lock(hashtext('jobbot:registry-admit'))"))
    if count(s) >= max_companies:
        weakest = weakest_evictable(s)
        if weakest is None or ref.open_jobs <= weakest[1]:
            return None
        s.execute(text("DELETE FROM companies WHERE id = :id"), {"id": weakest[0]})
        log.info("registry: evicted company %s (score %.2f) for %s", weakest[0], weakest[1], ref.name)
    return s.execute(text("""
        INSERT INTO companies (company_key, company_key_hash, name, ats_type, ats_slug, ats_host,
                               careers_url, email_domain, open_matching_jobs)
        VALUES (:key, :h, :name, CAST(:ats AS ats_type), :slug, :host, :url, :dom, :open)
        ON CONFLICT (company_key_hash) DO UPDATE SET name = EXCLUDED.name
        RETURNING id"""), {"key": key, "h": h64(key), "name": ref.name.strip(), "ats": ref.ats,
                           "slug": ref.slug, "host": ref.host, "url": ref.careers_url,
                           "dom": ref.email_domain, "open": ref.open_jobs}).scalar_one()


def bump_on_application(s: Session, company_key_hash: int) -> None:
    """S_new = S_old * exp(-dt / tau) + 1 (called inside the apply transaction)."""
    s.execute(text(f"""
        UPDATE companies c SET eviction_score = {CURRENT_SCORE_SQL} + 1,
               score_updated_at = now(), last_applied_at = now()
        WHERE c.company_key_hash = :h"""), {"h": company_key_hash})


def evict_to_capacity(s: Session, max_companies: int | None = None) -> int:
    max_companies = max_companies or get_settings().max_companies
    over = count(s) - max_companies
    if over <= 0:
        return 0
    ids = s.execute(text(f"""
        SELECT c.id FROM companies c WHERE NOT {PROTECTED_SQL}
        ORDER BY {CURRENT_SCORE_SQL} ASC, c.last_applied_at ASC NULLS FIRST, c.id ASC
        LIMIT :n"""), {"n": over}).scalars().all()
    if ids:
        s.execute(text("DELETE FROM companies WHERE id = ANY(:ids)"), {"ids": list(ids)})
    return len(ids)


def due_for_poll(s: Session, limit: int = 1000, high_priority_only: bool = False) -> list[dict]:
    """Companies whose poll interval has passed. High priority (30-60 min,
    section 12): starred by any user, open referral item, or hiring fast."""
    hp = """(EXISTS (SELECT 1 FROM company_stars st WHERE st.company_key_hash = c.company_key_hash)
             OR EXISTS (SELECT 1 FROM referral_queue q WHERE q.company_key_hash = c.company_key_hash
                        AND q.status NOT IN ('applied', 'closed', 'skipped'))
             OR c.open_matching_jobs >= 10)"""
    interval = f"CASE WHEN {hp} THEN 45 ELSE c.poll_interval_min END"
    where = f"c.ats_type <> 'unknown' AND (c.last_polled_at IS NULL OR c.last_polled_at < now() - make_interval(mins => {interval}))"
    if high_priority_only:
        where += f" AND {hp}"
    rows = s.execute(text(f"""SELECT c.* FROM companies c WHERE {where}
                              ORDER BY c.last_polled_at ASC NULLS FIRST LIMIT :n"""), {"n": limit})
    return [dict(r) for r in rows.mappings()]


def mark_polled(s: Session, company_id: int, open_jobs: int | None = None) -> None:
    s.execute(text("""UPDATE companies SET last_polled_at = now(),
                      open_matching_jobs = COALESCE(:o, open_matching_jobs) WHERE id = :id"""),
              {"id": company_id, "o": open_jobs})


def mark_slug_broken(s: Session, company_id: int) -> None:
    """404 from the ATS: the company moved boards. Re-probe on the next weekly run."""
    s.execute(text("""UPDATE companies SET ats_type = 'unknown', last_probed_at = NULL,
                      last_polled_at = now() WHERE id = :id"""), {"id": company_id})


# --- discovery / probing ----------------------------------------------------------
def slug_candidates(name: str) -> list[str]:
    base = company_key(name)
    words = base.split()
    cands = ["".join(words), "-".join(words), words[0] if words else ""]
    return [c for c in dict.fromkeys(cands) if c and re.match(r"^[a-z0-9-]+$", c)]


def probe(f: Fetcher, ref: CompanyRef) -> CompanyRef | None:
    """Find (ats, slug) for a company: careers URL first, then name-derived
    slugs against each keyless ATS API."""
    from app.ingest.sources import ashby, breezy, greenhouse, lever, oracle, recruitee, rippling, smartrecruiters, workday
    probers = {"greenhouse": greenhouse.probe, "lever": lever.probe, "ashby": ashby.probe,
               "smartrecruiters": smartrecruiters.probe, "recruitee": recruitee.probe, "rippling": rippling.probe,
               "breezy": breezy.probe, "oracle": oracle.probe}
    det = detect(ref.careers_url) if ref.careers_url else None
    if det and det.ats in probers and det.slug:
        try:
            if probers[det.ats](f, det.slug):
                return CompanyRef(**{**ref.__dict__, "ats": det.ats, "slug": det.slug, "host": det.host})
        except (NotFoundError, SourceError):
            pass
    if det and det.ats == "workday" and det.slug and "/" in det.slug:
        try:
            if workday.probe(f, det.slug, det.host):
                return CompanyRef(**{**ref.__dict__, "ats": "workday", "slug": det.slug, "host": det.host})
        except (NotFoundError, SourceError):
            pass
    for ats in PROBE_ORDER:
        for slug in slug_candidates(ref.name):
            try:
                if probers[ats](f, slug):
                    return CompanyRef(**{**ref.__dict__, "ats": ats, "slug": slug})
            except NotFoundError:
                continue
            except SourceError as e:
                log.warning("probe %s/%s: %s", ats, slug, e)
                break
    if ref.careers_url:
        return CompanyRef(**{**ref.__dict__, "ats": "jsonld"})
    return None


def _stored_board_status(f: Fetcher, ats: str | None, slug: str | None, host: str | None) -> str:
    """'ok' (board answers), 'gone' (404/410), or 'unknown' (couldn't tell: outage, odd payload,
    or an empty board, which may just mean no openings this week)."""
    from app.ingest.sources import ashby, breezy, greenhouse, lever, oracle, recruitee, rippling, smartrecruiters, workday
    probers = {"greenhouse": greenhouse.probe, "lever": lever.probe, "ashby": ashby.probe,
               "smartrecruiters": smartrecruiters.probe, "recruitee": recruitee.probe, "rippling": rippling.probe,
               "breezy": breezy.probe, "oracle": oracle.probe}
    if not slug or ats in (None, "unknown"):
        return "gone"
    try:
        if ats in probers:
            return "ok" if probers[ats](f, slug) else "unknown"
        if ats == "workday" and host:
            return "ok" if workday.probe(f, slug, host) else "unknown"
    except NotFoundError:
        return "gone"
    except Exception as e:          # SourceError, or a payload of an unexpected shape
        log.warning("re-validating %s/%s: %r", ats, slug, e)
    return "unknown"


def reprobe_stale(s: Session, f: Fetcher, limit: int = 200) -> int:
    """Weekly: companies switch ATS. Re-detect anything not probed in 7 days.

    A stored board that still answers is kept (name-derived guesses never replace it). A board is
    only cleared when it is confirmed gone and nothing else is found; a failed or inconclusive
    check (outage, rate limit) never erases good data."""
    rows = s.execute(text("""SELECT id, name, careers_url, ats_type::text AS ats_type, ats_slug, ats_host
                             FROM companies
                             WHERE last_probed_at IS NULL OR last_probed_at < now() - interval '7 days'
                             ORDER BY last_probed_at ASC NULLS FIRST LIMIT :n"""), {"n": limit}).mappings().all()
    changed = 0
    for r in rows:
        status = "gone" if r["ats_type"] in (None, "unknown", "jsonld") else \
            _stored_board_status(f, r["ats_type"], r["ats_slug"], r["ats_host"])
        found = None
        if status != "ok":
            found = probe(f, CompanyRef(name=r["name"], careers_url=r["careers_url"]))
        if found and (found.ats != r["ats_type"] or found.slug != r["ats_slug"]):
            changed += 1
            s.execute(text("""UPDATE companies SET last_probed_at = now(),
                              ats_type = CAST(:ats AS ats_type), ats_slug = :slug, ats_host = :host
                              WHERE id = :id"""),
                      {"id": r["id"], "ats": found.ats, "slug": found.slug, "host": found.host})
        elif found is None and status == "gone" and r["ats_type"] not in (None, "unknown"):
            changed += 1
            s.execute(text("""UPDATE companies SET last_probed_at = now(), ats_type = 'unknown',
                              ats_slug = NULL, ats_host = NULL WHERE id = :id"""), {"id": r["id"]})
        else:                       # still valid, unchanged, or inconclusive: keep what we have
            s.execute(text("UPDATE companies SET last_probed_at = now() WHERE id = :id"), {"id": r["id"]})
    return changed


def seed_from_csv(s: Session, path: Path) -> int:
    """CSV columns: name, careers_url (optional), ats (optional), slug (optional)."""
    n = 0
    with path.open() as fh:
        for row in csv.DictReader(fh):
            if not row.get("name"):
                continue
            det = detect(row.get("careers_url"))
            ref = CompanyRef(name=row["name"], careers_url=row.get("careers_url") or None,
                             ats=row.get("ats") or (det.ats if det else "unknown"),
                             slug=row.get("slug") or (det.slug if det else None),
                             host=det.host if det else None)
            if admit(s, ref) is not None:
                s.execute(text("UPDATE companies SET pinned = true WHERE company_key_hash = :h"),
                          {"h": h64(company_key(ref.name))})
                n += 1
    return n
