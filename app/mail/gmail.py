"""Gmail REST client over httpx (no Google SDK): OAuth refresh, list, get,
label/star, trash/untrash, send.

Scopes (section 13): gmail.modify (read, label, trash) + gmail.send
(outreach). MAIL_DELETE_MODE=permanent would need the full mail scope;
the default is Trash, which Gmail empties after 30 days.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field

import httpx

from app.mail.message import Message, from_gmail

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send"]


class GmailError(Exception):
    pass


class GmailAuthError(GmailError):
    """Refresh token revoked or expired: the user must reconnect Gmail."""


@dataclass
class GmailClient:
    client_id: str
    client_secret: str
    refresh_token: str
    transport: httpx.BaseTransport | None = None
    _token: str | None = None
    _expires: float = 0.0
    _labels: dict[str, str] = field(default_factory=dict)

    def _http(self) -> httpx.Client:
        return httpx.Client(timeout=30, transport=self.transport)

    def access_token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        with self._http() as c:
            r = c.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                        "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        if r.status_code in (400, 401):
            raise GmailAuthError(f"token refresh failed: {r.text[:200]}")
        if r.status_code != 200:
            raise GmailError(f"token endpoint {r.status_code}")
        data = r.json()
        self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _req(self, method: str, path: str, **kw) -> dict:
        for attempt in range(3):
            with self._http() as c:
                r = c.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {self.access_token()}"}, **kw)
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code in (429, 500, 503) and attempt < 2:
                time.sleep(0 if self.transport else 2 ** attempt)
                continue
            if r.status_code >= 400:
                raise GmailError(f"{method} {path}: {r.status_code} {r.text[:200]}")
            return r.json() if r.content else {}
        raise GmailError(f"{method} {path}: retries exhausted")

    # --- reading -----------------------------------------------------------------
    def list_ids(self, query: str, max_results: int = 200) -> list[str]:
        ids: list[str] = []
        token = None
        while len(ids) < max_results:
            params = {"q": query, "maxResults": min(100, max_results - len(ids))}
            if token:
                params["pageToken"] = token
            data = self._req("GET", "/messages", params=params)
            ids += [m["id"] for m in data.get("messages") or []]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids

    def get(self, msg_id: str) -> Message:
        return from_gmail(self._req("GET", f"/messages/{msg_id}", params={"format": "full"}))

    # --- labels / actions ----------------------------------------------------------
    def label_id(self, name: str) -> str:
        if name in ("STARRED", "INBOX", "TRASH", "UNREAD", "IMPORTANT"):
            return name
        if not self._labels:
            self._labels = {l["name"]: l["id"] for l in self._req("GET", "/labels").get("labels", [])}
        if name not in self._labels:
            created = self._req("POST", "/labels", json={"name": name, "labelListVisibility": "labelShow",
                                                         "messageListVisibility": "show"})
            self._labels[name] = created["id"]
        return self._labels[name]

    def modify(self, msg_id: str, add: list[str] = (), remove: list[str] = ()) -> None:
        self._req("POST", f"/messages/{msg_id}/modify",
                  json={"addLabelIds": [self.label_id(a) for a in add],
                        "removeLabelIds": [self.label_id(r) for r in remove]})

    def trash(self, msg_id: str) -> None:
        self._req("POST", f"/messages/{msg_id}/trash")

    def untrash(self, msg_id: str) -> None:
        self._req("POST", f"/messages/{msg_id}/untrash")

    # --- sending -------------------------------------------------------------------
    def send(self, raw_mime: bytes, thread_id: str | None = None) -> dict:
        body = {"raw": base64.urlsafe_b64encode(raw_mime).decode().rstrip("=")}
        if thread_id:
            body["threadId"] = thread_id
        return self._req("POST", "/messages/send", json=body)
