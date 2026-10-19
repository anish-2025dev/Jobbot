"""Greenhouse job board API (public, keyless).
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"""
from __future__ import annotations

from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

BASE = "https://boards-api.greenhouse.io/v1/boards"


def fetch(f: Fetcher, slug: str, company: str, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("greenhouse", f"{BASE}/{slug}/jobs", params={"content": "true"})
    out = []
    for j in data.get("jobs", []):
        if not keep(j.get("title", ""), title_filter):
            continue
        offices = [o.get("name") for o in j.get("offices") or [] if o.get("name")]
        loc = (j.get("location") or {}).get("name") or (offices[0] if offices else None)
        out.append(RawJob(
            source="greenhouse", ats="greenhouse", company=j.get("company_name") or company,
            company_slug=slug, title=j.get("title", "").strip(),
            url=j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j['id']}",
            apply_url=j.get("absolute_url"),
            req_id=str(j.get("requisition_id") or j["id"]),
            description=j.get("content") or "", description_is_html=True,
            location=loc, locations=offices,
            posted_at=parse_iso(j.get("first_published") or j.get("updated_at")),
        ))
    return out


def probe(f: Fetcher, slug: str) -> bool:
    data = f.get_json("greenhouse", f"{BASE}/{slug}/jobs")
    return isinstance(data.get("jobs"), list)
