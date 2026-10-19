"""Truth validator (C11): nothing reaches a resume unless the vault supports it.

Rules, strictest first:
1. Unknown ids (experience, project, bullet, achievement) are dropped.
2. A rephrased bullet may only use numbers from ITS OWN source bullet. A
   real metric moved onto another project is still a false claim.
3. A rephrased bullet may only name skills found in its source bullet or in
   the tech list of the project/role it belongs to.
4. Diff check: most content words must come from the source bullet, and the
   text can't grow much. Fails -> the vault text is used verbatim.
5. Skills and keywords must exist in the vault (alias-aware); others are
   stripped and the user's own spelling is restored.
6. Cover letter and answers: any sentence with an untraceable number or
   skill is removed.

Failures never block a resume; they revert to the vault's own wording and
are listed in recipe.validator so the user can see what was rejected.
"""
from __future__ import annotations

import re

from app.resume.recipe import Recipe, RSection, RSkillGroup, ValidatorReport
from app.resume.select import display_names, vault_terms
from app.util.skills import extract_skills, norm_skill
from app.util.text import content_words, sentences
from app.vault.facts import VaultFacts, extract_numbers, vault_facts
from app.vault.schema import Vault

_YEARS_CLAIM = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", re.I)
MIN_SOURCE_OVERLAP = 0.6      # share of the rewrite's content words found in the source
MAX_GROWTH_CHARS = 30         # rewrite may be at most source + 30 chars


def _owners(v: Vault) -> dict[str, tuple[set[str], dict[str, str]]]:
    """owner id -> (owner tech, {bullet id: vault text})"""
    out = {}
    for o in list(v.experience) + list(v.projects):
        out[o.id] = ({norm_skill(t) for t in o.tech}, {b.id: b.text for b in o.bullets})
    return out


def check_bullet(source: str, rewrite: str, owner_tech: set[str], extra: tuple[str, ...]) -> str | None:
    """None if the rewrite is acceptable, else the reason it isn't."""
    if not rewrite or rewrite.strip() == source.strip():
        return None
    if "\n" in rewrite:
        return "multi-line"
    src_nums = set(extract_numbers(source))
    bad_nums = [n for n in extract_numbers(rewrite) if n not in src_nums]
    if bad_nums:
        return f"numbers not in source bullet: {bad_nums}"
    allowed = set(extract_skills(source, extra)) | owner_tech
    bad_skills = [s for s in extract_skills(rewrite, extra) if s not in allowed]
    if bad_skills:
        return f"skills not in source bullet or its tech list: {bad_skills}"
    rw, src = content_words(rewrite), content_words(source)
    if rw and len(rw & src) / len(rw) < MIN_SOURCE_OVERLAP:
        return "too different from the source bullet"
    if len(rewrite) > len(source) + MAX_GROWTH_CHARS:
        return "longer than the source bullet allows"
    return None


def experience_years(v: Vault, today: str | None = None) -> float:
    """Total years of work experience in the vault (overlaps merged)."""
    from datetime import date
    now = today or date.today().strftime("%Y-%m")

    def months(ym: str) -> int:
        y, m = ym.split("-")
        return int(y) * 12 + int(m) - 1

    spans = sorted((months(e.start), months(e.end or now) + 1) for e in v.experience)
    total, cur_s, cur_e = 0, None, None
    for s_, e_ in spans:
        if cur_e is None or s_ > cur_e:
            total += (cur_e - cur_s) if cur_e is not None else 0
            cur_s, cur_e = s_, e_
        else:
            cur_e = max(cur_e, e_)
    total += (cur_e - cur_s) if cur_e is not None else 0
    return total / 12


def _clean_prose(text: str, facts: VaultFacts, extra: tuple[str, ...], report: ValidatorReport,
                 context_ok: frozenset[str] = frozenset(), max_years: float = 0.0) -> str:
    kept = []
    for sent in sentences(text):
        nums = facts.untraceable_numbers(sent)
        skills = [s for s in facts.untraceable_skills(extract_skills(sent, extra)) if s not in context_ok]
        # "N years of X" is a claim about experience, checked against the vault
        # even when N happens to appear elsewhere (e.g. a CGPA out of 10).
        years = [float(m) for m in _YEARS_CLAIM.findall(sent) if float(m) > max_years + 1e-9]
        if nums or skills or years:
            report.removed_sentences.append(sent)
        else:
            kept.append(sent)
    return " ".join(kept)


def validate(v: Vault, r: Recipe) -> Recipe:
    facts = vault_facts(v)
    extra = vault_terms(v)
    owners = _owners(v)
    names = display_names(v)
    report = ValidatorReport()
    out = r.model_copy(deep=True)

    def fix_sections(sections: list[RSection], valid_ids: set[str]) -> list[RSection]:
        cleaned = []
        seen: set[str] = set()
        for sec in sections:
            if sec.id not in valid_ids or sec.id in seen:
                report.dropped_ids.append(sec.id)
                continue
            seen.add(sec.id)
            tech, texts = owners[sec.id]
            bullets = []
            for b in sec.bullets:
                if b.id not in texts or b.id in {x.id for x in bullets}:
                    report.dropped_ids.append(b.id)
                    continue
                if b.text is not None:
                    reason = check_bullet(texts[b.id], b.text, tech, extra)
                    if reason or b.text.strip() == texts[b.id].strip():
                        if reason:
                            report.reverted_bullets.append(f"{b.id}: {reason}")
                        b = b.model_copy(update={"text": None})
                bullets.append(b)
            if bullets:
                cleaned.append(sec.model_copy(update={"bullets": bullets}))
        return cleaned

    out.experience = fix_sections(r.experience, {e.id for e in v.experience})
    out.projects = fix_sections(r.projects, {p.id for p in v.projects})

    ach_ids = {a.id for a in v.achievements}
    out.achievements = [a for a in dict.fromkeys(r.achievements) if a in ach_ids]
    report.dropped_ids += [a for a in r.achievements if a not in ach_ids]

    groups = []
    for g in r.skills:
        items = []
        for it in g.items:
            n = norm_skill(it)
            if n in facts.skills:
                items.append(names.get(n, it))
            else:
                report.stripped_skills.append(it)
        items = list(dict.fromkeys(items))
        if items:
            groups.append(RSkillGroup(category=g.category, items=items))
    out.skills = groups

    kept_kw = []
    for k in r.keywords:
        (kept_kw.append(norm_skill(k)) if norm_skill(k) in facts.skills else report.stripped_skills.append(k))
    out.keywords = list(dict.fromkeys(kept_kw))

    # Naming the employer or the role ("Java Developer at Oracle") is not a
    # skill claim, so those terms are exempt in prose.
    context_ok = frozenset(extract_skills(f"{r.title} {r.company}", extra))
    yrs = experience_years(v)
    if r.cover_letter:
        paragraphs = [p for p in re.split(r"\n\s*\n", r.cover_letter) if p.strip()]
        out.cover_letter = "\n\n".join(
            c for c in (_clean_prose(p, facts, extra, report, context_ok, yrs) for p in paragraphs) if c)
    out.answers = {q: _clean_prose(a, facts, extra, report, context_ok, yrs) for q, a in r.answers.items()}
    out.validator = report
    return out
