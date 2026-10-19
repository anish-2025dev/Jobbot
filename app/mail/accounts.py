"""Load a user's mail connection: Gmail OAuth (preferred) or IMAP/SMTP."""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.crypto import decrypt_json, encrypt_json
from app.mail.gmail import GmailClient
from app.mail.imap_client import ImapClient


def save_credential(s: Session, user_id: UUID, kind: str, username: str, secret: dict, meta: dict | None = None,
                    host: str | None = None) -> None:
    host = host or kind
    s.execute(text("""
        INSERT INTO credentials (user_id, host, username, secret_enc, kind, meta)
        VALUES (:u, :h, :n, :s, :k, CAST(:m AS jsonb))
        ON CONFLICT (user_id, host) DO UPDATE SET username = EXCLUDED.username, secret_enc = EXCLUDED.secret_enc,
            kind = EXCLUDED.kind, meta = EXCLUDED.meta, rotated_at = now()"""),
        {"u": user_id, "h": host, "n": username, "s": encrypt_json(secret, user_id, host), "k": kind,
         "m": json.dumps(meta or {})})


def load_credential(s: Session, user_id: UUID, host: str) -> tuple[str, dict, dict] | None:
    row = s.execute(text("SELECT username, secret_enc, meta FROM credentials WHERE user_id = :u AND host = :h"),
                    {"u": user_id, "h": host}).first()
    if row is None:
        return None
    return row.username, decrypt_json(row.secret_enc, user_id, host), row.meta or {}


def mail_client(s: Session, user_id: UUID, transport=None):
    """GmailClient, ImapClient, or None if the user hasn't connected mail."""
    g = load_credential(s, user_id, "gmail")
    if g:
        _, sec, _ = g
        return GmailClient(client_id=sec["client_id"], client_secret=sec["client_secret"],
                           refresh_token=sec["refresh_token"], transport=transport)
    i = load_credential(s, user_id, "imap")
    if i:
        user, sec, meta = i
        return ImapClient(username=user, password=sec["password"],
                          imap_host=meta.get("imap_host", "imap.gmail.com"),
                          smtp_host=meta.get("smtp_host", "smtp.gmail.com"),
                          trash_folder=meta.get("trash_folder") or None)
    return None


def mail_address(s: Session, user_id: UUID) -> str | None:
    row = s.execute(text("""SELECT username FROM credentials WHERE user_id = :u AND host IN ('gmail', 'imap')
                            ORDER BY host LIMIT 1"""), {"u": user_id}).first()
    return row.username if row else None
