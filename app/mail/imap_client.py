"""IMAP + SMTP fallback (section 13) for users without their own Google
Cloud project: Gmail with an app password, or any IMAP provider.
Same surface as GmailClient for what the bot needs."""
from __future__ import annotations

import imaplib
import re
import smtplib
from dataclasses import dataclass
from email import message_from_bytes

from app.mail.message import Message, from_rfc822


class ImapError(Exception):
    pass


@dataclass
class ImapClient:
    username: str
    password: str
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"
    trash_folder: str = "[Gmail]/Trash"
    _conn: imaplib.IMAP4_SSL | None = None

    def _imap(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            conn = imaplib.IMAP4_SSL(self.imap_host)
            try:
                conn.login(self.username, self.password)
            except imaplib.IMAP4.error as e:
                raise ImapError(f"IMAP login failed: {e}") from e
            conn.select("INBOX")
            self._conn = conn
        return self._conn

    def list_ids(self, query: str = "", max_results: int = 200, since_days: int = 2) -> list[str]:
        from datetime import date, timedelta
        since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        typ, data = self._imap().uid("search", None, f"(SINCE {since})")
        if typ != "OK":
            raise ImapError("search failed")
        return [u.decode() for u in data[0].split()][-max_results:]

    def get(self, msg_id: str) -> Message:
        typ, data = self._imap().uid("fetch", msg_id, "(RFC822 X-GM-THRID X-GM-LABELS)")
        if typ != "OK" or not data or data[0] is None:
            raise ImapError(f"fetch {msg_id} failed")
        meta, raw = data[0][0].decode(errors="replace"), data[0][1]
        msg = from_rfc822(raw, msg_id)
        m = re.search(r"X-GM-THRID (\d+)", meta)
        if m:
            msg.thread_id = m.group(1)
        return msg

    def modify(self, msg_id: str, add: list[str] = (), remove: list[str] = ()) -> None:
        conn = self._imap()
        for label in add:
            if label == "STARRED":
                conn.uid("store", msg_id, "+FLAGS", "(\\Flagged)")
            else:
                conn.uid("store", msg_id, "+X-GM-LABELS", f'("{label}")')
        for label in remove:
            if label == "STARRED":
                conn.uid("store", msg_id, "-FLAGS", "(\\Flagged)")

    def trash(self, msg_id: str) -> None:
        conn = self._imap()
        conn.uid("copy", msg_id, f'"{self.trash_folder}"')
        conn.uid("store", msg_id, "+FLAGS", "(\\Deleted)")
        conn.expunge()

    def untrash(self, msg_id: str) -> None:
        raise ImapError("restore from Trash is not available over IMAP; restore it in the mail app")

    def send(self, raw_mime: bytes, thread_id: str | None = None) -> dict:
        msg = message_from_bytes(raw_mime)
        with smtplib.SMTP_SSL(self.smtp_host, 465) as s:
            s.login(self.username, self.password)
            s.send_message(msg)
        return {"id": msg.get("Message-ID", ""), "threadId": thread_id}
