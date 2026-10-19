"""Pick the most relevant vault content for one job (section 7, step 2).

Deterministic and LLM-free: every user gets a tailored resume even with
LLM_PROVIDER=none. The LLM step (tailor.py) only rephrases what this picks.

Selection fits an estimated one-page line budget up front, so the compile
loop (drop lowest-ranked bullet, max 5 times) rarely has to run.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.embed import cosine, embed
from app.resume.jd import JobDescription
from app.resume.recipe import DEFAULT_ORDER, RBullet, Recipe, RSection, RSkillGroup
from app.util.skills import extract_skills, norm_skill
from app.vault.facts import extract_numbers, vault_facts
from app.vault.schema import Vault

CHARS_PER_LINE = 98       # 10pt body, 0.5in margins, bullet indent
LINE_BUDGET = 54          # usable lines on one A4/Letter page with this template
MIN_BULLETS = 2
MAX_BULLETS_PER_PROJECT = 4
MAX_BULLETS_PER_ROLE = 4
MAX_SKILLS_PER_GROUP = 12


@dataclass
class _Scored:
    owner: str
    id: str
    text: str
    score: float


def vault_terms(v: Vault) -> tuple[str, ...]:
    terms = [s for g in v.skills for s in g.items] + list(v.extra_keywords)
    terms += [t for p in v.projects for t in p.tech] + [t for e in v.experience for t in e.tech]
    return tuple(dict.fromkeys(terms))


def display_names(v: Vault) -> dict[str, str]:
    """normalized skill -> the user's own spelling."""
    out: dict[str, str] = {}
    for s in vault_terms(v):
        out.setdefault(norm_skill(s), s)
    return out


def _bullet_score(text: str, jd: JobDescription, jd_terms: set[str], extra: tuple[str, ...]) -> float:
    hits = len(set(extract_skills(text, extra)) & jd_terms)
    sim = max(cosine(embed(text), jd.embedding), 0.0)
    metric = 1.0 if extract_numbers(text) else 0.0
    return round(0.45 * min(hits, 3) / 3 + 0.35 * min(sim / 0.5, 1.0) + 0.20 * metric, 4)


def _lines(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_LINE))


def estimate_lines(v: Vault, r: Recipe) -> float:
    texts = {b.id: b.text for o in list(v.projects) + list(v.experience) for b in o.bullets}
    lines = 3.0                                          # name + contact
    sections = 0
    if v.education:
        sections += 1
        lines += sum(2 + (1 if e.coursework else 0) for e in v.education)
    for group in (r.experience, r.projects):
        if group:
            sections += 1
            for sec in group:
                lines += 1 + sum(_lines(b.text or texts.get(b.id, "")) for b in sec.bullets)
    if r.skills:
        sections += 1
        lines += sum(math.ceil((len(g.category) + sum(len(i) + 2 for i in g.items)) / CHARS_PER_LINE)
                     for g in r.skills)
    if r.achievements:
        sections += 1
        lines += len(r.achievements)
    if v.certifications:
        sections += 1
        lines += len(v.certifications)
    return lines + sections * 1.6


def select(v: Vault, jd: JobDescription, *, vault_revision: int, bucket: str = "base",
           line_budget: int = LINE_BUDGET) -> Recipe:
    extra = vault_terms(v)
    facts = vault_facts(v)
    jd_terms = set(jd.must) | set(jd.nice) | set(extract_skills(jd.title, extra))
    supported = [s for s in jd.skills if s in facts.skills]
    missing = [s for s in jd.must if s not in facts.skills]

    # --- experience: every role, most recent first, best bullets first ---------
    roles = sorted(v.experience, key=lambda e: (e.end or "9999-99", e.start), reverse=True)
    exp_sections: list[RSection] = []
    pool: list[tuple[RSection, _Scored]] = []
    for e in roles:
        scored = sorted((_Scored(e.id, b.id, b.text, _bullet_score(b.text, jd, jd_terms, extra))
                         for b in e.bullets), key=lambda s: -s.score)
        sec = RSection(id=e.id, bullets=[RBullet(id=s.id, rank=s.score + 1.0) for s in scored[:MIN_BULLETS]])
        exp_sections.append(sec)
        pool += [(sec, s) for s in scored[MIN_BULLETS:MAX_BULLETS_PER_ROLE]]

    # --- projects: rank by tech overlap, bullet quality and similarity ----------
    ranked_projects = []
    denom = max(1, min(len(jd_terms), 6))
    for p in v.projects:
        tech = {norm_skill(t) for t in p.tech} | set(extract_skills(" ".join(b.text for b in p.bullets), extra))
        scored = sorted((_Scored(p.id, b.id, b.text, _bullet_score(b.text, jd, jd_terms, extra))
                         for b in p.bullets), key=lambda s: -s.score)
        top = [s.score for s in scored[:3]] or [0.0]
        sim = max(cosine(embed(f"{p.name} {' '.join(p.tech)} {' '.join(b.text for b in p.bullets)}"),
                         jd.embedding), 0.0)
        score = 0.5 * min(len(tech & jd_terms) / denom, 1.0) + 0.3 * (sum(top) / len(top)) + 0.2 * min(sim / 0.5, 1.0)
        ranked_projects.append((score, p, scored))
    ranked_projects.sort(key=lambda t: -t[0])

    recipe = Recipe(vault_revision=vault_revision, jd_hash=jd.jd_hash, title=jd.title,
                    company=jd.company, bucket=bucket, tailoring="heuristic",
                    order=list(DEFAULT_ORDER), experience=exp_sections,
                    keywords=supported, missing_skills=missing)
    recipe.skills = _skills(v, jd, supported)
    recipe.achievements = [a.id for a in sorted(
        v.achievements, key=lambda a: -_bullet_score(a.text, jd, jd_terms, extra))][:3]

    proj_sections: list[RSection] = []
    for i, (pscore, p, scored) in enumerate(ranked_projects[:4]):
        sec = RSection(id=p.id, bullets=[RBullet(id=s.id, rank=s.score + pscore) for s in scored[:MIN_BULLETS]])
        est = estimate_lines(v, recipe.model_copy(update={"projects": proj_sections + [sec]}))
        if proj_sections and est > line_budget:
            break
        if i == 3 and est + 4 > line_budget:
            break  # a 4th project only when it fits with room to spare
        proj_sections.append(sec)
        pool += [(sec, s) for s in scored[MIN_BULLETS:MAX_BULLETS_PER_PROJECT]]
    recipe.projects = proj_sections

    # --- fill remaining budget with the best remaining bullets ------------------
    for sec, s in sorted(pool, key=lambda t: -t[1].score):
        if not any(sec is ps for ps in recipe.projects) and not any(sec is es for es in recipe.experience):
            continue
        sec.bullets.append(RBullet(id=s.id, rank=s.score))
        if estimate_lines(v, recipe) > line_budget:
            sec.bullets.pop()
    return recipe


def _skills(v: Vault, jd: JobDescription, supported: list[str]) -> list[RSkillGroup]:
    """Vault skill groups with JD-matched items first, in JD order."""
    pos = {s: i for i, s in enumerate(jd.skills)}
    groups = []
    listed: set[str] = set()
    for g in v.skills:
        items = sorted(g.items, key=lambda it: (pos.get(norm_skill(it), 10_000), g.items.index(it)))
        listed |= {norm_skill(i) for i in items}
        groups.append(RSkillGroup(category=g.category, items=items[:MAX_SKILLS_PER_GROUP]))
    matched_extra = [k for k in v.extra_keywords if norm_skill(k) in set(supported) and norm_skill(k) not in listed]
    if matched_extra:
        groups.append(RSkillGroup(category="Additional", items=matched_extra[:MAX_SKILLS_PER_GROUP]))
    return groups
