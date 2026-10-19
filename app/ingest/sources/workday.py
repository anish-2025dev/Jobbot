"""Workday career sites (CXS JSON endpoints behind every myworkdayjobs.com site).
List:   POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
        {"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}   (max 20/page)
Detail: GET  https://{host}/wday/cxs/{tenant}/{site}{externalPath}

Some tenants sit behind Cloudflare; when plain HTTP gets a 403 the runner can
pass `browser` (a Playwright-backed requester that first loads the career
site for cookies). Polite rates come from the shared limiter (0.3 req/s)."""
from __future__ import annotations

from typing import Callable

from app.ingest.detect import workday_parts
from app.ingest.http import Fetcher, NotFoundError, SourceError
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso, parse_relative_posted

PAGE = 20
MAX_POSTINGS = 200
BrowserRequester = Callable[[str, str, dict | None], dict]


def fetch(f: Fetcher, slug: str, host: str, company: str, title_filter: TitleFilter = None,
          browser: BrowserRequester | None = None, search_text: str = "") -> list[RawJob]:
    tenant, site, origin = workday_parts(slug, host)
    api = f"{origin}/wday/cxs/{tenant}/{site}"
    rate_key = f"workday:{host}"

    def post(url: str, body: dict) -> dict:
        try:
            return f.post_json("workday", url, body, rate_key=rate_key)
        except SourceError:
            if browser is None:
                raise
            return browser("POST", url, body)

    def get(url: str) -> dict:
        try:
            return f.get_json("workday", url, rate_key=rate_key)
        except SourceError:
            if browser is None:
                raise
            return browser("GET", url, None)

    out, offset, total = [], 0, None
    while offset < MAX_POSTINGS:
        page = post(f"{api}/jobs", {"appliedFacets": {}, "limit": PAGE, "offset": offset,
                                    "searchText": search_text})
        postings = page.get("jobPostings") or []
        total = page.get("total", total)
        for p in postings:
            if not keep(p.get("title", ""), title_filter) or not p.get("externalPath"):
                continue
            try:
                info = get(f"{api}{p['externalPath']}").get("jobPostingInfo") or {}
            except NotFoundError:
                continue
            req = info.get("jobReqId") or next(iter(p.get("bulletFields") or []), None)
            remote = (info.get("remoteType") or "").lower()
            out.append(RawJob(
                source="workday", ats="workday", company=company, company_slug=slug,
                title=(info.get("title") or p.get("title") or "").strip(),
                url=info.get("externalUrl") or f"{origin}/{site}{p['externalPath']}",
                apply_url=(info.get("externalUrl") or f"{origin}/{site}{p['externalPath']}").rstrip("/") + "/apply",
                req_id=str(req) if req else p["externalPath"],
                description=info.get("jobDescription") or "", description_is_html=True,
                location=info.get("location") or p.get("locationsText"),
                posted_at=parse_iso(info.get("startDate")) or parse_relative_posted(info.get("postedOn") or p.get("postedOn")),
                workplace="remote" if "remote" in remote else "hybrid" if "hybrid" in remote else None,
                employment_type=info.get("timeType"),
            ))
        offset += len(postings)
        if not postings or (total is not None and offset >= total):
            break
    return out


def probe(f: Fetcher, slug: str, host: str) -> bool:
    tenant, site, origin = workday_parts(slug, host)
    page = f.post_json("workday", f"{origin}/wday/cxs/{tenant}/{site}/jobs",
                       {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                       rate_key=f"workday:{host}")
    return "jobPostings" in page
