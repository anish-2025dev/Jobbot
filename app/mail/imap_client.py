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
    trash_folder: str | None = None      # None: find the \\Trash special-use folder (RFC 6154)
    _conn: imaplib.IMAP4_SSL | None = None
    _trash: str | None = None

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

    def _caps(self) -> set[str]:
        conn = self._imap()
        typ, data = conn.capability()             # post-login list; the greeting's may omit MOVE/UIDPLUS
        if typ == "OK" and data and data[0]:
            raw = data[0].decode(errors="replace") if isinstance(data[0], bytes) else str(data[0])
            return {c.upper() for c in raw.split()}
        return {c.upper() for c in conn.capabilities}

    def _trash_name(self) -> str:
        if self._trash:
            return self._trash
        if self.trash_folder:
            self._trash = self.trash_folder
            return self._trash
        typ, data = self._imap().list()
        if typ == "OK":
            for line in data or []:
                text_ = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
                if "\\Trash" in text_.split(")")[0]:
                    m = re.search(r'"([^"]+)"\s*$', text_) or re.search(r"(\S+)\s*$", text_)
                    if m:
                        self._trash = m.group(1)
                        return self._trash
        raise ImapError("no Trash folder found: set trash_folder in the IMAP credential's meta")

    def trash(self, msg_id: str) -> None:
        """Move one message to Trash. Never deletes when the move/copy didn't succeed, and never
        expunges other messages that happen to carry the Deleted flag."""
        conn, folder, caps = self._imap(), self._trash_name(), self._caps()
        if "MOVE" in caps:                                   # RFC 6851: atomic, nothing to expunge
            typ, data = conn.uid("move", msg_id, f'"{folder}"')
            if typ != "OK":
                raise ImapError(f"move to {folder} failed: {data!r}")
            return
        typ, data = conn.uid("copy", msg_id, f'"{folder}"')
        if typ != "OK":
            raise ImapError(f"copy to {folder} failed, message left in place: {data!r}")
        typ, data = conn.uid("store", msg_id, "+FLAGS", "(\\Deleted)")
        if typ != "OK":
            raise ImapError(f"flagging {msg_id} failed: {data!r}")
        if "UIDPLUS" in caps:                                # RFC 4315: expunge only this message
            typ, data = conn.uid("expunge", msg_id)
            if typ != "OK":
                raise ImapError(f"expunge {msg_id} failed: {data!r}")
        # Without UIDPLUS a plain EXPUNGE would also remove other flagged mail; the copy is already
        # in Trash and the original stays flagged \Deleted (hidden by most clients) until the user's
        # own client expunges it.

    def untrash(self, msg_id: str) -> None:
        raise ImapError("restore from Trash is not available over IMAP; restore it in the mail app")

    def send(self, raw_mime: bytes, thread_id: str | None = None) -> dict:
        msg = message_from_bytes(raw_mime)
        with smtplib.SMTP_SSL(self.smtp_host, 465) as s:
            s.login(self.username, self.password)
            s.send_message(msg)
        return {"id": msg.get("Message-ID", ""), "threadId": thread_id}
