"""Waiting list (section 9, C8): max 30 jobs per user, each lives 24 h
(pin +24 h), rolling displacement by rank. Never shows an entry that would
break C4/C5; shows 'ready at' while the 3-hour gap (C6) blocks it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.apply import gate, readypack
from app.config import get_settings
from app.resume.recipe import Recipe
from app.util.hashing import h64
from app.vault.schema import Vault

RANK_SQL = "a.bucket DESC NULLS LAST, a.salary_max DESC NULLS LAST, a.posted_at DESC NULLS LAST, a.score DESC NULLS LAST"


def _rank(app) -> tuple:
    ts = app["posted_at"].timestamp() if app.get("posted_at") else 0
    return (app.get("bucket") if app.get("bucket") is not None else -1,
            app.get("salary_max") if app.get("salary_max") is not None else -1, ts, app.get("score") or 0)


def add_to_waiting(s: Session, application_id: int, *, extra: dict | None = None,
                   notify: Callable | None = None) -> bool:
    cfg = get_settings()
    app = dict(s.execute(text("SELECT * FROM applications WHERE id = :i"), {"i": application_id}).mappings().one())
    uid = app["user_id"]
    s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": h64(f"waiting:{uid}")})
    if s.execute(text("SELECT 1 FROM waiting_list WHERE application_id = :i"), {"i": application_id}).first():
        return True
    rows = s.execute(text(f"""SELECT w.id, w.application_id, a.bucket, a.salary_max, a.posted_at, a.score
                              FROM waiting_list w JOIN applications a ON a.id = w.application_id
                              WHERE w.user_id = :u ORDER BY {RANK_SQL}"""), {"u": uid}).mappings().all()
    if len(rows) >= cfg.waiting_list_max:
        lowest = dict(rows[-1])
        if _rank(app) <= _rank(lowest):
            s.execute(text("DELETE FROM applications WHERE id = :i AND applied_at IS NULL AND status = 'waiting'"),
                      {"i": application_id})
            return False
        s.execute(text("DELETE FROM waiting_list WHERE id = :w"), {"w": lowest["id"]})
        s.execute(text("DELETE FROM applications WHERE id = :i AND applied_at IS NULL AND status = 'waiting'"),
                  {"i": lowest["application_id"]})
    vault = Vault.model_validate(s.execute(text("SELECT data FROM vaults WHERE user_id = :u"), {"u": uid}).scalar_one())
    recipe = Recipe.model_validate(app["recipe"]) if app.get("recipe") else None
    expires = datetime.now(timezone.utc) + timedelta(hours=cfg.waiting_list_ttl_hours)
    referral = s.execute(text("SELECT status, referral FROM referral_queue WHERE application_id = :i"),
                         {"i": application_id}).mappings().first()
    pack = readypack.build(app, vault, recipe, expires_at=expires, extra=extra,
                           referral=dict(referral) if referral else None)
    s.execute(text("""INSERT INTO waiting_list (user_id, application_id, expires_at, ready_pack)
                      VALUES (:u, :a, now() + make_interval(hours => :ttl), CAST(:p AS jsonb))"""),
              {"u": uid, "a": application_id, "ttl": cfg.waiting_list_ttl_hours, "p": json.dumps(pack, default=str)})
    if notify and app.get("bucket") == 3:
        notify(uid, f"High-match job needs you: {app['company_name']} - {app['title']}\n"
                    f"Reason: {app.get('failure_reason') or 'manual apply'}\n{pack['continue_url']}",
               buttons=[[("Pin 24h", f"pin:{application_id}")]])
    return True


def pin(s: Session, user_id: UUID, application_id: int) -> datetime | None:
    return s.execute(text("""UPDATE waiting_list SET expires_at = expires_at + interval '24 hours',
                             pin_count = pin_count + 1 WHERE user_id = :u AND application_id = :a
                             RETURNING expires_at"""), {"u": user_id, "a": application_id}).scalar()


def listing(s: Session, user_id: UUID) -> list[dict]:
    rows = s.execute(text(f"""
        SELECT w.id AS waiting_id, w.expires_at, w.pin_count, w.ready_pack, a.*
        FROM waiting_list w JOIN applications a ON a.id = w.application_id
        WHERE w.user_id = :u AND w.expires_at > now() ORDER BY {RANK_SQL}"""), {"u": user_id}).mappings().all()
    out = []
    for r in rows:
        r = dict(r)
        g = gate.check(s, r, via="manual")
        if not g.ok and g.not_before is None:
            continue                                     # would break C4/C5: never shown
        r["ready_at"] = g.not_before.isoformat() if g.not_before else None
        out.append(r)
    return out


def mark_applied(s: Session, user_id: UUID, application_id: int) -> gate.GateResult:
    owner = s.execute(text("SELECT user_id FROM applications WHERE id = :a"), {"a": application_id}).scalar()
    if owner != user_id:
        return gate.GateResult(False, "not found")
    return gate.manual_applied(s, application_id)
