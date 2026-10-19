"""P2 done-test: alerts arrive, rejections and low-value mail clean
themselves up, positives stay 7+ days, unmatched mail is never touched."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.mail import otp as otp_store
from app.mail.processor import cleanup_done_positives, process_mailbox, undo
from tests.fakes.mail import FakeMailbox, make_msg
from tests.integration.conftest import seed_user

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_redis():
    from app.redis_client import get_redis
    get_redis.cache_clear()
    r = get_redis()
    if r is None:
        pytest.skip("redis not available")
    r.flushdb()
    yield r
    r.flushdb()


def make_app(eng, uid, company="Northwind Payments", title="Software Development Engineer I", status="applied", **kw):
    from app.util.hashing import company_key, h64
    with eng.begin() as c:
        return c.execute(text("""
            INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title,
                apply_url, status, applied_at, source_id, bucket, components)
            VALUES (:u, :ch, :ch, :k, :cn, :t, 'https://boards.greenhouse.io/nw/jobs/1', CAST(:st AS application_status),
                    now() - interval '2 days', 1, 3, '{800,1000,900,900,300,500}') RETURNING id"""),
            {"u": uid, "ch": kw.get("canon", abs(hash((company, title))) % 10**15), "k": h64(company_key(company)),
             "cn": company, "t": title, "st": status}).scalar_one()


def run(eng, uid, box, notify=None):
    from app import db
    sent = []
    note = notify or (lambda u, t, buttons=None: sent.append(t))
    rep = process_mailbox(box, db.worker_session, uid, notify=note, now=datetime.now(timezone.utc))
    return rep, sent


def test_priorities_end_to_end(module_db):
    uid = seed_user(module_db)
    app_id = make_app(module_db, uid)
    rej_id = make_app(module_db, uid, company="Contoso", title="Backend Engineer")
    box = FakeMailbox()
    offer = box.add(make_msg("hr@northwindpay.com", "Offer letter - Northwind Payments",
                             "Congratulations! We are pleased to offer you the SDE I role."))
    interview = box.add(make_msg("no-reply@greenhouse.io", "Interview with Northwind Payments",
                                 "Northwind Payments would like to schedule your interview by October 30."))
    rejection = box.add(make_msg("careers@contoso.com", "Your application to Contoso",
                                 "We regret to inform you that we will not be moving forward."))
    confirm = box.add(make_msg("no-reply@greenhouse.io", "Thank you for applying to Contoso",
                               "Thank you for applying to Contoso. We have received your application."))
    personal = box.add(make_msg("friend@gmail.com", "Dinner?", "Are we still on for Saturday?"))
    newsletter = box.add(make_msg("news@randomshop.com", "Big sale", "Unfortunately the sale ends soon"))

    rep, sent = run(module_db, uid, box)

    # P0: kept, starred, labeled Offer
    assert offer.id not in box.trashed and {"STARRED", "JobBot/Offer"} <= box.labels[offer.id]
    # P1: kept, starred, labeled Action, alert with deadline
    assert interview.id not in box.trashed and "JobBot/Action" in box.labels[interview.id]
    assert any("Interview" in t and "Deadline" in t for t in sent)
    # P3/P4: trashed instantly
    assert rejection.id in box.trashed and confirm.id in box.trashed
    # unmatched mail: never touched at all
    assert not box.touched(personal.id) and not box.touched(newsletter.id)
    assert rep.untouched == 2

    with module_db.connect() as c:
        st = dict(c.execute(text("SELECT id, status FROM applications WHERE user_id = :u"), {"u": uid}).all())
        assert st[app_id] == "positive"
        assert st[rej_id] == "rejected"
        assert c.execute(text("SELECT deleted_at IS NOT NULL FROM applications WHERE id = :i"), {"i": rej_id}).scalar()
        resp = c.execute(text("SELECT class, priority, keep_until - received_at AS keep FROM responses WHERE user_id = :u ORDER BY priority"),
                         {"u": uid}).all()
        assert [(r.class_ if hasattr(r, "class_") else r[0], r.priority) for r in resp] == [("offer", 0), ("interview", 1)]
        assert all(r.keep >= timedelta(days=7) for r in resp)
        actions = c.execute(text("SELECT gmail_id, action FROM mail_actions WHERE user_id = :u"), {"u": uid}).all()
        assert {(a.gmail_id, a.action) for a in actions} >= {(rejection.id, "trash"), (confirm.id, "trash"),
                                                            (offer.id, "star_label")}
        # positive outcome recorded once, anonymously
        assert c.execute(text("SELECT sum(count) FROM outcomes_daily WHERE outcome >= 2")).scalar() >= 1

    # idempotent: a second run does nothing new
    before = list(box.calls)
    rep2, _ = run(module_db, uid, box)
    assert box.calls == before


def test_positive_kept_until_done_and_seven_days(module_db):
    from app import db
    uid = seed_user(module_db)
    make_app(module_db, uid, company="Fabrikam", title="SDE")
    box = FakeMailbox()
    m = box.add(make_msg("talent@fabrikam.com", "Fabrikam coding assessment", "Please complete the HackerRank test."))
    run(module_db, uid, box)
    with db.worker_session() as s:
        assert cleanup_done_positives(box, s, uid) == 0              # not done yet
        s.execute(text("UPDATE responses SET user_done_at = now() WHERE gmail_id = :g"), {"g": m.id})
        assert cleanup_done_positives(box, s, uid) == 0              # done, but < 7 days
        s.execute(text("UPDATE responses SET keep_until = now() - interval '1 minute' WHERE gmail_id = :g"), {"g": m.id})
        assert cleanup_done_positives(box, s, uid) == 1
    assert m.id in box.trashed


def test_offer_never_auto_trashed(module_db):
    from app import db
    uid = seed_user(module_db)
    make_app(module_db, uid, company="Litware", title="SDE")
    box = FakeMailbox()
    m = box.add(make_msg("hr@litware.com", "Your offer", "Congratulations, we are delighted to offer you the role."))
    run(module_db, uid, box)
    with db.worker_session() as s:
        s.execute(text("UPDATE responses SET user_done_at = now(), keep_until = now() - interval '30 days' WHERE gmail_id = :g"),
                  {"g": m.id})
        assert cleanup_done_positives(box, s, uid) == 0
    assert m.id not in box.trashed


def test_undo_restores(module_db):
    from app import db
    uid = seed_user(module_db)
    make_app(module_db, uid, company="Tailspin", title="SDE")
    box = FakeMailbox()
    m = box.add(make_msg("jobs@tailspin.com", "Update", "We regret to inform you that the position has been filled."))
    run(module_db, uid, box)
    assert m.id in box.trashed
    with db.worker_session() as s:
        aid = s.execute(text("SELECT id FROM mail_actions WHERE gmail_id = :g AND action = 'trash'"), {"g": m.id}).scalar()
        assert undo(box, s, uid, aid)
        assert not undo(box, s, uid, aid)                            # only once
    assert m.id not in box.trashed


def test_otp_stored_in_redis_then_mail_trashed(module_db, clean_redis):
    uid = seed_user(module_db)
    otp_store.register_waiter(uid, "myworkday.com")
    box = FakeMailbox()
    m = box.add(make_msg("noreply@myworkday.com", "Your verification code", "Your verification code is 552901."))
    run(module_db, uid, box)
    assert m.id in box.trashed
    keys = clean_redis.keys("*")
    assert all("552901" not in k for k in keys)                     # never in a key name
    assert otp_store.take_code(uid, "myworkday.com") == "552901"
    assert otp_store.take_code(uid, "myworkday.com") is None        # consumed once
    assert 0 < clean_redis.ttl(f"otpwait:{uid}:myworkday.com") <= 300
    with pytest.MonkeyPatch.context():
        pass
    from app import db
    with db.worker_session() as s:                                   # never written to Postgres
        dump = json.dumps([list(r) for r in s.execute(text("SELECT * FROM mail_actions")).all()], default=str)
    assert "552901" not in dump


def test_otp_from_unknown_site_untouched(module_db):
    uid = seed_user(module_db)
    box = FakeMailbox()
    m = box.add(make_msg("noreply@somebank.com", "Your OTP", "Your OTP is 123456 for login."))
    run(module_db, uid, box)
    assert not box.touched(m.id)


def test_job_alerts_parsed_and_trashed(module_db):
    uid = seed_user(module_db)
    box = FakeMailbox()
    html = ('<a href="https://www.linkedin.com/comm/jobs/view/4011112222/">Software Engineer I</a>'
            "<p>Northwind Payments · Bengaluru, India</p>")
    m = box.add(make_msg("jobalerts-noreply@linkedin.com", "1 new job for software engineer", html=html))
    rep, _ = run(module_db, uid, box)
    assert m.id in box.trashed
    assert [(j.title, j.company) for j in rep.alert_jobs] == [("Software Engineer I", "Northwind Payments")]


def test_unmatched_clear_positive_alerts_without_touching(module_db):
    uid = seed_user(module_db)
    box = FakeMailbox()
    m = box.add(make_msg("recruiter@unknownco.ai", "Interview opportunity",
                         "I came across your profile. Would you be available for an interview call next week?"))
    rep, sent = run(module_db, uid, box)
    assert not box.touched(m.id) and rep.alerts == 1 and sent


def test_bounce_marks_outreach_log(module_db):
    from app.crypto import keyed_hash64
    uid = seed_user(module_db)
    with module_db.begin() as c:
        c.execute(text("""INSERT INTO outreach_log (user_id, recipient_hash, company_key_hash) VALUES (:u, :h, 1)"""),
                  {"u": uid, "h": keyed_hash64("priya@acme.com")})
    box = FakeMailbox()
    box.add(make_msg("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
                     "Your message wasn't delivered to priya@acme.com because the address couldn't be found."))
    run(module_db, uid, box)
    with module_db.connect() as c:
        assert c.execute(text("SELECT status FROM outreach_log WHERE user_id = :u"), {"u": uid}).scalar() == 1


def test_digest_composes(module_db):
    from app import db
    from app.notify.digest import compose
    uid = seed_user(module_db)
    make_app(module_db, uid, company="Wingtip", title="SDE")
    box = FakeMailbox()
    box.add(make_msg("hr@wingtip.com", "Assessment", "Please complete the coding assessment by October 29."))
    run(module_db, uid, box)
    with db.worker_session() as s:
        text_ = compose(s, uid)
    assert "Needs action (1)" in text_ and "Wingtip" in text_ and "Storage:" in text_
