"""Tier D fallback for JavaScript-only career pages: render with a browser,
then ask the cheap LLM to list the postings. Only runs when both a browser
renderer and an LLM are configured; otherwise the page is skipped."""
from __future__ import annotations

import logging
from typing import Callable

from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso
from app.llm.client import LLM, LLMError, LLMUnavailable

log = logging.getLogger(__name__)
Renderer = Callable[[str], tuple[str, list[tuple[str, str]]]]   # url -> (visible text, [(href, text)])

PROMPT = """Below is the visible text and the links of a company careers page.
List every job opening you can see. Return JSON:
{{"jobs": [{{"title": "...", "location": "...", "url": "absolute link to the posting", "posted": "ISO date or null"}}]}}
Use only openings present on the page. Do not invent any.

PAGE TEXT:
{text}

LINKS:
{links}"""


def fetch(careers_url: str, company: str, render: Renderer | None, llm: LLM,
          title_filter: TitleFilter = None) -> list[RawJob]:
    if render is None or not llm.available:
        return []
    text, links = render(careers_url)
    link_lines = "\n".join(f"{t} -> {h}" for h, t in links[:200] if h.startswith("http"))
    try:
        data = llm.complete_json(PROMPT.format(text=text[:12000], links=link_lines[:6000]), tier="cheap")
    except (LLMError, LLMUnavailable) as e:
        log.warning("generic extraction failed for %s: %s", careers_url, e)
        return []
    valid_links = {h for h, _ in links}
    out = []
    for j in (data.get("jobs") if isinstance(data, dict) else None) or []:
        if not isinstance(j, dict) or not j.get("title") or not keep(j["title"], title_filter):
            continue
        url = j.get("url") if j.get("url") in valid_links else careers_url   # never trust invented links
        out.append(RawJob(source="career_generic", ats="generic", company=company, title=j["title"].strip(),
                          url=url, apply_url=url, location=j.get("location"),
                          posted_at=parse_iso(j.get("posted")), description="", description_is_html=False))
    return out
