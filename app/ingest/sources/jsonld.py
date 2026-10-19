"""Tier D: schema.org JobPosting JSON-LD on any career page (Darwinbox,
Keka, Zoho Recruit and most custom sites emit it for Google for Jobs).
Listing pages without JSON-LD are scanned for job links (capped)."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from app.ingest.detect import detect
from app.ingest.http import Fetcher, NotFoundError, SourceError
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

_SCRIPT = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_JOB_LINK = re.compile(r"/(jobs?|careers?|openings?|positions?|vacanc(y|ies)|requisitions?)/[^\"'#?]{3,}", re.I)
_UNIT = {"YEAR": "year", "MONTH": "month", "HOUR": "hour", "WEEK": "week", "DAY": "day"}
MAX_LINKS = 30


def _walk(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "JobPosting" in types:
            yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _walk(node[key])


def postings_from_html(html: str) -> list[dict]:
    out = []
    for raw in _SCRIPT.findall(html or ""):
        try:
            data = json.loads(raw.strip().rstrip(";"))
        except json.JSONDecodeError:
            continue
        out.extend(_walk(data))
    return out


def _location(p: dict) -> str | None:
    locs = p.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs] if locs else []
    names = []
    for l in locs:
        addr = (l or {}).get("address") or {}
        if isinstance(addr, str):
            names.append(addr)
            continue
        country = addr.get("addressCountry")
        country = country.get("name") if isinstance(country, dict) else country
        parts = [addr.get("addressLocality"), addr.get("addressRegion"), country]
        names.append(", ".join(x for x in parts if x))
    return "; ".join(n for n in names if n) or None


def to_raw(p: dict, page_url: str, company_hint: str) -> RawJob:
    org = p.get("hiringOrganization") or {}
    org_name = org.get("name") if isinstance(org, dict) else str(org)
    ident = p.get("identifier")
    req = ident.get("value") if isinstance(ident, dict) else ident
    sal = p.get("baseSalary") or {}
    val = sal.get("value") if isinstance(sal, dict) else None
    val = val if isinstance(val, dict) else {}
    url = p.get("url") or page_url
    det = detect(url)
    remote = "TELECOMMUTE" in json.dumps(p.get("jobLocationType") or "").upper()
    return RawJob(
        source="career_jsonld", ats=det.ats if det else "jsonld", company=org_name or company_hint,
        company_slug=det.slug if det else None, title=(p.get("title") or "").strip(), url=url,
        apply_url=p.get("directApply") and url or url, req_id=str(req) if req else None,
        description=p.get("description") or "", description_is_html=True, location=_location(p),
        posted_at=parse_iso(p.get("datePosted")), workplace="remote" if remote else None,
        salary_min=val.get("minValue") or val.get("value"), salary_max=val.get("maxValue") or val.get("value"),
        salary_currency=(sal.get("currency") if isinstance(sal, dict) else None),
        salary_period=_UNIT.get(str(val.get("unitText") or "").upper()),
        employment_type=p.get("employmentType") if isinstance(p.get("employmentType"), str) else None,
        company_domain=(urlparse(url).hostname or "").removeprefix("www.") or None,
    )


def fetch(f: Fetcher, careers_url: str, company: str, title_filter: TitleFilter = None) -> list[RawJob]:
    html = f.request("career_jsonld", "GET", careers_url, headers={"Accept": "text/html"}).text
    found = postings_from_html(html)
    pages = [(careers_url, p) for p in found]
    if not found:
        host = urlparse(careers_url).hostname
        links = []
        for m in re.finditer(r'href=["\']([^"\']+)["\']', html):
            href = urljoin(careers_url, m.group(1))
            if urlparse(href).hostname == host and _JOB_LINK.search(urlparse(href).path) and href not in links:
                links.append(href)
        for link in links[:MAX_LINKS]:
            try:
                sub = f.request("career_jsonld", "GET", link, headers={"Accept": "text/html"}).text
            except (NotFoundError, SourceError):
                continue
            pages += [(link, p) for p in postings_from_html(sub)]
    return [j for j in (to_raw(p, u, company) for u, p in pages) if j.title and keep(j.title, title_filter)]
