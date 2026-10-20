"""Rippling ATS job board (public, keyless).
GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs          -> [{uuid, name, url, workLocation, department}]
GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}   -> description, createdOn, workLocations

The list has no descriptions, so details are fetched only for titles that pass the
title filter (capped per board per round)."""
from __future__ import annotations

from app.ingest.http import Fetcher, NotFoundError
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

BASE = "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs"
MAX_DETAILS = 25


def fetch(f: Fetcher, slug: str, company: str, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("rippling", BASE.format(slug=slug))
    out = []
    for j in data if isinstance(data, list) else []:
        title = " ".join((j.get("name") or "").split())
        if not title or not keep(title, title_filter):
            continue
        detail = {}
        if len(out) < MAX_DETAILS and j.get("uuid"):
            try:
                detail = f.get_json("rippling", f"{BASE.format(slug=slug)}/{j['uuid']}") or {}
            except NotFoundError:
                continue                    # closed between the list and the detail call
        desc = detail.get("description") or {}
        locs = detail.get("workLocations") or [(j.get("workLocation") or {}).get("label")]
        out.append(RawJob(
            source="rippling", ats="rippling", company=detail.get("companyName") or company, company_slug=slug,
            title=title, url=j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{j.get('uuid')}",
            req_id=j.get("uuid"), description="".join(v for v in desc.values() if isinstance(v, str)),
            description_is_html=True, location=", ".join(l for l in locs if l) or None,
            locations=[l for l in locs if l], posted_at=parse_iso(detail.get("createdOn")),
            employment_type=((detail.get("employmentType") or {}).get("id")),
        ))
    return out


def probe(f: Fetcher, slug: str) -> bool:
    return isinstance(f.get_json("rippling", BASE.format(slug=slug)), list)
