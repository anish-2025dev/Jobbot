"""When to apply (section 8.5): daytime hours in the company's time zone,
30-120 s random jitter between applications on the same portal."""
from __future__ import annotations

import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.redis_client import get_redis

DAY_START, DAY_END = 9, 19          # local hours
JITTER = (30, 120)


def local_now(tz: str | None, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    try:
        return now.astimezone(ZoneInfo(tz or "Asia/Kolkata"))
    except (ZoneInfoNotFoundError, ValueError):
        return now.astimezone(ZoneInfo("Asia/Kolkata"))


def in_daytime(tz: str | None, now: datetime | None = None) -> bool:
    local = local_now(tz, now)
    return local.weekday() < 6 and DAY_START <= local.hour < DAY_END     # Mon-Sat, 9:00-19:00


def portal_ready(portal: str, now_ts: float | None = None) -> bool:
    """True if the jittered gap since the last application on this portal passed."""
    r = get_redis()
    if r is None:
        return True
    nxt = r.get(f"apply:next:{portal}")
    import time
    return nxt is None or float(nxt) <= (now_ts or time.time())


def mark_portal_used(portal: str, rnd: random.Random | None = None) -> float:
    import time
    gap = (rnd or random).uniform(*JITTER)
    r = get_redis()
    if r is not None:
        r.set(f"apply:next:{portal}", str(time.time() + gap), ex=600)
    return gap
