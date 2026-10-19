"""Telegram Bot API client and notifier.

Long polling (getUpdates) instead of a webhook, so it works on a home PC or
a free VM without a public URL. Linking: the dashboard shows a one-time
code; the user sends `/start <code>` to the bot.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)
MAX_LEN = 4000
Buttons = list[list[tuple[str, str]]]


class TelegramError(Exception):
    pass


@dataclass
class TelegramBot:
    token: str
    transport: httpx.BaseTransport | None = None

    def _call(self, method: str, payload: dict, timeout: float = 30) -> dict:
        with httpx.Client(timeout=timeout + 10, transport=self.transport) as c:
            r = c.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload)
        data = r.json() if r.content else {}
        if r.status_code != 200 or not data.get("ok"):
            raise TelegramError(f"{method}: {r.status_code} {str(data)[:200]}")
        return data.get("result") or {}

    def send(self, chat_id: int, text_: str, buttons: Buttons | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text_[:MAX_LEN], "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": label[:60], "callback_data": data[:64]} for label, data in row] for row in buttons]}
        return self._call("sendMessage", payload)

    def answer_callback(self, callback_id: str, text_: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text_[:190]})

    def updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=timeout) or []


def chat_id_for(s: Session, user_id: UUID) -> int | None:
    return s.execute(text("SELECT telegram_chat_id FROM users WHERE id = :u"), {"u": user_id}).scalar()


def new_link_code(s: Session, user_id: UUID) -> str:
    code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")[:8].upper()
    s.execute(text("UPDATE users SET telegram_link_code = :c WHERE id = :u"), {"c": code, "u": user_id})
    return code


def link_chat(s: Session, code: str, chat_id: int) -> UUID | None:
    return s.execute(text("""UPDATE users SET telegram_chat_id = :chat, telegram_link_code = NULL
                             WHERE telegram_link_code = :c RETURNING id"""),
                     {"chat": chat_id, "c": code.strip().upper()}).scalar()


class Notifier:
    """notify(user_id, text, buttons=None). Silently skips users who haven't
    linked Telegram; never raises into the caller (a notification failure
    must not roll back an apply or a mail action)."""

    def __init__(self, bot: TelegramBot | None, session_factory: Callable):
        self.bot = bot
        self.sessions = session_factory
        self.sent: list[tuple[UUID, str]] = []

    def __call__(self, user_id: UUID, text_: str, buttons: Buttons | None = None) -> bool:
        self.sent.append((user_id, text_))
        if self.bot is None:
            log.info("notify %s: %s", user_id, text_.splitlines()[0] if text_ else "")
            return False
        try:
            with self.sessions() as s:
                chat = chat_id_for(s, user_id)
            if chat is None:
                return False
            self.bot.send(chat, text_, buttons)
            return True
        except Exception as e:
            log.warning("telegram notify failed for %s: %s", user_id, e)
            return False


def get_notifier() -> Notifier:
    from app import db
    from app.config import get_settings
    token = get_settings().telegram_bot_token
    return Notifier(TelegramBot(token) if token else None, db.worker_session)
