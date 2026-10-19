"""Vault editor API.

Concurrency: every write that changes an existing vault needs
`If-Match: "<revision>"` (from the ETag of the last GET). A stale revision
returns 409, a missing header 428, so two editor tabs can't silently
overwrite each other. Resume recipes record the revision they were built from.
"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ValidationError

from app.api.deps import principal, require_admin
from app import db
from app.vault import repo
from app.vault.lint import lint
from app.vault.patch import merge_patch
from app.vault.schema import Email, Project, Vault


router = APIRouter(dependencies=[Depends(principal)])


class NewUser(BaseModel):
    email: Email
    display_name: str | None = None


def _parse_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    v = value.strip().removeprefix("W/").strip('"')
    if not v.isdigit():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "If-Match must be a revision number")
    return int(v)


def _validate(data: dict) -> Vault:
    try:
        return Vault.model_validate(data)
    except ValidationError as e:
        raise HTTPException(422, json.loads(e.json(include_url=False)))


def _body(vault: Vault, revision: int, response: Response, updated_at=None) -> dict:
    response.headers["ETag"] = f'"{revision}"'
    out = {"revision": revision, "data": vault.to_storage(), "warnings": lint(vault)}
    if updated_at is not None:
        out["updated_at"] = updated_at.isoformat()
    return out


def _ensure_user(s, user_id: UUID) -> None:
    if not repo.user_exists(s, user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")


def _save(user_id: UUID, vault: Vault, if_match: int | None, response: Response) -> dict:
    with db.user_session(user_id) as s:
        _ensure_user(s, user_id)
        try:
            if if_match is None:
                if repo.get_vault(s, user_id) is not None:
                    raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED,
                                        "vault exists; send If-Match with its revision")
                rev = repo.create_vault(s, user_id, vault.to_storage())
                response.status_code = status.HTTP_201_CREATED
            else:
                rev = repo.update_vault(s, user_id, vault.to_storage(), if_match)
        except repo.Conflict as c:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                {"message": "revision conflict", "current_revision": c.current_revision})
        except repo.NotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no vault yet; PUT without If-Match")
    return _body(vault, rev, response)


def _load_for_edit(user_id: UUID, if_match: str | None) -> tuple[dict, int]:
    expected = _parse_if_match(if_match)
    if expected is None:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "If-Match header required")
    with db.user_session(user_id) as s:
        _ensure_user(s, user_id)
        stored = repo.get_vault(s, user_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no vault yet")
    if stored.revision != expected:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            {"message": "revision conflict", "current_revision": stored.revision})
    return stored.data, expected


# ------------------------------------------------------------------ routes
@router.post("/users", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
def create_user(body: NewUser) -> dict:
    with db.worker_session() as s:
        try:
            uid = repo.create_user(s, body.email, body.display_name)
        except repo.Conflict:
            raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    return {"id": str(uid)}


@router.get("/vault/schema")
def vault_schema() -> dict:
    """JSON Schema for building the editor form."""
    return Vault.model_json_schema()


@router.get("/users/{user_id}/vault")
def get_vault(user_id: UUID, response: Response) -> dict:
    with db.user_session(user_id) as s:
        _ensure_user(s, user_id)
        stored = repo.get_vault(s, user_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no vault yet")
    return _body(Vault.model_validate(stored.data), stored.revision, response, stored.updated_at)


@router.put("/users/{user_id}/vault")
def put_vault(user_id: UUID, response: Response, data: dict = Body(...),
              if_match: str | None = Header(default=None)) -> dict:
    return _save(user_id, _validate(data), _parse_if_match(if_match), response)


@router.patch("/users/{user_id}/vault")
def patch_vault(user_id: UUID, response: Response, patch: dict = Body(...),
                if_match: str | None = Header(default=None)) -> dict:
    current, rev = _load_for_edit(user_id, if_match)
    return _save(user_id, _validate(merge_patch(current, patch)), rev, response)


@router.post("/users/{user_id}/vault/validate")
def validate_vault(user_id: UUID, data: dict = Body(...)) -> dict:
    """Dry run: never saves."""
    try:
        v = Vault.model_validate(data)
    except ValidationError as e:
        return {"valid": False, "errors": json.loads(e.json(include_url=False)), "warnings": []}
    return {"valid": True, "errors": [], "warnings": lint(v)}


@router.put("/users/{user_id}/vault/projects/{project_id}")
def upsert_project(user_id: UUID, project_id: str, response: Response, project: dict = Body(...),
                   if_match: str | None = Header(default=None)) -> dict:
    if project.get("id", project_id) != project_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "project id in body must match the URL")
    project = {**project, "id": project_id}
    try:
        Project.model_validate(project)
    except ValidationError as e:
        raise HTTPException(422, json.loads(e.json(include_url=False)))
    current, rev = _load_for_edit(user_id, if_match)
    old = current.get("projects", [])
    if any(p.get("id") == project_id for p in old):
        projects = [project if p.get("id") == project_id else p for p in old]   # keep position
    else:
        projects = old + [project]
    return _save(user_id, _validate({**current, "projects": projects}), rev, response)


@router.delete("/users/{user_id}/vault/projects/{project_id}")
def delete_project(user_id: UUID, project_id: str, response: Response,
                   if_match: str | None = Header(default=None)) -> dict:
    current, rev = _load_for_edit(user_id, if_match)
    projects = [p for p in current.get("projects", []) if p.get("id") != project_id]
    if len(projects) == len(current.get("projects", [])):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return _save(user_id, _validate({**current, "projects": projects}), rev, response)
