"""In-memory mailbox with the GmailClient surface the bot uses."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.mail.message import Message

_ids = itertools.count(1)


def make_msg(from_addr: str, subject: str, body: str = "", *, html: str = "", from_name: str = "",
             thread_id: str | None = None, in_reply_to: str | None = None, date: datetime | None = None) -> Message:
    mid = f"m{next(_ids):05d}"
    return Message(id=mid, thread_id=thread_id or f"t{mid}", from_addr=from_addr.lower(), from_name=from_name,
                   to=["me@example.com"], subject=subject, date=date or datetime.now(timezone.utc),
                   snippet=(body or "")[:120], text=body, html=html, in_reply_to=in_reply_to)


@dataclass
class FakeMailbox:
    messages: dict[str, Message] = field(default_factory=dict)
    labels: dict[str, set[str]] = field(default_factory=dict)
    trashed: set[str] = field(default_factory=set)
    sent: list[bytes] = field(default_factory=list)
    calls: list[tuple] = field(default_factory=list)

    def add(self, msg: Message) -> Message:
        self.messages[msg.id] = msg
        self.labels[msg.id] = {"INBOX"}
        return msg

    def list_ids(self, query: str = "", max_results: int = 200) -> list[str]:
        return [m for m in self.messages if m not in self.trashed][:max_results]

    def get(self, msg_id: str) -> Message:
        return self.messages[msg_id]

    def modify(self, msg_id: str, add=(), remove=()) -> None:
        self.calls.append(("modify", msg_id, tuple(add), tuple(remove)))
        labels = self.labels.setdefault(msg_id, set())
        labels |= set(add)
        labels -= set(remove)

    def trash(self, msg_id: str) -> None:
        self.calls.append(("trash", msg_id))
        self.trashed.add(msg_id)

    def untrash(self, msg_id: str) -> None:
        self.calls.append(("untrash", msg_id))
        self.trashed.discard(msg_id)

    def send(self, raw_mime: bytes, thread_id: str | None = None) -> dict:
        self.sent.append(raw_mime)
        n = len(self.sent)
        return {"id": f"sent{n}", "threadId": thread_id or f"thr-sent{n}"}

    def touched(self, msg_id: str) -> bool:
        return any(c[1] == msg_id for c in self.calls)
