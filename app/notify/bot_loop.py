"""Telegram update loop: /start linking, commands, and inline-button
callbacks (mark done, undo, pin, approve/skip outreach, assisted submit)."""
from __future__ import annotations

import logging
import time
from typing import Callable
from uuid import UUID

from sqlalchemy import text

from app import db
from app.notify.telegram import TelegramBot, TelegramError, link_chat
from app.redis_client import get_redis

log = logging.getLogger(__name__)
Handler = Callable[[object, UUID, str], str]
HANDLERS: dict[str, Handler] = {}


def handler(prefix: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[prefix] = fn
        return fn
    return deco


def user_for_chat(s, chat_id: int) -> UUID | None:
    return s.execute(text("SELECT id FROM users WHERE telegram_chat_id = :c AND deleted_at IS NULL"),
                     {"c": chat_id}).scalar()


HELP = ("JobBot commands:\n/digest - today's summary\n/waiting - jobs waiting for you\n"
        "/pause - pause auto-apply and outreach\n/resume - resume\n/help")


def _ensure_handlers() -> None:
    import app.outreach.actions  # noqa: F401  (registers ok/skip/pin)


def handle_update(bot: TelegramBot, upd: dict) -> None:
    _ensure_handlers()
    if "callback_query" in upd:
        cq = upd["callback_query"]
        chat = (cq.get("message") or {}).get("chat", {}).get("id")
        data = cq.get("data") or ""
        prefix, _, arg = data.partition(":")
        with db.worker_session() as s:
            uid = user_for_chat(s, chat) if chat else None
            if uid is None or prefix not in HANDLERS:
                reply = "Not linked." if uid is None else "Unknown action."
            else:
                try:
                    reply = HANDLERS[prefix](s, uid, arg)
                except Exception as e:      # report, don't crash the loop
                    log.exception("callback %s failed", data)
                    reply = f"Failed: {e}"[:180]
        bot.answer_callback(cq["id"], reply)
        return
    msg = upd.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    body = (msg.get("text") or "").strip()
    if not chat or not body.startswith("/"):
        return
    cmd, _, arg = body.partition(" ")
    cmd = cmd.split("@")[0].lower()
    with db.worker_session() as s:
        if cmd == "/start":
            uid = link_chat(s, arg, chat) if arg else None
            bot.send(chat, "Linked. You'll get alerts here.\n\n" + HELP if uid else
                     "Send /start <code> with the code from your dashboard settings.")
            return
        uid = user_for_chat(s, chat)
        if uid is None:
            bot.send(chat, "This chat isn't linked yet. Send /start <code> from your dashboard.")
            return
        if cmd == "/digest":
            from app.notify.digest import compose
            bot.send(chat, compose(s, uid))
        elif cmd == "/waiting":
            from app.notify.digest import waiting_lines
            bot.send(chat, "\n".join(waiting_lines(s, uid, 10)) or "Waiting list is empty.")
        elif cmd in ("/pause", "/resume"):
            paused = cmd == "/pause"
            s.execute(text("""UPDATE users SET settings = jsonb_set(coalesce(settings, '{}'), '{paused}',
                              to_jsonb(CAST(:p AS boolean))) WHERE id = :u"""), {"p": paused, "u": uid})
            bot.send(chat, "Paused auto-apply and outreach." if paused else "Resumed.")
        else:
            bot.send(chat, HELP)


def run_forever(bot: TelegramBot, stop: Callable[[], bool] = lambda: False) -> None:
    r = get_redis()
    offset = int(r.get("tg:offset") or 0) if r is not None else None
    while not stop():
        try:
            updates = bot.updates(offset or None)
        except (TelegramError, Exception) as e:
            log.warning("getUpdates failed: %s", e)
            time.sleep(5)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                handle_update(bot, upd)
            except Exception:
                log.exception("update %s failed", upd.get("update_id"))
            if r is not None:
                r.set("tg:offset", offset)


# --- built-in handlers --------------------------------------------------------------
@handler("done")
def _done(s, uid: UUID, gmail_id: str) -> str:
    n = s.execute(text("""UPDATE responses SET user_done_at = now() WHERE user_id = :u AND gmail_id = :g
                          AND user_done_at IS NULL"""), {"u": uid, "g": gmail_id}).rowcount
    return "Marked done. It stays at least 7 days." if n else "Already done."


@handler("undo")
def _undo(s, uid: UUID, action_id: str) -> str:
    from app.mail.accounts import mail_client
    from app.mail.processor import undo
    client = mail_client(s, uid)
    if client is None:
        return "Mail isn't connected."
    return "Restored." if undo(client, s, uid, int(action_id)) else "Nothing to undo."
