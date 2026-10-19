"""Who to contact (section 10.1) and finding verified addresses (10.2).

2-3 people per job, mixed roles, mid-level only: 1 recruiter, 1 engineering
manager / lead for the team, 1 peer engineer (the person most able to submit
an actual referral). Excluded by default: VP, Director, Head of, Chief,
C-level, Founder, Partner.

C14 checks happen here AND again right before each send: never anyone on
the do-not-contact list, never anyone contacted in the last 90 days, at
most 3 contacts per company per 30 days, at most 3 per job.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crypto import keyed_hash64
from app.outreach import providers as P

log = logging.getLogger(__name__)

EXCLUDED = re.compile(r"\b(vp|v\.p\.|vice president|director|head of|chief|ceo|cto|cfo|coo|cio|cpo|cmo|"
                      r"founder|co-founder|cofounder|president|owner|board)\b", re.I)
PARTNER = re.compile(r"\bpartner\b", re.I)       # excluded unless it's a recruiting title ("Talent Partner")
RECRUITER = re.compile(r"recruit|talent acquisition|sourcer|\bhr\b|human resources|people partner|hrbp|"
                       r"campus|university (relations|hiring)|talent partner", re.I)
MANAGER = re.compile(r"engineering manager|software (development |engineering )?manager|development manager|"
                     r"\bem\b|team lead|tech(nical)? lead|lead (software |backend |frontend )?engineer|"
                     r"manager,? (software )?engineering|engineering lead", re.I)
PEER = re.compile(r"software (development )?engineer|\bsde\b|\bswe\b|developer|backend engineer|frontend engineer|"
                  r"full[- ]?stack|member of technical staff|\bmts\b|programmer|platform engineer|data engineer|"
                  r"ml engineer|machine learning engineer|sre|site reliability", re.I)
JUNIOR = re.compile(r"\bintern\b|trainee|apprentice", re.I)
TITLE_QUERY = {
    "recruiter": ["recruiter", "technical recruiter", "talent acquisition"],
    "manager": ["engineering manager", "software engineering manager", "tech lead", "team lead"],
    "peer": ["software engineer", "senior software engineer", "software development engineer", "backend engineer"],
}


def classify_role(title: str) -> str | None:
    t = title or ""
    if not t or EXCLUDED.search(t) or JUNIOR.search(t):
        return None
    if RECRUITER.search(t):
        return "recruiter"
    if PARTNER.search(t):
        return None
    if MANAGER.search(t):
        return "manager"
    if PEER.search(t):
        return "peer"
    return None


def _function_words(job_title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", job_title.lower())
            if w in {"backend", "frontend", "data", "platform", "mobile", "android", "ios", "ml", "infra",
                     "security", "payments", "cloud", "devops", "fullstack", "full", "stack", "ai"}}


def rank_candidates(people: list[P.Person], job_title: str, job_location: str | None) -> dict[str, P.Person]:
    """Best person per role: title matches the job's function, then location."""
    func = _function_words(job_title)
    city = (job_location or "").split(",")[0].strip().lower()
    best: dict[str, tuple[float, P.Person]] = {}
    for p in people:
        role = classify_role(p.title)
        if role is None:
            continue
        score = 0.0
        tw = set(re.findall(r"[a-z]+", p.title.lower()))
        score += 2 * len(func & tw)
        if city and p.location and city in p.location.lower():
            score += 1.5
        if role == "peer" and re.search(r"\b(ii|2|senior|sr)\b", p.title.lower()):
            score += 0.5            # SDE II / Senior SDE can usually refer
        if p.email_verified:
            score += 0.5
        if role not in best or score > best[role][0]:
            best[role] = (score, p)
    return {r: p for r, (_, p) in best.items()}


# --- C14 rules ------------------------------------------------------------------------
def blocked(s: Session, user_id: UUID, email: str) -> str | None:
    h = keyed_hash64(email)
    if s.execute(text("SELECT 1 FROM do_not_contact WHERE user_id = :u AND email_hash = :h"), {"u": user_id, "h": h}).first():
        return "do-not-contact"
    recent = s.execute(text("""SELECT 1 FROM outreach_log WHERE user_id = :u AND recipient_hash = :h
                               AND sent_at > now() - interval '90 days' LIMIT 1"""), {"u": user_id, "h": h}).first()
    contacted = s.execute(text("""SELECT 1 FROM outreach_contacts WHERE user_id = :u AND lower(email) = lower(:e)
                                  AND last_contacted_at > now() - interval '90 days'"""), {"u": user_id, "e": email}).first()
    if recent or contacted:
        return "contacted in the last 90 days"
    return None


def company_contacts_30d(s: Session, user_id: UUID, company_key_hash: int) -> int:
    """Contacts emailed in 30 days + contacts already queued for that company."""
    sent = s.execute(text("""SELECT count(DISTINCT recipient_hash) FROM outreach_log WHERE user_id = :u
                             AND company_key_hash = :k AND sent_at > now() - interval '30 days'"""),
                     {"u": user_id, "k": company_key_hash}).scalar()
    queued = s.execute(text("""SELECT coalesce(sum((SELECT count(*) FROM jsonb_array_elements(contacts) c
                                   WHERE c->>'status' IN ('drafted', 'approved', 'scheduled'))), 0)
                               FROM referral_queue WHERE user_id = :u AND company_key_hash = :k
                               AND status IN ('awaiting_approval', 'scheduled')"""),
                       {"u": user_id, "k": company_key_hash}).scalar()
    return int(sent) + int(queued)


@dataclass
class Found:
    contact_id: int
    role: str
    name: str
    email: str
    title: str
    location: str | None


def _cached(s: Session, user_id: UUID, company_key_hash: int) -> list[dict]:
    return [dict(r) for r in s.execute(text("""
        SELECT id, name, title, role_type, email, location, provider FROM outreach_contacts
        WHERE user_id = :u AND company_key_hash = :k
          AND (created_at > now() - interval '90 days' OR replied_positive)"""),
        {"u": user_id, "k": company_key_hash}).mappings()]


def _store(s: Session, user_id: UUID, company_key_hash: int, p: P.Person, role: str) -> int:
    return s.execute(text("""
        INSERT INTO outreach_contacts (user_id, company_key_hash, name, title, role_type, location, email, provider, verified_at)
        VALUES (:u, :k, :n, :t, CAST(:r AS contact_role), :l, lower(:e), :p, now())
        ON CONFLICT (user_id, email) DO UPDATE SET title = EXCLUDED.title, role_type = EXCLUDED.role_type,
            verified_at = now()
        RETURNING id"""), {"u": user_id, "k": company_key_hash, "n": p.name, "t": p.title, "r": role,
                           "l": p.location, "e": p.email, "p": p.provider}).scalar_one()


def _verify(s: Session, user_id: UUID, provs: list, p: P.Person) -> bool:
    """A verified address from search counts; otherwise ask a verifier.
    'accept_all' (catch-all) and unknown are never used."""
    if p.email_verified and p.email:
        return True
    for prov in provs:
        if not P.usage_ok(s, user_id, prov.name):
            continue
        try:
            status = prov.verify(p.email)
        except P.CreditsExhausted:
            P.mark_exhausted(s, user_id, prov.name)
            continue
        except P.ProviderError as e:
            log.warning("verify via %s failed: %s", prov.name, e)
            continue
        if status is None:
            continue
        P.charge(s, user_id, prov.name)
        return status == "valid"
    return False


def find_contacts(s: Session, user_id: UUID, *, company: str, company_key_hash: int, domain: str | None,
                  job_title: str, job_location: str | None, transport=None) -> list[Found]:
    cfg = get_settings()
    room = min(cfg.outreach_contacts_per_job,
               cfg.outreach_company_contacts_30d - company_contacts_30d(s, user_id, company_key_hash))
    if room <= 0:
        return []
    chosen: dict[str, Found] = {}

    # 1. 90-day cache first (no provider credits)
    for c in _cached(s, user_id, company_key_hash):
        role = c["role_type"]
        if role not in chosen and not blocked(s, user_id, c["email"]):
            chosen[role] = Found(c["id"], role, c["name"], c["email"], c["title"], c["location"])
    if len(chosen) >= room:
        return list(chosen.values())[:room]

    # 2. providers in rotation
    provs = P.load_providers(s, user_id, transport)
    usable = [p for p in provs if P.usage_ok(s, user_id, p.name)]
    if not usable:
        raise P.AllExhausted("no email-finder provider with credits left")
    exhausted_now: set[str] = set()
    for prov in usable:
        if not domain:
            try:
                domain = prov.resolve_domain(company)
                P.charge(s, user_id, prov.name)
            except P.CreditsExhausted:
                P.mark_exhausted(s, user_id, prov.name)
                continue
            except P.ProviderError:
                domain = None
            if not domain:
                continue
        wanted = [r for r in ("peer", "manager", "recruiter") if r not in chosen]
        titles = [t for r in wanted for t in TITLE_QUERY[r]]
        try:
            people = prov.search(domain, titles)
            P.charge(s, user_id, prov.name)
        except P.CreditsExhausted:
            P.mark_exhausted(s, user_id, prov.name)
            exhausted_now.add(prov.name)
            continue
        except P.ProviderError as e:
            log.warning("%s search failed: %s", prov.name, e)
            continue
        best = rank_candidates(people, job_title, job_location)
        pattern = getattr(prov, "pattern", None)
        for role in wanted:
            p = best.get(role)
            if p is None:
                continue
            if not p.email and isinstance(prov, P.Apollo) and P.usage_ok(s, user_id, prov.name):
                try:
                    prov.reveal(p, domain)
                    P.charge(s, user_id, prov.name)
                except P.CreditsExhausted:
                    P.mark_exhausted(s, user_id, prov.name)
            if not p.email and pattern and p.first_name and p.last_name:
                p.email = pattern_email(pattern, p.first_name, p.last_name, domain)
                p.email_verified = False
            if not p.email or blocked(s, user_id, p.email):
                continue
            if not _verify(s, user_id, usable, p):
                continue
            cid = _store(s, user_id, company_key_hash, p, role)
            chosen[role] = Found(cid, role, p.name, p.email.lower(), p.title, p.location)
        if len(chosen) >= room:
            break
    if not chosen and exhausted_now == {p.name for p in usable}:
        raise P.AllExhausted("every email-finder provider ran out of credits")
    return list(chosen.values())[:room]


def pattern_email(pattern: str, first: str, last: str, domain: str) -> str:
    """Hunter-style pattern ('{first}.{last}', '{f}{last}') -> address."""
    f, l = re.sub(r"[^a-z]", "", first.lower()), re.sub(r"[^a-z]", "", last.lower())
    local = (pattern.replace("{first}", f).replace("{last}", l).replace("{f}", f[:1]).replace("{l}", l[:1]))
    return f"{local}@{domain}"


def add_dnc(s: Session, user_id: UUID, email: str) -> None:
    s.execute(text("""INSERT INTO do_not_contact (user_id, email_hash) VALUES (:u, :h) ON CONFLICT DO NOTHING"""),
              {"u": user_id, "h": keyed_hash64(email)})
