"""Recruitee careers API (public, keyless; full HTML descriptions).
GET https://{slug}.recruitee.com/api/offers/"""
from __future__ import annotations

from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

URL = "https://{slug}.recruitee.com/api/offers/"
_PERIOD = {"year": "year", "month": "month", "hour": "hour"}


def _when(value):
    return parse_iso(str(value).replace(" UTC", "+00:00")) if value else None


def fetch(f: Fetcher, slug: str, company: str, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("recruitee", URL.format(slug=slug))
    out = []
    for j in (data or {}).get("offers") or []:
        title = (j.get("title") or "").strip()
        if not title or not keep(title, title_filter) or j.get("status", "published") != "published":
            continue
        sal = j.get("salary") or {}
        workplace = "remote" if j.get("remote") else "hybrid" if j.get("hybrid") else "onsite" if j.get("on_site") else None
        out.append(RawJob(
            source="recruitee", ats="recruitee", company=j.get("company_name") or company, company_slug=slug, title=title,
            url=j.get("careers_url") or f"https://{slug}.recruitee.com/o/{j.get('slug')}",
            apply_url=j.get("careers_apply_url"), req_id=j.get("slug") or str(j.get("id")),   # = detect() on its URL (C4)
            description=f"{j.get('description') or ''}{j.get('requirements') or ''}", description_is_html=True,
            location=j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x) or None,
            locations=[l.get("name") for l in j.get("locations") or [] if l.get("name")],
            posted_at=_when(j.get("published_at") or j.get("created_at")), workplace=workplace,
            salary_min=sal.get("min"), salary_max=sal.get("max"), salary_currency=sal.get("currency"),
            salary_period=_PERIOD.get(sal.get("period") or ""), employment_type=j.get("employment_type_code"),
        ))
    return out


def probe(f: Fetcher, slug: str) -> bool:
    return isinstance((f.get_json("recruitee", URL.format(slug=slug)) or {}).get("offers"), list)
