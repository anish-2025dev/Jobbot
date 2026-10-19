"""Referral queue lifecycle (sections 10.4, C14, C15).

  enqueue  -> one company_cooldown slot reserved, company protected from eviction
  finding  -> contacts from the 90-day cache, then providers (rotation, verification)
  awaiting_approval (review mode; forced for the first OUTREACH_REVIEW_DAYS)
           -> one-tap approve/edit/skip per draft (Telegram / dashboard)
  scheduled-> sends at 9-11 am recipient time, 5-15 min apart, <= daily cap
  sent     -> apply_at = last send + REFERRAL_HOLD_HOURS (24 h, C15)
  referred -> apply immediately via the referral link / referrer field
  applying -> hold released; the apply worker submits (with or without referral)
  applied | closed | skipped

The separate referral queue never counts toward the 30-entry waiting list (C8).
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.apply import gate
from app.config import get_settings
from app.crypto import keyed_hash64
from app.outreach import compose as C
from app.outreach import contacts as K
from app.outreach import providers as P
from app.outreach import schedule as S

log = logging.getLogger(__name__)
Notify = Callable[..., None]
OPEN = ("finding", "awaiting_approval", "scheduled", "sent", "referred")


def _settings(s: Session, user_id: UUID) -> dict:
    return s.execute(text("SELECT settings FROM users WHERE id = :u"), {"u": user_id}).scalar() or {}


def _set_setting(s: Session, user_id: UUID, key: str, value) -> None:
    s.execute(text("""UPDATE users SET settings = jsonb_set(coalesce(settings, '{}'), CAST(:p AS text[]),
                      CAST(:v AS jsonb)) WHERE id = :u"""),
              {"p": "{" + key + "}", "v": json.dumps(value), "u": user_id})


def effective_mode(st: dict, now: datetime | None = None) -> str:
    """Review is forced for the first week after outreach is enabled."""
    now = now or datetime.now(timezone.utc)
    started = st.get("outreach_enabled_at")
    if started:
        try:
            if now - datetime.fromisoformat(started) < timedelta(days=get_settings().outreach_review_days):
                return "review"
        except ValueError:
            return "review"
    else:
        return "review"
    return st.get("outreach_mode", "review")


def outreach_paused(st: dict, now: datetime | None = None) -> bool:
    until = st.get("outreach_paused_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > (now or datetime.now(timezone.utc))
    except ValueError:
        return True


def can_send_mail(s: Session, user_id: UUID) -> bool:
    return s.execute(text("""SELECT 1 FROM credentials WHERE user_id = :u AND host IN ('gmail', 'imap')"""),
                     {"u": user_id}).first() is not None


def try_enqueue(s: Session, user_id: UUID, application_id: int) -> bool:
    cfg = get_settings()
    st = _settings(s, user_id)
    if not st.get("outreach") or st.get("paused") or outreach_paused(st) or not can_send_mail(s, user_id):
        return False
    if not P.load_providers(s, user_id) and not s.execute(text(
            "SELECT 1 FROM outreach_contacts WHERE user_id = :u LIMIT 1"), {"u": user_id}).first():
        return False
    today = s.execute(text("""SELECT count(*) FROM referral_queue WHERE user_id = :u
                              AND created_at > date_trunc('day', now())"""), {"u": user_id}).scalar()
    if today >= cfg.outreach_daily_jobs:                                   # C14: 10 jobs/day
        return False
    app = s.execute(text("SELECT * FROM applications WHERE id = :i AND user_id = :u"),
                    {"i": application_id, "u": user_id}).mappings().first()
    if app is None or app["applied_at"] is not None:
        return False
    if not gate.reserve_referral_slot(s, user_id, app["company_key_hash"]):   # C5 slot for the eventual apply
        return False
    job = {"title": app["title"], "company": app["company_name"], "req_id": app["req_id"],
           "apply_url": app["apply_url"], "location": app["location"], "ats": app["ats"],
           "canonical_hash": app["canonical_hash"], "bucket": app["bucket"]}
    rq = s.execute(text("""INSERT INTO referral_queue (user_id, application_id, canonical_hash, company_key_hash, job,
                           status, slot_reserved)
                           VALUES (:u, :a, :c, :k, CAST(:j AS jsonb), 'finding', true)
                           ON CONFLICT (user_id, canonical_hash) DO NOTHING RETURNING id"""),
                   {"u": user_id, "a": application_id, "c": app["canonical_hash"], "k": app["company_key_hash"],
                    "j": json.dumps(job)}).scalar()
    if rq is None:
        gate.release_referral_slot(s, user_id, app["company_key_hash"])
        return False
    s.execute(text("UPDATE applications SET uses_reserved_slot = true WHERE id = :i"), {"i": application_id})
    return True


def _skip(s: Session, rq: dict, reason: str) -> None:
    """Outreach not possible: the job goes straight to normal application."""
    s.execute(text("""UPDATE referral_queue SET status = 'skipped', closed_at = now(),
                      job = job || jsonb_build_object('skip_reason', CAST(:r AS text)) WHERE id = :i"""),
              {"i": rq["id"], "r": reason})
    if rq["slot_reserved"]:
        gate.release_referral_slot(s, rq["user_id"], rq["company_key_hash"])
        s.execute(text("UPDATE referral_queue SET slot_reserved = false WHERE id = :i"), {"i": rq["id"]})
        s.execute(text("UPDATE applications SET uses_reserved_slot = false WHERE id = :a"), {"a": rq["application_id"]})


def _company_domain(s: Session, company_key_hash: int) -> str | None:
    return s.execute(text("SELECT email_domain FROM companies WHERE company_key_hash = :k"),
                     {"k": company_key_hash}).scalar()


def _recipe_and_vault(s: Session, rq: dict):
    from app.resume.recipe import Recipe
    from app.vault.schema import Vault
    row = s.execute(text("SELECT recipe FROM applications WHERE id = :a"), {"a": rq["application_id"]}).scalar()
    vault = Vault.model_validate(s.execute(text("SELECT data FROM vaults WHERE user_id = :u"),
                                           {"u": rq["user_id"]}).scalar_one())
    return (Recipe.model_validate(row) if row else None), vault


def step_finding(s: Session, rq: dict, notify: Notify | None, transport=None, llm=None) -> None:
    job = rq["job"]
    try:
        found = K.find_contacts(s, rq["user_id"], company=job["company"], company_key_hash=rq["company_key_hash"],
                                domain=_company_domain(s, rq["company_key_hash"]), job_title=job["title"],
                                job_location=job.get("location"), transport=transport)
    except P.AllExhausted:
        return _skip(s, rq, "all email-finder providers out of credits")
    if not found:
        return _skip(s, rq, "no eligible verified contacts")
    recipe, vault = _recipe_and_vault(s, rq)
    if recipe is None:
        return _skip(s, rq, "no resume recipe")
    drafts = []
    for f in found:
        subject, body = C.compose(vault, recipe, role=f.role, first_name=f.name.split()[0] or "there", job=job)
        body = C.polish_with_llm(subject, body, llm)
        drafts.append({"contact_id": f.contact_id, "role": f.role, "name": f.name, "email": f.email,
                       "title": f.title, "location": f.location, "subject": subject, "body": body,
                       "status": "drafted"})
    st = _settings(s, rq["user_id"])
    mode = effective_mode(st)
    if mode == "auto":
        for d in drafts:
            d["status"] = "approved"
    s.execute(text("""UPDATE referral_queue SET contacts = CAST(:c AS jsonb),
                      status = CAST(:st AS referral_queue_status) WHERE id = :i"""),
              {"c": json.dumps(drafts), "st": "awaiting_approval" if mode == "review" else "scheduled", "i": rq["id"]})
    if mode == "review" and notify:
        for i, d in enumerate(drafts):
            notify(rq["user_id"], f"Referral draft ({d['role']}) to {d['name']}, {d['title']} at {job['company']}:\n\n"
                                  f"Subject: {d['subject']}\n\n{d['body']}",
                   buttons=[[("Approve", f"ok:{rq['id']}:{i}"), ("Skip", f"skip:{rq['id']}:{i}")]])
    if mode == "auto":
        _schedule(s, rq["id"])


def decide(s: Session, user_id: UUID, rq_id: int, index: int, approve: bool, *, subject: str | None = None,
           body: str | None = None) -> str:
    rq = s.execute(text("SELECT * FROM referral_queue WHERE id = :i AND user_id = :u FOR UPDATE"),
                   {"i": rq_id, "u": user_id}).mappings().first()
    if rq is None or rq["status"] != "awaiting_approval":
        return "This draft is no longer pending."
    contacts = list(rq["contacts"])
    if not 0 <= index < len(contacts):
        return "Unknown draft."
    c = contacts[index]
    if body is not None:
        if C.OPT_OUT not in body:
            body = body.rstrip() + "\n\n" + C.OPT_OUT          # the opt-out line is mandatory
        if C.words(body) >= C.MAX_WORDS:
            return f"Edited email must be under {C.MAX_WORDS} words."
        c["body"] = body
    if subject:
        c["subject"] = subject
    c["status"] = "approved" if approve else "skipped"
    contacts[index] = c
    s.execute(text("UPDATE referral_queue SET contacts = CAST(:c AS jsonb) WHERE id = :i"),
              {"c": json.dumps(contacts), "i": rq_id})
    if all(x["status"] != "drafted" for x in contacts):
        if any(x["status"] == "approved" for x in contacts):
            s.execute(text("UPDATE referral_queue SET status = 'scheduled' WHERE id = :i"), {"i": rq_id})
            _schedule(s, rq_id)
        else:
            _skip(s, dict(rq), "all drafts skipped")
    return "Approved." if approve else "Skipped."


def _sent_today(s: Session, user_id: UUID) -> int:
    return s.execute(text("""SELECT count(*) FROM outreach_log WHERE user_id = :u
                             AND sent_at > date_trunc('day', now())"""), {"u": user_id}).scalar()


def _schedule(s: Session, rq_id: int, now: datetime | None = None, rnd: random.Random | None = None) -> None:
    rq = s.execute(text("SELECT * FROM referral_queue WHERE id = :i"), {"i": rq_id}).mappings().one()
    tz = s.execute(text("""SELECT coalesce(c.timezone, u.timezone) FROM users u
                           LEFT JOIN companies c ON c.company_key_hash = :k WHERE u.id = :u"""),
                   {"k": rq["company_key_hash"], "u": rq["user_id"]}).scalar() or "Asia/Kolkata"
    contacts = list(rq["contacts"])
    approved = [c for c in contacts if c["status"] == "approved"]
    times = S.plan_sends(approved, now=now or datetime.now(timezone.utc), fallback_tz=tz,
                         already_today=_sent_today(s, rq["user_id"]), daily_cap=get_settings().outreach_daily_emails,
                         rnd=rnd)
    for c, t in zip(approved, times):
        c["scheduled_at"] = t.isoformat()
        c["status"] = "scheduled"
    s.execute(text("UPDATE referral_queue SET contacts = CAST(:c AS jsonb) WHERE id = :i"),
              {"c": json.dumps(contacts), "i": rq_id})


def step_send(s: Session, rq: dict, client, *, now: datetime | None = None, notify: Notify | None = None) -> int:
    """Send due emails for one queue item; returns how many were sent."""
    cfg = get_settings()
    now = now or datetime.now(timezone.utc)
    uid = rq["user_id"]
    st = _settings(s, uid)
    if outreach_paused(st, now):
        return 0
    contacts = list(rq["contacts"])
    recipe, vault = _recipe_and_vault(s, rq)
    from app.mail.accounts import mail_address
    from_addr = mail_address(s, uid) or vault.personal.email
    sent = 0
    pdf_bytes: bytes | None = None
    for c in contacts:
        if c["status"] != "scheduled" or datetime.fromisoformat(c["scheduled_at"]) > now:
            continue
        if _sent_today(s, uid) >= cfg.outreach_daily_emails:              # C14: 30 emails/day
            c["scheduled_at"] = S.next_slot(S.tz_for(c.get("location"), "Asia/Kolkata"),
                                            now.replace(hour=0, minute=0) + timedelta(days=1)).isoformat()
            continue
        why = K.blocked(s, uid, c["email"])                                 # re-check right before sending
        if why:
            c["status"] = "skipped"
            c["skip_reason"] = why
            continue
        if pdf_bytes is None and recipe is not None:
            from app.resume.engine import render_pdf
            pdf_bytes = render_pdf(vault, recipe).pdf
        raw, mid = C.build_mime(from_name=vault.personal.full_name, from_addr=from_addr, to_name=c["name"],
                                to_addr=c["email"], subject=c["subject"], body=c["body"], attachment=pdf_bytes,
                                attachment_name=f"{vault.personal.full_name.replace(' ', '_')}_Resume.pdf")
        res = client.send(raw)
        try:
            client.modify(res["id"], add=["JobBot/Outreach"])               # stays in Sent, labeled
        except Exception as e:
            log.warning("labeling sent outreach failed: %s", e)
        c.update(status="sent", sent_at=now.isoformat(), message_id=mid, gmail_message_id=res.get("id"),
                 gmail_thread_id=res.get("threadId"))
        s.execute(text("""INSERT INTO outreach_log (user_id, recipient_hash, company_key_hash, sent_at, gmail_message_id)
                          VALUES (:u, :h, :k, :t, :g)"""),
                  {"u": uid, "h": keyed_hash64(c["email"]), "k": rq["company_key_hash"], "t": now, "g": res.get("id")})
        s.execute(text("UPDATE outreach_contacts SET last_contacted_at = :t WHERE id = :i"),
                  {"t": now, "i": c["contact_id"]})
        sent += 1
    pending = [c for c in contacts if c["status"] == "scheduled"]
    done = [c for c in contacts if c["status"] == "sent"]
    params = {"c": json.dumps(contacts), "i": rq["id"]}
    if not pending:
        if done:
            last = max(datetime.fromisoformat(c["sent_at"]) for c in done)
            s.execute(text("""UPDATE referral_queue SET contacts = CAST(:c AS jsonb), status = 'sent', sent_at = :t,
                              apply_at = :t + make_interval(hours => :h) WHERE id = :i"""),
                      {**params, "t": last, "h": cfg.referral_hold_hours})
        else:
            s.execute(text("UPDATE referral_queue SET contacts = CAST(:c AS jsonb) WHERE id = :i"), params)
            _skip(s, rq, "no contact could be emailed")
    else:
        s.execute(text("UPDATE referral_queue SET contacts = CAST(:c AS jsonb) WHERE id = :i"), params)
    return sent


def job_still_open(rq: dict, transport=None) -> bool | None:
    """Check by req_id via the ATS API before applying (section 10.4.5).
    None = can't tell (apply anyway; the adapter will see a closed page)."""
    from app.ingest.detect import detect
    from app.ingest.http import NotFoundError, SourceError, make_fetcher
    job = rq["job"]
    det = detect(job.get("apply_url"))
    if det is None or det.ats not in ("greenhouse", "lever", "ashby") or not det.slug:
        return None
    f = make_fetcher(transport)
    try:
        if det.ats == "greenhouse" and det.req_id:
            f.get_json("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{det.slug}/jobs/{det.req_id}")
        elif det.ats == "lever" and det.req_id:
            f.get_json("lever", f"https://api.lever.co/v0/postings/{det.slug}/{det.req_id}")
        elif det.ats == "ashby":
            data = f.get_json("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{det.slug}")
            return any(j.get("id") == det.req_id for j in data.get("jobs", []))
        return True
    except NotFoundError:
        return False
    except SourceError:
        return None


def release_hold(s: Session, rq: dict, *, referral: dict | None = None, transport=None) -> str:
    """Hold over (apply_at reached) or referral received: hand the job to the
    apply worker. Closed jobs are closed; contacts stay cached."""
    if job_still_open(rq, transport) is False:
        s.execute(text("UPDATE referral_queue SET status = 'closed', closed_at = now() WHERE id = :i"), {"i": rq["id"]})
        s.execute(text("UPDATE applications SET status = 'closed', failure_reason = 'job closed during referral hold' "
                       "WHERE id = :a"), {"a": rq["application_id"]})
        if rq["slot_reserved"]:
            gate.release_referral_slot(s, rq["user_id"], rq["company_key_hash"])
        return "closed"
    upd = {"a": rq["application_id"]}
    if referral:
        s.execute(text("""UPDATE applications SET via_referral = true, referrer = :by,
                          apply_url = coalesce(:link, apply_url), status = 'queued', not_before = NULL
                          WHERE id = :a"""), {**upd, "by": referral.get("by"), "link": referral.get("link")})
    else:
        s.execute(text("UPDATE applications SET status = 'queued', not_before = NULL WHERE id = :a"), upd)
    s.execute(text("UPDATE referral_queue SET status = 'applying' WHERE id = :i"), {"i": rq["id"]})
    return "applying"


def on_reply(s: Session, user_id: UUID, match, cls, msg, notify: Notify | None = None) -> None:
    """Mail-processor hook for replies in outreach threads (section 10.4.2)."""
    if match.referral_id is None or match.contact_index is None:
        return
    rq = s.execute(text("SELECT * FROM referral_queue WHERE id = :i AND user_id = :u FOR UPDATE"),
                   {"i": match.referral_id, "u": user_id}).mappings().first()
    if rq is None:
        return
    contacts = list(rq["contacts"])
    c = contacts[match.contact_index]
    if cls.cls == "outreach_decline":
        K.add_dnc(s, user_id, c["email"])
        c["status"] = "declined"
    elif cls.cls == "referral_given":
        c["status"] = "referred"
        s.execute(text("UPDATE outreach_contacts SET replied_positive = true WHERE id = :i"), {"i": c["contact_id"]})
        referral = {"by": c["name"], "link": cls.link, "at": datetime.now(timezone.utc).isoformat()}
        import re as _re
        if not cls.link and _re.search(r"submitted (a |your )?referral|referred you (in|through|via) (our|the) (system|portal)",
                                       msg.body or "", _re.I):
            # The employee submitted it internally: the company will invite the
            # candidate. Applying separately could create a duplicate profile.
            referral["submitted_by_employee"] = True
        app = s.execute(text("SELECT status, applied_at FROM applications WHERE id = :a"),
                        {"a": rq["application_id"]}).mappings().first()
        if app and app["applied_at"] is not None:
            # referral arrived after we applied (10.4.4)
            s.execute(text("UPDATE applications SET status = 'applied_pending_referral' WHERE id = :a"),
                      {"a": rq["application_id"]})
            if notify:
                job = rq["job"]
                notify(user_id, f"{c['name']} referred you for {job['title']} at {job['company']}, but the bot had already "
                                f"applied. Suggested reply:\n\nThanks so much! I've already applied (req {job.get('req_id')}, "
                                f"{app['applied_at']:%d %b}); could you flag my profile to the hiring team?")
        else:
            s.execute(text("UPDATE referral_queue SET status = 'referred', referral = CAST(:r AS jsonb) WHERE id = :i"),
                      {"r": json.dumps(referral), "i": rq["id"]})
            if referral.get("submitted_by_employee"):
                if notify:
                    notify(user_id, f"{c['name']} submitted a referral for {rq['job']['title']} at {rq['job']['company']}. "
                                    "Watch for the company's invite email; the bot will not apply separately.")
            else:
                release_hold(s, {**dict(rq), "contacts": contacts}, referral=referral)
                if notify:
                    notify(user_id, f"Referral from {c['name']} for {rq['job']['title']} at {rq['job']['company']}. "
                                    "Applying now with the referral.")
    else:
        c["status"] = "replied"
        if cls.positive:
            s.execute(text("UPDATE outreach_contacts SET replied_positive = true WHERE id = :i"), {"i": c["contact_id"]})
    contacts[match.contact_index] = c
    s.execute(text("UPDATE referral_queue SET contacts = CAST(:c AS jsonb) WHERE id = :i"),
              {"c": json.dumps(contacts), "i": rq["id"]})


def check_reputation(s: Session, user_id: UUID, notify: Notify | None = None) -> bool:
    """Section 13: bounce rate > 3% over 30 days, or any spam complaint ->
    pause outreach 7 days and alert. Returns True if paused now."""
    cfg = get_settings()
    row = s.execute(text("""SELECT count(*) AS n, count(*) FILTER (WHERE status = 1) AS bounced,
                                   count(*) FILTER (WHERE status = 2) AS complaints
                            FROM outreach_log WHERE user_id = :u AND sent_at > now() - interval '30 days'"""),
                    {"u": user_id}).first()
    if not row.n:
        return False
    rate = row.bounced / row.n
    if rate > cfg.bounce_pause_rate or row.complaints:
        st = _settings(s, user_id)
        if outreach_paused(st):
            return False
        until = datetime.now(timezone.utc) + timedelta(days=cfg.outreach_pause_days)
        _set_setting(s, user_id, "outreach_paused_until", until.isoformat())
        if notify:
            notify(user_id, f"Outreach paused for {cfg.outreach_pause_days} days: bounce rate {rate:.0%}"
                            f"{' and a spam complaint' if row.complaints else ''}. This protects your sender reputation.")
        return True
    return False


def tick(s_factory, *, now: datetime | None = None, notify: Notify | None = None, mail_client_for=None,
         transport=None, llm=None) -> dict:
    """Advance every open queue item one step. Runs every few minutes."""
    now = now or datetime.now(timezone.utc)
    stats = {"finding": 0, "sent": 0, "released": 0, "applied": 0, "timed_out": 0}
    with s_factory() as s:
        rows = [dict(r) for r in s.execute(text(
            "SELECT * FROM referral_queue WHERE status IN ('finding', 'awaiting_approval', 'scheduled', 'sent', 'applying')"
        )).mappings()]
    for rq in rows:
        with s_factory() as s:
            rq = dict(s.execute(text("SELECT * FROM referral_queue WHERE id = :i FOR UPDATE SKIP LOCKED"),
                                {"i": rq["id"]}).mappings().first() or {})
            if not rq:
                continue
            st = rq["status"]
            if st == "finding":
                step_finding(s, rq, notify, transport, llm)
                stats["finding"] += 1
            elif st == "awaiting_approval":
                if rq["created_at"] < now - timedelta(hours=get_settings().outreach_approval_timeout_hours):
                    _skip(s, rq, "drafts not approved in time")
                    stats["timed_out"] += 1
            elif st == "scheduled":
                client = mail_client_for(s, rq["user_id"]) if mail_client_for else None
                if client is not None:
                    stats["sent"] += step_send(s, rq, client, now=now, notify=notify)
            elif st == "sent" and rq["apply_at"] and rq["apply_at"] <= now:
                release_hold(s, rq, transport=transport)                   # C15: apply at T+24h without referral
                stats["released"] += 1
            elif st == "applying":
                done = s.execute(text("SELECT applied_at FROM applications WHERE id = :a"),
                                 {"a": rq["application_id"]}).scalar()
                if done is not None:
                    s.execute(text("UPDATE referral_queue SET status = 'applied', closed_at = now() WHERE id = :i"),
                              {"i": rq["id"]})
                    stats["applied"] += 1
    return stats
