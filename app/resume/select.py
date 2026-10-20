"""Pick the most relevant vault content for one job (section 7, step 2).

Deterministic and LLM-free: every user gets a tailored resume even with
LLM_PROVIDER=none. The LLM step (tailor.py) only rephrases what this picks.

1. Match the job to one of the user's target roles (title keywords first,
   then role family, then text similarity). The role supplies the summary,
   puts its skills first and prefers its projects.
2. Experience: the most relevant roles (max 4), best bullets first.
3. Projects: 3-4 by tech overlap with the JD, bullet quality, similarity
   and the role's preference.
4. Skills: from a pool of up to 200, the ~28 that matter for this job: JD
   must-haves, then nice-to-haves, the role's priority skills, and the tech
   of the chosen projects, grouped under the user's own categories.

Selection fits an estimated one-page line budget up front, so the compile
loop (drop lowest-ranked bullet, max 5 times) rarely has to run.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.embed import cosine, embed
from app.resume.jd import JobDescription
from app.resume.recipe import DEFAULT_ORDER, RBullet, Recipe, RSection, RSkillGroup
from app.util.skills import extract_skills, norm_skill
from app.vault.facts import extract_numbers, vault_facts
from app.vault.schema import TargetRole, Vault

CHARS_PER_LINE = 98       # 10pt body, 0.5in margins, bullet indent
LINE_BUDGET = 54          # usable lines on one A4/Letter page with this template
MIN_BULLETS = 2
MAX_BULLETS_PER_PROJECT = 4
MAX_BULLETS_PER_ROLE = 4
MAX_SKILLS_PER_GROUP = 12
MAX_RESUME_SKILLS = 28
MIN_RESUME_SKILLS = 12
MAX_SKILL_GROUPS = 5
MAX_ROLES_SHOWN = 4


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


def _phrase_in(phrase: str, text: str) -> bool:
    p = re.sub(r"[^a-z0-9+#]+", " ", phrase.lower()).strip()
    return bool(p) and re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", text) is not None


def match_role(v: Vault, title: str, jd: JobDescription | None = None) -> tuple[TargetRole | None, bool]:
    """(best target role for this job title, whether the match is strong).

    Strong = the title contains one of the role's keywords (or its name), or the
    title's role family equals the role's. Weak matches still steer skills and
    projects, but the general summary is preferred over the role's own."""
    if not v.target_roles:
        return None, False
    from app.ingest.normalize import families
    t = " " + re.sub(r"[^a-z0-9+#]+", " ", title.lower()) + " "
    title_fams = families(title)
    best, best_score, strong = None, -1.0, False
    for i, r in enumerate(v.target_roles):
        score, hit = 0.0, False
        for kw in [r.name, *r.title_keywords]:
            if _phrase_in(kw, t):
                score = max(score, 2.0 + min(len(kw), 30) / 100)      # longer keyword = more specific
                hit = True
        role_fams = families(" ".join([r.name, *r.title_keywords]))
        if title_fams and role_fams and title_fams & role_fams:
            score = max(score, 1.0 + len(title_fams & role_fams) / 10)
            hit = True
        if jd is not None:
            text = " ".join([r.name, *r.title_keywords, *r.skills, r.summary or ""])
            score += 0.5 * max(cosine(embed(text), jd.embedding), 0.0)
        score -= i * 1e-3                                             # ties: the user's order
        if score > best_score:
            best, best_score, strong = r, score, hit
    return best, strong


def _lines(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_LINE))


def estimate_lines(v: Vault, r: Recipe) -> float:
    texts = {b.id: b.text for o in list(v.projects) + list(v.experience) for b in o.bullets}
    lines = 3.0                                          # name + contact
    sections = 0
    if r.summary:
        sections += 1
        lines += _lines(r.summary) * 1.1
    shown = [e for e in v.education if e.show_on_resume]
    if shown:
        sections += 1
        lines += sum(1 + (1 if (e.score_text() or e.coursework) else 0) for e in shown)
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
    role, strong = match_role(v, jd.title, jd)
    role_projects = {pid: i for i, pid in enumerate(role.projects)} if role else {}

    # --- experience: the most relevant roles (max 4), shown most recent first ----
    scored_roles = []
    for e in v.experience:
        scored = sorted((_Scored(e.id, b.id, b.text, _bullet_score(b.text, jd, jd_terms, extra))
                         for b in e.bullets), key=lambda s: -s.score)
        rel = sum(s.score for s in scored[:3]) / max(1, min(3, len(scored)))
        rel += 0.3 if role and role.id in e.roles else 0.0
        scored_roles.append((rel, e, scored))
    if len(scored_roles) > MAX_ROLES_SHOWN:
        scored_roles = sorted(scored_roles, key=lambda t: -t[0])[:MAX_ROLES_SHOWN]
    scored_roles.sort(key=lambda t: (t[1].end or "9999-99", t[1].start), reverse=True)
    exp_sections: list[RSection] = []
    pool: list[tuple[RSection, _Scored]] = []
    for _rel, e, scored in scored_roles:
        sec = RSection(id=e.id, bullets=[RBullet(id=s.id, rank=s.score + 1.0) for s in scored[:MIN_BULLETS]])
        exp_sections.append(sec)
        pool += [(sec, s) for s in scored[MIN_BULLETS:MAX_BULLETS_PER_ROLE]]
    shown_roles = [e for _rel, e, _s in scored_roles]

    # --- projects: tech overlap, bullet quality, similarity, role preference -------
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
        if role is not None:
            if p.id in role_projects:
                score += 0.30 - 0.03 * role_projects[p.id]
            elif role.id in p.roles:
                score += 0.20
        ranked_projects.append((score, p, scored))
    ranked_projects.sort(key=lambda t: -t[0])

    summary = None
    if role is not None and role.summary and (strong or not v.summary):
        summary = role.summary
    elif v.summary:
        summary = v.summary
    recipe = Recipe(vault_revision=vault_revision, jd_hash=jd.jd_hash, title=jd.title,
                    company=jd.company, bucket=bucket, tailoring="heuristic",
                    order=list(DEFAULT_ORDER), experience=exp_sections,
                    role_id=role.id if role else None, summary=summary,
                    keywords=supported, missing_skills=missing)
    recipe.skills = pick_skills(v, jd, supported, role, shown_roles)
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
    recipe.skills = pick_skills(v, jd, supported, role, shown_roles + [v.project(sec.id) for sec in proj_sections])

    # --- fill remaining budget with the best remaining bullets ------------------
    for sec, s in sorted(pool, key=lambda t: -t[1].score):
        if not any(sec is ps for ps in recipe.projects) and not any(sec is es for es in recipe.experience):
            continue
        sec.bullets.append(RBullet(id=s.id, rank=s.score))
        if estimate_lines(v, recipe) > line_budget:
            sec.bullets.pop()
    return recipe


def pick_skills(v: Vault, jd: JobDescription, supported: list[str], role: TargetRole | None,
                shown: list) -> list[RSkillGroup]:
    """The skills worth listing for this job, from the whole pool, in the user's categories.

    Priority: JD must-haves > JD nice-to-haves / other JD mentions > the role's skills
    (in the role's order) > tech used in the experience and projects on this resume.
    A short list is padded with the pool's first skills, so a JD that names nothing the
    vault knows still gets a sensible skills section."""
    must, nice, mentioned = set(jd.must), set(jd.nice), set(jd.skills)
    role_pos = {norm_skill(s): i for i, s in enumerate(role.skills)} if role else {}
    used = {norm_skill(t) for o in shown if o is not None for t in o.tech}
    jd_pos = {s: i for i, s in enumerate(jd.skills)}

    entries: list[tuple[float, int, str, str]] = []          # (score, pool order, category, item)
    seen: set[str] = set()
    for g in v.skills:
        for it in g.items:
            n = norm_skill(it)
            if n in seen:
                continue
            seen.add(n)
            score = 0.0
            if n in must:
                score += 4.0
            elif n in nice:
                score += 3.0
            elif n in mentioned:
                score += 2.5
            if n in role_pos:
                score += 1.5 - min(role_pos[n], 30) * 0.03
            if n in used:
                score += 0.8
            if n in jd_pos:
                score += 0.2 - min(jd_pos[n], 40) * 0.005
            entries.append((score, len(entries), g.category, it))
    picked = [e for e in sorted(entries, key=lambda e: (-e[0], e[1])) if e[0] > 0][:MAX_RESUME_SKILLS]
    if len(picked) < MIN_RESUME_SKILLS:
        have = {e[3] for e in picked}
        picked += [e for e in entries if e[3] not in have][:MIN_RESUME_SKILLS - len(picked)]

    groups: dict[str, list[tuple[float, int, str, str]]] = {}
    for e in picked:
        groups.setdefault(e[2], []).append(e)
    ranked = sorted(groups.items(), key=lambda kv: (-max(x[0] for x in kv[1]), min(x[1] for x in kv[1])))
    out = [RSkillGroup(category=cat, items=[x[3] for x in sorted(items, key=lambda x: (-x[0], x[1]))][:MAX_SKILLS_PER_GROUP])
           for cat, items in ranked[:MAX_SKILL_GROUPS]]
    # Groups past the limit: their JD-matched skills join the last group rather than vanish.
    for _cat, items in ranked[MAX_SKILL_GROUPS:]:
        if out:
            out[-1].items += [x[3] for x in items if x[0] >= 2.5][:4]
    listed = {norm_skill(i) for g in v.skills for i in g.items}
    matched_extra = [k for k in v.extra_keywords if norm_skill(k) in set(supported) and norm_skill(k) not in listed]
    if matched_extra:
        out.append(RSkillGroup(category="Additional", items=matched_extra[:MAX_SKILLS_PER_GROUP]))
    return out
