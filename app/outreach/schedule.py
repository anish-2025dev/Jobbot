"""Send times (section 10.3): 9-11 am in the recipient's time zone, spaced
5-15 minutes apart, within OUTREACH_DAILY_EMAILS per user per day."""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

US_WEST = ("california", "san francisco", "seattle", "washington state", "oregon", "los angeles", "san jose",
           "mountain view", "palo alto", "sunnyvale", "nevada", "arizona", "utah", "ca,", ", ca", ", wa")
TZ_HINTS = [
    (("india", "bengaluru", "bangalore", "delhi", "gurugram", "gurgaon", "noida", "hyderabad", "pune", "mumbai",
      "chennai", "kolkata", "ahmedabad", "jaipur"), "Asia/Kolkata"),
    (("singapore",), "Asia/Singapore"), (("dubai", "abu dhabi", "uae", "united arab emirates"), "Asia/Dubai"),
    (("london", "united kingdom", "england", "uk"), "Europe/London"), (("dublin", "ireland"), "Europe/Dublin"),
    (("berlin", "munich", "germany"), "Europe/Berlin"), (("amsterdam", "netherlands"), "Europe/Amsterdam"),
    (("paris", "france"), "Europe/Paris"), (("toronto", "canada", "ontario"), "America/Toronto"),
    (("sydney", "melbourne", "australia"), "Australia/Sydney"), (("tokyo", "japan"), "Asia/Tokyo"),
    (US_WEST, "America/Los_Angeles"),
    (("united states", "usa", "new york", "boston", "austin", "texas", "chicago", "atlanta", "florida", "virginia"),
     "America/New_York"),
]


def tz_for(location: str | None, fallback: str) -> str:
    loc = (location or "").lower()
    for hints, tz in TZ_HINTS:
        if any(h in loc for h in hints):
            return tz
    return fallback


def next_slot(tz: str, after: datetime, rnd: random.Random | None = None) -> datetime:
    """Earliest time >= `after` inside 9:00-11:00 local, on a weekday."""
    rnd = rnd or random
    zone = ZoneInfo(tz)
    local = after.astimezone(zone)
    for _ in range(8):
        start = datetime.combine(local.date(), time(9, 0), zone)
        end = datetime.combine(local.date(), time(11, 0), zone)
        if local.weekday() < 5 and local < end:
            t = max(local, start)
            if t < end:
                return t.astimezone(timezone.utc)
        local = datetime.combine(local.date() + timedelta(days=1), time(9, 0), zone)
    return after


def plan_sends(contacts: list[dict], *, now: datetime, fallback_tz: str, already_today: int,
               daily_cap: int, rnd: random.Random | None = None) -> list[datetime | None]:
    """Send time per contact, spaced 5-15 min apart; None if over today's cap
    (those slide to the next day's window)."""
    rnd = rnd or random.Random()
    out, last = [], now
    used = already_today
    for c in contacts:
        tz = tz_for(c.get("location"), fallback_tz)
        t = next_slot(tz, last + timedelta(minutes=rnd.uniform(5, 15)) if out else last, rnd)
        if used >= daily_cap and t.date() == now.date():
            t = next_slot(tz, (now + timedelta(days=1)).replace(hour=0, minute=0), rnd)
        out.append(t)
        last = t
        used += 1
    return out
