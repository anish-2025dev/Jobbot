"""Accounts and per-user API access (P6, section 2 multi-user).

Passwords: scrypt (stdlib), per-user salt. Tokens: HMAC-SHA256 signed,
carry the user id, an expiry and a fingerprint of the password hash, so a
password change revokes every older token without a token table.

Who may call what:
- admin X-API-Key: everything (scripts, operator)
- Bearer token:    only routes for its own user (/users/{own id}/...) and /me
- Postgres RLS:    user-facing queries also run as the non-owner jobbot_api
                   role with app.user_id set (app/db.py), so a bug in a route
                   still can't read another user's rows.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from uuid import UUID

from fastapi import Header, HTTPException, Request, status
from sqlalchemy import text

from app.config import get_settings

TOKEN_TTL_S = 14 * 86400
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
MIN_PASSWORD = 10


class AuthError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        hashlib.scrypt(b"x", salt=b"0" * 16, **_SCRYPT)          # equalize timing for unknown users
        return False
    _, salt_b64, dk_b64 = stored.split("$")
    dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


def password_problems(password: str, email: str = "") -> list[str]:
    out = []
    if len(password) < MIN_PASSWORD:
        out.append(f"at least {MIN_PASSWORD} characters")
    if email and email.split("@")[0].lower() in password.lower():
        out.append("must not contain your email name")
    if password.lower() in {"password123", "qwertyuiop", "1234567890", "iloveyou12"}:
        out.append("too common")
    return out


def _key() -> bytes:
    s = get_settings()
    secret = s.session_secret or s.encryption_key
    if not secret:
        raise AuthError("SESSION_SECRET (or ENCRYPTION_KEY) must be set")
    return hashlib.sha256(f"jobbot-token|{secret}".encode()).digest()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pw_fp(password_hash: str | None) -> str:
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:12]


def issue_token(user_id: UUID, password_hash: str | None, ttl_s: int = TOKEN_TTL_S) -> str:
    payload = json.dumps({"u": str(user_id), "e": int(time.time()) + ttl_s, "p": _pw_fp(password_hash)},
                         separators=(",", ":")).encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def read_token(token: str) -> dict:
    try:
        p64, s64 = token.split(".")
        payload, sig = _unb64(p64), _unb64(s64)
    except Exception as e:
        raise AuthError("malformed token") from e
    if not hmac.compare_digest(sig, hmac.new(_key(), payload, hashlib.sha256).digest()):
        raise AuthError("bad token signature")
    data = json.loads(payload)
    if data["e"] < time.time():
        raise AuthError("token expired")
    return data


@dataclass
class Principal:
    admin: bool
    user_id: UUID | None


def _admin_ok(x_api_key: str) -> bool:
    expected = get_settings().admin_api_key
    return bool(expected) and bool(x_api_key) and hmac.compare_digest(x_api_key, expected)


def resolve(x_api_key: str, authorization: str) -> Principal:
    if _admin_ok(x_api_key):
        return Principal(admin=True, user_id=None)
    if authorization.lower().startswith("bearer "):
        try:
            data = read_token(authorization[7:].strip())
        except AuthError as e:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e))
        from app import db
        uid = UUID(data["u"])
        with db.worker_session() as s:
            row = s.execute(text("SELECT password_hash, deleted_at FROM users WHERE id = :u"), {"u": uid}).first()
        if row is None or row.deleted_at is not None or _pw_fp(row.password_hash) != data["p"]:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")
        return Principal(admin=False, user_id=uid)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required (Bearer token) or X-API-Key")


def principal(request: Request, x_api_key: str = Header(default=""),
              authorization: str = Header(default="")) -> Principal:
    """Any authenticated caller. For routes under /users/{user_id}, a user
    token must belong to that user."""
    p = resolve(x_api_key, authorization)
    target = request.path_params.get("user_id")
    if target is not None and not p.admin and str(p.user_id) != str(target):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")   # don't reveal other ids exist
    return p


def require_admin(x_api_key: str = Header(default="")) -> None:
    if not _admin_ok(x_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin X-API-Key required")
