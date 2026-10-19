"""Ashby posting API (public, keyless).
GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"""
from __future__ import annotations

from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

BASE = "https://api.ashbyhq.com/posting-api/job-board"
_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}


def fetch(f: Fetcher, slug: str, company: str, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("ashby", f"{BASE}/{slug}", params={"includeCompensation": "true"})
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False or not keep(j.get("title", ""), title_filter):
            continue
        comp = next((c for c in (j.get("compensation") or {}).get("summaryComponents") or []
                     if c.get("compensationType") == "Salary"), {})
        interval = (comp.get("interval") or "").upper()
        wp = (j.get("workplaceType") or "").lower()
        out.append(RawJob(
            source="ashby", ats="ashby", company=company, company_slug=slug,
            title=j.get("title", "").strip(),
            url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j['id']}",
            apply_url=j.get("applyUrl"), req_id=j.get("id"),
            description=j.get("descriptionHtml") or j.get("descriptionPlain") or "",
            description_is_html=bool(j.get("descriptionHtml")),
            location=j.get("location"),
            locations=[s.get("location") for s in j.get("secondaryLocations") or [] if s.get("location")],
            posted_at=parse_iso(j.get("publishedAt")),
            workplace="remote" if j.get("isRemote") and not wp else _WORKPLACE.get(wp),
            salary_min=comp.get("minValue"), salary_max=comp.get("maxValue"),
            salary_currency=comp.get("currencyCode"),
            salary_period="year" if "YEAR" in interval else "month" if "MONTH" in interval
            else "hour" if "HOUR" in interval else None,
            salary_text=(j.get("compensation") or {}).get("compensationTierSummary"),
            employment_type=j.get("employmentType"),
        ))
    return out


def probe(f: Fetcher, slug: str) -> bool:
    return isinstance(f.get_json("ashby", f"{BASE}/{slug}").get("jobs"), list)
