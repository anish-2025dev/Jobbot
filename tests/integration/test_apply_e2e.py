"""P3 done-test: end-to-end auto-apply on test postings with all caps
enforced. Real Chromium (Playwright) fills real forms on a local fake ATS;
the gate runs on real Postgres."""
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.ats_site import FakeATSSite
from tests.integration.conftest import seed_user

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]
VAULT = json.loads((ROOT / "examples/vault.example.json").read_text())
JD = (ROOT / "examples/jd.example.txt").read_text()
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")


def _has_chromium():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


if not _has_chromium():
    pytestmark = [pytestmark, pytest.mark.skip(reason="no Chromium for Playwright")]


@pytest.fixture(scope="module")
def site():
    with FakeATSSite() as s:
        yield s


@pytest.fixture(scope="module")
def browser():
    from app.apply.browser import browser_session
    with browser_session() as ctx:
        yield ctx


@pytest.fixture(autouse=True)
def redis_clean():
    from app.redis_client import get_redis
    get_redis.cache_clear()
    r = get_redis()
    if r:
        r.flushdb()


def recipe_json():
    from app.resume.engine import build_recipe, prepare_jd
    from app.vault.schema import Vault
    v = Vault.model_validate(VAULT)
    return build_recipe(v, prepare_jd(v, JD, "Software Development Engineer I", "Acme"), vault_revision=1).to_storage()


RECIPE = None


def new_app(eng, uid, url, *, company="Acme", ats="greenhouse", canon=None, bucket=3, title="Software Development Engineer I",
            fingerprint=None):
    global RECIPE
    RECIPE = RECIPE or recipe_json()
    from app.util.hashing import company_key, h64
    canon = canon if canon is not None else abs(hash(url)) % 10**15
    with eng.begin() as c:
        return c.execute(text("""
            INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title, apply_url,
                status, recipe, vault_revision, ats, bucket, score, location, source_id, components)
            VALUES (:u, :c, :fp, :k, :cn, :t, :url, 'queued', CAST(:r AS jsonb), 1, CAST(:ats AS ats_type), :b, 0.85,
                    'Bengaluru, India', 1, '{900,1000,1000,900,300,500}') RETURNING id"""),
            {"u": uid, "c": canon, "fp": fingerprint if fingerprint is not None else canon,
             "k": h64(company_key(company)), "cn": company, "t": title, "url": url,
             "r": json.dumps(RECIPE), "ats": ats, "b": bucket}).scalar_one()


def app_row(eng, aid):
    with eng.connect() as c:
        return c.execute(text("SELECT * FROM applications WHERE id = :i"), {"i": aid}).mappings().first()


def ledger_count(eng, uid):
    with eng.connect() as c:
        return c.execute(text("SELECT count(*) FROM applied_ledger WHERE user_id = :u"), {"u": uid}).scalar()


def run(aid, browser, **kw):
    from app.apply.runner import run_application
    return run_application(aid, browser, **kw)


def test_greenhouse_happy_path(module_db, site, browser):
    uid = seed_user(module_db, settings={"auto_apply": True})
    url = site.add("/greenhouse/acme/jobs/1001")
    aid = new_app(module_db, uid, url)
    out = run(aid, browser)
    assert out.status == "applied", out
    sub = site.submissions[-1]
    f = sub.fields
    assert (f["first_name"], f["last_name"], f["email"]) == ("Sample", "Candidate", "sample.candidate@example.com")
    assert (f["q_auth"], f["q_spons"], f["q_notice"]) == ("Yes", "No", "Immediate")
    assert f["eeo_gender"] == "Decline To Self Identify" and f["eeo_vet"] == "I don't wish to answer"
    assert f["q_linkedin"].startswith("https://www.linkedin.com")
    fname, size = sub.files["resume"]
    assert fname == "Sample_Candidate_Resume.pdf" and size > 10_000
    assert "cover_letter" not in sub.files or sub.files["cover_letter"][1] == 0
    row = app_row(module_db, aid)
    assert row["status"] == "applied" and row["applied_at"] is not None
    assert ledger_count(module_db, uid) == 1


def test_caps_gap_and_duplicates(module_db, site, browser):
    uid = seed_user(module_db, settings={"auto_apply": True})
    urls = [site.add(f"/greenhouse/capco/jobs/{i}") for i in range(2001, 2006)]
    a1 = new_app(module_db, uid, urls[0], company="CapCo")
    assert run(a1, browser).status == "applied"

    a2 = new_app(module_db, uid, urls[1], company="CapCo")
    out = run(a2, browser)
    assert out.status == "requeued" and "C6" in out.reason            # 3-hour gap
    nb = app_row(module_db, a2)["not_before"]
    assert timedelta(hours=2, minutes=59) < nb - app_row(module_db, a1)["applied_at"] <= timedelta(hours=3, seconds=5)

    def age_gap():
        with module_db.begin() as c:
            c.execute(text("""UPDATE company_cooldown SET last_applied_at = last_applied_at - interval '4 hours',
                              applied_ats = ARRAY(SELECT t - interval '4 hours' FROM unnest(applied_ats) t)
                              WHERE user_id = :u"""), {"u": uid})

    age_gap()
    assert run(a2, browser).status == "applied"
    age_gap()
    a3 = new_app(module_db, uid, urls[2], company="CapCo")
    assert run(a3, browser).status == "applied"
    age_gap()
    a4 = new_app(module_db, uid, urls[3], company="CapCo")
    out = run(a4, browser)
    assert out.status == "closed" and "C5" in out.reason                # 3 per 30 days
    n_subs = len([s for s in site.submissions if "/capco/" in s.path])
    assert n_subs == 3

    # same opening via another portal: different canonical id, same fingerprint -> never twice (C4)
    dup = new_app(module_db, uid, urls[4], company="OtherCo", canon=987654321,
                  fingerprint=app_row(module_db, a1)["fingerprint"])
    out = run(dup, browser)
    assert out.status == "closed" and "C4" in out.reason


def test_captcha_goes_assisted_without_submitting(module_db, site, browser):
    uid = seed_user(module_db, settings={"auto_apply": True})
    aid = new_app(module_db, uid, site.add("/greenhouse/capt/jobs/3001", "captcha"), company="CaptchaCo")
    before = len(site.submissions)
    out = run(aid, browser)
    assert out.status == "assisted" and "CAPTCHA" in out.reason
    assert len(site.submissions) == before and ledger_count(module_db, uid) == 0
    with module_db.connect() as c:
        pack = c.execute(text("SELECT ready_pack FROM waiting_list WHERE application_id = :a"), {"a": aid}).scalar()
    assert pack["failure"]["step"] == "captcha" and pack["copy"]["Email"] == "sample.candidate@example.com"


def test_unknown_question_then_answer_and_retry(module_db, site, browser):
    from app.apply.runner import save_answers
    uid = seed_user(module_db, settings={"auto_apply": True})
    aid = new_app(module_db, uid, site.add("/greenhouse/q/jobs/4001", "unknown_question"), company="QCo")
    out = run(aid, browser)
    assert out.status == "assisted" and "disagreed" in out.reason
    with module_db.connect() as c:
        pack = c.execute(text("SELECT ready_pack FROM waiting_list WHERE application_id = :a"), {"a": aid}).scalar()
    q = pack["unresolved_questions"][0]
    assert "disagreed with your manager" in q
    save_answers(uid, {q: "During my internship I raised a concern about test coverage and we agreed on a plan."})
    with module_db.begin() as c:
        c.execute(text("UPDATE applications SET status = 'queued', mode = 'auto' WHERE id = :a"), {"a": aid})
    assert run(aid, browser).status == "applied"
    assert site.submissions[-1].fields["q_story"].startswith("During my internship")


def test_pdf_rejected_uses_docx_twin(module_db, site, browser):
    uid = seed_user(module_db, settings={"auto_apply": True})
    aid = new_app(module_db, uid, site.add("/greenhouse/docx/jobs/5001", "reject_pdf"), company="DocxCo")
    assert run(aid, browser).status == "applied"
    fname, size = site.submissions[-1].files["resume"]
    assert fname.endswith(".docx") and size > 5_000


def test_server_validation_error_releases_claim(module_db, site, browser):
    uid = seed_user(module_db, settings={"auto_apply": True})
    aid = new_app(module_db, uid, site.add("/greenhouse/err/jobs/6001", "server_error"), company="ErrCo")
    out = run(aid, browser)
    assert out.status in ("manual", "assisted") and "not accepted" in out.reason
    assert ledger_count(module_db, uid) == 0                          # claim released
    with module_db.connect() as c:
        n = c.execute(text("SELECT cardinality(applied_ats) FROM company_cooldown WHERE user_id = :u"), {"u": uid}).scalar()
        assert n in (0, None)
        assert c.execute(text("SELECT 1 FROM waiting_list WHERE application_id = :a"), {"a": aid}).scalar() == 1


def test_lever_with_radio_and_learned_answer(module_db, site, browser):
    from app.apply.runner import save_answers
    uid = seed_user(module_db, settings={"auto_apply": True})
    save_answers(uid, {"Why do you want to work at Lever Labs? ✱": "Your backend platform matches the APIs I build."})
    url = site.add("/lever/leverlabs/5ac21346-8e0c-4494-8e7a-3eb92ff77902")
    aid = new_app(module_db, uid, url, company="Lever Labs", ats="lever", title="Backend Engineer")
    out = run(aid, browser)
    assert out.status == "applied", out
    f = site.submissions[-1].fields
    assert f["name"] == "Sample Candidate" and f["org"] == "Acme Health Tech"
    assert f["cards[r1][field0]"] == "Yes"                            # willing to relocate (vault: true)
    assert f["cards[c1][field0]"].startswith("Your backend platform")
    assert site.submissions[-1].files["resume"][0].endswith(".pdf")


def test_adapter_pauses_after_failures(module_db, site, browser):
    from app import db
    from app.apply import health
    with db.worker_session() as s:
        for _ in range(6):
            health.record(s, "lever", False)
    uid = seed_user(module_db, settings={"auto_apply": True})
    aid = new_app(module_db, uid, site.add("/lever/paused/5ac21346-8e0c-4494-8e7a-3eb92ff77903"),
                  company="Paused Inc", ats="lever")
    out = run(aid, browser)
    assert out.status == "manual" and "paused" in out.reason
    with db.worker_session() as s:
        health.resume(s, "lever")


def test_referral_hold_blocks_apply(module_db, site, browser):
    uid = seed_user(module_db, settings={"auto_apply": True})
    aid = new_app(module_db, uid, site.add("/greenhouse/hold/jobs/7001"), company="HoldCo")
    row = app_row(module_db, aid)
    with module_db.begin() as c:
        c.execute(text("""INSERT INTO referral_queue (user_id, application_id, canonical_hash, company_key_hash, job,
                          status, apply_at) VALUES (:u, :a, :c, :k, '{}', 'sent', now() + interval '20 hours')"""),
                  {"u": uid, "a": aid, "c": row["canonical_hash"], "k": row["company_key_hash"]})
    out = run(aid, browser)
    assert out.status == "requeued" and "C15" in out.reason


def test_dry_run_fills_without_submitting(module_db, site, browser):
    uid = seed_user(module_db, settings={"auto_apply": True})
    aid = new_app(module_db, uid, site.add("/greenhouse/dry/jobs/8001"), company="DryCo")
    before = len(site.submissions)
    out = run(aid, browser, submit=False)
    assert out.status == "dry-run" and out.step == "dry-run" and len(site.submissions) == before and ledger_count(module_db, uid) == 0
