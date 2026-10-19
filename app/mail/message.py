"""Normalized email message from either the Gmail API or IMAP."""
from __future__ import annotations

import base64
import email
import email.policy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from app.util.text import html_links, html_to_text


@dataclass
class Message:
    id: str
    thread_id: str | None
    from_addr: str
    from_name: str
    to: list[str]
    subject: str
    date: datetime | None
    snippet: str = ""
    text: str = ""
    html: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        return self.from_addr.rsplit("@", 1)[-1].lower() if "@" in self.from_addr else ""

    @property
    def body(self) -> str:
        """Plain text body (from text/plain, else stripped HTML)."""
        return self.text or html_to_text(self.html)

    @property
    def links(self) -> list[tuple[str, str]]:
        found = html_links(self.html) if self.html else []
        for url in re.findall(r"https?://[^\s<>\"')]+", self.text or ""):
            found.append((url.rstrip(".,"), ""))
        return found


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _walk_gmail(part: dict, out: dict) -> None:
    mime = part.get("mimeType", "")
    data = (part.get("body") or {}).get("data")
    if data and mime == "text/plain" and not out.get("text"):
        out["text"] = _b64(data).decode("utf-8", "replace")
    elif data and mime == "text/html" and not out.get("html"):
        out["html"] = _b64(data).decode("utf-8", "replace")
    for p in part.get("parts") or []:
        _walk_gmail(p, out)


def from_gmail(msg: dict) -> Message:
    payload = msg.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
    body: dict = {}
    _walk_gmail(payload, body)
    name, addr = parseaddr(headers.get("from", ""))
    date = None
    if msg.get("internalDate"):
        date = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    elif headers.get("date"):
        try:
            date = parsedate_to_datetime(headers["date"])
        except (TypeError, ValueError):
            date = None
    return Message(
        id=msg["id"], thread_id=msg.get("threadId"), from_addr=addr.lower(), from_name=name,
        to=[a.lower() for _, a in getaddresses([headers.get("to", "")]) if a],
        subject=headers.get("subject", ""), date=date, snippet=msg.get("snippet", ""),
        text=body.get("text", ""), html=body.get("html", ""), headers=headers,
        labels=msg.get("labelIds") or [], in_reply_to=headers.get("in-reply-to"),
        references=(headers.get("references") or "").split())


def from_rfc822(raw: bytes, msg_id: str, labels: list[str] | None = None) -> Message:
    m = email.message_from_bytes(raw, policy=email.policy.default)
    text = html = ""
    for part in m.walk() if m.is_multipart() else [m]:
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            continue
        if ctype == "text/plain" and not text:
            text = content
        elif ctype == "text/html" and not html:
            html = content
    name, addr = parseaddr(str(m.get("from", "")))
    try:
        date = parsedate_to_datetime(str(m["date"])) if m["date"] else None
    except (TypeError, ValueError):
        date = None
    headers = {k.lower(): str(v) for k, v in m.items()}
    return Message(id=msg_id, thread_id=headers.get("x-gm-thrid"), from_addr=addr.lower(), from_name=name,
                   to=[a.lower() for _, a in getaddresses([headers.get("to", "")]) if a],
                   subject=headers.get("subject", ""), date=date, text=text, html=html, headers=headers,
                   labels=labels or [], in_reply_to=headers.get("in-reply-to"),
                   references=(headers.get("references") or "").split())
