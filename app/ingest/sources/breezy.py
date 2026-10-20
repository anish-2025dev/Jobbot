"""Breezy HR public job feed (keyless). GET https://{slug}.breezy.hr/json
The feed has no descriptions; scoring uses title, location and type."""
from __future__ import annotations

from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

URL = "https://{slug}.breezy.hr/json"


def fetch(f: Fetcher, slug: str, company: str, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("breezy", URL.format(slug=slug))
    out = []
    for j in data if isinstance(data, list) else []:
        title = (j.get("name") or "").strip()
        if not title or not keep(title, title_filter):
            continue
        loc = j.get("location") or {}
        where = loc.get("name") or ", ".join(x for x in ((loc.get("city")), (loc.get("country") or {}).get("name")) if x)
        out.append(RawJob(
            source="breezy", ats="breezy", company=(j.get("company") or {}).get("name") or company, company_slug=slug,
            title=title, url=j.get("url") or f"https://{slug}.breezy.hr/p/{j.get('friendly_id')}",
            req_id=j.get("id"), location=where or None, posted_at=parse_iso(j.get("published_date")),
            workplace="remote" if loc.get("is_remote") else None, salary_text=j.get("salary") or None,
            employment_type=(j.get("type") or {}).get("name"), description_is_html=False,
        ))
    return out


def probe(f: Fetcher, slug: str) -> bool:
    return isinstance(f.get_json("breezy", URL.format(slug=slug)), list)
