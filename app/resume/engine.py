"""Resume engine orchestration (section 7).

build_recipe(): cache lookup -> heuristic pick -> optional LLM tailoring ->
                cover letter + answers -> truth validator.
render_pdf():   view -> compile -> ATS check; while over one page, drop the
                lowest-ranked bullet and recompile (max MAX_ONE_PAGE_LOOPS).
                Returns the trimmed recipe so the stored recipe re-renders
                to the same one-page PDF.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import get_settings
from app.llm.client import LLM
from app.resume import ats
from app.resume.compile import Compiled, compile_view
from app.resume.cover import cover_letter, standard_answers, template_cover_letter
from app.resume.jd import JobDescription
from app.resume.model import build_view
from app.resume.recipe import Recipe
from app.resume.render import to_docx
from app.resume.select import MIN_BULLETS, select, vault_terms
from app.resume.tailor import tailor
from app.resume.validator import validate
from app.vault.schema import Vault

log = logging.getLogger(__name__)


@dataclass
class Rendered:
    pdf: bytes
    recipe: Recipe
    report: ats.AtsReport
    engine: str
    loops: int


def prepare_jd(v: Vault, text: str, title: str = "", company: str = "", location: str = "") -> JobDescription:
    return JobDescription(text=text, title=title, company=company, location=location,
                          extra_terms=vault_terms(v))


def build_recipe(v: Vault, jd: JobDescription, *, vault_revision: int, bucket: str = "base",
                 llm: LLM | None = None, cached: Recipe | None = None,
                 questions: list[str] | None = None) -> Recipe:
    if cached is not None and cached.vault_revision == vault_revision:
        recipe = cached.model_copy(deep=True)
        recipe.title, recipe.company, recipe.jd_hash = jd.title, jd.company, jd.jd_hash
        recipe.bucket = bucket  # type: ignore[assignment]
        recipe.tailoring = "cached"
        recipe.cover_letter = template_cover_letter(v, recipe)
    else:
        recipe = select(v, jd, vault_revision=vault_revision, bucket=bucket)
        if llm is not None and bucket in ("high", "medium"):
            recipe = tailor(v, jd, recipe, llm)
        recipe.cover_letter = cover_letter(v, jd, recipe, llm)
    answers = standard_answers(v)
    for q in questions or []:
        answers.setdefault(q, "")
    recipe.answers = {k: a for k, a in answers.items() if a}
    return validate(v, recipe)


def _drop_one(recipe: Recipe) -> bool:
    """Remove the lowest-ranked bullet from any section above the minimum;
    then the lowest-ranked extra project; then an achievement."""
    candidates = [(b.rank, sec, b) for sec in recipe.experience + recipe.projects
                  for b in sec.bullets if len(sec.bullets) > MIN_BULLETS]
    if candidates:
        _, sec, b = min(candidates, key=lambda t: t[0])
        sec.bullets.remove(b)
        return True
    if len(recipe.projects) > 2:
        weakest = min(recipe.projects, key=lambda s: max(b.rank for b in s.bullets))
        recipe.projects.remove(weakest)
        return True
    if recipe.achievements:
        recipe.achievements.pop()
        return True
    return False


def render_pdf(v: Vault, recipe: Recipe, *, paper: str | None = None) -> Rendered:
    max_loops = get_settings().max_one_page_loops
    current = recipe.model_copy(deep=True)
    loops = 0
    while True:
        view = build_view(v, current)
        compiled: Compiled = compile_view(view, paper)
        report = ats.check(compiled.pdf, view, current.keywords)
        if report.one_page or loops >= max_loops or not _drop_one(current):
            break
        loops += 1
    if not report.one_page:
        log.warning("resume still %d pages after %d loops", report.pages, loops)
    return Rendered(pdf=compiled.pdf, recipe=current, report=report, engine=compiled.engine, loops=loops)


def render_docx(v: Vault, recipe: Recipe, *, paper: str | None = None) -> bytes:
    return to_docx(build_view(v, recipe), paper or get_settings().resume_paper)
