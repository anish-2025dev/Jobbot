"""Vault persistence with optimistic concurrency on `revision`."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, current_revision: int | None):
        super().__init__(f"revision conflict (current: {current_revision})")
        self.current_revision = current_revision


@dataclass
class StoredVault:
    data: dict
    revision: int
    updated_at: datetime


def create_user(s: Session, email: str, display_name: str | None = None) -> UUID:
    try:
        with s.begin_nested():
            return s.execute(
                text("INSERT INTO users (email, display_name) VALUES (:e, :n) RETURNING id"),
                {"e": email.strip().lower(), "n": display_name},
            ).scalar_one()
    except IntegrityError as e:
        raise Conflict(None) from e


def user_exists(s: Session, user_id: UUID) -> bool:
    return bool(s.execute(text("SELECT 1 FROM users WHERE id = :u AND deleted_at IS NULL"),
                          {"u": user_id}).scalar())


def get_vault(s: Session, user_id: UUID, for_update: bool = False) -> StoredVault | None:
    row = s.execute(
        text("SELECT data, revision, updated_at FROM vaults WHERE user_id = :u"
             + (" FOR UPDATE" if for_update else "")),
        {"u": user_id},
    ).first()
    return StoredVault(*row) if row else None


def create_vault(s: Session, user_id: UUID, data: dict) -> int:
    rev = s.execute(
        text("""INSERT INTO vaults (user_id, data) VALUES (:u, CAST(:d AS jsonb))
                ON CONFLICT (user_id) DO NOTHING RETURNING revision"""),
        {"u": user_id, "d": json.dumps(data)},
    ).scalar()
    if rev is None:
        current = get_vault(s, user_id)
        raise Conflict(current.revision if current else None)
    return rev


def update_vault(s: Session, user_id: UUID, data: dict, expected_revision: int) -> int:
    rev = s.execute(
        text("""UPDATE vaults SET data = CAST(:d AS jsonb), revision = revision + 1, updated_at = now()
                WHERE user_id = :u AND revision = :r RETURNING revision"""),
        {"u": user_id, "d": json.dumps(data), "r": expected_revision},
    ).scalar()
    if rev is None:
        current = get_vault(s, user_id)
        if current is None:
            raise NotFound()
        raise Conflict(current.revision)
    return rev
