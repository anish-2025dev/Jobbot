"""Scheduler (section 12). One process; every job is guarded by a Redis
lock, so running two schedulers by mistake never double-runs anything.

| job              | when                         | what                                        |
|------------------|------------------------------|---------------------------------------------|
| ingest           | every 2 h                    | Tier A registry + Tier B aggregators        |
| ingest_priority  | every 45 min                 | starred / referral / fast-hiring companies  |
| hn               | daily                        | HN Who's Hiring thread                      |
| mail             | every 15 min                 | ingestion, cleanup P0-P4, Tier C alerts,    |
|                  |                              | reminders, outreach replies                 |
| outreach         | every 5 min                  | referral queue: find, send 9-11 am, holds   |
| hourly           | every hour                   | size check, waiting-list expiry, reputation |
| digest           | hourly check, per user hour  | morning Telegram digest                     |
| nightly          | daily 21:30 UTC (03:00 IST)  | partitions, batch deletes, VACUUM           |
| weekly           | Sunday 22:00 UTC             | registry eviction + slug re-probe, tuning   |
"""
from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import text

from app import db
from app.redis_client import get_redis

log = logging.getLogger("jobbot.scheduler")


@dataclass
class Job:
    name: str
    fn: Callable[[], object]
    every_s: int | None = None                     # interval jobs
    at: tuple[int, int] | None = None               # (hour, minute) UTC, daily
    weekday: int | None = None                      # 0 = Monday, with `at`
    lock_s: int = 3600


_memory: dict[str, float] = {}


def _last(name: str) -> float:
    r = get_redis()
    v = r.get(f"sched:last:{name}") if r is not None else _memory.get(name)
    return float(v) if v else 0.0


def _mark(name: str, ts: float) -> None:
    r = get_redis()
    if r is not None:
        r.set(f"sched:last:{name}", str(ts), ex=14 * 86400)
    else:
        _memory[name] = ts


def due(job: Job, now: datetime) -> bool:
    last = _last(job.name)
    ts = now.timestamp()
    if job.every_s is not None:
        return ts - last >= job.every_s
    h, m = job.at
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if job.weekday is not None and now.weekday() != job.weekday:
        return False
    return now >= slot and last < slot.timestamp()


def run_job(job: Job, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    r = get_redis()
    if r is not None and not r.set(f"sched:lock:{job.name}", "1", nx=True, ex=job.lock_s):
        return False                                   # another scheduler (or a long previous run) holds it
    t0 = time.monotonic()
    try:
        result = job.fn()
        log.info("job %s ok in %.1fs: %s", job.name, time.monotonic() - t0, _short(result))
    except Exception:
        log.exception("job %s failed", job.name)
    finally:
        _mark(job.name, now.timestamp())
        if r is not None:
            r.delete(f"sched:lock:{job.name}")
    return True


def _short(v) -> str:
    s = repr(v)
    return s if len(s) < 300 else s[:300] + "..."


# --- job bodies ----------------------------------------------------------------------
def _fetcher():
    from app.ingest.http import make_fetcher
    return make_fetcher()


def job_ingest(high_priority_only: bool = False, include_hn: bool = False):
    from app.apply.routing import make_router
    from app.ingest.runner import run_round
    from app.notify.telegram import get_notifier
    res = run_round(_fetcher(), high_priority_only=high_priority_only, include_hn=include_hn,
                    tier_b=not high_priority_only, router=make_router(get_notifier()))
    if res.skipped_reason and "C1" in res.skipped_reason:
        _alert_admins(f"Ingestion paused: {res.skipped_reason}")
    return {"fetched": res.fetched, "unique": res.unique, "polled": res.companies_polled,
            "admitted": res.companies_admitted, "skipped": res.skipped_reason, "errors": len(res.errors)}


def mail_users() -> list:
    with db.worker_session() as s:
        return s.execute(text("""SELECT DISTINCT c.user_id FROM credentials c JOIN users u ON u.id = c.user_id
                                 WHERE c.host IN ('gmail', 'imap') AND u.deleted_at IS NULL""")).scalars().all()


def job_mail():
    from app.ingest.runner import run_round
    from app.llm.client import get_llm
    from app.mail.accounts import mail_client
    from app.mail.gmail import GmailAuthError
    from app.mail.processor import cleanup_done_positives, due_reminders, process_mailbox
    from app.notify.telegram import get_notifier
    from app.outreach.queue import on_reply
    notify, llm = get_notifier(), get_llm()
    totals = {"users": 0, "scanned": 0, "trashed": 0, "alerts": 0, "alert_jobs": 0, "errors": 0}
    for uid in mail_users():
        try:
            with db.worker_session() as s:
                client = mail_client(s, uid)        # decrypting a bad credential must not stop other users
        except Exception:
            log.exception("loading mail credentials for %s failed", uid)
            totals["errors"] += 1
            continue
        if client is None:
            continue
        try:
            rep = process_mailbox(client, db.worker_session, uid, notify=notify, llm=llm,
                                  on_outreach_reply=lambda s, u, m, c, msg: on_reply(s, u, m, c, msg, notify))
            with db.worker_session() as s:
                cleanup_done_positives(client, s, uid)
                for r in due_reminders(s, uid):
                    notify(uid, f"Reminder: {r['class']} for {r['company_name'] or 'a reply'} is due {r['deadline']:%d %b %H:%M} UTC.")
        except GmailAuthError:
            notify(uid, "Gmail disconnected (access revoked or expired). Reconnect it in JobBot settings.")
            totals["errors"] += 1
            continue
        except Exception:
            log.exception("mail for %s failed", uid)
            totals["errors"] += 1
            continue
        totals["users"] += 1
        totals["scanned"] += rep.scanned
        totals["trashed"] += rep.trashed
        totals["alerts"] += rep.alerts
        if rep.alert_jobs:                               # Tier C: parsed job alerts join the pipeline (manual-only)
            from app.apply.routing import make_router
            try:
                run_round(_fetcher(), tier_a=False, tier_b=False, user_ids=[uid], extra_raws=rep.alert_jobs,
                          router=make_router(notify))
                totals["alert_jobs"] += len(rep.alert_jobs)
            except Exception:
                log.exception("job alerts for %s failed", uid)
                totals["errors"] += 1
    return totals


def job_outreach():
    from app.llm.client import get_llm
    from app.mail.accounts import mail_client
    from app.notify.telegram import get_notifier
    from app.outreach.queue import tick
    return tick(db.worker_session, notify=get_notifier(), mail_client_for=mail_client, llm=get_llm())


def job_hourly():
    from app import janitor
    from app.notify.telegram import get_notifier
    from app.outreach.queue import check_reputation
    notify = get_notifier()
    rep = janitor.hourly(notify=_alert_admins)
    with db.worker_session() as s:
        ids = s.execute(text("SELECT DISTINCT user_id FROM outreach_log WHERE sent_at > now() - interval '30 days'")).scalars().all()
    for uid in ids:
        with db.worker_session() as s:
            check_reputation(s, uid, notify)
    return {"db_mb": round(rep.db_mb, 1), "state": rep.state, "deleted": rep.deleted}


def job_digest(now: datetime | None = None):
    """Each user's digest at their digest_hour (default 8) in their time zone, once a day."""
    from zoneinfo import ZoneInfo
    from app.notify.digest import compose
    from app.notify.telegram import get_notifier
    notify = get_notifier()
    now = now or datetime.now(timezone.utc)
    r = get_redis()
    sent = 0
    with db.worker_session() as s:
        users = s.execute(text("""SELECT id, timezone, settings FROM users WHERE telegram_chat_id IS NOT NULL
                                  AND deleted_at IS NULL""")).all()
    for u in users:
        try:
            local = now.astimezone(ZoneInfo(u.timezone or "Asia/Kolkata"))
        except Exception:
            local = now
        if local.hour != int((u.settings or {}).get("digest_hour", 8)):
            continue
        key = f"digest:{u.id}:{local.date()}"
        if r is not None and not r.set(key, "1", nx=True, ex=2 * 86400):
            continue
        with db.worker_session() as s:
            notify(u.id, compose(s, u.id))
        sent += 1
    return {"sent": sent}


def job_nightly():
    from app import janitor
    rep = janitor.nightly()
    return {"db_mb": round(rep.db_mb, 1), "state": rep.state, "partitions": rep.partitions_dropped,
            "deleted": {k: v for k, v in rep.deleted.items() if v}}


def job_weekly():
    from app import janitor
    from app.tuning import tune_all
    rep = janitor.weekly(_fetcher())
    tuned = tune_all(db.worker_session)
    return {"registry": rep.deleted, "tuned": {k: v.reason or "updated" for k, v in tuned.items()}}


def _alert_admins(msg: str) -> None:
    from app.notify.telegram import get_notifier
    notify = get_notifier()
    with db.worker_session() as s:
        admins = s.execute(text("SELECT id FROM users WHERE is_admin AND deleted_at IS NULL")).scalars().all()
    for a in admins:
        notify(a, msg)
    log.warning("admin alert: %s", msg)


def jobs() -> list[Job]:
    return [
        Job("ingest", lambda: job_ingest(), every_s=2 * 3600, lock_s=2 * 3600),
        Job("ingest_priority", lambda: job_ingest(high_priority_only=True), every_s=45 * 60, lock_s=3600),
        Job("hn", lambda: job_ingest(include_hn=True), at=(4, 0), lock_s=3600),
        Job("mail", job_mail, every_s=15 * 60, lock_s=30 * 60),
        Job("outreach", job_outreach, every_s=5 * 60, lock_s=20 * 60),
        Job("hourly", job_hourly, every_s=3600, lock_s=30 * 60),
        Job("digest", job_digest, every_s=15 * 60, lock_s=10 * 60),
        Job("nightly", job_nightly, at=(21, 30), lock_s=3 * 3600),
        Job("weekly", job_weekly, at=(22, 0), weekday=6, lock_s=6 * 3600),
    ]


def run_forever(tick_s: float = 30.0) -> None:
    stop = {"flag": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.update(flag=True))
    log.info("scheduler started (%s)", ", ".join(j.name for j in jobs()))
    while not stop["flag"]:
        now = datetime.now(timezone.utc)
        for job in jobs():
            if stop["flag"]:
                break
            if due(job, now):
                run_job(job, now)
        for _ in range(int(tick_s)):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("scheduler stopped")


def run_named(name: str) -> object:
    job = next((j for j in jobs() if j.name == name), None)
    if job is None:
        raise SystemExit(f"unknown job {name}; one of {[j.name for j in jobs()]}")
    return job.fn()
