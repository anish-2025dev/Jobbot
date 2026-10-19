"""Gmail OAuth (section 13). Each user brings their own Google Cloud OAuth
client (Desktop or Web type, published "In production" so refresh tokens
don't expire after 7 days). The state parameter is HMAC-signed, expires, and
carries a hash of a nonce that lives in a cookie of the browser that started
the flow, so a consent link can't be completed from another browser (login CSRF)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.config import get_settings
from app.mail.gmail import SCOPES, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
STATE_TTL = 900


class OAuthError(Exception):
    pass


def _secret() -> bytes:
    s = get_settings()
    return hashlib.sha256(f"oauth-state|{s.session_secret or s.encryption_key}".encode()).digest()


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def _nonce_tag(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()[:32]


def make_state(user_id: UUID, nonce: str) -> str:
    payload = json.dumps({"u": str(user_id), "e": int(time.time()) + STATE_TTL, "n": _nonce_tag(nonce)},
                         separators=(",", ":")).encode()
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(payload + b"." + sig).decode().rstrip("=")


def read_state(state: str, nonce: str | None) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))
        payload, sig = raw.rsplit(b".", 1)
    except Exception as e:
        raise OAuthError("malformed state") from e
    if not hmac.compare_digest(sig, hmac.new(_secret(), payload, hashlib.sha256).digest()[:16]):
        raise OAuthError("bad state signature")
    data = json.loads(payload)
    if data["e"] < time.time():
        raise OAuthError("state expired")
    if not nonce or not hmac.compare_digest(str(data.get("n", "")), _nonce_tag(nonce)):
        raise OAuthError("this sign-in was started in another browser; start it again from the dashboard")
    return UUID(data["u"])


def consent_url(user_id: UUID, client_id: str, redirect_uri: str, nonce: str,
                login_hint: str | None = None) -> str:
    q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
         "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
         "include_granted_scopes": "true", "state": make_state(user_id, nonce)}
    if login_hint:
        q["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(q)}"


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str,
                  transport: httpx.BaseTransport | None = None) -> dict:
    with httpx.Client(timeout=30, transport=transport) as c:
        r = c.post(TOKEN_URL, data={"code": code, "client_id": client_id, "client_secret": client_secret,
                                    "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    if r.status_code != 200:
        raise OAuthError(f"token exchange failed: {r.text[:200]}")
    data = r.json()
    if "refresh_token" not in data:
        raise OAuthError("no refresh token returned; remove the app's access in your Google account and retry")
    granted = set((data.get("scope") or "").split())
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        raise OAuthError(f"missing scopes: {missing}")
    return data
