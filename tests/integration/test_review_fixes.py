"""Regression tests for the CodeRabbit review of PR #8 that need Postgres (and, for the
form-loop test, Chromium)."""
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text

from app.ingest import registry
from app.ingest.http import make_fetcher
from app.ratelimit import RateLimiter, UnlimitedBuckets
from tests.fakes.ats import FakeUniverse
from tests.fakes.mail import FakeMailbox, make_msg
from tests.integration.conftest import seed_user

pytestmark = pytest.mark.integration


def _company(s, name, ats, slug, host=None, careers_url=None):
    cid = registry.admit(s, registry.CompanyRef(name=name, ats=ats, slug=slug, host=host, careers_url=careers_url))
    s.execute(text("UPDATE companies SET last_probed_at = NULL WHERE id = :i"), {"i": cid})
    return cid


def _board(s, cid):
    return tuple(s.execute(text("SELECT ats_type::text, ats_slug, ats_host FROM companies WHERE id = :i"),
                           {"i": cid}).first())


# --- registry re-probe keeps good data ---------------------------------------------------
def test_reprobe_keeps_valid_boards_and_survives_outages(module_db):
    from app import db
    uni = FakeUniverse(seed=5)
    uni.add_company("Northwind Payments", "greenhouse", "northwind", n_jobs=2)   # name can't derive this slug
    base = uni.transport()

    def handler(req: httpx.Request) -> httpx.Response:
        if "/boards/outage-co/" in str(req.url):
            return httpx.Response(503)                     # a transient outage on a stored board
        return base.handle_request(req)
    f = make_fetcher(httpx.MockTransport(handler), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)
    with db.worker_session() as s:
        keep = _company(s, "Northwind Payments", "greenhouse", "northwind")
        outage = _company(s, "Outage Co", "greenhouse", "outage-co")
        gone = _company(s, "Gone Co", "greenhouse", "gone-co")                  # 404: really moved away
        registry.reprobe_stale(s, f)
        assert _board(s, keep) == ("greenhouse", "northwind", None)
        assert _board(s, outage) == ("greenhouse", "outage-co", None)           # not erased by a 503
        assert _board(s, gone) == ("unknown", None, None)
        probed = s.execute(text("SELECT count(*) FROM companies WHERE id IN (:a, :b, :c) AND last_probed_at IS NOT NULL"),
                           {"a": keep, "b": outage, "c": gone}).scalar()
        assert probed == 3


def test_reprobe_keeps_a_discovered_workday_board(module_db):
    from app import db
    uni = FakeUniverse(seed=6)
    uni.add_company("WD Corp", "workday", "wdcorp", n_jobs=2)
    f = make_fetcher(uni.transport(), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)
    with db.worker_session() as s:
        cid = _company(s, "WD Corp", "workday", "wdcorp/careers", host="wdcorp.wd5.myworkdayjobs.com")
        registry.reprobe_stale(s, f)
        assert _board(s, cid) == ("workday", "wdcorp/careers", "wdcorp.wd5.myworkdayjobs.com")


# --- one malformed board doesn't abort the round ---------------------------------------
def test_malformed_board_is_isolated(module_db):
    from app import db
    from app.ingest.runner import RoundResult, poll_registry
    from app.ingest.scoring import profile_from_vault
    from app.vault.schema import Vault
    uni = FakeUniverse(seed=7)
    uni.add_company("Healthy Co", "lever", "healthy", n_jobs=3, junior=True)
    base = uni.transport()

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "/boards/cloudflare/" in u:
            return httpx.Response(200, text="<html>Just a moment...</html>")
        if "/boards/arrayco/" in u:
            return httpx.Response(200, json=[1, 2, 3])
        return base.handle_request(req)
    f = make_fetcher(httpx.MockTransport(handler), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)
    with db.worker_session() as s:
        s.execute(text("UPDATE companies SET last_polled_at = now()"))         # only this test's companies are due
        ids = [_company(s, "Cloudflare Co", "greenhouse", "cloudflare"),
               _company(s, "Array Co", "greenhouse", "arrayco"),
               _company(s, "Healthy Co", "lever", "healthy")]
        s.execute(text("UPDATE companies SET last_polled_at = NULL WHERE id = ANY(:i)"), {"i": ids})
    import json
    from pathlib import Path
    vault = Vault.model_validate(json.loads((Path(__file__).parents[2] / "examples/vault.example.json").read_text()))
    uid = seed_user(module_db)
    res = RoundResult()
    jobs = poll_registry(f, [profile_from_vault(uid, vault, 1)], res)
    assert any(j.company_slug == "healthy" for j in jobs)                      # the healthy board still delivered
    assert any("Cloudflare Co" in e for e in res.errors) and any("Array Co" in e for e in res.errors)
    with db.worker_session() as s:
        polled = dict(s.execute(text("SELECT name, last_polled_at IS NOT NULL FROM companies WHERE id = ANY(:i)"),
                                {"i": ids}).all())
    # wrong JSON shape: waits the normal interval instead of failing every round;
    # an HTML challenge page is a source error, retried next round and counted against source health
    assert polled == {"Array Co": True, "Healthy Co": True, "Cloudflare Co": False}


# --- "unclear" mail never marks an application positive ------------------------------------
def test_unclear_mail_does_not_mark_application_positive(module_db):
    from app import db
    from app.mail.processor import process_mailbox
    from app.util.hashing import company_key, h64
    uid = seed_user(module_db)
    with module_db.begin() as c:
        aid = c.execute(text("""
            INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title,
                apply_url, status, applied_at, source_id, bucket, components)
            VALUES (:u, 777001, 777001, :k, 'Northwind Payments', 'SDE I', 'https://boards.greenhouse.io/nw/jobs/1',
                    'applied', now() - interval '2 days', 1, 3, '{800,1000,900,900,300,500}') RETURNING id"""),
            {"u": uid, "k": h64(company_key("Northwind Payments"))}).scalar_one()
    box = FakeMailbox()
    box.add(make_msg("hr@northwindpay.com", "Northwind Payments quarterly update",
                     "Here is what our teams shipped this quarter. Unfortunately the office party moved to Friday."))
    process_mailbox(box, db.worker_session, uid, notify=lambda *a, **k: None, now=datetime.now(timezone.utc))
    with module_db.connect() as c:
        row = c.execute(text("SELECT status, outcome_recorded FROM applications WHERE id = :a"), {"a": aid}).first()
        assert (row.status, row.outcome_recorded) == ("applied", False)
        assert c.execute(text("SELECT count(*) FROM training_examples WHERE user_id = :u"), {"u": uid}).scalar() == 0


# --- the multi-step form loop always ends --------------------------------------------------
def _has_chromium():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def browser():
    if not _has_chromium():
        pytest.skip("no Chromium for Playwright")
    from app.apply.browser import browser_session
    with browser_session() as ctx:
        yield ctx


@pytest.mark.parametrize("page_html,expect_reason", [
    ("<form><p>Step</p><button>Next</button></form>", "after 12 steps"),
    ("<form><div role='alert'>Phone number is required</div><button>Next</button></form>", "not accepted"),
])
def test_form_loop_is_bounded(module_db, browser, monkeypatch, page_html, expect_reason):
    from app.apply import runner
    from app.apply.adapters.base import Adapter, StepResult
    from tests.integration.test_apply_e2e import new_app

    class StuckWizard(Adapter):            # "Next" that never moves on, like a rejected Workday step
        name = "stuckwizard"
        clicks = 0

        def open(self, page, url):
            page.set_content(page_html)
            return StepResult(True, "open")

        def fields(self, page):
            return []

        def advance(self, page):
            StuckWizard.clicks += 1
            return True
    monkeypatch.setattr(runner, "get_adapter", lambda ats, url: StuckWizard())
    uid = seed_user(module_db)
    aid = new_app(module_db, uid, f"https://careers.example.com/jobs/{abs(hash(page_html)) % 10**6}")
    out = runner.run_application(aid, browser)
    assert out.status == "assisted" and expect_reason in out.reason
    assert StuckWizard.clicks <= runner.MAX_FORM_STEPS
    with module_db.connect() as c:
        row = c.execute(text("SELECT status, failure_step, claimed_at FROM applications WHERE id = :a"), {"a": aid}).first()
    assert (row.status, row.failure_step, row.claimed_at) == ("waiting", "form", None)   # never claimed or submitted
