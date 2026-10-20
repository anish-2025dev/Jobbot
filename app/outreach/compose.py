"""The outreach email (section 10.3).

Plain text, under 120 words, from the user's own Gmail, personalized by
role: recruiter = "please consider/forward", manager = "fit for your team,
would value a look", peer = "would you be open to referring me". Job title
+ req ID + link, 2-3 truthful lines from the recipe, the tailored resume
attached, and the opt-out line. One message per person; no follow-ups.
"""
from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.llm.client import LLM, LLMError, LLMUnavailable
from app.resume.recipe import Recipe
from app.vault.schema import Vault

OPT_OUT = "If this isn't relevant, just let me know and I won't reach out again."
MAX_WORDS = 120
ASK = {
    "peer": "Would you be open to referring me for this role?",
    "manager": "I think I could be a good fit for your team and would value a quick look at my profile.",
    "recruiter": "Could you consider my profile, or forward it to the hiring team?",
}
SUBJECT = {
    "peer": "Referral request: {title} ({req})",
    "manager": "{title} on your team ({req})",
    "recruiter": "Application: {title} ({req})",
}


def words(text: str) -> int:
    return len(re.findall(r"\b[\w'.@/-]+\b", text))


def _intro(v: Vault) -> str:
    edu = max(v.higher_education(), key=lambda e: e.graduation_year)
    degree = edu.degree + (f" {edu.field_of_study}" if edu.field_of_study else "")
    return f"I'm {v.personal.full_name}, a {degree} student at {edu.institution} (graduating {edu.graduation_year})."


def _highlights(v: Vault, r: Recipe, n: int = 2) -> list[str]:
    """Shortest top-ranked recipe bullets, verbatim (already truth-checked)."""
    texts = {b.id: b.text for o in list(v.projects) + list(v.experience) for b in o.bullets}
    owner = {b.id: (o.name if hasattr(o, "name") else o.company) for o in list(v.projects) + list(v.experience)
             for b in o.bullets}
    ranked = sorted(r.all_bullets(), key=lambda t: -t[1].rank)[:6]
    picks = sorted(ranked, key=lambda t: len(t[1].text or texts.get(t[1].id, "")))[:n]
    return [f"{owner[b.id]}: {(b.text or texts[b.id]).rstrip('.')}" for _, b in picks if b.id in texts]


def compose(v: Vault, r: Recipe, *, role: str, first_name: str, job: dict) -> tuple[str, str]:
    req = job.get("req_id") or "see link"
    subject = SUBJECT[role].format(title=job["title"], req=req)
    sig_link = next((l.url for l in v.personal.links if "linkedin" in l.url.lower()), "")
    for n in (2, 1, 0):
        lines = [f"Hi {first_name},", "",
                 f"{_intro(v)} I'm applying for the {job['title']} role at {job['company']} (req {req}): {job['apply_url']}", ""]
        hl = _highlights(v, r, n)
        if hl:
            lines.append("Relevant work:")
            lines += [f"- {h}" for h in hl]
            lines.append("")
        lines += [ASK[role] + " My resume for this role is attached.", "", "Thanks,", v.personal.full_name,
                  " | ".join(x for x in (v.personal.phone, sig_link) if x), "", OPT_OUT]
        body = "\n".join(lines)
        if words(body) < MAX_WORDS:
            return subject, body
    return subject, body


def polish_with_llm(subject: str, body: str, llm: LLM | None) -> str:
    """Optional: the strong model may reword for tone. The result is used only
    if it keeps every fact (no new numbers or links), the opt-out line, and
    stays under 120 words; otherwise the template stands."""
    if llm is None or not llm.available:
        return body
    prompt = ("Rewrite this short referral email to sound natural and polite. Keep EVERY fact, number, link and the "
              f"last line exactly. Do not add claims. Under {MAX_WORDS} words. Plain text only.\n\n{body}")
    try:
        out = llm.complete(prompt, tier="strong", max_tokens=400).strip()
    except (LLMError, LLMUnavailable):
        return body
    nums = lambda t: set(re.findall(r"\d+(?:\.\d+)?%?", t))
    links = lambda t: set(re.findall(r"https?://\S+", t))
    if (OPT_OUT in out and words(out) < MAX_WORDS and nums(out) <= nums(body) and links(out) == links(body)):
        return out
    return body


def build_mime(*, from_name: str, from_addr: str, to_name: str, to_addr: str, subject: str, body: str,
               attachment: bytes | None, attachment_name: str) -> tuple[bytes, str]:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = formataddr((to_name, to_addr))
    msg["Subject"] = subject
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else "jobbot.local"
    mid = make_msgid(domain=domain)
    msg["Message-ID"] = mid
    msg.set_content(body)
    if attachment:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename=attachment_name)
    return msg.as_bytes(), mid
