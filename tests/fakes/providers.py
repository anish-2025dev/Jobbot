"""Fake Apollo / Hunter / Snov backends (documented response shapes)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx

PEOPLE = [
    {"first_name": "Priya", "last_name": "Sharma", "position": "Senior Software Engineer, Backend", "city": "Bengaluru"},
    {"first_name": "Arjun", "last_name": "Mehta", "position": "Engineering Manager, Payments", "city": "Bengaluru"},
    {"first_name": "Neha", "last_name": "Kapoor", "position": "Technical Recruiter", "city": "Gurugram"},
    {"first_name": "Rahul", "last_name": "Verma", "position": "VP Engineering", "city": "Bengaluru"},
    {"first_name": "Sana", "last_name": "Iqbal", "position": "Software Engineering Intern", "city": "Pune"},
]


@dataclass
class FakeProviders:
    domain: str = "northwindpay.com"
    exhausted: set[str] = field(default_factory=set)
    catch_all: set[str] = field(default_factory=set)       # addresses verifiers report as accept_all
    invalid: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def email(self, p) -> str:
        return f"{p['first_name'].lower()}.{p['last_name'].lower()}@{self.domain}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        u = urlparse(str(req.url))
        host, path = u.hostname, u.path
        q = parse_qs(u.query)
        self.calls.append((host, path))
        if host == "api.hunter.io":
            if "hunter" in self.exhausted:
                return httpx.Response(429, json={"errors": [{"details": "You have reached your monthly usage limit"}]})
            if q.get("api_key") != ["hunter-key"]:
                return httpx.Response(401, json={"errors": [{"details": "Invalid API key"}]})
            if path == "/v2/domain-search":
                if "company" in q:
                    return httpx.Response(200, json={"data": {"domain": self.domain}})
                return httpx.Response(200, json={"data": {"domain": self.domain, "pattern": "{first}.{last}", "emails": [
                    {"value": self.email(p), "type": "personal", "confidence": 95, "first_name": p["first_name"],
                     "last_name": p["last_name"], "position": p["position"],
                     "verification": {"status": "valid" if self.email(p) not in self.catch_all else "accept_all"}}
                    for p in PEOPLE]}})
            if path == "/v2/email-verifier":
                e = q["email"][0]
                status = "accept_all" if e in self.catch_all else "invalid" if e in self.invalid else "valid"
                return httpx.Response(200, json={"data": {"email": e, "status": status}})
        if host == "api.apollo.io":
            if "apollo" in self.exhausted:
                return httpx.Response(422 if False else 429, json={"error": "insufficient credits"})
            body = json.loads(req.content or b"{}")
            if req.headers.get("x-api-key") != "apollo-key":
                return httpx.Response(401)
            if path == "/api/v1/mixed_people/search":
                return httpx.Response(200, json={"people": [
                    {"id": f"ap{i}", "first_name": p["first_name"], "last_name": p["last_name"], "title": p["position"],
                     "city": p["city"], "country": "India", "email": "email_not_unlocked@domain.com",
                     "email_status": None} for i, p in enumerate(PEOPLE)]})
            if path == "/api/v1/people/match":
                p = PEOPLE[int(body["id"][2:])]
                return httpx.Response(200, json={"person": {"email": self.email(p), "email_status": "verified"}})
            if path == "/api/v1/mixed_companies/search":
                return httpx.Response(200, json={"organizations": [{"primary_domain": self.domain}]})
        if host == "api.snov.io":
            if path == "/v1/oauth/access_token":
                return httpx.Response(200, json={"access_token": "snov-token", "expires_in": 3600})
            form = parse_qs(req.content.decode())
            if form.get("access_token") != ["snov-token"]:
                return httpx.Response(401)
            if path == "/v1/get-domain-emails-with-info":
                return httpx.Response(200, json={"success": True, "emails": [
                    {"email": self.email(p), "firstName": p["first_name"], "lastName": p["last_name"],
                     "position": p["position"], "status": "verified"} for p in PEOPLE]})
            if path == "/v1/add-emails-to-verification":
                return httpx.Response(200, json={"success": True})
            if path == "/v1/get-emails-verification-status":
                e = form["emails[]"][0]
                return httpx.Response(200, json={e: {"result": {"smtpStatus": "valid", "isCatchall": e in self.catch_all}}})
        return httpx.Response(404)
