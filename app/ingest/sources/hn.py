"""HN 'Who is hiring?' via the Algolia API: newest thread by user
whoishiring, top-level comments parsed from the conventional first line
'Company | Role | Location | REMOTE | ...'."""
from __future__ import annotations

import re

from app.ingest.detect import detect
from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import parse_iso
from app.util.text import html_links, html_to_text

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"
_ROLE = re.compile(r"engineer|developer|sde|swe|programmer|intern|scientist|architect|devops|sre|analyst", re.I)
_LOC = re.compile(r"remote|onsite|on-site|hybrid|india|bangalore|bengaluru|delhi|gurgaon|gurugram|noida|"
                  r"hyderabad|pune|mumbai|chennai|london|berlin|new york|nyc|sf|san francisco|usa|europe", re.I)


def latest_thread(f: Fetcher) -> str | None:
    data = f.get_json("hn_whos_hiring", SEARCH, params={"tags": "story,author_whoishiring", "hitsPerPage": 10})
    for h in data.get("hits") or []:
        if "who is hiring" in (h.get("title") or "").lower():
            return str(h["objectID"])
    return None


def parse_comment(c: dict) -> RawJob | None:
    html = c.get("text") or ""
    text = html_to_text(html)
    first = text.split("\n", 1)[0]
    parts = [p.strip() for p in first.split("|") if p.strip()]
    if len(parts) < 2:
        return None
    company = re.sub(r"\(.*?\)", "", parts[0]).strip()
    role = next((p for p in parts[1:] if _ROLE.search(p)), None)
    if not role or not company:
        return None
    location = next((p for p in parts[1:] if _LOC.search(p) and p != role), None)
    links = [h for h, _ in html_links(html) if h.startswith("http")]
    url = links[0] if links else f"https://news.ycombinator.com/item?id={c['id']}"
    det = detect(url)
    remote = location and re.search(r"remote", location, re.I)
    return RawJob(source="hn_whos_hiring", ats=det.ats if det else "unknown", company=company,
                  company_slug=det.slug if det else None, title=role[:200], url=url,
                  apply_url=url, req_id=(det.req_id if det and det.req_id else f"hn-{c['id']}"),
                  description=html, description_is_html=True, location=location,
                  posted_at=parse_iso(c.get("created_at")), workplace="remote" if remote else None)


def fetch(f: Fetcher, thread_id: str | None = None, max_comments: int = 600) -> list[RawJob]:
    thread_id = thread_id or latest_thread(f)
    if not thread_id:
        return []
    item = f.get_json("hn_whos_hiring", ITEM.format(id=thread_id))
    out = []
    for c in (item.get("children") or [])[:max_comments]:
        job = parse_comment(c)
        if job:
            out.append(job)
    return out
