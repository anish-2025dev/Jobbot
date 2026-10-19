"""SmartRecruiters public postings API (keyless).
List:   GET https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100&offset=N
Detail: GET .../postings/{id}   (description lives only here)
Details are fetched only for titles that pass the users' role filter."""
from __future__ import annotations

from app.ingest.http import Fetcher, NotFoundError
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

BASE = "https://api.smartrecruiters.com/v1/companies"
MAX_POSTINGS = 500


def fetch(f: Fetcher, company_id: str, company: str, title_filter: TitleFilter = None) -> list[RawJob]:
    out, offset = [], 0
    while offset < MAX_POSTINGS:
        page = f.get_json("smartrecruiters", f"{BASE}/{company_id}/postings",
                          params={"limit": 100, "offset": offset})
        items = page.get("content") or []
        for p in items:
            if not keep(p.get("name", ""), title_filter):
                continue
            try:
                d = f.get_json("smartrecruiters", f"{BASE}/{company_id}/postings/{p['id']}")
            except NotFoundError:
                continue
            sections = (d.get("jobAd") or {}).get("sections") or {}
            desc = "".join(f"<h3>{(sections.get(k) or {}).get('title', '')}</h3>{(sections.get(k) or {}).get('text', '')}"
                           for k in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"))
            loc = d.get("location") or p.get("location") or {}
            loc_text = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            exp = (d.get("experienceLevel") or p.get("experienceLevel") or {}).get("id")
            out.append(RawJob(
                source="smartrecruiters", ats="smartrecruiters",
                company=(d.get("company") or {}).get("name") or company, company_slug=company_id,
                title=(d.get("name") or p.get("name") or "").strip(),
                url=d.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company_id}/{p['id']}",
                apply_url=d.get("applyUrl"), req_id=str(p["id"]),   # posting id, as detect() reads it from URLs
                description=desc, description_is_html=True, location=loc_text or None,
                posted_at=parse_iso(d.get("releasedDate") or p.get("releasedDate")),
                workplace="remote" if loc.get("remote") else None,
                experience_months={"entry_level": 0, "internship": 0, "associate": 12,
                                   "mid_senior_level": 48}.get(exp or "", None),
                employment_type=(d.get("typeOfEmployment") or {}).get("label"),
            ))
        offset += len(items)
        if not items or offset >= int(page.get("totalFound") or 0):
            break
    return out


def probe(f: Fetcher, company_id: str) -> bool:
    page = f.get_json("smartrecruiters", f"{BASE}/{company_id}/postings", params={"limit": 1})
    return int(page.get("totalFound") or 0) > 0
