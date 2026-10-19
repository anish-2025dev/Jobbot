"""P4 done-test: a test job runs the full cycle: contacts found, emails
approved and sent, reply classified, application submitted at T+24h.
Plus: referral received before the hold ends, late referral, C14 caps,
exhausted providers, and the sender-reputation guard."""
import email
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.ats_site import FakeATSSite
from tests.fakes.mail import FakeMailbox, make_msg
from tests.fakes.providers import FakeProviders
from tests.integration.conftest import seed_user

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]
VAULT = json.loads((ROOT / "examples/vault.example.json").read_text())
JD = (ROOT / "examples/jd.example.txt").read_text()
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")


@pytest.fixture(scope="module")
def site():
    with FakeATSSite() as s:
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


class Env:
    def __init__(self, eng, site, *, mode="review", company="Northwind Payments", domain="northwindpay.com"):
        from app import db
        from app.mail.accounts import save_credential
        from app.util.hashing import company_key, h64
        now = datetime.now(timezone.utc)
        enabled = now if mode == "review" else now - timedelta(days=8)
        self.eng, self.site, self.company = eng, site, company
        self.uid = seed_user(eng, settings={"auto_apply": True, "outreach": True, "outreach_mode": mode,
                                            "outreach_enabled_at": enabled.isoformat()})
        self.key = h64(company_key(company))
        self.fp = FakeProviders(domain=domain)
        self.box = FakeMailbox()
        self.notes: list[str] = []
        with db.worker_session() as s:
            save_credential(s, self.uid, "hunter", "me", {"api_key": "hunter-key"})
            save_credential(s, self.uid, "gmail", "sample.candidate@gmail.com",
                            {"client_id": "x", "client_secret": "y", "refresh_token": "z"})
            s.execute(text("""INSERT INTO companies (company_key, company_key_hash, name, ats_type, email_domain, timezone)
                              VALUES (:ck, :k, :n, 'greenhouse', :d, 'Asia/Kolkata') ON CONFLICT DO NOTHING"""),
                      {"ck": company_key(company), "k": self.key, "n": company, "d": domain})

    def notify(self, uid, t, buttons=None):
        self.notes.append(t)

    def app(self, path, title="Software Development Engineer I"):
        from app.resume.engine import build_recipe, prepare_jd
        from app.vault.schema import Vault
        v = Vault.model_validate(VAULT)
        recipe = build_recipe(v, prepare_jd(v, JD, title, self.company), vault_revision=1, bucket="high").to_storage()
        url = self.site.add(path)
        with self.eng.begin() as c:
            return c.execute(text("""
                INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title,
                    apply_url, status, recipe, vault_revision, ats, bucket, score, location, req_id, source_id, components)
                VALUES (:u, :c, :c, :k, :cn, :t, :url, 'queued', CAST(:r AS jsonb), 1, 'greenhouse', 3, 0.9,
                        'Bengaluru, India', :req, 1, '{900,1000,1000,900,300,500}') RETURNING id"""),
                {"u": self.uid, "c": abs(hash(path)) % 10**15, "k": self.key, "cn": self.company, "t": title,
                 "url": url, "r": json.dumps(recipe), "req": "R" + path.rsplit("/", 1)[-1]}).scalar_one()

    def tick(self, now=None):
        from app import db
        from app.outreach.queue import tick
        return tick(db.worker_session, now=now, notify=self.notify, mail_client_for=lambda s, u: self.box,
                    transport=self.fp.transport())

    def rq(self):
        with self.eng.connect() as c:
            return c.execute(text("SELECT * FROM referral_queue WHERE user_id = :u ORDER BY id DESC LIMIT 1"),
                             {"u": self.uid}).mappings().first()

    def mail(self):
        from app import db
        from app.mail.processor import process_mailbox
        from app.outreach.queue import on_reply
        return process_mailbox(self.box, db.worker_session, self.uid, notify=self.notify,
                               on_outreach_reply=lambda s, u, m, c, msg: on_reply(s, u, m, c, msg, self.notify))


def enqueue(env, app_id):
    from app import db
    from app.outreach.queue import try_enqueue
    with db.worker_session() as s:
        return try_enqueue(s, env.uid, app_id)


def test_full_cycle_without_referral(module_db, site, browser):
    from app import db
    from app.apply.runner import run_application
    from app.notify import bot_loop
    env = Env(module_db, site)
    aid = env.app("/greenhouse/northwind/jobs/9001")
    assert enqueue(env, aid)
    with module_db.connect() as c:                                      # C5 slot reserved at T0
        assert c.execute(text("SELECT reserved_slots FROM company_cooldown WHERE user_id = :u"), {"u": env.uid}).scalar() == 1

    env.tick()                                                          # finding -> drafts (review mode)
    rq = env.rq()
    assert rq["status"] == "awaiting_approval"
    roles = {c["role"]: c for c in rq["contacts"]}
    assert set(roles) == {"peer", "manager", "recruiter"}
    assert roles["peer"]["name"] == "Priya Sharma" and roles["manager"]["name"] == "Arjun Mehta"
    assert all("Rahul" not in c["name"] and "Sana" not in c["name"] for c in rq["contacts"])   # VP + intern excluded
    assert len([n for n in env.notes if n.startswith("Referral draft")]) == 3

    # one-tap approvals through the Telegram callback handler
    from app.notify.telegram import TelegramBot, new_link_code
    import httpx
    with db.worker_session() as s:
        code = new_link_code(s, env.uid)
    answers = []
    bot = TelegramBot("t", transport=httpx.MockTransport(
        lambda r: answers.append(json.loads(r.content)) or httpx.Response(200, json={"ok": True, "result": True})))
    bot_loop.handle_update(bot, {"update_id": 1, "message": {"chat": {"id": 4242}, "text": f"/start {code}"}})
    idx = {c["role"]: i for i, c in enumerate(rq["contacts"])}
    for role, data in (("peer", "ok"), ("manager", "ok"), ("recruiter", "skip")):
        bot_loop.handle_update(bot, {"update_id": 2, "callback_query": {
            "id": "cb", "data": f"{data}:{rq['id']}:{idx[role]}", "message": {"chat": {"id": 4242}}}})
    rq = env.rq()
    assert rq["status"] == "scheduled"
    times = [datetime.fromisoformat(c["scheduled_at"]) for c in rq["contacts"] if c["status"] == "scheduled"]
    assert len(times) == 2
    from zoneinfo import ZoneInfo
    assert all(9 <= t.astimezone(ZoneInfo("Asia/Kolkata")).hour < 11 for t in times)        # 9-11 am recipient time
    assert 5 * 60 <= abs((times[1] - times[0]).total_seconds()) <= 15 * 60                  # 5-15 min apart

    send_time = max(times) + timedelta(minutes=1)
    env.tick(now=send_time)
    assert len(env.box.sent) == 2
    for raw in env.box.sent:
        m = email.message_from_bytes(raw)
        body = next(p for p in m.walk() if p.get_content_type() == "text/plain").get_payload(decode=True).decode()
        assert "If this isn't relevant, just let me know and I won't reach out again." in body
        assert any(p.get_filename() and p.get_filename().endswith(".pdf") for p in m.walk())
    assert all("JobBot/Outreach" in env.box.labels[f"sent{i}"] for i in (1, 2))           # labeled in Sent
    rq = env.rq()
    assert rq["status"] == "sent"
    assert rq["apply_at"] - rq["sent_at"] == timedelta(hours=24)                            # C15
    with module_db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM outreach_log WHERE user_id = :u"), {"u": env.uid}).scalar() == 2

    # replies: a question (alert, hold unchanged) and a decline (do-not-contact)
    sent = {c["role"]: c for c in rq["contacts"] if c["status"] == "sent"}
    env.box.add(make_msg(sent["peer"]["email"], "Re: Referral request", "Sure, can you send me your resume and the job ID?",
                         thread_id=sent["peer"]["gmail_thread_id"]))
    env.box.add(make_msg(sent["manager"]["email"], "Re: SDE on your team", "Sorry, I'm not able to refer. Please don't contact me again.",
                         thread_id=sent["manager"]["gmail_thread_id"]))
    env.mail()
    assert any("Reply to your referral ask" in n for n in env.notes)
    after = env.rq()
    assert after["apply_at"] == rq["apply_at"] and after["status"] == "sent"
    from app.crypto import keyed_hash64
    with module_db.connect() as c:
        assert c.execute(text("SELECT 1 FROM do_not_contact WHERE user_id = :u AND email_hash = :h"),
                         {"u": env.uid, "h": keyed_hash64(sent["manager"]["email"])}).scalar() == 1

    # T+24h: not a minute early, then applied without a referral
    env.tick(now=rq["apply_at"] - timedelta(minutes=1))
    assert env.rq()["status"] == "sent"
    env.tick(now=rq["apply_at"] + timedelta(minutes=1))
    assert env.rq()["status"] == "applying"
    out = run_application(aid, browser)
    assert out.status == "applied", out
    assert site.submissions[-1].fields["q_hear"] != "Employee referral"
    env.tick()
    assert env.rq()["status"] == "applied"
    with module_db.connect() as c:                                       # reserved slot consumed, not leaked
        row = c.execute(text("SELECT reserved_slots, cardinality(applied_ats) FROM company_cooldown WHERE user_id = :u"),
                        {"u": env.uid}).first()
        assert tuple(row) == (0, 1)


def test_referral_before_hold_applies_immediately(module_db, site, browser):
    from app.apply.runner import run_application
    env = Env(module_db, site, mode="auto", company="Contoso Ltd", domain="contoso.in")
    aid = env.app("/greenhouse/contoso/jobs/9101")
    assert enqueue(env, aid)
    env.tick()                                                           # auto mode: straight to scheduled
    rq = env.rq()
    assert rq["status"] == "scheduled" and not [n for n in env.notes if n.startswith("Referral draft")]
    t = max(datetime.fromisoformat(c["scheduled_at"]) for c in rq["contacts"]) + timedelta(minutes=1)
    env.tick(now=t)
    rq = env.rq()
    peer = next(c for c in rq["contacts"] if c["role"] == "peer")
    link = site.add("/greenhouse/contoso/jobs/9102")
    env.box.add(make_msg(peer["email"], "Re: Referral request",
                         f"Done! I've referred you. Use this referral link to apply: {link}",
                         thread_id=peer["gmail_thread_id"]))
    env.mail()
    rq = env.rq()
    assert rq["status"] == "applying" and rq["referral"]["by"] == "Priya Sharma"
    assert any("Applying now with the referral" in n for n in env.notes)
    out = run_application(aid, browser)
    assert out.status == "applied", out
    sub = site.submissions[-1]
    assert sub.path.endswith("/9102") and sub.fields["q_hear"] == "Employee referral"
    with module_db.connect() as c:
        assert c.execute(text("SELECT via FROM applied_ledger WHERE user_id = :u"), {"u": env.uid}).scalar() == 1


def test_late_referral_gets_ready_reply(module_db, site):
    env = Env(module_db, site, mode="auto", company="Fabrikam", domain="fabrikam.io")
    aid = env.app("/greenhouse/fabrikam/jobs/9201")
    assert enqueue(env, aid)
    env.tick()
    rq = env.rq()
    t = max(datetime.fromisoformat(c["scheduled_at"]) for c in rq["contacts"]) + timedelta(minutes=1)
    env.tick(now=t)
    with module_db.begin() as c:                                         # the bot already applied
        c.execute(text("UPDATE applications SET status = 'applied', applied_at = now() WHERE id = :a"), {"a": aid})
    peer = next(c for c in env.rq()["contacts"] if c["role"] == "peer")
    env.box.add(make_msg(peer["email"], "Re: Referral", "I've referred you, here's the referral link https://fabrikam.io/r/1",
                         thread_id=peer["gmail_thread_id"]))
    env.mail()
    with module_db.connect() as c:
        assert c.execute(text("SELECT status FROM applications WHERE id = :a"), {"a": aid}).scalar() == "applied_pending_referral"
    assert any("I've already applied (req R9201" in n for n in env.notes)


def test_c14_person_and_company_caps(module_db, site):
    env = Env(module_db, site, mode="auto", company="Tailspin Toys", domain="tailspin.io")
    a1 = env.app("/greenhouse/tailspin/jobs/9301")
    assert enqueue(env, a1)
    env.tick()
    rq1 = env.rq()
    t = max(datetime.fromisoformat(c["scheduled_at"]) for c in rq1["contacts"]) + timedelta(minutes=1)
    env.tick(now=t)
    first_people = {c["email"] for c in env.rq()["contacts"] if c["status"] == "sent"}
    assert len(first_people) == 3
    a2 = env.app("/greenhouse/tailspin/jobs/9302", title="Backend Engineer - New Grad")
    assert enqueue(env, a2)
    env.tick()
    rq2 = env.rq()
    # 3 contacts at this company in 30 days already: nobody new, outreach skipped, job applies normally
    assert rq2["status"] == "skipped"
    with module_db.connect() as c:
        assert c.execute(text("SELECT uses_reserved_slot FROM applications WHERE id = :a"), {"a": a2}).scalar() is False


def test_all_providers_exhausted_skips_to_application(module_db, site):
    env = Env(module_db, site, company="Litware", domain="litware.dev")
    env.fp.exhausted.add("hunter")
    aid = env.app("/greenhouse/litware/jobs/9401")
    assert enqueue(env, aid)
    env.tick()
    rq = env.rq()
    assert rq["status"] == "skipped" and "credits" in rq["job"]["skip_reason"]
    with module_db.connect() as c:
        assert c.execute(text("SELECT reserved_slots FROM company_cooldown WHERE user_id = :u"), {"u": env.uid}).scalar() == 0
        assert c.execute(text("SELECT exhausted_until IS NOT NULL FROM provider_usage WHERE user_id = :u AND provider = 'hunter'"),
                         {"u": env.uid}).scalar()


def test_bounce_rate_pauses_outreach(module_db, site):
    from app import db
    from app.outreach.queue import check_reputation, try_enqueue
    env = Env(module_db, site, company="Proseware", domain="proseware.co")
    with module_db.begin() as c:
        for st in (0, 0, 1):
            c.execute(text("INSERT INTO outreach_log (user_id, recipient_hash, company_key_hash, status) VALUES (:u, :h, 1, :s)"),
                      {"u": env.uid, "h": st * 7 + 1, "s": st})
    with db.worker_session() as s:
        assert check_reputation(s, env.uid, env.notify)
    assert any("Outreach paused for 7 days" in n for n in env.notes)
    aid = env.app("/greenhouse/proseware/jobs/9501")
    with db.worker_session() as s:
        assert not try_enqueue(s, env.uid, aid)


def test_daily_job_cap(module_db, site):
    from app import db
    from app.outreach.queue import try_enqueue
    env = Env(module_db, site, company="Wingtip", domain="wingtip.io")
    ids = [env.app(f"/greenhouse/wingtip/jobs/96{i:02d}") for i in range(11)]
    with module_db.begin() as c:     # different companies so C5 slots don't interfere
        for i, a in enumerate(ids):
            c.execute(text("UPDATE applications SET company_key_hash = :k WHERE id = :a"), {"k": 5_000 + i, "a": a})
    with db.worker_session() as s:
        results = [try_enqueue(s, env.uid, a) for a in ids]
    assert results.count(True) == 10 and results[-1] is False          # C14: 10 jobs/day
