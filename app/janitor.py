"""Storage janitor (section 4 cleanup, C1).

hourly():  size check -> ok / tight (jobs retention 3 days) / pause (ingestion
           stops, alert); expired waiting-list entries; referral holds are
           driven by the outreach tick.
nightly(): drop whole daily partitions past retention (no bloat); batch
           deletes of expired rows in chunks of 5,000; outcome rollups before
           hard deletes; VACUUM ANALYZE.
weekly():  registry eviction to MAX_COMPANIES and ATS slug re-probing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text

from app.budget import budget_state
from app.config import get_settings
from app import db

log = logging.getLogger(__name__)
CHUNK = 5000


@dataclass
class JanitorReport:
    db_mb: float = 0.0
    state: str = "ok"
    deleted: dict[str, int] = field(default_factory=dict)
    partitions_dropped: dict[str, int] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)


def _batched_delete(table: str, where: str, params: dict | None = None, key: str = "id") -> int:
    """DELETE ... WHERE <where> in chunks, each its own transaction."""
    total = 0
    while True:
        with db.worker_session() as s:
            n = s.execute(text(f"""
                WITH doomed AS (SELECT ctid FROM {table} WHERE {where} LIMIT {CHUNK})
                DELETE FROM {table} t USING doomed WHERE t.ctid = doomed.ctid"""), params or {}).rowcount
        total += n
        if n < CHUNK:
            return total


def current_state() -> tuple[float, str]:
    cfg = get_settings()
    used = db.db_size_mb()
    _, state = budget_state(used, cfg.db_budget_mb, cfg.db_warn_pct, cfg.db_pause_pct)
    return used, state


def jobs_retention_days(state: str) -> int:
    cfg = get_settings()
    return cfg.jobs_retention_days_tight if state in ("tight", "pause") else cfg.jobs_retention_days


def drop_old_partitions(today: date, state: str) -> dict[str, int]:
    cfg = get_settings()
    keep = {"jobs": jobs_retention_days(state), "seen_jobs": cfg.seen_jobs_retention_days,
            "jd_signatures": cfg.jd_signature_retention_days}
    out = {}
    with db.worker_session() as s:
        for parent, days in keep.items():
            out[parent] = s.execute(text("SELECT jobbot_drop_daily_partitions(:p, CAST(:d AS date) - :n + 1)"),
                                    {"p": parent, "d": today, "n": days}).scalar_one()
            s.execute(text("SELECT jobbot_ensure_daily_partitions(:p, CAST(:d AS date), CAST(:d AS date) + 7)"),
                      {"p": parent, "d": today})
    return out


def record_outcomes_and_delete(where: str, outcome_sql: str, label: str, params: dict | None = None) -> int:
    """Roll each application into outcomes_daily (anonymous) and
    training_examples (per user, for P6 tuning) before hard-deleting it."""
    total = 0
    while True:
        with db.worker_session() as s:
            rows = s.execute(text(f"""
                SELECT id, user_id, source_id, bucket, components, {outcome_sql} AS outcome, outcome_recorded
                FROM applications WHERE {where} LIMIT {CHUNK}"""), params or {}).all()
            if not rows:
                return total
            for r in rows:
                if not r.outcome_recorded and r.source_id is not None and r.bucket is not None:
                    s.execute(text("""
                        INSERT INTO outcomes_daily (day, score_bucket, source_id, outcome, count)
                        VALUES (current_date, :b, :src, :o, 1)
                        ON CONFLICT (day, score_bucket, source_id, outcome)
                        DO UPDATE SET count = outcomes_daily.count + 1"""),
                              {"b": r.bucket, "src": r.source_id, "o": r.outcome})
                    if r.components:
                        s.execute(text("""INSERT INTO training_examples (user_id, components, label)
                                          VALUES (:u, :c, :l)"""),
                                  {"u": r.user_id, "c": r.components, "l": 1 if r.outcome >= 2 else 0})
            s.execute(text("DELETE FROM applications WHERE id = ANY(:ids)"), {"ids": [r.id for r in rows]})
            total += len(rows)
        if len(rows) < CHUNK:
            return total


def hourly(notify=None) -> JanitorReport:
    rep = JanitorReport()
    rep.db_mb, rep.state = current_state()
    cfg = get_settings()
    if rep.state == "tight":
        rep.partitions_dropped = drop_old_partitions(date.today(), rep.state)
    if rep.state == "pause":
        rep.alerts.append(f"Storage at {rep.db_mb:.0f} MB of {cfg.db_budget_mb} MB budget: ingestion paused.")
    # waiting-list expiry (C8): entries live 24 h unless pinned
    with db.worker_session() as s:
        expired = s.execute(text("""
            DELETE FROM waiting_list WHERE expires_at < now() RETURNING application_id""")).scalars().all()
        if expired:
            s.execute(text("""DELETE FROM applications WHERE id = ANY(:ids) AND status = 'waiting'
                              AND applied_at IS NULL"""), {"ids": list(expired)})
    rep.deleted["waiting_list"] = len(expired)
    with db.worker_session() as s:   # picks orphaned by a crashed apply worker (never claimed)
        rep.deleted["requeued_orphans"] = s.execute(text("""
            UPDATE applications SET status = 'queued' WHERE status = 'applying' AND claimed_at IS NULL
              AND last_status_at < now() - interval '30 minutes'""")).rowcount
    if notify and rep.alerts:
        for a in rep.alerts:
            notify(a)
    return rep


def nightly(today: date | None = None, vacuum: bool = True) -> JanitorReport:
    cfg = get_settings()
    today = today or date.today()
    rep = JanitorReport()
    rep.db_mb, rep.state = current_state()
    rep.partitions_dropped = drop_old_partitions(today, rep.state)
    d = rep.deleted

    # applications: rejected -> soft-deleted 7 days, then hard delete (outcome 1);
    # no reply after 45 days -> delete (outcome 0). Ledger rows stay.
    d["applications_rejected"] = record_outcomes_and_delete(
        "deleted_at IS NOT NULL AND deleted_at < now() - interval '7 days'", "1", "rejected")
    d["applications_no_reply"] = record_outcomes_and_delete(
        "deleted_at IS NULL AND status IN ('applied', 'applied_pending_referral') "
        "AND applied_at < now() - make_interval(days => :n)", "0", "no_reply", {"n": cfg.no_reply_delete_days})
    d["applications_stale_queue"] = _batched_delete(
        "applications", "status IN ('queued', 'failed', 'closed') AND applied_at IS NULL "
        "AND last_status_at < now() - interval '14 days'")
    # submitted recipes are kept 7 days (re-render for interview prep), longer only if positive
    with db.worker_session() as s:
        d["recipes_trimmed"] = s.execute(text("""
            UPDATE applications SET recipe = NULL WHERE recipe IS NOT NULL AND status <> 'positive'
              AND applied_at < now() - interval '7 days'""")).rowcount
    d["responses"] = _batched_delete(
        "responses", "(deleted_at IS NOT NULL AND deleted_at < now() - interval '7 days') "
                     "OR (user_done_at IS NOT NULL AND keep_until < now() AND priority > 0)")
    # C4 ledger: >= 90 days, then only once the opening has stopped appearing
    # (not seen in the last 7 days = closed). A still-open job keeps its row.
    d["applied_ledger"] = _batched_delete(
        "applied_ledger", "applied_at < now() - make_interval(days => :n) AND NOT EXISTS "
        "(SELECT 1 FROM seen_jobs sj WHERE sj.user_id = applied_ledger.user_id "
        " AND (sj.canonical_hash = applied_ledger.canonical_hash OR sj.fingerprint = applied_ledger.fingerprint))",
        {"n": cfg.ledger_min_days})
    d["company_cooldown"] = _batched_delete(
        "company_cooldown", "reserved_slots = 0 AND (last_applied_at IS NULL OR last_applied_at < now() - interval '30 days')")
    with db.worker_session() as s:   # prune timestamps outside the rolling 30-day window
        s.execute(text("""UPDATE company_cooldown SET applied_ats = ARRAY(
                              SELECT t FROM unnest(applied_ats) t WHERE t > now() - interval '30 days')
                          WHERE EXISTS (SELECT 1 FROM unnest(applied_ats) t WHERE t <= now() - interval '30 days')"""))
    d["recipe_cache"] = _batched_delete("recipe_cache", "created_at < now() - make_interval(days => :n)",
                                        {"n": cfg.recipe_cache_days})
    d["mail_actions"] = _batched_delete("mail_actions", "acted_at < now() - make_interval(days => :n)",
                                        {"n": cfg.mail_actions_days})
    d["outreach_contacts"] = _batched_delete(
        "outreach_contacts", "NOT replied_positive AND COALESCE(last_contacted_at, created_at) < now() - interval '90 days'")
    d["outreach_log"] = _batched_delete("outreach_log", "sent_at < now() - interval '30 days'")
    d["referral_queue"] = _batched_delete(
        "referral_queue", "status IN ('applied', 'closed', 'skipped') AND closed_at < now() - interval '7 days'")
    d["training_examples"] = _batched_delete("training_examples", "created_on < current_date - 180")
    d["outcomes_daily"] = _batched_delete("outcomes_daily", "day < current_date - 365", key="day")
    d["provider_usage_reset"] = 0
    with db.worker_session() as s:   # monthly credit reset (section 12)
        d["provider_usage_reset"] = s.execute(text("""
            UPDATE provider_usage SET credits_used = 0, exhausted_until = NULL,
                   period_start = date_trunc('month', now())::date
            WHERE period_start < date_trunc('month', now())::date""")).rowcount
    if vacuum:
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            for t in ("applications", "responses", "applied_ledger", "company_cooldown", "recipe_cache",
                      "mail_actions", "outreach_contacts", "outreach_log", "referral_queue", "waiting_list"):
                c.execute(text(f"VACUUM (ANALYZE) {t}"))
    rep.db_mb, rep.state = current_state()
    return rep


def weekly(fetcher=None) -> JanitorReport:
    from app.ingest import registry
    rep = JanitorReport()
    with db.worker_session() as s:
        rep.deleted["companies_evicted"] = registry.evict_to_capacity(s)
    if fetcher is not None:
        with db.worker_session() as s:
            rep.deleted["companies_reprobed_changed"] = registry.reprobe_stale(s, fetcher)
    rep.db_mb, rep.state = current_state()
    return rep
