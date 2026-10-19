"""Resume generation for a stored user: vault + revision from the DB, cache
lookup, build, render. Used by the API, the apply worker and outreach."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from app.llm.client import get_llm
from app.resume import cache
from app.resume.engine import Rendered, build_recipe, prepare_jd, render_docx, render_pdf
from app.resume.recipe import Recipe
from app.vault import repo
from app.vault.schema import Vault


class NoVault(Exception):
    pass


@dataclass
class Built:
    recipe: Recipe
    vault: Vault
    revision: int
    cache_similarity: float | None


def load_vault(s: Session, user_id: UUID) -> tuple[Vault, int]:
    stored = repo.get_vault(s, user_id)
    if stored is None:
        raise NoVault(str(user_id))
    return Vault.model_validate(stored.data), stored.revision


def build_for_job(s: Session, user_id: UUID, *, jd_text: str, title: str, company: str,
                  bucket: str = "base", questions: list[str] | None = None,
                  use_cache: bool = True) -> Built:
    vault, rev = load_vault(s, user_id)
    jd = prepare_jd(vault, jd_text, title, company)
    hit = cache.find(s, user_id, jd.embedding, rev) if use_cache else None
    # Section 7: full tailoring only for High/Medium; Low uses cache or base.
    llm = get_llm() if bucket in ("high", "medium") else None
    recipe = build_recipe(vault, jd, vault_revision=rev, bucket=bucket, llm=llm,
                          cached=hit[0] if hit else None, questions=questions)
    if hit is None and use_cache:
        cache.store(s, user_id, jd.embedding, recipe)
    return Built(recipe=recipe, vault=vault, revision=rev, cache_similarity=hit[1] if hit else None)


def render(vault: Vault, recipe: Recipe, fmt: str = "pdf") -> tuple[bytes, Rendered | None]:
    if fmt == "docx":
        return render_docx(vault, recipe), None
    out = render_pdf(vault, recipe)
    return out.pdf, out
