"""Company verification counts postings once across overlapping Workday searches."""
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def verify(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "scripts/companies"))
    from scripts.companies import verify
    return verify


@pytest.mark.parametrize("title,tech", [("IT", True), ("IT Support", True), ("it manager", True),
                                       ("Audit Manager", False), ("Credit Officer", False),
                                       ("Software Engineer", True), ("Data Analyst", True)])
def test_tech_it_is_a_standalone_word(verify, title, tech):
    assert bool(verify.TECH.search(title)) is tech


def test_workday_deduplicates_all_searches_and_keeps_total(verify, monkeypatch):
    first = {"externalPath": "/job/1", "title": "Software Engineer I", "locationsText": "India"}
    second = {"externalPath": "/job/2", "title": "Software Engineer I", "locationsText": "India"}
    fallback = {"title": "Graduate Developer", "locationsText": "India"}
    elsewhere = {"title": "Graduate Developer", "locationsText": "London"}
    calls = []

    def handler(request):
        calls.append(request)
        jobs = [first, first, fallback] if len(calls) == 1 else [first, second, fallback, elsewhere, second]
        return httpx.Response(200, json={"total": 100, "jobPostings": jobs})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(verify, "client", client)
        result = verify.workday("acme.wd5.myworkdayjobs.com/careers")
    assert len(calls) == 5
    assert result == {"jobs": 100, "tech": 4, "india": 3, "india_tech": 3,
                      "entry_tech": 4, "india_entry": 3}
