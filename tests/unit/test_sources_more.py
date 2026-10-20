"""New sources: Recruitee, Rippling, Breezy, Oracle boards; keyless feeds; Jooble/Findwork;
alert portals. Payloads mirror the live APIs (field names checked 2026-10-20)."""
import json

import httpx
import pytest

from app.ingest.detect import detect, is_manual_portal
from app.ingest.http import make_fetcher
from app.ingest.sources import breezy, oracle, recruitee, rippling, search_apis
from app.ingest.sources.feeds import FEEDS
from app.ingest.sources.oracle import _LOCATION_IDS
from app.mail.classify import is_job_alert
from app.ratelimit import RateLimiter, UnlimitedBuckets

RECRUITEE = {"offers": [{"id": 2783543, "slug": "backend-engineer-2", "title": "Backend Engineer", "status": "published",
                         "description": "<p>Python and PostgreSQL</p>", "requirements": "<ul><li>Go</li></ul>",
                         "location": "Bengaluru, Karnataka, India", "city": "Bengaluru", "country": "India",
                         "remote": False, "hybrid": True, "on_site": False, "company_name": "Acme",
                         "careers_url": "https://acme.recruitee.com/o/backend-engineer-2",
                         "careers_apply_url": "https://acme.recruitee.com/o/backend-engineer-2/c/new",
                         "published_at": "2026-10-15 09:31:36 UTC", "employment_type_code": "fulltime_permanent",
                         "salary": {"min": 1200000, "max": 1800000, "currency": "INR", "period": "year"}},
                        {"id": 1, "slug": "x", "title": "Old", "status": "closed"}]}
RIPPLING_LIST = [{"uuid": "ef0b7266-678c-4a23-a439-1a5e7362b604", "name": " Software  Engineer I ",
                  "url": "https://ats.rippling.com/acme/jobs/ef0b7266-678c-4a23-a439-1a5e7362b604",
                  "workLocation": {"label": "Bengaluru, India"}},
                 {"uuid": "11111111-2222-3333-4444-555555555555", "name": "Account Executive", "url": "u", "workLocation": {}}]
RIPPLING_DETAIL = {"uuid": "ef0b7266-678c-4a23-a439-1a5e7362b604", "companyName": "Acme",
                   "description": {"company": "<p>About Acme</p>", "role": "<p>Build APIs in Go</p>"},
                   "workLocations": ["Bengaluru, India"], "createdOn": "2026-10-06T16:36:07.947000-07:00",
                   "employmentType": {"label": "SALARIED_FT", "id": "Salaried, full-time"}}
BREEZY = [{"id": "98323abf2296", "friendly_id": "98323abf2296-sde", "name": "SDE Intern",
           "url": "https://acme.breezy.hr/p/98323abf2296-sde", "published_date": "2026-10-15T14:37:22.684Z",
           "type": {"id": "internship", "name": "Internship"}, "location": {"name": "Pune, IN", "is_remote": False},
           "company": {"name": "Acme"}, "salary": ""}]
ORACLE_FACET = {"items": [{"locationsFacet": [{"Id": 1, "Name": "United States"}, {"Id": 300000000289360, "Name": "India"}]}]}
ORACLE_PAGE = {"items": [{"TotalJobsCount": 2, "requisitionList": [
    {"Id": "210779473", "Title": "Software Engineer III - Java Fullstack", "PostedDate": "2026-10-20",
     "PrimaryLocation": "Bengaluru, Karnataka, India", "ShortDescriptionStr": "Java, Spring, React",
     "secondaryLocations": [{"Name": "Hyderabad, Telangana, India"}], "WorkplaceType": "Hybrid"},
    {"Id": "210779474", "Title": "Control Management Associate", "PostedDate": "2026-10-19",
     "PrimaryLocation": "Mumbai, Maharashtra, India"}]}]}


def _fetcher(routes: dict, seen: list | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        for prefix, body in routes.items():
            if str(req.url).startswith(prefix):
                return httpx.Response(200, json=body(req) if callable(body) else body)
        return httpx.Response(404, json={"error": "Not Found"})
    return make_fetcher(httpx.MockTransport(handler), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)


def test_recruitee():
    f = _fetcher({"https://acme.recruitee.com/api/offers/": RECRUITEE})
    jobs = recruitee.fetch(f, "acme", "Acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert (j.ats, j.req_id, j.workplace, j.salary_max, j.salary_period) == ("recruitee", "backend-engineer-2", "hybrid", 1800000, "year")
    assert j.posted_at.isoformat() == "2026-10-15T09:31:36+00:00" and "Go" in j.description
    assert detect(j.url).req_id == j.req_id                   # same identity from a link (C4)
    assert recruitee.probe(f, "acme")


def test_rippling_details_only_for_matching_titles():
    seen = []
    f = _fetcher({"https://api.rippling.com/platform/api/ats/v1/board/acme/jobs/ef0b": RIPPLING_DETAIL,
                  "https://api.rippling.com/platform/api/ats/v1/board/acme/jobs": RIPPLING_LIST}, seen)
    jobs = rippling.fetch(f, "acme", "Acme", lambda t: "engineer" in t.lower())
    assert [j.title for j in jobs] == ["Software Engineer I"]
    assert "Build APIs" in jobs[0].description and jobs[0].posted_at.year == 2026
    assert len(seen) == 2                                     # list + one detail, none for the sales job
    assert detect(jobs[0].url).req_id == jobs[0].req_id


def test_breezy():
    jobs = breezy.fetch(_fetcher({"https://acme.breezy.hr/json": BREEZY}), "acme", "Acme")
    assert jobs[0].title == "SDE Intern" and jobs[0].employment_type == "Internship"
    assert detect(jobs[0].url) .req_id == jobs[0].req_id == "98323abf2296"


def test_oracle_uses_country_location_facet():
    _LOCATION_IDS.clear()
    seen = []

    def page(req):
        return ORACLE_FACET if "facetsList" in str(req.url) else ORACLE_PAGE
    f = _fetcher({"https://jpmc.fa.oraclecloud.com/hcmRestApi/": page}, seen)
    jobs = oracle.fetch(f, "jpmc.fa.oraclecloud.com/CX_1001", "JPMorgan Chase", country="India")
    assert len(jobs) == 2 and jobs[0].workplace == "hybrid"
    assert "locationId=300000000289360" in seen[1].url.params["finder"]
    assert jobs[0].url == "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210779473"
    assert detect(jobs[0].url).slug == "jpmc.fa.oraclecloud.com/CX_1001" and detect(jobs[0].url).req_id == "210779473"
    assert "Hyderabad, Telangana, India" in jobs[0].locations
    with pytest.raises(ValueError):
        oracle.fetch(f, "evil.example.com/CX_1", "x")


def test_feeds_parse_and_filter():
    payloads = {
        "https://remotive.com/api/remote-jobs": {"jobs": [{"id": 1, "url": "https://remotive.com/remote-jobs/1", "title": "Backend Developer",
                                                          "company_name": "A", "publication_date": "2026-10-17T16:04:29",
                                                          "candidate_required_location": "Worldwide", "description": "<p>Go</p>"}]},
        "https://remoteok.com/api": [{"legal": "notice"}, {"id": "9", "position": "Data Engineer", "company": "B",
                                                            "url": "https://remoteok.com/remote-jobs/9", "date": "2026-10-18T19:00:02+00:00",
                                                            "apply_url": "https://boards.greenhouse.io/bco/jobs/123", "salary_max": 0}],
        "https://www.arbeitnow.com/api/job-board-api": {"data": [{"slug": "s", "company_name": "C", "title": "Sales Lead",
                                                                   "url": "https://www.arbeitnow.com/jobs/s", "created_at": 1792504849}]},
        "https://jobicy.com/api/v2/remote-jobs": {"jobs": [{"id": 5, "url": "https://jobicy.com/jobs/5", "jobTitle": "Junior Developer",
                                                            "companyName": "D", "jobGeo": "APAC", "pubDate": "2026-10-20T10:01:43+00:00"}]},
        "https://himalayas.app/jobs/api": {"jobs": [{"title": "QA Engineer", "companyName": "E", "guid": "https://himalayas.app/x",
                                                     "applicationLink": "https://himalayas.app/x", "pubDate": 1792502768,
                                                     "locationRestrictions": ["India"]}], "nextCursor": None},
        "https://www.themuse.com/api/public/jobs": {"page_count": 1, "results": [{"id": 7, "name": "Software Engineer, New Grad",
                                                                                  "company": {"name": "F"}, "locations": [{"name": "Bengaluru, India"}],
                                                                                  "refs": {"landing_page": "https://www.themuse.com/jobs/f/7"},
                                                                                  "publication_date": "2026-09-12T18:34:50Z"}]},
        "https://www.workingnomads.com/api/exposed_jobs/": [{"url": "https://www.workingnomads.com/job/go/1/", "title": "Rater",
                                                             "company_name": "G", "pub_date": "2026-10-20T07:41:37-04:00"}],
    }
    f = _fetcher(payloads)
    tech = lambda t: any(w in t.lower() for w in ("developer", "engineer"))  # noqa: E731
    got = {k: feed.fetch(f, tech) for k, feed in FEEDS.items()}
    assert {k: len(v) for k, v in got.items()} == {"remotive": 3, "remoteok": 1, "arbeitnow": 0, "jobicy": 1,
                                                    "himalayas": 1, "themuse": 1, "workingnomads": 0}
    ro = got["remoteok"][0]
    assert ro.ats == "greenhouse" and ro.company_slug == "bco"   # ATS link: board gets discovered
    assert got["himalayas"][0].location == "India" and got["remotive"][0].workplace == "remote"


def test_jooble_and_findwork():
    seen = []
    f = _fetcher({"https://jooble.org/api/KEY": {"jobs": [{"title": "Java Developer", "company": "H", "link": "https://jooble.org/desc/1",
                                                          "location": "Pune", "snippet": "Spring", "updated": "2026-10-19T00:00:00", "id": 1}]},
                  "https://findwork.dev/api/jobs/": {"results": [{"id": 2, "role": "Python Developer", "company_name": "I",
                                                                  "url": "https://findwork.dev/2", "remote": True, "date_posted": "2026-10-19T00:00:00Z"}]}}, seen)
    assert search_apis.jooble(f, "KEY", "java developer", "India")[0].title == "Java Developer"
    assert json.loads(seen[0].content) == {"keywords": "java developer", "location": "India", "page": "1"}
    fw = search_apis.findwork(f, "TOK", "python")
    assert fw[0].workplace == "remote" and seen[1].headers["Authorization"] == "Token TOK"


@pytest.mark.parametrize("url,portal,req", [
    ("https://in.indeed.com/viewjob?jk=a1b2c3d4e5f6", "indeed", "a1b2c3d4e5f6"),
    ("https://www.glassdoor.co.in/job-listing/x?jobListingId=1009876543210", "glassdoor", "1009876543210"),
    ("https://internshala.com/job/detail/software-developer-job-in-bangalore-at-acme1729076587", "internshala", "1729076587"),
    ("https://cutshort.io/job/Backend-Engineer-Acme-xYz12AbC", "cutshort", "xYz12AbC"),
    ("https://unstop.com/jobs/sde-acme-1234567", "unstop", "1234567"),
    ("https://www.hirist.tech/j/sde-1-java-1234567.html", "hirist", "1234567"),
    ("https://www.foundit.in/job/software-engineer-acme-bengaluru-34567890", "foundit", "34567890"),
    ("https://www.naukri.com/job-listings-sde-acme-bengaluru-0-to-1-years-131024500123", "naukri", "131024500123"),
])
def test_alert_portal_links(url, portal, req):
    d = detect(url)
    assert (d.ats, d.req_id) == (portal, req) and is_manual_portal(d)


@pytest.mark.parametrize("sender,subject,alert", [
    ("jobs@internshala.com", "12 new internships for you", True),
    ("jobs@internshala.com", "Congratulations! You have been shortlisted", False),
    ("noreply@unstop.com", "Top 10 jobs matching your profile", True),
    ("noreply@unstop.com", "Your test link for Acme hiring challenge", False),
    ("alerts@linkedin.com", "Software Engineer: 5 new jobs", True),
    ("alerts@linkedin.com", "Interview with Acme", False),
    ("noreply@naukri.com", "Recruiter from Acme viewed your profile", False),
    ("someone@cutshort.io", "Quick question about your profile", False),   # portal sender, not an alert subject
])
def test_alert_classification_never_eats_positive_mail(sender, subject, alert):
    assert is_job_alert(sender, subject) is alert


@pytest.mark.parametrize("status", [404, 403, 500])
def test_jooble_key_never_in_error_text(status):
    f = make_fetcher(httpx.MockTransport(lambda req: httpx.Response(status, text="<html>nope</html>")),
                     RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)
    with pytest.raises(Exception) as e:
        search_apis.jooble(f, "SECRETKEY123", "sde", "India")
    assert "SECRETKEY123" not in str(e.value)


def test_jooble_key_not_in_non_json_error():
    f = make_fetcher(httpx.MockTransport(lambda req: httpx.Response(200, text="<html>challenge</html>")),
                     RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)
    with pytest.raises(Exception) as e:
        search_apis.jooble(f, "SECRETKEY123", "sde")
    assert "SECRETKEY123" not in str(e.value)
