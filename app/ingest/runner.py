"""One ingestion round: fetch (shared) -> normalize (shared) -> per-user pipeline.

Schedule (section 12): Tier A/B every 2 h, high-priority companies every
30-60 min, HN daily. Ingestion is skipped entirely while the DB is at the
pause threshold (C1).
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text

from app.budget import budget_state
from app.config import get_settings
from app import db
from app.ingest import registry
from app.ingest.detect import is_manual_portal, detect
from app.ingest.http import Fetcher, NotFoundError, SourceError, record_source_result, source_active
from app.ingest.models import RawJob
from app.ingest.normalize import NormalizedJob, families, normalize
from app.ingest.pipeline import Context, Router, ensure_partitions, process_user
from app.ingest.scoring import UserProfile, profile_from_vault
from app.ingest.sources import adzuna, ashby, greenhouse, hn, jsearch, jsonld, lever, smartrecruiters, workday
from app.util.hashing import canonical_hash
from app.vault.schema import Vault

log = logging.getLogger(__name__)


@dataclass
class RoundResult:
    fetched: int = 0
    unique: int = 0
    per_user: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    companies_polled: int = 0
    companies_admitted: int = 0
    pre_skipped: int = 0
    skipped_reason: str | None = None
    seconds: float = 0.0


def ingestion_paused() -> str | None:
    cfg = get_settings()
    _, state = budget_state(db.db_size_mb(), cfg.db_budget_mb, cfg.db_warn_pct, cfg.db_pause_pct)
    return "storage budget at pause threshold (C1)" if state == "pause" else None


def load_profiles(s, user_ids: list[UUID] | None = None) -> list[UserProfile]:
    q = """SELECT u.id, u.settings, v.data, v.revision FROM users u JOIN vaults v ON v.user_id = u.id
           WHERE u.deleted_at IS NULL"""
    params = {}
    if user_ids:
        q += " AND u.id = ANY(:ids)"
        params["ids"] = list(user_ids)
    profiles = []
    for r in s.execute(text(q), params):
        try:
            vault = Vault.model_validate(r.data)
        except Exception as e:  # a vault that no longer validates is skipped, not fatal
            log.warning("user %s vault invalid, skipping: %s", r.id, e)
            continue
        starred = set(s.execute(text("SELECT company_key_hash FROM company_stars WHERE user_id = :u"),
                                {"u": r.id}).scalars())
        profiles.append(profile_from_vault(r.id, vault, r.revision, r.settings or {}, starred))
    return profiles


def title_filter_for(profiles: list[UserProfile]):
    wanted = set().union(*(p.families for p in profiles)) if profiles else set()
    return lambda title: bool(families(title) & wanted)


def fetch_company(f: Fetcher, c: dict, title_filter) -> list[RawJob]:
    ats, slug, name = c["ats_type"], c["ats_slug"], c["name"]
    if ats == "greenhouse":
        return greenhouse.fetch(f, slug, name, title_filter)
    if ats == "lever":
        return lever.fetch(f, slug, name, title_filter)
    if ats == "ashby":
        return ashby.fetch(f, slug, name, title_filter)
    if ats == "smartrecruiters":
        return smartrecruiters.fetch(f, slug, name, title_filter)
    if ats == "workday":
        return workday.fetch(f, slug, c["ats_host"], name, title_filter)
    if ats == "jsonld" and c.get("careers_url"):
        return jsonld.fetch(f, c["careers_url"], name, title_filter)
    return []


SOURCE_OF_ATS = {"greenhouse": "greenhouse", "lever": "lever", "ashby": "ashby",
                 "smartrecruiters": "smartrecruiters", "workday": "workday", "jsonld": "career_jsonld"}


def poll_registry(f: Fetcher, profiles: list[UserProfile], result: RoundResult, *, limit: int = 1000,
                  high_priority_only: bool = False, workers: int = 8) -> list[RawJob]:
    with db.worker_session() as s:
        due = registry.due_for_poll(s, limit=limit, high_priority_only=high_priority_only)
        active = {src: source_active(s, src) for src in set(SOURCE_OF_ATS.values())}
    due = [c for c in due if active.get(SOURCE_OF_ATS.get(c["ats_type"], ""), False)]
    tf = title_filter_for(profiles)
    out: list[RawJob] = []
    outcomes: list[tuple[dict, str, int | None, str | None]] = []   # (company, status, n, error)

    def one(c):
        try:
            return c, "ok", fetch_company(f, c, tf), None
        except NotFoundError as e:
            return c, "notfound", [], str(e)
        except SourceError as e:
            return c, "error", [], str(e)
        except Exception as e:      # malformed payload (wrong JSON shape, missing key): this company only
            log.warning("poll failed for %s (%s): %r", c.get("name"), c.get("ats_type"), e)
            return c, "malformed", [], f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(one, c) for c in due]):
            c, status, jobs, err = fut.result()
            out.extend(jobs)
            outcomes.append((c, status, len(jobs), err))
    with db.worker_session() as s:
        per_source: dict[str, list[bool]] = {}
        for c, status, n, err in outcomes:
            src = SOURCE_OF_ATS.get(c["ats_type"], "career_jsonld")
            if status == "ok":
                registry.mark_polled(s, c["id"], n)
                per_source.setdefault(src, []).append(True)
            elif status == "notfound":
                registry.mark_slug_broken(s, c["id"])
            elif status == "malformed":
                # The board answered but its payload didn't parse: wait the normal interval instead of
                # retrying every round, and don't count it against the whole source's health.
                registry.mark_polled(s, c["id"])            # keeps open_matching_jobs as it was
                result.errors.append(f"{c['name']}: {err}")
            else:
                per_source.setdefault(src, []).append(False)
                result.errors.append(f"{c['name']}: {err}")
        for src, results in per_source.items():
            # A source is failing only if every company on it failed this round.
            record_source_result(s, src, ok=any(results), error=None if any(results) else "all polls failed")
    result.companies_polled += len(due)
    return out


def poll_aggregators(f: Fetcher, profiles: list[UserProfile], result: RoundResult,
                     include_hn: bool = False) -> list[RawJob]:
    cfg = get_settings()
    out: list[RawJob] = []
    queries = Counter()
    for p in profiles:
        with db.worker_session() as s:
            v = s.execute(text("SELECT data FROM vaults WHERE user_id = :u"), {"u": p.user_id}).scalar()
        exp = (v or {}).get("expectations", {})
        for role in exp.get("role_types", [])[:3]:
            for loc in [l for l in exp.get("locations", []) if l.lower() != "remote"][:3] or ["India"]:
                queries[(role, loc)] += 1
    top = [q for q, _ in queries.most_common(cfg.aggregator_queries_per_run)]
    with db.worker_session() as s:
        js_ok, az_ok = source_active(s, "jsearch"), source_active(s, "adzuna")
        hn_ok = source_active(s, "hn_whos_hiring")
    for role, loc in top:
        if cfg.jsearch_api_key and js_ok:
            try:
                out += jsearch.fetch(f, cfg.jsearch_api_key, f"{role} in {loc}")
                _record("jsearch", True)
            except (SourceError, NotFoundError) as e:
                _record("jsearch", False, str(e))
                result.errors.append(f"jsearch: {e}")
        if cfg.adzuna_app_id and cfg.adzuna_app_key and az_ok:
            try:
                out += adzuna.fetch(f, cfg.adzuna_app_id, cfg.adzuna_app_key, role, loc)
                _record("adzuna", True)
            except (SourceError, NotFoundError) as e:
                _record("adzuna", False, str(e))
                result.errors.append(f"adzuna: {e}")
    if include_hn and hn_ok:
        try:
            out += hn.fetch(f)
            _record("hn_whos_hiring", True)
        except (SourceError, NotFoundError) as e:
            _record("hn_whos_hiring", False, str(e))
    return out


def _record(source: str, ok: bool, err: str | None = None) -> None:
    with db.worker_session() as s:
        record_source_result(s, source, ok, err)


def discover_companies(raws: list[RawJob], result: RoundResult) -> None:
    """Aggregator jobs that link to a keyless ATS reveal a board to poll
    directly; admit those companies (subject to the C7 admission rule)."""
    found: dict[tuple[str, str], tuple[RawJob, int]] = {}
    for r in raws:
        if r.source in SOURCE_OF_ATS.values() or not r.company_slug:
            continue
        if r.ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "workday"):
            key = (r.ats, r.company_slug)
            prev = found.get(key)
            found[key] = (r, (prev[1] if prev else 0) + 1)
    if not found:
        return
    with db.worker_session() as s:
        for (ats, slug), (r, n) in found.items():
            det = detect(r.apply_url or r.url)
            ref = registry.CompanyRef(name=r.company, ats=ats, slug=slug, host=det.host if det else None,
                                      email_domain=r.company_domain, open_jobs=n)
            if registry.find(s, r.company) is None and registry.admit(s, ref) is not None:
                result.companies_admitted += 1


def prefilter_seen(raws: list[RawJob], profiles: list[UserProfile]) -> list[RawJob]:
    """Skip postings every user has already processed, before the costly
    normalization. Uses the canonical hash, which needs no text processing;
    on a typical round after the first, this drops most of the fetch."""
    if not raws or not profiles:
        return raws
    canon = [canonical_hash(r.ats, r.company, r.req_id, r.url) for r in raws]
    uniq = list(set(canon))
    seen_by_all: set[int] | None = None
    with db.worker_session() as s:
        for p in profiles:
            seen = set(s.execute(text("""SELECT canonical_hash FROM seen_jobs WHERE user_id = :u
                                         AND canonical_hash = ANY(:c)"""), {"u": p.user_id, "c": uniq}).scalars())
            seen_by_all = seen if seen_by_all is None else seen_by_all & seen
            if not seen_by_all:
                return raws
    return [r for r, c in zip(raws, canon) if c not in seen_by_all]


def _is_http(url: str | None) -> bool:
    return bool(url) and urlsplit(url.strip()).scheme.lower() in ("http", "https")


def normalize_all(raws: list[RawJob], profiles: list[UserProfile], today: datetime) -> list[NormalizedJob]:
    terms = tuple(sorted(set().union(*(p.terms for p in profiles)))) if profiles else ()
    seen: set[int] = set()
    out = []
    for r in raws:
        if not r.title or not r.company or not _is_http(r.url):
            continue                # also drops javascript:/data: links a page could smuggle in
        if r.apply_url and not _is_http(r.apply_url):
            r.apply_url = None
        try:
            n = normalize(r, today=today, vault_terms=terms)
        except Exception as e:      # one malformed posting never kills a round
            log.warning("normalize failed for %s/%s: %s", r.source, r.url, e)
            continue
        if n.canonical_hash in seen:
            continue
        seen.add(n.canonical_hash)
        if is_manual_portal(detect(r.apply_url or r.url)):
            r.easy_apply_only = True
        out.append(n)
    return out


def run_round(f: Fetcher, *, tier_a: bool = True, tier_b: bool = True, include_hn: bool = False,
              high_priority_only: bool = False, user_ids: list[UUID] | None = None,
              router: Router | None = None, today: date | None = None,
              now: datetime | None = None, extra_raws: list[RawJob] | None = None) -> RoundResult:
    t0 = time.monotonic()
    result = RoundResult()
    paused = ingestion_paused()
    if paused:
        result.skipped_reason = paused
        return result
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    with db.worker_session() as s:
        profiles = load_profiles(s, user_ids)
    if not profiles:
        result.skipped_reason = "no users with a vault"
        return result

    raws: list[RawJob] = list(extra_raws or [])
    if tier_a:
        raws += poll_registry(f, profiles, result, high_priority_only=high_priority_only)
    if tier_b and not high_priority_only:
        raws += poll_aggregators(f, profiles, result, include_hn=include_hn)
    result.fetched = len(raws)
    discover_companies(raws, result)
    fresh = prefilter_seen(raws, profiles)
    result.pre_skipped = len(raws) - len(fresh)
    jobs = normalize_all(fresh, profiles, now)
    result.unique = len(jobs)

    with db.worker_session() as s:
        ensure_partitions(s, today)
        ctx = Context.load(s)
    for p in profiles:
        with db.worker_session() as s:
            st = process_user(s, p, jobs, ctx, day=today, now=now, router=router)
        result.per_user[str(p.user_id)] = st.as_dict()
    result.seconds = round(time.monotonic() - t0, 2)
    return result
