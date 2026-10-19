"""Mail ingestion + cleanup by priority (sections 11, 11.1), every 15 min.

| P0 | offer                                   | never auto-trashed; starred + "JobBot/Offer"        |
| P1 | interview, test, recruiter, info request,| starred + "JobBot/Action"; kept >= 7 days and until |
|    | referral given, outreach question        | the user marks done; deadline reminders              |
| P2 | unclear / positive-looking              | kept and alerted like P1                             |
| P3 | rejection, outreach decline             | trashed instantly                                    |
| P4 | confirmation, OTP, verification, account | trashed instantly after processing                   |
|    | created, parsed job alerts, bounces      |                                                     |

Only mail matched to the bot's own applications, outreach, alerts, OTPs or
sign-ins is ever modified. Unmatched mail is read (to alert on a clear
positive) but never labeled, starred or trashed. Every action is logged in
mail_actions and can be undone for 30 days.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import keyed_hash64
from app.ingest.models import RawJob
from app.llm.client import LLM
from app.mail import otp as otp_store
from app.mail.alerts_parse import parse_alert
from app.mail.classify import POSITIVE, Classification, classify
from app.mail.match import Match, UserMailContext, match
from app.mail.message import Message
from app.redis_client import get_redis

log = logging.getLogger(__name__)
Notify = Callable[..., None]

PRIORITY = {"offer": 0, "interview": 1, "test": 1, "recruiter": 1, "info_request": 1, "referral_given": 1,
            "outreach_question": 1, "unclear": 2, "rejection": 3, "outreach_decline": 3,
            "confirmation": 4, "otp": 4, "verification": 4, "account_created": 4, "job_alert": 4, "bounce": 4}
LABEL_OFFER, LABEL_ACTION, LABEL_OUTREACH = "JobBot/Offer", "JobBot/Action", "JobBot/Outreach"


@dataclass
class MailReport:
    scanned: int = 0
    matched: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    trashed: int = 0
    starred: int = 0
    alerts: int = 0
    alert_jobs: list[RawJob] = field(default_factory=list)
    untouched: int = 0
    errors: list[str] = field(default_factory=list)


def _seen_key(user_id) -> str:
    return f"mail:seen:{user_id}"


def already_processed(s: Session, user_id: UUID, gmail_id: str) -> bool:
    r = get_redis()
    if r is not None and r.sismember(_seen_key(user_id), gmail_id):
        return True
    return s.execute(text("""SELECT 1 FROM mail_actions WHERE user_id = :u AND gmail_id = :g
                             UNION ALL SELECT 1 FROM responses WHERE user_id = :u AND gmail_id = :g LIMIT 1"""),
                     {"u": user_id, "g": gmail_id}).first() is not None


def mark_processed(user_id, gmail_id: str) -> None:
    r = get_redis()
    if r is not None:
        r.sadd(_seen_key(user_id), gmail_id)
        r.expire(_seen_key(user_id), 7 * 86400)


def log_action(s: Session, user_id: UUID, gmail_id: str, priority: int, action: str) -> int:
    return s.execute(text("""INSERT INTO mail_actions (user_id, gmail_id, priority, action)
                             VALUES (:u, :g, :p, CAST(:a AS mail_action)) RETURNING id"""),
                     {"u": user_id, "g": gmail_id, "p": priority, "a": action}).scalar_one()


def trash(client, s: Session, user_id: UUID, msg_id: str, priority: int) -> None:
    if get_settings().mail_delete_mode == "permanent" and hasattr(client, "delete"):
        client.delete(msg_id)
    else:
        client.trash(msg_id)
    log_action(s, user_id, msg_id, priority, "trash")


def star_label(client, s: Session, user_id: UUID, msg_id: str, priority: int, label: str) -> None:
    client.modify(msg_id, add=["STARRED", label])
    log_action(s, user_id, msg_id, priority, "star_label")


def undo(client, s: Session, user_id: UUID, action_id: int) -> bool:
    row = s.execute(text("""SELECT gmail_id, action, undone_at, acted_at FROM mail_actions
                            WHERE id = :i AND user_id = :u"""), {"i": action_id, "u": user_id}).first()
    if row is None or row.undone_at is not None or row.acted_at < datetime.now(timezone.utc) - timedelta(days=30):
        return False
    if row.action == "trash":
        client.untrash(row.gmail_id)
    elif row.action == "star_label":
        client.modify(row.gmail_id, remove=["STARRED"])
    s.execute(text("UPDATE mail_actions SET undone_at = now() WHERE id = :i"), {"i": action_id})
    log_action(s, user_id, row.gmail_id, 4, "restore")
    return True


def _record_response(s: Session, user_id: UUID, msg: Message, c: Classification, m: Match, priority: int) -> int | None:
    keep = (msg.date or datetime.now(timezone.utc)) + timedelta(days=get_settings().positive_mail_min_days)
    cls = c.cls if c.cls in ("offer", "interview", "test", "recruiter", "info_request", "referral_given",
                             "outreach_question") else "unclear"
    return s.execute(text("""
        INSERT INTO responses (user_id, application_id, class, priority, deadline, snippet, gmail_id,
                               gmail_thread_id, received_at, keep_until)
        VALUES (:u, :a, CAST(:c AS response_class), :p, :d, :sn, :g, :t, :r, :k)
        ON CONFLICT (user_id, gmail_id) DO NOTHING RETURNING id"""),
        {"u": user_id, "a": m.application_id, "c": cls, "p": priority, "d": c.deadline,
         "sn": (msg.snippet or msg.body)[:500], "g": msg.id, "t": msg.thread_id,
         "r": msg.date or datetime.now(timezone.utc), "k": keep}).scalar()


def _alert_text(msg: Message, c: Classification, app: dict | None) -> str:
    head = {"offer": "Offer", "interview": "Interview", "test": "Test / assessment", "recruiter": "Recruiter",
            "info_request": "Info requested", "referral_given": "Referral received",
            "outreach_question": "Reply to your referral ask", "unclear": "Possible positive reply"}.get(c.cls, c.cls)
    lines = [f"{head}: {app['company_name']} - {app['title']}" if app else f"{head}: {msg.from_name or msg.from_addr}",
             f"Subject: {msg.subject[:120]}"]
    if c.deadline:
        lines.append(f"Deadline: {c.deadline:%d %b %Y %H:%M} UTC")
    if c.link:
        lines.append(c.link)
    return "\n".join(lines)


def process_message(client, s: Session, ctx: UserMailContext, msg: Message, rep: MailReport, *,
                    notify: Notify | None = None, llm: LLM | None = None, now: datetime | None = None,
                    on_outreach_reply: Callable | None = None) -> None:
    uid = ctx.user_id
    m = match(msg, ctx, llm)
    context = "outreach" if m.kind == "outreach" else "application" if m.kind == "application" else None
    c = classify(msg, context=context, now=now, llm=llm)
    rep.by_class[c.cls] = rep.by_class.get(c.cls, 0) + 1
    p = PRIORITY.get(c.cls, 2)

    if m.kind == "none" and c.cls == "bounce" and c.bounced_recipient:
        # A bounce of one of our own outreach emails is our mail.
        hit = s.execute(text("""SELECT 1 FROM outreach_log WHERE user_id = :u AND recipient_hash = :h LIMIT 1"""),
                        {"u": uid, "h": keyed_hash64(c.bounced_recipient)}).first()
        if hit:
            m = Match("outreach", confidence=0.9, how="bounce of outreach email")
    if m.kind == "none":
        # Never modified. Alert only on a clear positive (not "unclear": that
        # would alert on every personal email).
        if c.cls in POSITIVE and c.confidence >= 0.75:
            if _record_response(s, uid, msg, c, m, 2) and notify:
                notify(uid, _alert_text(msg, c, None))
                rep.alerts += 1
        rep.untouched += 1
        return
    rep.matched += 1
    app = next((a for a in ctx.apps if a["id"] == m.application_id), None)

    if m.kind == "alert" and c.cls == "job_alert":
        rep.alert_jobs += parse_alert(msg)
        trash(client, s, uid, msg.id, 4)
        rep.trashed += 1
        return
    if m.kind == "signin" or c.cls in ("otp", "verification", "account_created"):
        if c.otp:
            otp_store.put_code(uid, msg.domain, c.otp)
        if c.link:
            otp_store.put_link(uid, msg.domain, c.link)
        trash(client, s, uid, msg.id, 4)              # OTP mail gone right after the code is stored
        rep.trashed += 1
        return
    if c.cls == "bounce":
        if c.bounced_recipient:
            s.execute(text("""UPDATE outreach_log SET status = 1 WHERE user_id = :u AND recipient_hash = :h
                              AND status = 0"""), {"u": uid, "h": keyed_hash64(c.bounced_recipient)})
        trash(client, s, uid, msg.id, 4)
        rep.trashed += 1
        return

    if m.kind == "outreach" and on_outreach_reply is not None:
        on_outreach_reply(s, uid, m, c, msg)

    if c.cls == "rejection":
        if m.application_id:
            s.execute(text("""UPDATE applications SET status = 'rejected', deleted_at = now(), last_status_at = now()
                              WHERE id = :a AND user_id = :u AND status <> 'positive'"""),
                      {"a": m.application_id, "u": uid})
        trash(client, s, uid, msg.id, 3)
        rep.trashed += 1
        return
    if c.cls == "outreach_decline":
        trash(client, s, uid, msg.id, 3)
        rep.trashed += 1
        return
    if c.cls == "confirmation":
        if m.application_id:
            s.execute(text("""UPDATE applications SET gmail_thread_id = COALESCE(gmail_thread_id, :t),
                              last_status_at = now() WHERE id = :a AND user_id = :u"""),
                      {"a": m.application_id, "u": uid, "t": msg.thread_id})
        trash(client, s, uid, msg.id, 4)
        rep.trashed += 1
        return

    # P0 / P1 / P2: keep, star, label, alert
    label = LABEL_OFFER if c.cls == "offer" else LABEL_ACTION
    star_label(client, s, uid, msg.id, p, label)
    rep.starred += 1
    # A colleague's reply in an outreach thread is not an employer response;
    # the outreach hook owns those state changes. Only a real positive class moves the
    # application: "unclear" (newsletters, soft rejections, low-confidence labels) is kept
    # and starred above but must not mark the application positive or feed the tuning data.
    if m.application_id and m.kind == "application" and c.cls in POSITIVE:
        s.execute(text("""UPDATE applications SET status = 'positive', last_status_at = now(),
                          gmail_thread_id = COALESCE(gmail_thread_id, :t) WHERE id = :a AND user_id = :u"""),
                  {"a": m.application_id, "u": uid, "t": msg.thread_id})
        _record_positive_outcome(s, m.application_id, c.cls)
    if _record_response(s, uid, msg, c, m, p) and notify:
        notify(uid, _alert_text(msg, c, app), buttons=[[("Mark done", f"done:{msg.id}")]])
        rep.alerts += 1


def _record_positive_outcome(s: Session, application_id: int, cls: str) -> None:
    """Anonymous outcomes_daily + per-user training row, once per application."""
    row = s.execute(text("""UPDATE applications SET outcome_recorded = true WHERE id = :a AND NOT outcome_recorded
                            RETURNING user_id, source_id, bucket, components"""), {"a": application_id}).first()
    if row is None or row.source_id is None or row.bucket is None:
        return
    outcome = {"offer": 4, "interview": 3, "test": 3}.get(cls, 2)
    s.execute(text("""INSERT INTO outcomes_daily (day, score_bucket, source_id, outcome, count)
                      VALUES (current_date, :b, :s, :o, 1)
                      ON CONFLICT (day, score_bucket, source_id, outcome) DO UPDATE SET count = outcomes_daily.count + 1"""),
              {"b": row.bucket, "s": row.source_id, "o": outcome})
    if row.components:
        s.execute(text("INSERT INTO training_examples (user_id, components, label) VALUES (:u, :c, 1)"),
                  {"u": row.user_id, "c": row.components})


def process_mailbox(client, s_factory, user_id: UUID, *, notify: Notify | None = None, llm: LLM | None = None,
                    query: str = "newer_than:2d -in:chats -in:sent", max_messages: int = 200,
                    now: datetime | None = None, on_outreach_reply: Callable | None = None) -> MailReport:
    """s_factory: context manager yielding a Session (one transaction per message)."""
    rep = MailReport()
    ids = client.list_ids(query, max_results=max_messages)
    with s_factory() as s:
        ctx = UserMailContext.load(s, user_id, otp_store.waiting_domains(user_id))
    for mid in ids:
        rep.scanned += 1
        try:
            with s_factory() as s:
                if already_processed(s, user_id, mid):
                    continue
                msg = client.get(mid)
                process_message(client, s, ctx, msg, rep, notify=notify, llm=llm, now=now,
                                on_outreach_reply=on_outreach_reply)
            mark_processed(user_id, mid)
        except Exception as e:      # one bad message never stops the run
            log.exception("mail %s failed", mid)
            rep.errors.append(f"{mid}: {e}")
    return rep


def cleanup_done_positives(client, s: Session, user_id: UUID) -> int:
    """P1/P2 mail is trashed only after the user marked it done AND the
    7-day minimum passed. P0 (offers) never."""
    rows = s.execute(text("""SELECT id, gmail_id, priority FROM responses
                             WHERE user_id = :u AND priority >= 1 AND user_done_at IS NOT NULL
                               AND keep_until < now() AND deleted_at IS NULL"""), {"u": user_id}).all()
    for r in rows:
        trash(client, s, user_id, r.gmail_id, r.priority)
        s.execute(text("UPDATE responses SET deleted_at = now() WHERE id = :i"), {"i": r.id})
    return len(rows)


def due_reminders(s: Session, user_id: UUID, within_hours: int = 24) -> list[dict]:
    """Positives with a deadline in the next `within_hours`, not yet reminded."""
    rows = s.execute(text("""
        SELECT r.id, r.class, r.deadline, a.company_name, a.title FROM responses r
        LEFT JOIN applications a ON a.id = r.application_id
        WHERE r.user_id = :u AND r.user_done_at IS NULL AND r.deleted_at IS NULL
          AND r.deadline BETWEEN now() AND now() + make_interval(hours => :h)"""),
        {"u": user_id, "h": within_hours}).mappings().all()
    r = get_redis()
    out = []
    for row in rows:
        key = f"remind:{row['id']}"
        if r is not None and not r.set(key, "1", nx=True, ex=3 * 86400):
            continue
        out.append(dict(row))
    return out
