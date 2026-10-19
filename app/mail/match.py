"""Match an email to the bot's own activity (section 11): outreach thread,
application, job alert, or a sign-in/OTP the bot triggered. Mail that
matches nothing is never modified (section 11.1)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.llm.client import LLM, LLMError, LLMUnavailable
from app.mail.classify import ALERT_SENDERS, ATS_SENDERS
from app.mail.message import Message
from app.util.hashing import company_key
from app.util.text import content_words, jaccard

PERSONAL_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.co.in",
                    "icloud.com", "live.com", "rediffmail.com", "protonmail.com", "proton.me", "zoho.com"}


@dataclass
class Match:
    kind: str                         # outreach | application | alert | signin | none
    application_id: int | None = None
    referral_id: int | None = None
    contact_index: int | None = None
    confidence: float = 0.0
    how: str = ""


@dataclass
class UserMailContext:
    user_id: UUID
    outreach_threads: dict[str, tuple[int, int, int]] = field(default_factory=dict)   # thread -> (rq, idx, app)
    outreach_message_ids: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    app_threads: dict[str, int] = field(default_factory=dict)
    apps: list[dict] = field(default_factory=list)
    waiting_domains: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, s: Session, user_id: UUID, waiting_domains: set[str] | None = None) -> "UserMailContext":
        ctx = cls(user_id=user_id, waiting_domains=waiting_domains or set())
        for r in s.execute(text("""SELECT id, application_id, contacts FROM referral_queue
                                   WHERE user_id = :u AND status NOT IN ('closed', 'skipped')"""), {"u": user_id}):
            for i, c in enumerate(r.contacts or []):
                if c.get("gmail_thread_id"):
                    ctx.outreach_threads[c["gmail_thread_id"]] = (r.id, i, r.application_id)
                if c.get("message_id"):
                    ctx.outreach_message_ids[c["message_id"]] = (r.id, i, r.application_id)
        rows = s.execute(text("""
            SELECT a.id, a.company_name, a.company_key_hash, a.title, a.apply_url, a.gmail_thread_id, a.ats,
                   a.status, c.email_domain
            FROM applications a LEFT JOIN companies c ON c.company_key_hash = a.company_key_hash
            WHERE a.user_id = :u AND a.deleted_at IS NULL
              AND (a.applied_at IS NULL OR a.applied_at > now() - interval '60 days')
            ORDER BY a.applied_at DESC NULLS LAST LIMIT 2000"""), {"u": user_id}).mappings().all()
        for r in rows:
            d = dict(r)
            d["key_tokens"] = [t for t in company_key(d["company_name"]).split() if len(t) > 2]
            ctx.apps.append(d)
            if d["gmail_thread_id"]:
                ctx.app_threads[d["gmail_thread_id"]] = d["id"]
        return ctx


def _domain_matches(domain: str, app: dict) -> bool:
    if app.get("email_domain") and (domain == app["email_domain"] or domain.endswith("." + app["email_domain"])):
        return True
    base = domain.split(".")
    return any(tok in base or any(tok in part for part in base[:-1]) for tok in app["key_tokens"][:2])


def _mentions(blob: str, app: dict) -> bool:
    name = company_key(app["company_name"])
    return bool(name) and re.search(rf"\b{re.escape(name)}\b", blob) is not None


def _best_by_title(msg: Message, cands: list[dict]) -> dict:
    words = content_words(f"{msg.subject} {msg.body[:2000]}")
    return max(cands, key=lambda a: jaccard(words, content_words(a["title"])))


def match(msg: Message, ctx: UserMailContext, llm: LLM | None = None) -> Match:
    if msg.thread_id and msg.thread_id in ctx.outreach_threads:
        rq, idx, app = ctx.outreach_threads[msg.thread_id]
        return Match("outreach", application_id=app, referral_id=rq, contact_index=idx, confidence=1.0, how="thread")
    for ref in [msg.in_reply_to, *msg.references]:
        if ref and ref in ctx.outreach_message_ids:
            rq, idx, app = ctx.outreach_message_ids[ref]
            return Match("outreach", application_id=app, referral_id=rq, contact_index=idx, confidence=0.98,
                         how="in-reply-to")
    if msg.thread_id and msg.thread_id in ctx.app_threads:
        return Match("application", application_id=ctx.app_threads[msg.thread_id], confidence=1.0, how="thread")
    if ALERT_SENDERS.search(msg.from_addr):
        return Match("alert", confidence=0.95, how="alert sender")

    domain = msg.domain
    blob = company_key(f"{msg.from_name} {msg.subject} {msg.body[:4000]}")
    if any(domain == d or domain.endswith("." + d) for d in ctx.waiting_domains):
        return Match("signin", confidence=0.9, how="pending sign-in")

    cands: list[dict] = []
    how = ""
    if ATS_SENDERS.search(domain):
        cands = [a for a in ctx.apps if _mentions(blob, a)]
        how = "ats sender + company named"
    elif domain and domain not in PERSONAL_DOMAINS:
        cands = [a for a in ctx.apps if _domain_matches(domain, a)]
        how = "company domain"
    else:
        cands = [a for a in ctx.apps if _mentions(blob, a)]
        how = "personal sender + company named"
    if len(cands) == 1:
        return Match("application", application_id=cands[0]["id"], confidence=0.85, how=how)
    if len(cands) > 1:
        return Match("application", application_id=_best_by_title(msg, cands)["id"], confidence=0.7, how=how + " + title")
    if domain in PERSONAL_DOMAINS and llm is not None and llm.available and ctx.apps:
        m = _llm_match(msg, ctx.apps[:40], llm)
        if m:
            return m
    return Match("none")


def _llm_match(msg: Message, apps: list[dict], llm: LLM) -> Match | None:
    listing = "\n".join(f"{a['id']}: {a['company_name']} - {a['title']}" for a in apps)
    prompt = (f"An email from a personal address. Which of these job applications is it about, if any?\n"
              f"{listing}\n\nFrom: {msg.from_name}\nSubject: {msg.subject}\n\n{msg.body[:2500]}\n\n"
              'Return JSON {"application_id": <id or null>, "confidence": 0-1}.')
    try:
        data = llm.complete_json(prompt, tier="cheap", max_tokens=80)
    except (LLMError, LLMUnavailable):
        return None
    ids = {a["id"] for a in apps}
    aid = data.get("application_id") if isinstance(data, dict) else None
    if aid in ids and float(data.get("confidence") or 0) >= 0.6:
        return Match("application", application_id=aid, confidence=0.6, how="llm")
    return None
