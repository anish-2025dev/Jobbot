"""Tier B search APIs that need a free operator key, queried per (role, location) like
JSearch and Adzuna.

Jooble   POST https://jooble.org/api/{key}  {"keywords", "location", "page"}
         -> {"totalCount", "jobs": [{title, location, snippet, salary, source, type, link, company, updated, id}]}
         Covers Indian boards (Naukri, Shine, TimesJobs, company sites) via its aggregation.
Findwork GET  https://findwork.dev/api/jobs/?search=&location=&sort_by=date  (Authorization: Token <key>)
         -> {"results": [{id, role, company_name, location, remote, url, text, date_posted, employment_type, keywords}]}
"""
from __future__ import annotations

from app.ingest.http import Fetcher, NotFoundError, SourceError
from app.ingest.models import RawJob
from app.ingest.sources.common import parse_iso
from app.ingest.sources.feeds import _job


def jooble(f: Fetcher, api_key: str, keywords: str, location: str = "") -> list[RawJob]:
    try:
        data = f.post_json("jooble", f"https://jooble.org/api/{api_key}",
                           {"keywords": keywords, "location": location, "page": "1"},
                           display_url="https://jooble.org/api/<key>")      # the key is in the path
    except (SourceError, NotFoundError) as e:
        message = str(e).replace(api_key, "[REDACTED]") if api_key else str(e)
        raise type(e)(message) from None
    out = []
    for j in (data or {}).get("jobs") or []:
        if not j.get("link") or not j.get("title"):
            continue
        out.append(_job("jooble", company=j.get("company") or "", title=j["title"], url=j["link"],
                        req_id=str(j.get("id")), description=j.get("snippet") or "", location=j.get("location"),
                        posted_at=parse_iso(j.get("updated")), salary_text=j.get("salary") or None,
                        employment_type=j.get("type") or None))
    return out


def findwork(f: Fetcher, api_key: str, search: str, location: str = "") -> list[RawJob]:
    params = {"search": search, "sort_by": "date"}
    if location:
        params["location"] = location
    data = f.get_json("findwork", "https://findwork.dev/api/jobs/", params=params,
                      headers={"Authorization": f"Token {api_key}"})
    out = []
    for j in (data or {}).get("results") or []:
        if not j.get("url") or not j.get("role"):
            continue
        out.append(_job("findwork", company=j.get("company_name") or "", title=j["role"], url=j["url"],
                        req_id=str(j.get("id")), description=j.get("text") or "", location=j.get("location"),
                        workplace="remote" if j.get("remote") else None, posted_at=parse_iso(j.get("date_posted")),
                        employment_type=j.get("employment_type")))
    return out
