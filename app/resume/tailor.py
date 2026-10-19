"""Optional LLM tailoring (section 7, step 2) on top of the heuristic pick.

The LLM may: choose among the vault's projects and bullets, reorder skills,
and rephrase / trim / reorder bullets to mirror the JD's wording. It may not
add facts. Whatever it returns goes through validator.validate(), which
reverts any bullet that gained a number, a skill or too many new words.

Only High/Medium buckets get full tailoring (cost control); Low uses the
closest cached recipe or the heuristic one.
"""
from __future__ import annotations

import json
import logging

from app.llm.client import LLM, LLMError, LLMUnavailable
from app.resume.jd import JobDescription
from app.resume.recipe import RBullet, Recipe, RSection
from app.vault.schema import Vault

log = logging.getLogger(__name__)

SYSTEM = (
    "You tailor a resume to a job description using ONLY the candidate's own material. "
    "Never invent facts, numbers, tools, employers or outcomes. You may rephrase, trim and "
    "reorder existing bullets so they use the job's vocabulary where it is truthful. "
    "Keep every number exactly as written in its own bullet. Keep each bullet one line, "
    "under 200 characters, starting with a strong past-tense verb."
)


def _material(v: Vault) -> dict:
    return {
        "experience": [{"id": e.id, "title": e.title, "company": e.company, "tech": e.tech,
                        "bullets": [{"id": b.id, "text": b.text} for b in e.bullets]} for e in v.experience],
        "projects": [{"id": p.id, "name": p.name, "tech": p.tech,
                      "bullets": [{"id": b.id, "text": b.text} for b in p.bullets]} for p in v.projects],
        "skills": [{"category": g.category, "items": g.items} for g in v.skills],
    }


def tailor(v: Vault, jd: JobDescription, base: Recipe, llm: LLM) -> Recipe:
    """LLM-refined copy of `base`; returns `base` unchanged if the LLM is
    unavailable or replies with something unusable."""
    if not llm.available:
        return base
    prompt = (
        f"JOB TITLE: {jd.title}\nCOMPANY: {jd.company}\n"
        f"JOB DESCRIPTION (truncated):\n{jd.text[:6000]}\n\n"
        f"CANDIDATE MATERIAL (JSON):\n{json.dumps(_material(v), ensure_ascii=False)}\n\n"
        f"CURRENT PICK: experience {[s.id for s in base.experience]}, "
        f"projects {[s.id for s in base.projects]}\n\n"
        "Return JSON with this shape:\n"
        '{"experience": [{"id": "...", "bullets": [{"id": "...", "text": "..."}]}],\n'
        ' "projects": [{"id": "...", "bullets": [{"id": "...", "text": "..."}]}],\n'
        ' "skill_order": ["most relevant skill first", "..."]}\n'
        "Rules: include every experience entry; choose 3-4 projects; 2-4 bullets each, best first; "
        "use only ids that exist above."
    )
    try:
        data = llm.complete_json(prompt, system=SYSTEM, tier="strong", max_tokens=2500)
    except (LLMError, LLMUnavailable) as e:
        log.warning("tailoring skipped: %s", e)
        return base
    if not isinstance(data, dict):
        return base

    ranks = {b.id: b.rank for _, b in base.all_bullets()}

    def sections(key: str) -> list[RSection]:
        out = []
        for _i, sec in enumerate(data.get(key) or []):
            if not isinstance(sec, dict) or not isinstance(sec.get("id"), str):
                continue
            bullets = []
            for j, b in enumerate(sec.get("bullets") or []):
                if isinstance(b, dict) and isinstance(b.get("id"), str):
                    text = b.get("text") if isinstance(b.get("text"), str) else None
                    # keep heuristic rank for drop order, nudged by LLM order
                    bullets.append(RBullet(id=b["id"], text=text, rank=ranks.get(b["id"], 0.5) - j * 0.01))
            if bullets:
                out.append(RSection(id=sec["id"], bullets=bullets))
        return out

    exp, proj = sections("experience"), sections("projects")
    if not proj:
        return base
    out = base.model_copy(deep=True)
    out.experience = exp or out.experience
    out.projects = proj
    order = [s for s in data.get("skill_order") or [] if isinstance(s, str)]
    if order:
        pos = {s.casefold(): i for i, s in enumerate(order)}
        for g in out.skills:
            g.items.sort(key=lambda it: pos.get(it.casefold(), 10_000))
    out.tailoring = "llm"
    return out
