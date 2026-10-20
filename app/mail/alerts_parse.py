"""Tier C: job-alert emails (section 5) from LinkedIn, Naukri, Indeed, Glassdoor,
foundit, Wellfound, Instahyre, Internshala, Cutshort, Hirist, Unstop, Shine,
TimesJobs, iimjobs, apna and YC Work at a Startup. Parsing the alerts the user
already receives needs no scraping and carries no account risk. These jobs are
manual-only (C12): the bot never applies inside these portals."""
from __future__ import annotations

import re

from app.ingest.detect import detect
from app.ingest.models import RawJob
from app.mail.message import Message

SOURCES = {"linkedin": "alert_linkedin", "naukri": "alert_naukri", "wellfound": "alert_wellfound",
           "instahyre": "alert_instahyre", "indeed": "alert_indeed", "glassdoor": "alert_glassdoor",
           "foundit": "alert_foundit", "monsterindia": "alert_foundit", "internshala": "alert_internshala",
           "cutshort": "alert_cutshort", "hirist": "alert_hirist", "unstop": "alert_unstop", "shine": "alert_shine",
           "timesjobs": "alert_timesjobs", "iimjobs": "alert_iimjobs", "apna": "alert_apna",
           "workatastartup": "alert_workatastartup", "ycombinator": "alert_workatastartup"}
_SKIP_TEXT = re.compile(r"^(view( job)?|apply( now)?|see (all|more)|easy apply|unsubscribe|save|more jobs|\d+ applicants?)$", re.I)
_EXP = re.compile(r"^\d+\s*-\s*\d+\s*yrs?$|^\d+\+?\s*yrs?$", re.I)


def portal_of(msg: Message) -> str | None:
    d = msg.domain
    for key in SOURCES:
        if key in d:
            return key
    return None


def _title_from_url(url: str) -> str:
    m = re.search(r"job-listings-([a-z0-9-]+?)-\d{6,}", url)
    if m:
        return m.group(1).replace("-", " ").title()
    return ""


def parse_alert(msg: Message) -> list[RawJob]:
    portal = portal_of(msg)
    if portal is None:
        return []
    source = SOURCES[portal]
    lines = [l.strip() for l in msg.body.split("\n") if l.strip()]
    jobs: dict[str, RawJob] = {}
    for href, label in msg.links:
        det = detect(href)
        if det is None or det.req_id is None:
            continue
        key = f"{det.ats}:{det.req_id}"
        title = label.strip() if label and not _SKIP_TEXT.match(label.strip()) else ""
        if key in jobs:
            if title and not jobs[key].title:
                jobs[key].title = title
            continue
        jobs[key] = RawJob(source=source, ats="unknown", company="", title=title or _title_from_url(href),
                           url=href.split("?")[0], apply_url=href.split("?")[0], req_id=det.req_id,
                           description="", description_is_html=False, easy_apply_only=True, posted_at=msg.date)
    # company / location: the 1-3 lines after each title in the text body
    for job in jobs.values():
        if not job.title:
            continue
        try:
            i = next(i for i, l in enumerate(lines) if l.lower() == job.title.lower())
        except StopIteration:
            continue
        rest = [l for l in lines[i + 1:i + 5] if not _SKIP_TEXT.match(l)]
        if rest:
            parts = re.split(r"\s+[·•|]\s+", rest[0])
            job.company = parts[0]
            if len(parts) > 1:
                job.location = parts[1]
            elif len(rest) > 1:
                nxt = rest[1]
                if _EXP.match(nxt) and len(rest) > 2:
                    job.description = f"Experience: {nxt}"
                    nxt = rest[2]
                job.location = nxt
    return [j for j in jobs.values() if j.title and j.company]
