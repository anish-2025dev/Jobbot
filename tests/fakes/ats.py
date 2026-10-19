"""A fake job-board universe behind httpx.MockTransport.

Serves the documented response shapes of Greenhouse, Lever, Ashby,
SmartRecruiters, Workday (CXS), JSearch, Adzuna, HN/Algolia and plain
career pages with JSON-LD, so fetchers, normalization and the full pipeline
run exactly as they would against the real APIs.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx

TITLES_JUNIOR = ["Software Development Engineer I", "Backend Engineer - New Grad", "SDE 1 (Python)",
                 "Graduate Engineer Trainee", "Associate Software Engineer", "Full Stack Developer",
                 "Software Engineer, Backend", "Junior Data Engineer"]
TITLES_OTHER = ["Senior Software Engineer", "Staff Engineer, Payments", "Engineering Manager",
                "Account Executive", "Talent Acquisition Partner", "Product Designer", "Sales Engineer"]
CITIES = ["Bengaluru, India", "Gurugram, India", "Hyderabad, India", "Pune, India", "Noida, India",
          "Remote - India", "London, UK", "San Francisco, CA", "Chennai, India"]
STACKS = [["Python", "FastAPI", "PostgreSQL", "Redis", "Docker"], ["Java", "Spring Boot", "MySQL", "Kafka"],
          ["Go", "Kubernetes", "PostgreSQL", "gRPC"], ["React", "TypeScript", "Node.js", "MongoDB"],
          ["Python", "Airflow", "dbt", "SQL", "Spark"], ["C#", ".NET", "SQL Server", "Azure"]]


def jd_html(title: str, stack: list[str], exp: str, batch: str | None, salary: str | None, seed: int) -> str:
    rnd = random.Random(seed)
    lines = [f"<p>We are looking for a {title} to build reliable services for our customers. "
             f"Team {seed % 97} owns core platform APIs used by thousands of businesses.</p>",
             "<h3>What you'll do</h3><ul>",
             f"<li>Design and build services with {stack[0]} and {stack[1]}</li>",
             f"<li>Work with {stack[2]} and ship with CI/CD</li>",
             "<li>Write unit and integration tests and participate in design reviews</li></ul>",
             "<h3>Requirements</h3><ul>", f"<li>{exp}</li>",
             f"<li>Strong fundamentals in data structures and algorithms</li><li>Experience with {stack[0]} and SQL</li></ul>",
             "<h3>Nice to have</h3><ul>", f"<li>{stack[-1]}</li></ul>"]
    if batch:
        lines.append(f"<p>Open to {batch} graduates.</p>")
    if salary:
        lines.append(f"<p>Compensation: {salary}</p>")
    lines.append(f"<p>Ref {rnd.randint(1000, 9999)}-{seed}. We are an equal opportunity employer.</p>")
    return "".join(lines)


@dataclass
class FakeJob:
    id: str
    title: str
    location: str
    html: str
    posted: datetime
    req: str
    remote: bool = False


@dataclass
class FakeCompany:
    name: str
    ats: str
    slug: str
    jobs: list[FakeJob] = field(default_factory=list)
    host: str | None = None       # workday


class FakeUniverse:
    def __init__(self, seed: int = 7, now: datetime | None = None):
        self.rnd = random.Random(seed)
        self.now = now or datetime.now(timezone.utc)
        self.companies: dict[tuple[str, str], FakeCompany] = {}
        self.requests: list[httpx.Request] = []
        self.fail_hosts: dict[str, int] = {}       # host -> status to return
        self.jsearch: list[dict] = []
        self.adzuna: list[dict] = []
        self.hn_comments: list[dict] = []
        self.pages: dict[str, str] = {}            # url -> html (career pages)
        self._n = 0

    # --- building -------------------------------------------------------------
    def make_job(self, title: str | None = None, location: str | None = None, junior: bool | None = None,
                 batch: str | None = None, salary: str | None = None, age_hours: float | None = None,
                 stack: list[str] | None = None, exp: str | None = None) -> FakeJob:
        self._n += 1
        junior = self.rnd.random() < 0.6 if junior is None else junior
        title = title or self.rnd.choice(TITLES_JUNIOR if junior else TITLES_OTHER)
        exp = exp or ("0-2 years of experience" if junior else f"{self.rnd.choice([3, 5, 8])}+ years of experience")
        stack = stack or self.rnd.choice(STACKS)
        loc = location or self.rnd.choice(CITIES)
        age = self.rnd.uniform(1, 400) if age_hours is None else age_hours
        return FakeJob(id=str(100000 + self._n), title=title, location=loc,
                       html=jd_html(title, stack, exp, batch, salary, self._n),
                       posted=self.now - timedelta(hours=age), req=f"R{50000 + self._n}",
                       remote="remote" in loc.lower())

    def add_company(self, name: str, ats: str, slug: str, n_jobs: int = 5, **job_kw) -> FakeCompany:
        host = f"{slug}.wd5.myworkdayjobs.com" if ats == "workday" else None
        c = FakeCompany(name=name, ats=ats, slug=f"{slug}/careers" if ats == "workday" else slug, host=host)
        c.jobs = [self.make_job(**job_kw) for _ in range(n_jobs)]
        self.companies[(ats, c.slug if ats != "workday" else slug)] = c
        return c

    # --- serving --------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        u = urlparse(str(req.url))
        host, path = u.hostname or "", u.path
        q = parse_qs(u.query)
        if host in self.fail_hosts:
            return httpx.Response(self.fail_hosts[host])
        try:
            if host == "boards-api.greenhouse.io":
                return self._greenhouse(path)
            if host == "api.lever.co":
                return self._lever(path)
            if host == "api.ashbyhq.com":
                return self._ashby(path)
            if host == "api.smartrecruiters.com":
                return self._smartrecruiters(path, q)
            if host.endswith(".myworkdayjobs.com"):
                return self._workday(host, path, req)
            if host == "jsearch.p.rapidapi.com":
                if req.headers.get("x-rapidapi-key") != "js-key":
                    return httpx.Response(403)
                return httpx.Response(200, json={"status": "OK", "data": self.jsearch})
            if host == "api.adzuna.com":
                return httpx.Response(200, json={"results": self.adzuna, "count": len(self.adzuna)})
            if host == "hn.algolia.com":
                if "search_by_date" in path:
                    return httpx.Response(200, json={"hits": [
                        {"objectID": "999", "title": "Ask HN: Who is hiring? (October 2026)"}]})
                return httpx.Response(200, json={"id": 999, "children": self.hn_comments})
            if str(req.url).split("?")[0] in self.pages:
                return httpx.Response(200, text=self.pages[str(req.url).split("?")[0]],
                                      headers={"content-type": "text/html"})
        except KeyError:
            return httpx.Response(404)
        return httpx.Response(404)

    def _co(self, ats: str, slug: str) -> FakeCompany:
        return self.companies[(ats, slug)]

    def _greenhouse(self, path: str) -> httpx.Response:
        slug = path.split("/")[3]
        c = self._co("greenhouse", slug)
        import html as h
        return httpx.Response(200, json={"jobs": [{
            "id": int(j.id), "title": j.title, "updated_at": j.posted.isoformat(),
            "first_published": j.posted.isoformat(), "requisition_id": j.req,
            "location": {"name": j.location}, "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/{j.id}",
            "content": h.escape(j.html), "offices": [{"name": j.location}], "departments": [],
            "company_name": c.name} for j in c.jobs], "meta": {"total": len(c.jobs)}})

    def _lever(self, path: str) -> httpx.Response:
        slug = path.split("/")[3]
        c = self._co("lever", slug)
        return httpx.Response(200, json=[{
            "id": f"{int(j.id):08d}-aaaa-bbbb-cccc-{int(j.id):012d}", "text": j.title,
            "categories": {"location": j.location, "commitment": "Full-time", "team": "Eng", "allLocations": [j.location]},
            "description": j.html, "lists": [], "additional": "",
            "hostedUrl": f"https://jobs.lever.co/{slug}/{int(j.id):08d}-aaaa-bbbb-cccc-{int(j.id):012d}",
            "applyUrl": f"https://jobs.lever.co/{slug}/{int(j.id):08d}-aaaa-bbbb-cccc-{int(j.id):012d}/apply",
            "createdAt": int(j.posted.timestamp() * 1000),
            "workplaceType": "remote" if j.remote else "onsite"} for j in c.jobs])

    def _ashby(self, path: str) -> httpx.Response:
        slug = path.split("/")[3]
        c = self._co("ashby", slug)
        return httpx.Response(200, json={"apiVersion": "1", "jobs": [{
            "id": f"{int(j.id):08d}-1111-2222-3333-{int(j.id):012d}", "title": j.title, "location": j.location,
            "secondaryLocations": [], "isRemote": j.remote, "workplaceType": "Remote" if j.remote else "OnSite",
            "descriptionHtml": j.html, "publishedAt": j.posted.isoformat(), "isListed": True,
            "jobUrl": f"https://jobs.ashbyhq.com/{slug}/{j.id}", "applyUrl": f"https://jobs.ashbyhq.com/{slug}/{j.id}/application",
            "employmentType": "FullTime"} for j in c.jobs]})

    def _smartrecruiters(self, path: str, q: dict) -> httpx.Response:
        parts = path.split("/")
        c = self._co("smartrecruiters", parts[3])
        if len(parts) > 5:   # detail
            j = next(j for j in c.jobs if j.id == parts[5])
            city, _, country = j.location.partition(", ")
            return httpx.Response(200, json={
                "id": j.id, "name": j.title, "refNumber": j.req, "releasedDate": j.posted.isoformat(),
                "company": {"name": c.name}, "location": {"city": city, "country": country, "remote": j.remote},
                "jobAd": {"sections": {"jobDescription": {"title": "Job Description", "text": j.html}}},
                "postingUrl": f"https://jobs.smartrecruiters.com/{c.slug}/{j.id}",
                "applyUrl": f"https://jobs.smartrecruiters.com/{c.slug}/{j.id}/apply"})
        off, lim = int(q.get("offset", ["0"])[0]), int(q.get("limit", ["100"])[0])
        page = c.jobs[off:off + lim]
        return httpx.Response(200, json={"offset": off, "limit": lim, "totalFound": len(c.jobs), "content": [
            {"id": j.id, "name": j.title, "refNumber": j.req, "releasedDate": j.posted.isoformat()} for j in page]})

    def _workday(self, host: str, path: str, req: httpx.Request) -> httpx.Response:
        tenant = host.split(".")[0]
        c = self._co("workday", tenant)
        if path.endswith("/jobs") and req.method == "POST":
            body = json.loads(req.content)
            page = c.jobs[body["offset"]:body["offset"] + body["limit"]]
            return httpx.Response(200, json={"total": len(c.jobs), "jobPostings": [
                {"title": j.title, "externalPath": f"/job/{j.location.split(',')[0]}/{j.title.replace(' ', '-')}_{j.req}",
                 "locationsText": j.location, "postedOn": "Posted 2 Days Ago", "bulletFields": [j.req]} for j in page]})
        req_id = path.rsplit("_", 1)[-1]
        j = next(j for j in c.jobs if j.req == req_id)
        return httpx.Response(200, json={"jobPostingInfo": {
            "title": j.title, "jobDescription": j.html, "location": j.location, "postedOn": "Posted 2 Days Ago",
            "startDate": j.posted.date().isoformat(), "jobReqId": j.req, "timeType": "Full time",
            "externalUrl": f"https://{host}/careers{path.split('/careers', 1)[-1]}"}})
