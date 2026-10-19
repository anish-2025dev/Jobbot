import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.outreach import compose as C
from app.outreach import providers as P
from app.outreach.contacts import classify_role, pattern_email, rank_candidates
from app.outreach.queue import effective_mode
from app.outreach.schedule import next_slot, plan_sends, tz_for
from app.resume.engine import build_recipe, prepare_jd
from app.vault.schema import Vault
from tests.fakes.providers import FakeProviders

ROOT = Path(__file__).parents[2]
VAULT = Vault.model_validate(json.loads((ROOT / "examples/vault.example.json").read_text()))
JD = (ROOT / "examples/jd.example.txt").read_text()
JOB = {"title": "Software Development Engineer I", "company": "Northwind Payments", "req_id": "R12345",
       "apply_url": "https://boards.greenhouse.io/northwind/jobs/1"}


@pytest.mark.parametrize("title,role", [
    ("Senior Software Engineer, Backend", "peer"), ("SDE II", "peer"), ("Software Development Engineer", "peer"),
    ("Engineering Manager, Payments", "manager"), ("Tech Lead", "manager"), ("Technical Recruiter", "recruiter"),
    ("Talent Acquisition Partner", "recruiter"), ("VP Engineering", None), ("Director of Engineering", None),
    ("Head of Talent", None), ("Co-Founder & CTO", None), ("Software Engineering Intern", None), ("Sales Lead", None),
])
def test_classify_role(title, role):
    assert classify_role(title) == role


def test_rank_prefers_function_and_location():
    people = [P.Person("A", "One", "Software Engineer, Frontend", location="Pune"),
              P.Person("B", "Two", "Senior Software Engineer, Backend", location="Bengaluru"),
              P.Person("C", "Three", "Engineering Manager, Payments", location="Bengaluru"),
              P.Person("D", "Four", "Recruiter"), P.Person("E", "Five", "VP Engineering")]
    best = rank_candidates(people, "Backend Engineer, Payments", "Bengaluru, India")
    assert best["peer"].first_name == "B" and best["manager"].first_name == "C" and best["recruiter"].first_name == "D"
    assert all(p.first_name != "E" for p in best.values())


def test_pattern_email():
    assert pattern_email("{first}.{last}", "Priya", "Sharma", "x.com") == "priya.sharma@x.com"
    assert pattern_email("{f}{last}", "Priya", "Sharma", "x.com") == "psharma@x.com"


# --- providers wire format ----------------------------------------------------------
def test_hunter_search_and_verify():
    fp = FakeProviders(catch_all={"neha.kapoor@northwindpay.com"})
    h = P.Hunter("hunter-key", fp.transport())
    people = h.search("northwindpay.com", ["software engineer"])
    assert len(people) == 5 and h.pattern == "{first}.{last}"
    neha = next(p for p in people if p.first_name == "Neha")
    assert not neha.email_verified                         # catch-all never counts as verified
    assert h.verify("priya.sharma@northwindpay.com") == "valid"
    assert h.verify("neha.kapoor@northwindpay.com") == "accept_all"
    assert h.resolve_domain("Northwind Payments") == "northwindpay.com"


def test_apollo_search_reveal():
    fp = FakeProviders()
    a = P.Apollo("apollo-key", fp.transport())
    people = a.search("northwindpay.com", ["engineering manager"])
    assert all(p.email is None for p in people)            # locked emails are not addresses
    p = a.reveal(people[1], "northwindpay.com")
    assert p.email == "arjun.mehta@northwindpay.com" and p.email_verified


def test_snov_token_search_verify():
    fp = FakeProviders(catch_all={"priya.sharma@northwindpay.com"})
    s = P.Snov("cid", "sec", fp.transport())
    assert len(s.search("northwindpay.com", [])) == 5
    assert s.verify("priya.sharma@northwindpay.com") == "accept_all"
    assert s.verify("arjun.mehta@northwindpay.com") == "valid"


def test_exhausted_is_detected():
    fp = FakeProviders(exhausted={"hunter"})
    with pytest.raises(P.CreditsExhausted):
        P.Hunter("hunter-key", fp.transport()).search("x.com", [])


# --- compose ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def recipe():
    return build_recipe(VAULT, prepare_jd(VAULT, JD, JOB["title"], JOB["company"]), vault_revision=1, bucket="high")


@pytest.mark.parametrize("role,ask", [("peer", "open to referring me"), ("manager", "value a quick look"),
                                      ("recruiter", "forward it to the hiring team")])
def test_compose_rules(recipe, role, ask):
    subject, body = C.compose(VAULT, recipe, role=role, first_name="Priya", job=JOB)
    assert C.words(body) < 120
    assert body.rstrip().endswith(C.OPT_OUT)
    assert ask in body and "R12345" in subject and JOB["apply_url"] in body
    assert body.startswith("Hi Priya,")
    # truthful: every number in the email exists in the vault
    from app.vault.facts import extract_numbers, vault_facts
    facts = vault_facts(VAULT).numbers
    assert all(n in facts for n in extract_numbers(body.split("Relevant work:")[-1].split("Thanks,")[0]))


def test_mime_has_attachment_and_message_id(recipe):
    raw, mid = C.build_mime(from_name="Sample Candidate", from_addr="me@gmail.com", to_name="Priya",
                            to_addr="priya@x.com", subject="s", body="b", attachment=b"%PDF-1.7 x",
                            attachment_name="Sample_Candidate_Resume.pdf")
    import email
    m = email.message_from_bytes(raw)
    assert m["Message-ID"] == mid and mid.endswith("@gmail.com>")
    att = [p for p in m.walk() if p.get_filename()]
    assert att and att[0].get_filename() == "Sample_Candidate_Resume.pdf"


def test_llm_polish_rejected_if_it_adds_facts():
    import httpx
    from app.llm.client import LLM
    body = "Hi P,\nI built 6 endpoints. https://x.dev\n\n" + C.OPT_OUT
    def llm(text_):
        return LLM("openai", "k", "https://x", "c", "s", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": text_}}]})))
    assert C.polish_with_llm("s", body, llm("Hi P, I built 60 endpoints. https://x.dev\n\n" + C.OPT_OUT)) == body
    assert C.polish_with_llm("s", body, llm("Hi P, I built 6 endpoints. https://x.dev")) == body   # opt-out dropped
    good = "Hello P,\nI built 6 endpoints. https://x.dev\n\n" + C.OPT_OUT
    assert C.polish_with_llm("s", body, llm(good)) == good


# --- scheduling -----------------------------------------------------------------------
def test_slots_are_9_to_11_local_weekdays():
    sat_8am_ist = datetime(2026, 10, 17, 2, 30, tzinfo=timezone.utc)   # Saturday 08:00 IST
    t = next_slot("Asia/Kolkata", sat_8am_ist).astimezone(ZoneInfo("Asia/Kolkata"))
    assert t.weekday() == 0 and t.hour == 9                            # Monday 09:00
    noon = datetime(2026, 10, 19, 6, 30, tzinfo=timezone.utc)          # Monday 12:00 IST -> Tuesday 09:00
    assert next_slot("Asia/Kolkata", noon).astimezone(ZoneInfo("Asia/Kolkata")).day == 20


def test_plan_spacing_tz_and_cap():
    now = datetime(2026, 10, 19, 3, 45, tzinfo=timezone.utc)          # Monday 09:15 IST
    contacts = [{"location": "Bengaluru"}, {"location": "Gurugram"}, {"location": "London, UK"}]
    times = plan_sends(contacts, now=now, fallback_tz="Asia/Kolkata", already_today=0, daily_cap=30,
                       rnd=random.Random(1))
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times[:2], times[1:2])]
    assert all(5 <= g <= 15 for g in gaps)
    assert 9 <= times[2].astimezone(ZoneInfo("Europe/London")).hour < 11
    capped = plan_sends(contacts[:1], now=now, fallback_tz="Asia/Kolkata", already_today=30, daily_cap=30)
    assert capped[0].date() > now.date()
    assert tz_for("Seattle, WA", "Asia/Kolkata") == "America/Los_Angeles" and tz_for(None, "X") == "X"


def test_review_mode_forced_for_first_week():
    now = datetime.now(timezone.utc)
    assert effective_mode({"outreach_mode": "auto", "outreach_enabled_at": now.isoformat()}, now) == "review"
    old = (now - timedelta(days=8)).isoformat()
    assert effective_mode({"outreach_mode": "auto", "outreach_enabled_at": old}, now) == "auto"
    assert effective_mode({"outreach_mode": "auto"}, now) == "review"
