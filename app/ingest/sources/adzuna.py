"""Adzuna (India endpoint). Descriptions are ~500-char snippets; predicted
salaries (salary_is_predicted = "1") are ignored."""
from __future__ import annotations

from app.ingest.detect import detect, is_manual_portal
from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import parse_iso

URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


def fetch(f: Fetcher, app_id: str, app_key: str, what: str, where: str = "", country: str = "in",
          max_days_old: int = 3, page: int = 1) -> list[RawJob]:
    params = {"app_id": app_id, "app_key": app_key, "what": what, "results_per_page": 50,
              "max_days_old": max_days_old, "sort_by": "date", "content-type": "application/json"}
    if where:
        params["where"] = where
    data = f.get_json("adzuna", URL.format(country=country, page=page), params=params)
    out = []
    for j in data.get("results") or []:
        predicted = str(j.get("salary_is_predicted", "0")) == "1"
        det = detect(j.get("redirect_url"))
        out.append(RawJob(
            source="adzuna", ats=det.ats if det and not is_manual_portal(det) else "unknown",
            company=(j.get("company") or {}).get("display_name") or "",
            title=(j.get("title") or "").strip(), url=j.get("redirect_url") or "",
            apply_url=j.get("redirect_url"), req_id=str(j.get("id")),
            description=j.get("description") or "", description_is_html=False,
            location=(j.get("location") or {}).get("display_name"),
            posted_at=parse_iso(j.get("created")),
            salary_min=None if predicted else j.get("salary_min"),
            salary_max=None if predicted else j.get("salary_max"),
            salary_currency=None if predicted else "INR" if country == "in" else None,
            salary_period=None if predicted else "year",
            employment_type=j.get("contract_time"),
            easy_apply_only=is_manual_portal(det),
        ))
    return out
