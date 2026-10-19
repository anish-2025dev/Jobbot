"""Telegram buttons for outreach drafts and the waiting list."""
from __future__ import annotations

from uuid import UUID

from app.notify.bot_loop import handler


@handler("ok")
def _approve(s, uid: UUID, arg: str) -> str:
    from app.outreach.queue import decide
    rq, idx = arg.split(":")
    return decide(s, uid, int(rq), int(idx), True)


@handler("skip")
def _skip(s, uid: UUID, arg: str) -> str:
    from app.outreach.queue import decide
    rq, idx = arg.split(":")
    return decide(s, uid, int(rq), int(idx), False)


@handler("pin")
def _pin(s, uid: UUID, arg: str) -> str:
    from app.apply.waiting import pin
    t = pin(s, uid, int(arg))
    return f"Pinned until {t:%d %b %H:%M} UTC." if t else "Not on your waiting list."
