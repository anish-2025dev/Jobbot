"""OTPs and sign-in links (section 8.4).

Codes live only in Redis with a 5-minute TTL, are consumed once (GETDEL)
and are never logged or written to Postgres. The apply worker registers a
waiter for a site's domain, then polls; mail ingestion (or the worker's own
on-demand mailbox check) fills it. SMS codes arrive via the authenticated
webhook and land in the same keys.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from app.redis_client import get_redis

OTP_TTL = 300
LINK_TTL = 1800


def _k(kind: str, user_id, domain: str) -> str:
    return f"{kind}:{user_id}:{domain.lower().removeprefix('www.')}"


def register_waiter(user_id, domain: str) -> float:
    r = get_redis()
    started = datetime.now(timezone.utc).timestamp()
    if r is not None:
        r.set(_k("otpwait", user_id, domain), str(started), ex=OTP_TTL)
    return started


def waiting_domains(user_id) -> set[str]:
    r = get_redis()
    if r is None:
        return set()
    return {k.split(":", 2)[2] for k in r.scan_iter(f"otpwait:{user_id}:*")}


def put_code(user_id, domain: str, code: str) -> None:
    r = get_redis()
    if r is not None:
        r.set(_k("otp", user_id, domain), code, ex=OTP_TTL)


def put_link(user_id, domain: str, link: str) -> None:
    r = get_redis()
    if r is not None:
        r.set(_k("verify", user_id, domain), link, ex=LINK_TTL)


def take_code(user_id, domain: str) -> str | None:
    r = get_redis()
    return None if r is None else r.getdel(_k("otp", user_id, domain))


def take_link(user_id, domain: str) -> str | None:
    r = get_redis()
    return None if r is None else r.getdel(_k("verify", user_id, domain))


def wait_for_code(user_id, domain: str, timeout: float = OTP_TTL, poll=None, interval: float = 3.0,
                  sleep=time.sleep) -> str | None:
    """Block until a code for `domain` arrives (or timeout). `poll` (e.g. an
    on-demand mailbox check) is called each round so the worker doesn't wait
    for the 15-minute ingestion cycle."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if poll is not None:
            poll()
        code = take_code(user_id, domain)
        if code:
            r = get_redis()
            if r is not None:
                r.delete(_k("otpwait", user_id, domain))
            return code
        sleep(interval)
    return None


def totp_code(s, user_id, domain: str) -> str | None:
    """Authenticator-app codes (section 8.4): the TOTP secret is stored
    encrypted in `credentials` (host = 'totp:<site>'); the code is generated
    on demand and never stored."""
    import pyotp

    from app.mail.accounts import load_credential
    site = domain.lower().removeprefix("www.")
    cred = load_credential(s, user_id, f"totp:{site}")
    if cred is None:
        return None
    return pyotp.TOTP(cred[1]["secret"].replace(" ", "").upper()).now()
