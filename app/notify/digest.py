"""Daily morning digest (section 11): positives with deadlines, referral
queue, waiting list top 10, Stretch jobs with missing skills, email-finder
credits, adapter health, storage."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingest.scoring import prep_days


def _hours_left(ts) -> str:
    if ts is None:
        return ""
    h = (ts - datetime.now(timezone.utc)).total_seconds() / 3600
    return f"{max(h, 0):.0f}h left"


def waiting_lines(s: Session, uid: UUID, n: int = 10) -> list[str]:
    rows = s.execute(text("""
        SELECT a.company_name, a.title, a.bucket, a.salary_max, w.expires_at FROM waiting_list w
        JOIN applications a ON a.id = w.application_id WHERE w.user_id = :u
        ORDER BY a.bucket DESC, a.salary_max DESC NULLS LAST, a.posted_at DESC NULLS LAST LIMIT :n"""),
        {"u": uid, "n": n}).all()
    names = {3: "High", 2: "Med", 1: "Low"}
    return [f"- [{names.get(r.bucket, '?')}] {r.company_name}: {r.title} ({_hours_left(r.expires_at)})" for r in rows]


def compose(s: Session, uid: UUID) -> str:
    cfg = get_settings()
    out = ["JobBot daily digest"]

    pos = s.execute(text("""
        SELECT r.class AS cls, r.deadline, a.company_name, a.title FROM responses r
        LEFT JOIN applications a ON a.id = r.application_id
        WHERE r.user_id = :u AND r.user_done_at IS NULL AND r.deleted_at IS NULL
        ORDER BY r.deadline NULLS LAST, r.received_at DESC LIMIT 15"""), {"u": uid}).all()
    out.append(f"\nNeeds action ({len(pos)}):")
    out += [f"- {r.cls}: {r.company_name or 'unmatched'} {('- ' + r.title) if r.title else ''}"
            f"{(' (due ' + format(r.deadline, '%d %b') + ')') if r.deadline else ''}" for r in pos] or ["- nothing"]

    rq = dict(s.execute(text("""SELECT status, count(*) FROM referral_queue WHERE user_id = :u
                                AND status NOT IN ('applied', 'closed', 'skipped') GROUP BY status"""),
                        {"u": uid}).all())
    applying_today = s.execute(text("""SELECT count(*) FROM referral_queue WHERE user_id = :u AND status = 'sent'
                                       AND apply_at < now() + interval '24 hours'"""), {"u": uid}).scalar()
    if rq:
        out.append("\nReferral queue: " + ", ".join(f"{k} {v}" for k, v in sorted(rq.items()))
                   + f"; applying in next 24h: {applying_today}")

    wl = waiting_lines(s, uid, 10)
    out.append(f"\nWaiting list (top {len(wl)}):")
    out += wl or ["- empty"]

    stretch = s.execute(text("""SELECT company_name, title, missing_skills FROM jobs WHERE user_id = :u AND bucket = 0
                                AND ingested_on >= current_date - 1 ORDER BY score DESC LIMIT 5"""), {"u": uid}).all()
    if stretch:
        out.append("\nStretch jobs (prep needed):")
        out += [f"- {r.company_name}: {r.title} | missing {', '.join(r.missing_skills[:4]) or 'n/a'}"
                f" (~{prep_days(list(r.missing_skills))} days)" for r in stretch]

    credits = s.execute(text("""SELECT provider, credits_used, credits_limit, exhausted_until FROM provider_usage
                                WHERE user_id = :u ORDER BY provider"""), {"u": uid}).all()
    if credits:
        out.append("\nEmail-finder credits: " + ", ".join(
            f"{r.provider} {'exhausted' if r.exhausted_until else (str(r.credits_limit - r.credits_used) + ' left' if r.credits_limit else str(r.credits_used) + ' used')}"
            for r in credits))

    paused = s.execute(text("SELECT adapter FROM adapter_health WHERE status = 'paused'")).scalars().all()
    if paused:
        out.append(f"\nPaused adapters: {', '.join(paused)}")
    size_mb = s.execute(text("SELECT pg_database_size(current_database())")).scalar() / 2**20
    out.append(f"\nStorage: {size_mb:.0f} MB of {cfg.db_budget_mb} MB")
    return "\n".join(out)[:4000]
