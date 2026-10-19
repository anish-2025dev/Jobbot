"""Email-finder providers (section 10.2): Apollo, Hunter, Snov.io via their
APIs with the user's own keys. No scraping.

Rotation (section 10.2.2): providers are tried in order; a provider whose
free credits run out (HTTP 402/429 quota responses) is marked exhausted
until the next month in provider_usage, and the next one is used. When all
are exhausted the job skips outreach and goes straight to application.

Every address is verified before use; unverified or catch-all addresses are
never used (section 10.2.4).

Vendor APIs change. Each provider is isolated here; check its current docs
and free-tier API access before relying on it (section 12).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)
ORDER = ("apollo", "hunter", "snov")


class ProviderError(Exception):
    pass


class CreditsExhausted(ProviderError):
    pass


class AllExhausted(ProviderError):
    pass


@dataclass
class Person:
    first_name: str
    last_name: str
    title: str
    email: str | None = None
    email_verified: bool = False          # provider says verified/valid (not catch-all)
    location: str | None = None
    provider: str = ""
    provider_id: str | None = None

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


def _raise_for(provider: str, r: httpx.Response) -> None:
    if r.status_code == 200:
        return
    body = r.text.lower()
    if r.status_code in (402, 429) or (r.status_code == 403 and ("limit" in body or "credit" in body or "quota" in body)):
        raise CreditsExhausted(f"{provider}: {r.status_code} {r.text[:150]}")
    raise ProviderError(f"{provider}: {r.status_code} {r.text[:150]}")


def _request(provider: str, transport, method: str, url: str, **kw) -> dict:
    """One provider call. Timeouts, connection errors and non-JSON bodies become ProviderError,
    so contact search moves on to the next provider instead of crashing the outreach tick."""
    try:
        with httpx.Client(timeout=30, transport=transport) as c:
            r = c.request(method, url, **kw)
    except httpx.HTTPError as e:
        raise ProviderError(f"{provider}: {type(e).__name__}: {e}") from e
    _raise_for(provider, r)
    try:
        data = r.json()
    except ValueError as e:
        raise ProviderError(f"{provider}: response is not JSON") from e
    if not isinstance(data, dict):
        raise ProviderError(f"{provider}: unexpected response shape ({type(data).__name__})")
    return data


@dataclass
class Apollo:
    api_key: str
    transport: httpx.BaseTransport | None = None
    name: str = "apollo"
    base: str = "https://api.apollo.io/api/v1"

    def _post(self, path: str, body: dict) -> dict:
        return _request(self.name, self.transport, "POST", f"{self.base}{path}", json=body,
                        headers={"X-Api-Key": self.api_key, "Content-Type": "application/json",
                                 "Cache-Control": "no-cache"})

    def resolve_domain(self, company: str) -> str | None:
        data = self._post("/mixed_companies/search", {"q_organization_name": company, "page": 1, "per_page": 1})
        orgs = data.get("organizations") or data.get("accounts") or []
        return (orgs[0].get("primary_domain") or orgs[0].get("domain")) if orgs else None

    def search(self, domain: str, titles: list[str], locations: list[str] | None = None) -> list[Person]:
        body = {"q_organization_domains_list": [domain], "person_titles": titles, "page": 1, "per_page": 25}
        if locations:
            body["person_locations"] = locations
        out = []
        for p in self._post("/mixed_people/search", body).get("people") or []:
            email = p.get("email")
            if email and "email_not_unlocked" in email:
                email = None
            loc = ", ".join(x for x in (p.get("city"), p.get("state"), p.get("country")) if x) or None
            out.append(Person(first_name=p.get("first_name") or "", last_name=p.get("last_name") or "",
                              title=p.get("title") or "", email=email,
                              email_verified=p.get("email_status") == "verified", location=loc,
                              provider=self.name, provider_id=p.get("id")))
        return out

    def reveal(self, person: Person, domain: str) -> Person:
        """people/match unlocks the email (costs a credit)."""
        body = {"id": person.provider_id} if person.provider_id else \
            {"first_name": person.first_name, "last_name": person.last_name, "domain": domain}
        p = self._post("/people/match", {**body, "reveal_personal_emails": False}).get("person") or {}
        person.email = p.get("email") or person.email
        person.email_verified = p.get("email_status") == "verified"
        return person

    def verify(self, email: str) -> str | None:
        return None                                   # Apollo has no standalone verifier; use email_status


@dataclass
class Hunter:
    api_key: str
    transport: httpx.BaseTransport | None = None
    name: str = "hunter"
    base: str = "https://api.hunter.io/v2"

    def _get(self, path: str, params: dict) -> dict:
        data = _request(self.name, self.transport, "GET", f"{self.base}{path}",
                        params={**params, "api_key": self.api_key})
        return data.get("data") or {}

    def resolve_domain(self, company: str) -> str | None:
        return self._get("/domain-search", {"company": company, "limit": 1}).get("domain")

    def search(self, domain: str, titles: list[str], locations: list[str] | None = None) -> list[Person]:
        data = self._get("/domain-search", {"domain": domain, "type": "personal", "limit": 25})
        self.pattern = data.get("pattern")
        out = []
        for e in data.get("emails") or []:
            status = ((e.get("verification") or {}).get("status") or "")
            out.append(Person(first_name=e.get("first_name") or "", last_name=e.get("last_name") or "",
                              title=e.get("position") or "", email=e.get("value"), email_verified=status == "valid",
                              provider=self.name))
        return out

    def find(self, first: str, last: str, domain: str) -> str | None:
        return self._get("/email-finder", {"domain": domain, "first_name": first, "last_name": last}).get("email")

    def verify(self, email: str) -> str | None:
        """valid | invalid | accept_all | webmail | disposable | unknown"""
        return self._get("/email-verifier", {"email": email}).get("status")


@dataclass
class Snov:
    client_id: str
    client_secret: str
    transport: httpx.BaseTransport | None = None
    name: str = "snov"
    base: str = "https://api.snov.io"
    _token: str | None = None
    _exp: float = 0

    def _auth(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        data = _request(self.name, self.transport, "POST", f"{self.base}/v1/oauth/access_token",
                        data={"grant_type": "client_credentials", "client_id": self.client_id,
                              "client_secret": self.client_secret})
        if not data.get("access_token"):
            raise ProviderError(f"{self.name}: no access token in response")
        self._token, self._exp = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _post(self, path: str, body: dict) -> dict:
        return _request(self.name, self.transport, "POST", f"{self.base}{path}",
                        data={**body, "access_token": self._auth()})

    def resolve_domain(self, company: str) -> str | None:
        return None

    def search(self, domain: str, titles: list[str], locations: list[str] | None = None) -> list[Person]:
        data = self._post("/v1/get-domain-emails-with-info", {"domain": domain, "type": "personal", "limit": 25,
                                                              "lastId": 0})
        return [Person(first_name=e.get("firstName") or "", last_name=e.get("lastName") or "",
                       title=e.get("position") or "", email=e.get("email"),
                       email_verified=(e.get("status") or "") == "verified", provider=self.name)
                for e in data.get("emails") or []]

    def verify(self, email: str) -> str | None:
        self._post("/v1/add-emails-to-verification", {"emails[]": email})
        res = self._post("/v1/get-emails-verification-status", {"emails[]": email}).get(email) or {}
        r = res.get("result") or {}
        if r.get("isCatchall"):
            return "accept_all"
        return {"valid": "valid", "not_valid": "invalid"}.get(r.get("smtpStatus"), "unknown")


# --- usage tracking / rotation ----------------------------------------------------------
def _next_month(today: date) -> datetime:
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc)


def usage_ok(s: Session, user_id: UUID, provider: str) -> bool:
    row = s.execute(text("""SELECT credits_used, credits_limit, exhausted_until FROM provider_usage
                            WHERE user_id = :u AND provider = :p"""), {"u": user_id, "p": provider}).first()
    if row is None:
        return True
    if row.exhausted_until and row.exhausted_until > datetime.now(timezone.utc):
        return False
    return row.credits_limit is None or row.credits_used < row.credits_limit


def charge(s: Session, user_id: UUID, provider: str, n: int = 1) -> None:
    s.execute(text("""INSERT INTO provider_usage (user_id, provider, credits_used) VALUES (:u, :p, :n)
                      ON CONFLICT (user_id, provider) DO UPDATE SET credits_used = provider_usage.credits_used + :n"""),
              {"u": user_id, "p": provider, "n": n})


def mark_exhausted(s: Session, user_id: UUID, provider: str) -> None:
    s.execute(text("""INSERT INTO provider_usage (user_id, provider, exhausted_until) VALUES (:u, :p, :t)
                      ON CONFLICT (user_id, provider) DO UPDATE SET exhausted_until = :t"""),
              {"u": user_id, "p": provider, "t": _next_month(date.today())})


def load_providers(s: Session, user_id: UUID, transport=None) -> list:
    from app.mail.accounts import load_credential
    out = []
    for name in ORDER:
        cred = load_credential(s, user_id, name)
        if not cred:
            continue
        _, sec, meta = cred
        if meta.get("monthly_limit"):
            s.execute(text("""INSERT INTO provider_usage (user_id, provider, credits_limit) VALUES (:u, :p, :l)
                              ON CONFLICT (user_id, provider) DO UPDATE SET credits_limit = :l"""),
                      {"u": user_id, "p": name, "l": int(meta["monthly_limit"])})
        if name == "apollo":
            out.append(Apollo(sec["api_key"], transport))
        elif name == "hunter":
            out.append(Hunter(sec["api_key"], transport))
        elif name == "snov":
            out.append(Snov(sec["client_id"], sec["client_secret"], transport))
    return out
