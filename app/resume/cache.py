"""Recipe cache (section 7, step 1): reuse a recipe built for a JD with
embedding similarity >= RECIPE_REUSE_SIMILARITY, same user, same vault
revision, younger than RECIPE_CACHE_DAYS. Stored with the embedding
provider's name so switching providers can't produce false hits."""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.embed import provider_name, to_pgvector
from app.resume.recipe import Recipe


def find(s: Session, user_id: UUID, embedding: list[float], vault_revision: int) -> tuple[Recipe, float] | None:
    cfg = get_settings()
    row = s.execute(text("""
        SELECT recipe, 1 - (jd_embedding <=> CAST(:e AS halfvec(256))) AS sim
        FROM recipe_cache
        WHERE user_id = :u AND vault_revision = :rev
          AND created_at > now() - make_interval(days => :days)
          AND recipe->>'provider' = :prov
        ORDER BY jd_embedding <=> CAST(:e AS halfvec(256))
        LIMIT 1"""), {"u": user_id, "e": to_pgvector(embedding), "rev": vault_revision,
                      "days": cfg.recipe_cache_days, "prov": provider_name()}).first()
    if row is None or row.sim < cfg.recipe_reuse_similarity:
        return None
    return Recipe.model_validate(row.recipe["recipe"]), float(row.sim)


def store(s: Session, user_id: UUID, embedding: list[float], recipe: Recipe) -> None:
    payload = {"provider": provider_name(), "recipe": recipe.to_storage()}
    s.execute(text("""
        INSERT INTO recipe_cache (user_id, jd_embedding, recipe, vault_revision)
        VALUES (:u, CAST(:e AS halfvec(256)), CAST(:r AS jsonb), :rev)"""),
        {"u": user_id, "e": to_pgvector(embedding), "r": json.dumps(payload), "rev": recipe.vault_revision})
