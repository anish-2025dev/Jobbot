"""Tier B job boards with public, keyless JSON feeds.

Each feed is polled at most every `interval_hours` (several ask for a few calls a day),
returns the newest postings, and is filtered by title before normalization. Most are
remote-first boards; the location hard filter keeps only what the user can take.
Postings link to the board's own page, so they reach the user as manual-apply jobs,
unless the link is a known ATS (then the company's board gets discovered too).

| key            | endpoint                                         | asks for            |
|----------------|--------------------------------------------------|---------------------|
| remotive       | remotive.com/api/remote-jobs                         | <= 4 calls/day      |
| remoteok       | remoteok.com/api                                 | link back, polite   |
| arbeitnow      | arbeitnow.com/api/job-board-api                  | polite              |
| jobicy         | jobicy.com/api/v2/remote-jobs?industry=...       | a few calls/day     |
| himalayas      | himalayas.app/jobs/api?limit=...                 | polite              |
| themuse        | themuse.com/api/public/jobs?category=&level=     | 500/hour unkeyed    |
| workingnomads  | workingnomads.com/api/exposed_jobs/              | polite              |
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.ingest.detect import detect, is_manual_portal
from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso


@dataclass(frozen=True)
class Feed:
    key: str
    fetch: Callable[[Fetcher, TitleFilter], list[RawJob]]
    interval_hours: int


def _job(source: str, *, company: str, title: str, url: str, apply_url: str | None = None, **kw) -> RawJob:
    det = detect(apply_url or url)
    ats = det.ats if det and not is_manual_portal(det) else "unknown"
    return RawJob(source=source, ats=ats, company=(company or "").strip(), title=" ".join((title or "").split()),
                  url=url, apply_url=apply_url, company_slug=det.slug if det and ats != "unknown" else None,
                  easy_apply_only=is_manual_portal(det), **kw)


def remotive(f: Fetcher, title_filter: TitleFilter = None) -> list[RawJob]:
    out = []
    data = f.get_json("remotive", "https://remotive.com/api/remote-jobs", params={"limit": 100})
    for j in (data or {}).get("jobs") or []:
        if not keep(j.get("title") or "", title_filter):
            continue
        out.append(_job("remotive", company=j.get("company_name"), title=j.get("title"), url=j.get("url"),
                        req_id=str(j.get("id")), description=j.get("description") or "",
                        location=j.get("candidate_required_location"), workplace="remote",
                        posted_at=parse_iso(j.get("publication_date")), salary_text=j.get("salary") or None,
                        employment_type=j.get("job_type")))
    return out


def remoteok(f: Fetcher, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("remoteok", "https://remoteok.com/api")
    out = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict) or not j.get("position"):
            continue                                     # first element is the legal notice
        if not keep(j["position"], title_filter):
            continue
        out.append(_job("remoteok", company=j.get("company"), title=j["position"], url=j.get("url"),
                        apply_url=j.get("apply_url"), req_id=str(j.get("id")), description=j.get("description") or "",
                        location=j.get("location") or "Remote", workplace="remote", posted_at=parse_iso(j.get("date")),
                        salary_min=j.get("salary_min") or None, salary_max=j.get("salary_max") or None,
                        salary_currency="USD" if j.get("salary_max") else None, salary_period="year" if j.get("salary_max") else None))
    return out


def arbeitnow(f: Fetcher, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("arbeitnow", "https://www.arbeitnow.com/api/job-board-api")
    out = []
    for j in (data or {}).get("data") or []:
        if not keep(j.get("title") or "", title_filter):
            continue
        out.append(_job("arbeitnow", company=j.get("company_name"), title=j.get("title"), url=j.get("url"),
                        req_id=j.get("slug"), description=j.get("description") or "", location=j.get("location"),
                        workplace="remote" if j.get("remote") else None, posted_at=parse_iso(j.get("created_at")),
                        employment_type=", ".join(j.get("job_types") or []) or None))
    return out


def jobicy(f: Fetcher, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("jobicy", "https://jobicy.com/api/v2/remote-jobs", params={"count": 100, "industry": "dev"})
    out = []
    for j in (data or {}).get("jobs") or []:
        if not keep(j.get("jobTitle") or "", title_filter):
            continue
        smin, smax = j.get("salaryMin") or j.get("annualSalaryMin"), j.get("salaryMax") or j.get("annualSalaryMax")
        out.append(_job("jobicy", company=j.get("companyName"), title=j.get("jobTitle"), url=j.get("url"),
                        req_id=str(j.get("id")), description=j.get("jobDescription") or j.get("jobExcerpt") or "",
                        location=j.get("jobGeo"), workplace="remote", posted_at=parse_iso(j.get("pubDate")),
                        salary_min=smin, salary_max=smax, salary_currency=j.get("salaryCurrency") if smax else None,
                        salary_period={"yearly": "year", "monthly": "month", "hourly": "hour"}.get(j.get("salaryPeriod") or "")
                        if smax else None, employment_type=", ".join(j.get("jobType") or []) or None))
    return out


def himalayas(f: Fetcher, title_filter: TitleFilter = None, pages: int = 5) -> list[RawJob]:
    items, cursor = [], None
    for _ in range(pages):                     # 20 per page; follow nextCursor for the newest ~100
        params = {"limit": 20, **({"cursor": cursor} if cursor else {})}
        data = f.get_json("himalayas", "https://himalayas.app/jobs/api", params=params) or {}
        items += data.get("jobs") or []
        cursor = data.get("nextCursor")
        if not cursor:
            break
    out = []
    for j in items:
        if not keep(j.get("title") or "", title_filter):
            continue
        restrictions = j.get("locationRestrictions") or []
        out.append(_job("himalayas", company=j.get("companyName"), title=j.get("title"),
                        url=j.get("guid") or j.get("applicationLink"), apply_url=j.get("applicationLink"),
                        req_id=j.get("guid"), description=j.get("description") or j.get("excerpt") or "",
                        location=", ".join(restrictions) if restrictions else "Worldwide", locations=list(restrictions),
                        workplace="remote", posted_at=parse_iso(j.get("pubDate")),
                        salary_min=j.get("minSalary"), salary_max=j.get("maxSalary"),
                        salary_currency=j.get("currency") if j.get("maxSalary") else None,
                        salary_period="year" if j.get("maxSalary") else None, employment_type=j.get("employmentType")))
    return out


def themuse(f: Fetcher, title_filter: TitleFilter = None) -> list[RawJob]:
    out = []
    for page in range(3):
        data = f.get_json("themuse", "https://www.themuse.com/api/public/jobs", params=[
            ("page", page), ("category", "Software Engineering"), ("category", "Data and Analytics"),
            ("category", "Data Science"), ("category", "IT"), ("level", "Entry Level"), ("level", "Internship")])
        for j in (data or {}).get("results") or []:
            if not keep(j.get("name") or "", title_filter):
                continue
            locs = [l.get("name") for l in j.get("locations") or [] if l.get("name")]
            url = (j.get("refs") or {}).get("landing_page")
            if not url:
                continue
            out.append(_job("themuse", company=(j.get("company") or {}).get("name"), title=j.get("name"), url=url,
                            req_id=str(j.get("id")), description=j.get("contents") or "", location="; ".join(locs) or None,
                            locations=locs, workplace="remote" if any("remote" in l.lower() for l in locs) else None,
                            posted_at=parse_iso(j.get("publication_date"))))
        if page + 1 >= int((data or {}).get("page_count") or 0):
            break
    return out


def workingnomads(f: Fetcher, title_filter: TitleFilter = None) -> list[RawJob]:
    data = f.get_json("workingnomads", "https://www.workingnomads.com/api/exposed_jobs/")
    out = []
    for j in data if isinstance(data, list) else []:
        if not keep(j.get("title") or "", title_filter):
            continue
        out.append(_job("workingnomads", company=j.get("company_name"), title=j.get("title"), url=j.get("url"),
                        req_id=j.get("url"), description=j.get("description") or "", location=j.get("location"),
                        workplace="remote", posted_at=parse_iso(j.get("pub_date"))))
    return out


FEEDS: dict[str, Feed] = {feed.key: feed for feed in (
    Feed("remotive", remotive, 6), Feed("remoteok", remoteok, 6), Feed("arbeitnow", arbeitnow, 6),
    Feed("jobicy", jobicy, 6), Feed("himalayas", himalayas, 4), Feed("themuse", themuse, 6),
    Feed("workingnomads", workingnomads, 6),
)}
