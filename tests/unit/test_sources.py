import json
from datetime import timedelta

import httpx
import pytest

from app.ingest.detect import detect, is_manual_portal
from app.ingest.http import Fetcher, NotFoundError, SourceError, make_fetcher
from app.ingest.sources import adzuna, ashby, greenhouse, hn, jsearch, jsonld, lever, smartrecruiters, workday
from app.ingest.sources.common import parse_iso, parse_relative_posted
from app.ratelimit import Limit, MemoryBuckets, RateLimiter, UnlimitedBuckets
from tests.fakes.ats import FakeUniverse


@pytest.fixture
def uni():
    return FakeUniverse(seed=3)


def fetcher(uni) -> Fetcher:
    return make_fetcher(uni.transport(), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)


# --- detection ---------------------------------------------------------------------
@pytest.mark.parametrize("url,ats,slug,req", [
    ("https://boards.greenhouse.io/stripe/jobs/4512345", "greenhouse", "stripe", "4512345"),
    ("https://job-boards.greenhouse.io/acme/jobs/77?gh_src=x", "greenhouse", "acme", "77"),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=88", "greenhouse", "acme", "88"),
    ("https://careers.acme.com/jobs?gh_jid=99", "greenhouse", None, "99"),
    ("https://jobs.lever.co/leverdemo/5ac21346-8e0c-4494-8e7a-3eb92ff77902/apply", "lever", "leverdemo",
     "5ac21346-8e0c-4494-8e7a-3eb92ff77902"),
    ("https://jobs.ashbyhq.com/ramp/1e3e0c4a-1111-2222-3333-444455556666", "ashby", "ramp",
     "1e3e0c4a-1111-2222-3333-444455556666"),
    ("https://jobs.smartrecruiters.com/Visa/743999912345-software-engineer", "smartrecruiters", "Visa", "743999912345"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Pune/SDE-I_R12345", "workday", "acme/careers", "R12345"),
    ("https://apply.workable.com/northwind/j/ABC123DEF/", "workable", "northwind", "ABC123DEF"),
    ("https://www.linkedin.com/jobs/view/3999999999/", "linkedin", None, "3999999999"),
])
def test_detect(url, ats, slug, req):
    d = detect(url)
    assert (d.ats, d.slug, d.req_id) == (ats, slug, req)


def test_manual_portals():
    assert is_manual_portal(detect("https://www.naukri.com/job-listings-sde-acme-123456789"))
    assert not is_manual_portal(detect("https://jobs.lever.co/x"))
    assert detect("https://example.com/careers") is None


# --- Tier A ---------------------------------------------------------------------------
def test_greenhouse(uni):
    c = uni.add_company("Acme Corp", "greenhouse", "acme", n_jobs=3)
    jobs = greenhouse.fetch(fetcher(uni), "acme", "Acme")
    assert len(jobs) == 3
    j = jobs[0]
    # identity is the posting id (what detect() reads from aggregator URLs), not the requisition id
    assert j.ats == "greenhouse" and j.company == "Acme Corp" and j.req_id == str(int(c.jobs[0].id))
    assert j.url == f"https://boards.greenhouse.io/acme/jobs/{c.jobs[0].id}"
    assert "&lt;" in j.description or "<p>" in j.description
    assert j.posted_at is not None


def test_title_filter_applies(uni):
    uni.add_company("Acme", "greenhouse", "acme", n_jobs=6)
    jobs = greenhouse.fetch(fetcher(uni), "acme", "Acme", title_filter=lambda t: "Engineer" in t)
    assert all("Engineer" in j.title for j in jobs)


def test_lever_epoch_ms_and_workplace(uni):
    uni.add_company("Lev", "lever", "lev", n_jobs=2, location="Remote - India")
    jobs = lever.fetch(fetcher(uni), "lev", "Lev")
    assert jobs[0].workplace == "remote" and jobs[0].posted_at.year >= 2025
    assert jobs[0].apply_url.endswith("/apply")


def test_ashby(uni):
    uni.add_company("Ash", "ashby", "ash", n_jobs=2)
    jobs = ashby.fetch(fetcher(uni), "ash", "Ash")
    assert len(jobs) == 2 and jobs[0].ats == "ashby" and jobs[0].description_is_html


def test_smartrecruiters_paginates_and_filters_before_detail(uni):
    c = uni.add_company("SR Co", "smartrecruiters", "srco", n_jobs=130)
    f = fetcher(uni)
    jobs = smartrecruiters.fetch(f, "srco", "SR Co", title_filter=lambda t: "Senior" not in t)
    detail_calls = [r for r in uni.requests if not r.url.path.endswith("/postings")]
    assert len(jobs) == len([j for j in c.jobs if "Senior" not in j.title])
    assert len(detail_calls) == len(jobs)           # no detail calls for filtered titles
    kept = [j for j in c.jobs if "Senior" not in j.title]
    assert {j.req_id for j in jobs} == {str(j.id) for j in kept}    # posting id, not refNumber


def test_workday(uni):
    c = uni.add_company("WD Inc", "workday", "wdinc", n_jobs=25)
    jobs = workday.fetch(fetcher(uni), "wdinc/careers", "wdinc.wd5.myworkdayjobs.com", "WD Inc")
    assert len(jobs) == 25                           # two pages of 20
    assert {j.req_id for j in jobs} == {j.req for j in c.jobs}
    posts = [r for r in uni.requests if r.method == "POST"]
    assert json.loads(posts[0].content)["limit"] == 20


def test_workday_browser_fallback_on_block(uni):
    uni.add_company("WD Inc", "workday", "wdinc", n_jobs=1)
    uni.fail_hosts["wdinc.wd5.myworkdayjobs.com"] = 403
    calls = []

    def browser(method, url, body):
        calls.append(method)
        uni.fail_hosts.clear()
        r = uni.handle(httpx.Request(method, url, json=body) if body else httpx.Request(method, url))
        uni.fail_hosts["wdinc.wd5.myworkdayjobs.com"] = 403
        return r.json()

    jobs = workday.fetch(fetcher(uni), "wdinc/careers", "wdinc.wd5.myworkdayjobs.com", "WD Inc", browser=browser)
    assert len(jobs) == 1 and calls == ["POST", "GET"]


def test_relative_posted():
    now = parse_iso("2026-10-18T12:00:00Z")
    assert parse_relative_posted("Posted Today", now) == now
    assert parse_relative_posted("Posted Yesterday", now) == now - timedelta(days=1)
    assert parse_relative_posted("Posted 30+ Days Ago", now) == now - timedelta(days=30)
    assert parse_iso(1700000000000).year == 2023 and parse_iso("bad") is None


# --- Tier B ---------------------------------------------------------------------------
def test_jsearch_maps_ats_links_and_manual_only(uni):
    uni.jsearch = [
        {"job_id": "js1", "employer_name": "Acme", "job_title": "SDE I", "employer_website": "https://www.acme.com",
         "job_apply_link": "https://www.linkedin.com/jobs/view/123456789",
         "apply_options": [{"publisher": "Greenhouse", "apply_link": "https://boards.greenhouse.io/acme/jobs/42"}],
         "job_description": "Build APIs in Python", "job_city": "Pune", "job_country": "IN",
         "job_posted_at_datetime_utc": "2026-10-17T10:00:00Z", "job_required_experience": {"no_experience_required": True}},
        {"job_id": "js2", "employer_name": "Beta", "job_title": "Backend Dev",
         "job_apply_link": "https://www.linkedin.com/jobs/view/987654321", "apply_options": [],
         "job_description": "Go services", "job_is_remote": True},
    ]
    jobs = jsearch.fetch(fetcher(uni), "js-key", "SDE in Pune")
    a, b = jobs
    assert (a.ats, a.company_slug, a.req_id) == ("greenhouse", "acme", "42")
    assert a.url == "https://boards.greenhouse.io/acme/jobs/42" and a.company_domain == "acme.com"
    assert a.experience_months == 0 and not a.easy_apply_only
    assert b.easy_apply_only and b.ats == "unknown" and b.workplace == "remote"


def test_jsearch_bad_key_is_source_error(uni):
    with pytest.raises(SourceError):
        jsearch.fetch(fetcher(uni), "wrong", "q")


def test_adzuna_ignores_predicted_salary(uni):
    uni.adzuna = [
        {"id": 1, "title": "SDE", "description": "Python", "redirect_url": "https://www.adzuna.in/land/ad/1",
         "company": {"display_name": "Acme"}, "location": {"display_name": "Pune"}, "created": "2026-10-17T00:00:00Z",
         "salary_min": 600000, "salary_max": 900000, "salary_is_predicted": "1"},
        {"id": 2, "title": "SDE II", "description": "Go", "redirect_url": "https://www.adzuna.in/land/ad/2",
         "company": {"display_name": "Beta"}, "location": {"display_name": "Delhi"},
         "salary_min": 1200000, "salary_max": 1500000, "salary_is_predicted": "0"}]
    a, b = adzuna.fetch(fetcher(uni), "id", "key", "software engineer", "Pune")
    assert a.salary_max is None and b.salary_max == 1500000 and b.salary_currency == "INR"


def test_hn_parses_first_line(uni):
    uni.hn_comments = [
        {"id": 1, "created_at": "2026-10-01T00:00:00Z", "author": "x",
         "text": "Acme (YC W21) | Backend Engineer | Remote (India) | Full-time<p>We use Go. "
                 "<a href=\"https://jobs.lever.co/acme/5ac21346-8e0c-4494-8e7a-3eb92ff77902\">apply</a>"},
        {"id": 2, "text": "Just a comment without the format"},
        {"id": 3, "text": "Beta | Account Executive | NYC"}]
    jobs = hn.fetch(fetcher(uni))
    assert len(jobs) == 1
    j = jobs[0]
    assert (j.company, j.title, j.workplace, j.ats, j.company_slug) == ("Acme", "Backend Engineer", "remote", "lever", "acme")


# --- Tier D ---------------------------------------------------------------------------
LD = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Software Engineer I",
      "description": "<p>Build APIs with Python and PostgreSQL. 0-2 years.</p>", "datePosted": "2026-10-16",
      "hiringOrganization": {"@type": "Organization", "name": "Contoso"},
      "identifier": {"@type": "PropertyValue", "value": "CT-901"},
      "jobLocation": {"@type": "Place", "address": {"addressLocality": "Hyderabad", "addressCountry": "IN"}},
      "baseSalary": {"currency": "INR", "value": {"minValue": 900000, "maxValue": 1400000, "unitText": "YEAR"}},
      "url": "https://careers.contoso.in/jobs/ct-901"}


def test_jsonld_direct_and_graph(uni):
    uni.pages["https://careers.contoso.in/"] = (
        '<html><script type="application/ld+json">' + json.dumps({"@graph": [{"@type": "Organization"}, LD]})
        + "</script></html>")
    jobs = jsonld.fetch(fetcher(uni), "https://careers.contoso.in/", "Contoso")
    j = jobs[0]
    assert (j.title, j.company, j.req_id, j.location) == ("Software Engineer I", "Contoso", "CT-901", "Hyderabad, IN")
    assert (j.salary_min, j.salary_max, j.salary_currency, j.salary_period) == (900000, 1400000, "INR", "year")


def test_jsonld_follows_job_links(uni):
    uni.pages["https://careers.contoso.in/"] = '<a href="/jobs/ct-901">SE I</a><a href="/about">About</a>'
    uni.pages["https://careers.contoso.in/jobs/ct-901"] = (
        '<script type="application/ld+json">' + json.dumps(LD) + "</script>")
    jobs = jsonld.fetch(fetcher(uni), "https://careers.contoso.in/", "Contoso")
    assert [j.req_id for j in jobs] == ["CT-901"]


# --- HTTP layer -----------------------------------------------------------------------
def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "0"}) if calls["n"] < 3 else httpx.Response(200, json={"jobs": []})

    f = make_fetcher(httpx.MockTransport(handler), RateLimiter(MemoryBuckets()), sleep=lambda s: None)
    assert f.get_json("greenhouse", "https://boards-api.greenhouse.io/v1/boards/x/jobs") == {"jobs": []}
    assert calls["n"] == 3


def test_404_is_not_found_and_500s_exhaust():
    f = make_fetcher(httpx.MockTransport(lambda r: httpx.Response(404)), RateLimiter(MemoryBuckets()), sleep=lambda s: None)
    with pytest.raises(NotFoundError):
        f.get_json("lever", "https://api.lever.co/v0/postings/x")
    f = make_fetcher(httpx.MockTransport(lambda r: httpx.Response(503)), RateLimiter(MemoryBuckets()), sleep=lambda s: None)
    with pytest.raises(SourceError):
        f.get_json("lever", "https://api.lever.co/v0/postings/x")


def test_memory_token_bucket():
    b = MemoryBuckets()
    lim = Limit(rate_per_s=1.0, burst=2)
    assert b.take("k", lim, now=0) and b.take("k", lim, now=0) and not b.take("k", lim, now=0)
    assert b.take("k", lim, now=1.0)                  # refilled one token
