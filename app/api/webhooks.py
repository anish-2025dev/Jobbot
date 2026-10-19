"""SMS OTP webhook (section 8.4). Android: an SMS-forwarder app; iPhone: an
iOS Shortcuts automation on messages containing "OTP"/"code". Both POST
here with the user's secret token. The code goes only to Redis (5-min TTL)
for the sign-ins the bot is waiting on right now; it is never logged or
stored in Postgres."""
from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import principal
from app.config import get_settings
from app.mail import otp
from app.mail.classify import extract_otp

router = APIRouter()


def sms_token(user_id: UUID | str) -> str:
    s = get_settings()
    key = hashlib.sha256(f"sms-webhook|{s.session_secret or s.encryption_key}".encode()).digest()
    return hmac.new(key, str(user_id).encode(), hashlib.sha256).hexdigest()[:40]


class Sms(BaseModel):
    text: str = Field(max_length=2000)
    sender: str | None = Field(default=None, max_length=100)


@router.get("/users/{user_id}/webhooks/sms", dependencies=[Depends(principal)])
def sms_setup(user_id: UUID, request: Request) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    return {"url": f"{base}/webhooks/sms/{user_id}", "header": "X-Webhook-Token", "token": sms_token(user_id),
            "body": {"text": "<message text>", "sender": "<optional>"}}


@router.post("/webhooks/sms/{user_id}")
def sms_in(user_id: UUID, body: Sms, x_webhook_token: str = Header(default="")) -> dict:
    if not x_webhook_token or not hmac.compare_digest(x_webhook_token, sms_token(user_id)):
        raise HTTPException(401, "bad webhook token")
    code = extract_otp(body.text)
    if not code:
        return {"accepted": False, "reason": "no code found"}
    domains = otp.waiting_domains(user_id)
    for d in domains:                      # SMS has no sender domain: hand it to every pending sign-in
        otp.put_code(user_id, d, code)
    return {"accepted": bool(domains), "waiting_sites": len(domains)}
