"""P5: Ashby, SmartRecruiters (shadow DOM), Workable, Workday (account +
verification + multi-step + custom listboxes), generic LLM-guided adapter."""
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from tests.fakes.mail import FakeMailbox, make_msg
from tests.fakes.p5_site import P5Site
from tests.integration.conftest import seed_user

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]
VAULT = json.loads((ROOT / "examples/vault.example.json").read_text())
JD = (ROOT / "examples/jd.example.txt").read_text()
_RECIPE = None


@pytest.fixture(scope="module")
def site():
    with P5Site() as s:
        yield s


@pytest.fixture(scope="module")
def browser():
    from app.apply.browser import browser_session
    try:
        with browser_session() as ctx:
            yield ctx
    except Exception as e:
        pytest.skip(f"no Chromium: {e}")


@pytest.fixture(autouse=True)
def redis_clean():
    from app.redis_client import get_redis
    get_redis.cache_clear()
    r = get_redis()
    if r:
        r.flushdb()


def new_app(eng, uid, url, ats, company):
    global _RECIPE
    from app.resume.engine import build_recipe, prepare_jd
    from app.util.hashing import company_key, h64
    from app.vault.schema import Vault
    if _RECIPE is None:
        v = Vault.model_validate(VAULT)
        _RECIPE = build_recipe(v, prepare_jd(v, JD, "Software Engineer", company), vault_revision=1).to_storage()
    with eng.begin() as c:
        return c.execute(text("""
            INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title,
                apply_url, status, recipe, vault_revision, ats, bucket, score, location)
            VALUES (:u, :c, :c, :k, :cn, 'Software Engineer', :url, 'queued', CAST(:r AS jsonb), 1,
                    CAST(:ats AS ats_type), 3, 0.85, 'Bengaluru, India') RETURNING id"""),
            {"u": uid, "c": abs(hash(url)) % 10**15, "k": h64(company_key(company)), "cn": company, "url": url,
             "r": json.dumps(_RECIPE), "ats": ats}).scalar_one()


def run(aid, browser, **kw):
    from app.apply.runner import run_application
    return run_application(aid, browser, **kw)


def test_ashby(module_db, site, browser):
    uid = seed_user(module_db)
    aid = new_app(module_db, uid, f"{site.base}/ashby/acme/1e3e0c4a-1111-2222-3333-444455556666", "ashby", "Ashby Acme")
    out = run(aid, browser)
    assert out.status == "applied", out
    f = site.submissions[-1].fields
    assert (f["name"], f["email"], f["spons"]) == ("Sample Candidate", "sample.candidate@example.com", "No")
    assert site.submissions[-1].files["resume"][0].endswith(".pdf")


def test_workable(module_db, site, browser):
    uid = seed_user(module_db)
    aid = new_app(module_db, uid, f"{site.base}/workable/northwind/j/ABC123", "workable", "Workable Co")
    assert run(aid, browser).status == "applied"
    f = site.submissions[-1].fields
    assert (f["firstname"], f["lastname"], f["phone"]) == ("Sample", "Candidate", "+91 98765 43210")


def test_smartrecruiters_shadow_dom(module_db, site, browser):
    uid = seed_user(module_db)
    aid = new_app(module_db, uid, f"{site.base}/smartrecruiters/Visa/743999912345", "smartrecruiters", "SR Visa")
    out = run(aid, browser)
    assert out.status == "applied", out
    f = site.submissions[-1].fields
    assert (f["firstName"], f["lastName"], f["email"]) == ("Sample", "Candidate", "sample.candidate@example.com")
    assert site.submissions[-1].files["resume"][1] > 10_000


def test_generic_adapter_with_llm_confirmation(module_db, site, browser):
    from app.llm.client import LLM
    uid = seed_user(module_db)
    aid = new_app(module_db, uid, f"{site.base}/custom/careers/sde", "jsonld", "Contoso Custom")
    llm = LLM("anthropic", "k", "https://llm.test", "c", "s", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(
            {"submitted": True, "confidence": 0.95})}]})))
    out = run(aid, browser, llm=llm)
    assert out.status == "applied", out
    f = site.submissions[-1].fields
    assert (f["full_name"], f["mail"], f["expected"]) == ("Sample Candidate", "sample.candidate@example.com", "12 LPA")


def test_generic_without_confirmation_keeps_claim(module_db, site, browser):
    uid = seed_user(module_db)
    aid = new_app(module_db, uid, f"{site.base}/custom/careers/sde2", "jsonld", "Contoso Two")
    out = run(aid, browser)                                   # no LLM: unusual success text can't be confirmed
    assert out.status == "unconfirmed"
    with module_db.connect() as c:                            # fail-safe: ledger kept, never applies twice
        assert c.execute(text("SELECT count(*) FROM applied_ledger WHERE user_id = :u"), {"u": uid}).scalar() == 1


def test_workday_account_verification_and_steps(module_db, site, browser):
    from app import db
    uid = seed_user(module_db)
    box = FakeMailbox()
    site.wd.on_verification_email = lambda email, link: box.add(make_msg(
        "noreply@myworkday.com", "Verify your email", f"Please verify your email address: {link}",
        html=f'<a href="{link}">Verify Email</a>'))
    url = f"{site.base}/workday/acme/job/4001"
    aid = new_app(module_db, uid, url, "workday", "Acme Workday")
    out = run(aid, browser, mail_client_for=lambda s, u: box)
    assert out.status == "applied", out
    sub = site.submissions[-1]
    f = sub.fields
    assert (f["first"], f["last"], f["city"]) == ("Sample", "Candidate", "Delhi, India")
    assert f["source"] == "Company Website"                     # custom listbox
    assert f["gender"] == "I do not wish to answer" and f["terms"] == "yes"
    assert (f["auth"], f["notice"]) == ("Yes", "Immediate")
    assert sub.files["resume"][0].endswith(".pdf")
    assert site.wd.created == ["sample.candidate@example.com"]
    # the verification mail was consumed and trashed (P4)
    assert all(m in box.trashed for m in box.messages)
    with db.worker_session() as s:
        row = s.execute(text("SELECT kind, secret_enc FROM credentials WHERE user_id = :u AND host = '127.0.0.1'"),
                        {"u": uid}).first()
        assert row.kind == "ats"
        password = site.wd.accounts["sample.candidate@example.com"]["password"]
        assert password.encode() not in bytes(row.secret_enc)         # encrypted at rest
        from app.mail.accounts import load_credential
        assert load_credential(s, uid, "127.0.0.1")[1]["password"] == password

    # second job at the same tenant: signs in with the stored account
    aid2 = new_app(module_db, uid, f"{site.base}/workday/acme/job/4002", "workday", "Acme Workday 2")
    assert run(aid2, browser, mail_client_for=lambda s, u: box).status == "applied"
    assert site.wd.created == ["sample.candidate@example.com"]          # no second account
