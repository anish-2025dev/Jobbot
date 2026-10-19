"""Lever postings API (public, keyless; bare JSON array, epoch-ms timestamps).
GET https://api.lever.co/v0/postings/{slug}?mode=json"""
from __future__ import annotations

from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

BASE = "https://api.lever.co/v0/postings"
_PERIOD = {"per-year-salary": "year", "per-month-salary": "month", "per-hour-wage": "hour"}


def fetch(f: Fetcher, slug: str, company: str, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("lever", f"{BASE}/{slug}", params={"mode": "json"})
    out = []
    for j in data if isinstance(data, list) else []:
        title = (j.get("text") or "").strip()
        if not keep(title, title_filter):
            continue
        cats = j.get("categories") or {}
        lists = "".join(f"<h3>{l.get('text', '')}</h3><ul>{l.get('content', '')}</ul>" for l in j.get("lists") or [])
        desc = f"{j.get('description') or ''}{lists}{j.get('additional') or ''}"
        sal = j.get("salaryRange") or {}
        out.append(RawJob(
            source="lever", ats="lever", company=company, company_slug=slug, title=title,
            url=j.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{j['id']}",
            apply_url=j.get("applyUrl"), req_id=j.get("id"),
            description=desc, description_is_html=True,
            location=cats.get("location"), locations=cats.get("allLocations") or [],
            posted_at=parse_iso(j.get("createdAt")),
            workplace=(j.get("workplaceType") or "").lower() or None,
            salary_min=sal.get("min"), salary_max=sal.get("max"), salary_currency=sal.get("currency"),
            salary_period=_PERIOD.get(sal.get("interval", "")),
            employment_type=cats.get("commitment"),
        ))
    return out


def probe(f: Fetcher, slug: str) -> bool:
    return isinstance(f.get_json("lever", f"{BASE}/{slug}", params={"mode": "json", "limit": 1}), list)
