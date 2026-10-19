"""Accounts, settings, credentials, Telegram linking, Gmail OAuth (P6, section 13)."""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import db
from app.auth import (Principal, hash_password, issue_token, password_problems, principal, verify_password)
from app.config import get_settings
from app.ratelimit import Limit, get_limiter
from app.vault.schema import Email

router = APIRouter()
LOGIN_LIMIT = Limit(rate_per_s=10 / 900, burst=10)          # 10 attempts per 15 min per email and per IP
SECRET_KINDS = {
    "gmail_client": ("client_id", "client_secret"),            # the user's own Google Cloud OAuth client
    "imap": ("password",),                                     # app password; username = mailbox address
    "apollo": ("api_key",), "hunter": ("api_key",), "snov": ("client_id", "client_secret"),
    "totp": ("secret",),                                       # authenticator secret; username = the site's domain
}
SETTINGS_KEYS = {"auto_apply", "outreach", "outreach_mode", "paused", "min_score", "digest_hour"}


class Signup(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=100)


class Login(BaseModel):
    email: Email
    password: str = Field(max_length=200)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(max_length=200)


def _throttle(*keys: str) -> None:
    lim = get_limiter()
    for k in keys:
        if not lim.try_acquire(f"login:{k}", LOGIN_LIMIT):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts; try again in a few minutes")


def _token_response(uid: UUID, pw_hash: str) -> dict:
    return {"user_id": str(uid), "token": issue_token(uid, pw_hash), "token_type": "bearer"}


@router.post("/auth/signup", status_code=201)
def signup(body: Signup, request: Request) -> dict:
    if not get_settings().allow_signup:
        raise HTTPException(403, "signups are closed on this instance")
    problems = password_problems(body.password, body.email)
    if problems:
        raise HTTPException(422, {"password": problems})
    _throttle(f"ip:{request.client.host if request.client else '-'}")
    pw = hash_password(body.password)
    with db.worker_session() as s:
        first = s.execute(text("SELECT NOT EXISTS (SELECT 1 FROM users WHERE password_hash IS NOT NULL)")).scalar()
        try:
            with s.begin_nested():
                uid = s.execute(text("""INSERT INTO users (email, display_name, password_hash, is_admin, settings)
                                        VALUES (lower(:e), :n, :p, :a, '{}') RETURNING id"""),
                                {"e": body.email, "n": body.display_name, "p": pw, "a": first}).scalar_one()
        except IntegrityError:
            raise HTTPException(409, "email already registered")
    return _token_response(uid, pw)


@router.post("/auth/login")
def login(body: Login, request: Request) -> dict:
    _throttle(f"email:{body.email.lower()}", f"ip:{request.client.host if request.client else '-'}")
    with db.worker_session() as s:
        row = s.execute(text("SELECT id, password_hash FROM users WHERE email = lower(:e) AND deleted_at IS NULL"),
                        {"e": body.email}).first()
    if row is None or not verify_password(body.password, row.password_hash if row else None):
        raise HTTPException(401, "wrong email or password")
    return _token_response(row.id, row.password_hash)


@router.post("/auth/password")
def change_password(body: PasswordChange, p: Principal = Depends(principal)) -> dict:
    if p.user_id is None:
        raise HTTPException(400, "use a user token")
    with db.worker_session() as s:
        row = s.execute(text("SELECT email, password_hash FROM users WHERE id = :u"), {"u": p.user_id}).first()
        if not verify_password(body.current_password, row.password_hash):
            raise HTTPException(401, "current password is wrong")
        problems = password_problems(body.new_password, row.email)
        if problems:
            raise HTTPException(422, {"password": problems})
        pw = hash_password(body.new_password)
        s.execute(text("UPDATE users SET password_hash = :p WHERE id = :u"), {"p": pw, "u": p.user_id})
    return _token_response(p.user_id, pw)                    # old tokens are now revoked


@router.get("/me")
def me(p: Principal = Depends(principal)) -> dict:
    if p.user_id is None:
        return {"admin": True}
    with db.user_session(p.user_id) as s:
        row = s.execute(text("""SELECT id, email, display_name, timezone, is_admin, telegram_chat_id IS NOT NULL AS tg,
                                settings FROM users WHERE id = :u"""), {"u": p.user_id}).mappings().one()
    return {"user_id": str(row["id"]), "email": row["email"], "display_name": row["display_name"],
            "timezone": row["timezone"], "is_admin": row["is_admin"], "telegram_linked": row["tg"],
            "settings": row["settings"] or {}}


@router.delete("/users/{user_id}", dependencies=[Depends(principal)])
def delete_account(user_id: UUID) -> dict:
    """Deletes the user and, by cascade, every row they own (vault,
    applications, ledger, contacts, credentials...)."""
    with db.worker_session() as s:
        n = s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id}).rowcount
    if not n:
        raise HTTPException(404, "user not found")
    return {"deleted": True}


# --- settings -----------------------------------------------------------------------
class SettingsPatch(BaseModel):
    auto_apply: bool | None = None
    outreach: bool | None = None
    outreach_mode: Literal["review", "auto"] | None = None
    paused: bool | None = None
    min_score: float | None = Field(default=None, ge=0.5, le=0.95)
    digest_hour: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = None


@router.get("/users/{user_id}/settings", dependencies=[Depends(principal)])
def get_settings_(user_id: UUID) -> dict:
    with db.user_session(user_id) as s:
        row = s.execute(text("SELECT settings, timezone FROM users WHERE id = :u"), {"u": user_id}).first()
    if row is None:
        raise HTTPException(404, "user not found")
    st = {k: v for k, v in (row.settings or {}).items() if k in SETTINGS_KEYS | {"outreach_enabled_at", "outreach_paused_until", "weights"}}
    return {**st, "timezone": row.timezone}


@router.patch("/users/{user_id}/settings", dependencies=[Depends(principal)])
def patch_settings(user_id: UUID, body: SettingsPatch) -> dict:
    changes = body.model_dump(exclude_none=True)
    tz = changes.pop("timezone", None)
    if tz:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, f"unknown time zone {tz}")
    with db.user_session(user_id) as s:
        cur = s.execute(text("SELECT settings FROM users WHERE id = :u FOR UPDATE"), {"u": user_id}).scalar() or {}
        if changes.get("outreach") and not cur.get("outreach_enabled_at"):
            # review mode is forced for the first week after enabling outreach (section 10.3)
            changes["outreach_enabled_at"] = datetime.now(timezone.utc).isoformat()
        s.execute(text("UPDATE users SET settings = coalesce(settings, '{}') || CAST(:c AS jsonb), "
                       "timezone = coalesce(:tz, timezone) WHERE id = :u"),
                  {"c": json.dumps(changes), "tz": tz, "u": user_id})
    return get_settings_(user_id)


# --- credentials (write-only) ----------------------------------------------------------
class CredentialIn(BaseModel):
    username: str = Field(default="", max_length=200)
    secret: dict[str, str]
    meta: dict[str, str | int | bool] = Field(default_factory=dict)


@router.put("/users/{user_id}/credentials/{kind}", dependencies=[Depends(principal)])
def put_credential(user_id: UUID, kind: str, body: CredentialIn) -> dict:
    if kind not in SECRET_KINDS:
        raise HTTPException(404, f"unknown credential kind; one of {sorted(SECRET_KINDS)}")
    missing = [k for k in SECRET_KINDS[kind] if not body.secret.get(k)]
    if missing:
        raise HTTPException(422, f"missing: {missing}")
    if kind == "imap" and "@" not in body.username:
        raise HTTPException(422, "username must be the mailbox address")
    host = kind
    if kind == "totp":
        import base64
        site = body.username.lower().removeprefix("www.").strip()
        try:
            base64.b32decode(body.secret["secret"].replace(" ", "").upper() + "=" * (-len(body.secret["secret"].replace(" ", "")) % 8))
        except Exception:
            raise HTTPException(422, "secret must be the base32 key shown when you set up the authenticator")
        if "." not in site:
            raise HTTPException(422, "username must be the site's domain, e.g. myworkday.com")
        host = f"totp:{site}"
    from app.mail.accounts import save_credential
    with db.user_session(user_id) as s:
        save_credential(s, user_id, kind, body.username or kind,
                        {k: body.secret[k] for k in SECRET_KINDS[kind]}, dict(body.meta), host=host)
    return {"saved": kind}


@router.get("/users/{user_id}/credentials", dependencies=[Depends(principal)])
def list_credentials(user_id: UUID) -> list[dict]:
    """What is connected. Secrets are never returned."""
    with db.user_session(user_id) as s:
        rows = s.execute(text("""SELECT host, kind, username, created_at, rotated_at FROM credentials
                                 WHERE user_id = :u ORDER BY host"""), {"u": user_id}).mappings().all()
    return [{"host": r["host"], "kind": r["kind"], "username": r["username"],
             "updated_at": (r["rotated_at"] or r["created_at"]).isoformat()} for r in rows]


@router.delete("/users/{user_id}/credentials/{host}", dependencies=[Depends(principal)])
def delete_credential(user_id: UUID, host: str) -> dict:
    with db.user_session(user_id) as s:
        n = s.execute(text("DELETE FROM credentials WHERE user_id = :u AND host = :h"), {"u": user_id, "h": host}).rowcount
    return {"deleted": bool(n)}


# --- Telegram -----------------------------------------------------------------------
@router.post("/users/{user_id}/telegram/link", dependencies=[Depends(principal)])
def telegram_link(user_id: UUID) -> dict:
    from app.notify.telegram import new_link_code
    with db.user_session(user_id) as s:
        code = new_link_code(s, user_id)
    return {"code": code, "instructions": f"Open your JobBot Telegram bot and send: /start {code}"}


# --- Gmail OAuth ----------------------------------------------------------------------
def _redirect_uri() -> str:
    return get_settings().public_base_url.rstrip("/") + "/oauth/google/callback"


OAUTH_COOKIE = "jobbot_oauth_nonce"


@router.get("/users/{user_id}/gmail/connect", dependencies=[Depends(principal)])
def gmail_connect(user_id: UUID, response: Response) -> dict:
    from app.mail.accounts import load_credential
    from app.mail.oauth import STATE_TTL, consent_url, new_nonce
    with db.user_session(user_id) as s:
        client = load_credential(s, user_id, "gmail_client")
        email = s.execute(text("SELECT email FROM users WHERE id = :u"), {"u": user_id}).scalar()
    if client is None:
        raise HTTPException(409, "save your Google OAuth client first: PUT /users/{id}/credentials/gmail_client")
    nonce = new_nonce()
    # Lax: sent on Google's top-level redirect back to us, not on cross-site subrequests.
    response.set_cookie(OAUTH_COOKIE, nonce, max_age=STATE_TTL, httponly=True, samesite="lax",
                        secure=_redirect_uri().startswith("https://"), path="/oauth/google")
    return {"url": consent_url(user_id, client[1]["client_id"], _redirect_uri(), nonce, login_hint=email),
            "redirect_uri": _redirect_uri()}


@router.get("/oauth/google/callback", response_class=HTMLResponse)
def gmail_callback(request: Request, state: str = Query(...), code: str | None = Query(default=None),
                   error: str | None = Query(default=None)) -> HTMLResponse:
    from app.mail.accounts import load_credential, save_credential
    from app.mail.oauth import OAuthError, exchange_code, read_state

    def page(msg: str, status: int = 200) -> HTMLResponse:          # msg may carry Google/user text: escape it
        r = HTMLResponse(f"<!doctype html><title>JobBot</title><body style='font-family:sans-serif;padding:2rem'>"
                         f"<h2>{html.escape(msg)}</h2><p><a href='/app/'>Back to JobBot</a></p></body>",
                         status_code=status)
        r.delete_cookie(OAUTH_COOKIE, path="/oauth/google")
        return r
    try:
        uid = read_state(state, request.cookies.get(OAUTH_COOKIE))
    except OAuthError as e:
        return page(f"Connection failed: {e}", 400)
    if error or not code:
        return page(f"Google returned: {error or 'no code'}")
    with db.worker_session() as s:
        client = load_credential(s, uid, "gmail_client")
    if client is None:
        return page("Save your Google OAuth client first.")
    try:
        tokens = exchange_code(code, client[1]["client_id"], client[1]["client_secret"], _redirect_uri())
    except OAuthError as e:
        return page(f"Connection failed: {e}")
    email = None
    try:
        from app.mail.gmail import GmailClient
        g = GmailClient(client[1]["client_id"], client[1]["client_secret"], tokens["refresh_token"])
        email = g._req("GET", "/profile").get("emailAddress")
    except Exception:
        pass
    with db.worker_session() as s:
        save_credential(s, uid, "gmail", email or "gmail",
                        {"client_id": client[1]["client_id"], "client_secret": client[1]["client_secret"],
                         "refresh_token": tokens["refresh_token"]})       # rotated on every reconnect
    return page(f"Gmail connected{' for ' + email if email else ''}.")
