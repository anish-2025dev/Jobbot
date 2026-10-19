"""Resume endpoints: build a tailored recipe and render it (PDF, DOCX, JSON)."""
from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from app.api.deps import principal
from app import db
from app.resume.compile import CompileError
from app.resume.recipe import Recipe
from app.resume.service import NoVault, build_for_job, load_vault, render

router = APIRouter(dependencies=[Depends(principal)])


class ResumeRequest(BaseModel):
    jd_text: str = Field(min_length=50, max_length=40_000)
    title: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=200)
    bucket: Literal["high", "medium", "low", "base"] = "high"
    format: Literal["pdf", "docx", "json"] = "pdf"
    questions: list[str] = Field(default_factory=list, max_length=30)


class RenderRequest(BaseModel):
    recipe: dict
    format: Literal["pdf", "docx"] = "pdf"


def _file(data: bytes, fmt: str, name: str, extra: dict | None = None) -> Response:
    media = {"pdf": "application/pdf",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[fmt]
    headers = {"Content-Disposition": f'attachment; filename="{name}.{fmt}"'}
    headers.update(extra or {})
    return Response(content=data, media_type=media, headers=headers)


def _filename(vault, company: str) -> str:
    stem = "".join(ch if ch.isalnum() else "_" for ch in f"{vault.personal.full_name}_{company}")
    return stem.strip("_")[:80] or "resume"


@router.post("/users/{user_id}/resume")
def make_resume(user_id: UUID, req: ResumeRequest) -> Response:
    try:
        with db.user_session(user_id) as s:
            built = build_for_job(s, user_id, jd_text=req.jd_text, title=req.title,
                                  company=req.company, bucket=req.bucket, questions=req.questions)
    except NoVault:
        raise HTTPException(404, "no vault yet")
    if req.format == "json":
        return Response(content=json.dumps({"recipe": built.recipe.to_storage(),
                                            "cache_similarity": built.cache_similarity}),
                        media_type="application/json")
    try:
        data, rendered = render(built.vault, built.recipe, req.format)
    except CompileError as e:
        raise HTTPException(503, f"resume compile failed: {e}")
    extra = {"X-ATS-Report": json.dumps(rendered.report.as_dict())} if rendered else {}
    return _file(data, req.format, _filename(built.vault, req.company), extra)


@router.post("/users/{user_id}/resume/render")
def render_recipe(user_id: UUID, req: RenderRequest) -> Response:
    with db.user_session(user_id) as s:
        try:
            vault, rev = load_vault(s, user_id)
        except NoVault:
            raise HTTPException(404, "no vault yet")
    recipe = Recipe.model_validate(req.recipe)
    if recipe.vault_revision != rev:
        raise HTTPException(409, f"recipe was built from vault revision {recipe.vault_revision}; current is {rev}")
    from app.resume.validator import validate
    recipe = validate(vault, recipe)       # a client-edited recipe is re-checked (C11)
    try:
        data, rendered = render(vault, recipe, req.format)
    except CompileError as e:
        raise HTTPException(503, f"resume compile failed: {e}")
    extra = {"X-ATS-Report": json.dumps(rendered.report.as_dict())} if rendered else {}
    return _file(data, req.format, _filename(vault, recipe.company or "resume"), extra)
