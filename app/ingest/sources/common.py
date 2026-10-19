from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

TitleFilter = Callable[[str], bool] | None


def parse_iso(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):           # epoch seconds or ms
        v = float(value)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_relative_posted(text: str | None, now: datetime | None = None) -> datetime | None:
    """Workday-style 'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.lower()
    if "today" in t or "just posted" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*(day|hour|week|month)", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"hour": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n),
             "month": timedelta(days=30 * n)}[unit]
    return now - delta


def keep(title: str, title_filter: TitleFilter) -> bool:
    return title_filter is None or title_filter(title)
