"""Recognize ATS postings and career-board slugs from URLs.

An aggregator listing (JSearch/LinkedIn/Adzuna) whose apply link points at
Greenhouse/Lever/... gets the same (ats, slug, req_id) as the direct fetch,
so its canonical hash matches and C4 holds across portals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class Detected:
    ats: str                    # ats_type value, or "linkedin"/"naukri"/... for manual-only portals
    slug: str | None = None
    req_id: str | None = None
    host: str | None = None


MANUAL_PORTALS = {
    "linkedin.com": "linkedin", "naukri.com": "naukri", "indeed.com": "indeed", "indeed.co.in": "indeed",
    "glassdoor.com": "glassdoor", "glassdoor.co.in": "glassdoor", "foundit.in": "foundit",
    "instahyre.com": "instahyre", "wellfound.com": "wellfound", "angel.co": "wellfound",
    "shine.com": "shine", "internshala.com": "internshala", "cutshort.io": "cutshort",
    "hirist.tech": "hirist", "hirist.com": "hirist", "unstop.com": "unstop", "timesjobs.com": "timesjobs",
    "iimjobs.com": "iimjobs", "apna.co": "apna", "workatastartup.com": "workatastartup",
    "foundit.sg": "foundit", "monsterindia.com": "foundit",
}

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def detect(url: str | None) -> Detected | None:
    if not url:
        return None
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    path = u.path or ""
    q = parse_qs(u.query)

    if host.endswith("greenhouse.io"):
        if "for" in q:  # embed: /embed/job_app?for=slug&token=123
            return Detected("greenhouse", q["for"][0], (q.get("token") or [None])[0], host)
        m = re.match(r"^/([^/]+)/jobs/(\d+)", path)
        if m:
            return Detected("greenhouse", m.group(1), m.group(2), host)
        m = re.match(r"^/([^/?#]+)/?$", path)
        if m and m.group(1) not in ("embed", "v1"):
            return Detected("greenhouse", m.group(1), None, host)
    if "gh_jid" in q:  # company site embedding a Greenhouse board
        return Detected("greenhouse", None, q["gh_jid"][0], host)

    if host == "jobs.lever.co" or host.endswith(".lever.co"):
        m = re.match(rf"^/([^/]+)(?:/({_UUID}))?", path, re.I)
        if m:
            return Detected("lever", m.group(1), m.group(2), host)

    if host == "jobs.ashbyhq.com":
        m = re.match(rf"^/([^/]+)(?:/({_UUID}))?", path, re.I)
        if m:
            return Detected("ashby", m.group(1), m.group(2), host)

    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        m = re.match(r"^/([^/]+)(?:/(\d+))?", path)
        if m:
            return Detected("smartrecruiters", m.group(1), m.group(2), host)

    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m:
        tenant = m.group(1)
        parts = [p for p in path.split("/") if p]
        if parts and re.match(r"^[a-z]{2}-[A-Z]{2}$", parts[0]):
            parts = parts[1:]
        site = parts[0] if parts else None
        req = None
        rm = re.search(r"_([A-Za-z]*-?\d[\w-]*)$", path.rstrip("/"))
        if rm and "job" in parts:
            req = rm.group(1)
        return Detected("workday", f"{tenant}/{site}" if site else tenant, req, host)

    m = re.match(r"^([a-z0-9-]+)\.recruitee\.com$", host)
    if m:
        om = re.match(r"^/o/([^/?#]+)", path)
        return Detected("recruitee", m.group(1), om.group(1) if om else None, host)

    if host == "ats.rippling.com":
        m = re.match(rf"^/(?:[a-z]{{2}}-[A-Z]{{2}}/)?([^/]+)/jobs(?:/({_UUID}))?", path, re.I)
        if m:
            return Detected("rippling", m.group(1), m.group(2), host)

    m = re.match(r"^([a-z0-9-]+)\.breezy\.hr$", host)
    if m and m.group(1) not in ("app", "api", "www"):
        pm = re.match(r"^/p/([0-9a-f]{12})", path)
        return Detected("breezy", m.group(1), pm.group(1) if pm else None, host)

    if host.endswith(".oraclecloud.com"):
        m = re.search(r"/sites/([A-Za-z0-9_]+)(?:/job/(\d+))?", path)
        if m:
            return Detected("oracle", f"{host}/{m.group(1)}", m.group(2), host)

    if host == "apply.workable.com":
        m = re.match(r"^/([^/]+)(?:/j/([A-Z0-9]+))?", path)
        if m:
            return Detected("workable", m.group(1), m.group(2), host)

    for domain, portal in MANUAL_PORTALS.items():
        if host == domain or host.endswith("." + domain):
            return Detected(portal, None, _portal_job_id(path, q), host)
    return None


def _portal_job_id(path: str, q: dict) -> str | None:
    """Posting id in a job-board link: a known query parameter (Indeed jk, Glassdoor
    jobListingId, TimesJobs jobid), else the trailing number of the path (Naukri, foundit,
    Internshala, Unstop, iimjobs, Hirist), else a trailing hash (Cutshort)."""
    for key in ("jk", "vjk", "jobListingId", "jobid", "jobId", "job_id", "jid"):
        if q.get(key):
            return q[key][0]
    m = (re.search(r"/jobs/view/(\d+)", path) or re.search(r"(\d{5,})(?:\.html?)?/?$", path)
         or re.search(r"/(?:jobs?|j)/(\d{4,})", path))
    if m:
        return m.group(1)
    m = re.search(r"/jobs?/[^/]*-([A-Za-z0-9]{6,})/?$", path)
    return m.group(1) if m else None


def is_manual_portal(det: Detected | None) -> bool:
    return det is not None and det.ats in set(MANUAL_PORTALS.values())


def workday_parts(slug: str, host: str) -> tuple[str, str, str]:
    """('tenant', 'site', 'https://tenant.wdN.myworkdayjobs.com')"""
    tenant, _, site = slug.partition("/")
    return tenant, site, f"https://{host}"
