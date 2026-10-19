"""Apply worker (section 8.5): continuous, daytime windows, 5-8 parallel
browser workers in production (APPLY_WORKERS), ~2-3 min per application."""
from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy import text

from app import db
from app.apply import timing
from app.apply.runner import Outcome, run_application
from app.ratelimit import Limit, get_limiter

APPLY_LIMIT = Limit(rate_per_s=1 / 90, burst=2)      # ~40 applications/hour per ATS domain at most
from app.ingest.detect import detect

log = logging.getLogger(__name__)


def next_application(s, now: datetime | None = None) -> dict | None:
    """Highest-ranked queued application that is due, with row lock so
    parallel workers never take the same one."""
    rows = s.execute(text("""
        SELECT a.id, a.user_id, a.apply_url, a.ats, c.timezone AS ctz, u.timezone AS utz, u.settings
        FROM applications a JOIN users u ON u.id = a.user_id
        LEFT JOIN companies c ON c.company_key_hash = a.company_key_hash
        WHERE a.status = 'queued' AND a.deleted_at IS NULL AND a.recipe IS NOT NULL
          AND (a.not_before IS NULL OR a.not_before <= now())
          AND NOT EXISTS (SELECT 1 FROM referral_queue q WHERE q.application_id = a.id
                          AND q.status IN ('finding', 'awaiting_approval', 'scheduled', 'sent') AND (q.apply_at IS NULL OR q.apply_at > now()))
          AND coalesce((u.settings->>'paused')::boolean, false) = false
          AND coalesce((u.settings->>'auto_apply')::boolean, false) = true
        ORDER BY a.bucket DESC, a.salary_max DESC NULLS LAST, a.posted_at DESC NULLS LAST
        LIMIT 50 FOR UPDATE OF a SKIP LOCKED""")).mappings().all()
    for r in rows:
        tz = r["ctz"] or r["utz"]
        portal = (detect(r["apply_url"]) and detect(r["apply_url"]).host) or r["ats"]
        if not timing.in_daytime(tz, now) or not timing.portal_ready(portal):
            continue
        # section 8.2 step 5 / 8.5: token bucket per ATS domain (on top of the jitter)
        if not get_limiter().try_acquire(f"apply:{portal}", APPLY_LIMIT):
            continue
        s.execute(text("UPDATE applications SET status = 'applying', last_status_at = now() WHERE id = :i"),
                  {"i": r["id"]})
        return {**dict(r), "portal": portal}
    return None


def work_once(context, notify=None, llm=None) -> Outcome | None:
    with db.worker_session() as s:
        nxt = next_application(s)
    if nxt is None:
        return None
    # 'applying' (claimed_at NULL) marks the pick so no other worker takes it;
    # the hourly janitor re-queues picks orphaned by a crashed worker.
    timing.mark_portal_used(nxt["portal"])
    out = run_application(nxt["id"], context, notify=notify, llm=llm)
    log.info("application %s -> %s %s", nxt["id"], out.status, out.reason)
    return out


def run_forever(workers: int = 1, stop=lambda: False) -> None:
    from app.apply.browser import browser_session
    from app.llm.client import get_llm
    from app.notify.telegram import get_notifier
    notify, llm = get_notifier(), get_llm()
    with browser_session() as context:
        while not stop():
            try:
                out = work_once(context, notify=notify, llm=llm)
            except Exception:
                log.exception("worker iteration failed")
                out = None
            time.sleep(5 if out is None else 1)
