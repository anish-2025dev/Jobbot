"""JSearch (RapidAPI wrapper over Google for Jobs: LinkedIn/Indeed/Glassdoor...).
Heavy duplicates: apply links that point at a known ATS get that ATS's
canonical id; LinkedIn/Naukri-only listings are flagged manual (C12)."""
from __future__ import annotations

from app.ingest.detect import detect, is_manual_portal
from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import parse_iso

URL = "https://jsearch.p.rapidapi.com/search"
_PERIOD = {"YEAR": "year", "MONTH": "month", "HOUR": "hour"}


def fetch(f: Fetcher, api_key: str, query: str, country: str = "in", date_posted: str = "3days",
          pages: int = 1) -> list[RawJob]:
    headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    data = f.get_json("jsearch", URL, headers=headers,
                      params={"query": query, "page": 1, "num_pages": pages, "country": country,
                              "date_posted": date_posted})
    out = []
    for j in data.get("data") or []:
        links = [j.get("job_apply_link")] + [o.get("apply_link") for o in j.get("apply_options") or []]
        links = [l for l in links if l]
        dets = [(l, detect(l)) for l in links]
        ats_link = next(((l, d) for l, d in dets if d and not is_manual_portal(d) and d.ats != "unknown"), None)
        manual_only = bool(dets) and all(is_manual_portal(d) for _, d in dets)
        det = ats_link[1] if ats_link else None
        exp = (j.get("job_required_experience") or {})
        months = 0 if exp.get("no_experience_required") else exp.get("required_experience_in_months")
        loc = ", ".join(x for x in (j.get("job_city"), j.get("job_state"), j.get("job_country")) if x)
        website = j.get("employer_website") or ""
        domain = website.split("//")[-1].split("/")[0].removeprefix("www.") or None
        out.append(RawJob(
            source="jsearch", ats=det.ats if det else "unknown", company=j.get("employer_name") or "",
            company_slug=det.slug if det else None, title=(j.get("job_title") or "").strip(),
            url=ats_link[0] if ats_link else (j.get("job_apply_link") or j.get("job_google_link") or ""),
            apply_url=ats_link[0] if ats_link else j.get("job_apply_link"),
            req_id=det.req_id if det and det.req_id else j.get("job_id"),
            description=j.get("job_description") or "", description_is_html=False,
            location=loc or None, posted_at=parse_iso(j.get("job_posted_at_datetime_utc")),
            workplace="remote" if j.get("job_is_remote") else None,
            salary_min=j.get("job_min_salary"), salary_max=j.get("job_max_salary"),
            salary_currency=j.get("job_salary_currency"),
            salary_period=_PERIOD.get((j.get("job_salary_period") or "").upper()),
            experience_months=months, employment_type=j.get("job_employment_type"),
            company_domain=domain, easy_apply_only=manual_only,
        ))
    return out
