"""Short cover letter and custom-question answers (section 7, step 8).

Deterministic template by default; the strong LLM writes a more natural
version when configured. Both pass through the truth validator, which
removes any sentence with a number or skill the vault can't back.
"""
from __future__ import annotations

import logging

from app.llm.client import LLM, LLMError, LLMUnavailable
from app.resume.jd import JobDescription
from app.resume.recipe import Recipe
from app.vault.schema import Vault

log = logging.getLogger(__name__)


def _bullet_text(v: Vault, r: Recipe, limit: int = 2) -> list[str]:
    texts = {b.id: b.text for o in list(v.experience) + list(v.projects) for b in o.bullets}
    ranked = sorted(r.all_bullets(), key=lambda t: -t[1].rank)
    return [(b.text or texts[b.id]).rstrip(".") for _, b in ranked[:limit] if b.id in texts]


def _project_names(v: Vault, r: Recipe) -> list[str]:
    names = {p.id: p.name for p in v.projects}
    return [names[s.id] for s in r.projects if s.id in names]


def template_cover_letter(v: Vault, r: Recipe) -> str:
    edu = max(v.education, key=lambda e: e.graduation_year)
    role = r.title or "this role"
    company = r.company or "your team"
    skills = ", ".join(r.keywords[:4]) if r.keywords else ", ".join(v.skills[0].items[:3])
    highlights = _bullet_text(v, r)
    projects = _project_names(v, r)[:2]
    p1 = (f"I am applying for the {role} position at {company}. I am completing a {edu.degree}"
          f"{' in ' + edu.field_of_study if edu.field_of_study else ''} at {edu.institution} "
          f"(graduating {edu.graduation_year}), and my work centres on {skills}.")
    p2 = ""
    if highlights:
        p2 = "Two examples of relevant work: " + "; ".join(h[0].lower() + h[1:] for h in highlights) + "."
    if projects:
        p2 += f" More detail is in my resume, including {' and '.join(projects)}."
    p3 = (f"I would welcome the chance to discuss how I can contribute to {company}. "
          f"Thank you for your time.\n\n{v.personal.full_name}")
    return "\n\n".join(p for p in (p1, p2.strip(), p3) if p)


def standard_answers(v: Vault) -> dict[str, str]:
    sa = v.standard_answers
    out: dict[str, str] = {}
    if sa.expected_ctc_lpa is not None:
        out["Expected CTC"] = f"{sa.expected_ctc_lpa:g} LPA"
    if sa.current_ctc_lpa is not None:
        out["Current CTC"] = f"{sa.current_ctc_lpa:g} LPA"
    if sa.notice_period_days is not None:
        out["Notice period"] = "Immediate" if sa.notice_period_days == 0 else f"{sa.notice_period_days} days"
    if sa.earliest_start:
        out["Earliest start date"] = sa.earliest_start
    if sa.willing_to_relocate is not None:
        out["Willing to relocate"] = "Yes" if sa.willing_to_relocate else "No"
    if sa.work_authorization:
        out["Work authorization"] = sa.work_authorization
    if sa.requires_sponsorship is not None:
        out["Requires visa sponsorship"] = "Yes" if sa.requires_sponsorship else "No"
    out.update(sa.custom)
    return out


def cover_letter(v: Vault, jd: JobDescription, r: Recipe, llm: LLM | None) -> str:
    base = template_cover_letter(v, r)
    if llm is None or not llm.available or r.bucket not in ("high", "medium"):
        return base
    prompt = (
        f"Write a cover letter under 180 words for {r.title} at {r.company}.\n"
        f"Use ONLY these facts about the candidate; do not add numbers, tools or claims:\n{base}\n\n"
        f"Job description (for tone and priorities only):\n{jd.text[:3000]}\n\n"
        "Plain text, 3 short paragraphs, end with the candidate's name. No placeholders."
    )
    try:
        text = llm.complete(prompt, tier="strong", max_tokens=600).strip()
    except (LLMError, LLMUnavailable) as e:
        log.warning("cover letter LLM skipped: %s", e)
        return base
    return text or base
