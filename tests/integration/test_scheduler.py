from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.fakes.mail import FakeMailbox, make_msg
from tests.integration.conftest import seed_user

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def redis_clean():
    from app.redis_client import get_redis
    get_redis.cache_clear()
    r = get_redis()
    if r is None:
        pytest.skip("redis needed")
    r.flushdb()
    yield r
    r.flushdb()


def test_due_interval_daily_weekly():
    from app.scheduler import Job, _mark, due
    now = datetime(2026, 10, 18, 21, 40, tzinfo=timezone.utc)          # a Sunday
    assert due(Job("i", lambda: 0, every_s=900), now)
    _mark("i", now.timestamp())
    assert not due(Job("i", lambda: 0, every_s=900), now + timedelta(minutes=10))
    assert due(Job("i", lambda: 0, every_s=900), now + timedelta(minutes=15))
    nightly = Job("n", lambda: 0, at=(21, 30))
    assert due(nightly, now)
    _mark("n", now.timestamp())
    assert not due(nightly, now + timedelta(hours=1))
    assert due(nightly, now + timedelta(days=1))
    weekly = Job("w", lambda: 0, at=(21, 30), weekday=6)
    assert due(weekly, now) and not due(weekly, now + timedelta(days=1))


def test_lock_prevents_double_run(redis_clean):
    from app.scheduler import Job, run_job
    calls = []
    job = Job("locked", lambda: calls.append(1), every_s=60)
    redis_clean.set("sched:lock:locked", "1", ex=60)               # another scheduler is running it
    assert run_job(job) is False and calls == []
    redis_clean.delete("sched:lock:locked")
    assert run_job(job) is True and calls == [1]
    assert redis_clean.get("sched:lock:locked") is None             # released


def test_failing_job_is_contained():
    from app.scheduler import Job, _last, run_job
    def boom():
        raise RuntimeError("x")
    assert run_job(Job("boom", boom, every_s=60)) is True
    assert _last("boom") > 0                                         # marked, so it doesn't hot-loop


def test_mail_job_end_to_end(module_db, monkeypatch):
    """Scheduler's mail job: processes the mailbox, feeds parsed job alerts
    into the pipeline (Tier C, manual-only), sends reminders."""
    from app import db, scheduler
    from app.mail import accounts
    from app.mail.accounts import save_credential
    uid = seed_user(module_db, settings={"auto_apply": True})
    with db.worker_session() as s:
        save_credential(s, uid, "gmail", "me@gmail.com", {"client_id": "a", "client_secret": "b", "refresh_token": "c"})
    box = FakeMailbox()
    html = ('<a href="https://www.linkedin.com/comm/jobs/view/4011112222/">Software Development Engineer I</a>'
            "<p>Northwind Payments · Bengaluru, India</p>")
    box.add(make_msg("jobalerts-noreply@linkedin.com", "1 new job for software engineer", html=html))
    monkeypatch.setattr(accounts, "mail_client", lambda s, u, transport=None: box)
    sent = []
    from app.notify import telegram
    monkeypatch.setattr(telegram, "get_notifier", lambda: (lambda u, t, buttons=None: sent.append(t)))
    out = scheduler.job_mail()
    assert out["users"] == 1 and out["alert_jobs"] == 1 and out["trashed"] == 1
    with module_db.connect() as c:
        job = c.execute(text("SELECT title, company_name FROM jobs WHERE user_id = :u"), {"u": uid}).first()
        app_ = c.execute(text("SELECT status, mode FROM applications WHERE user_id = :u"), {"u": uid}).first()
    assert job.company_name == "Northwind Payments"
    assert app_ is None or (app_.status, app_.mode) == ("waiting", "manual")   # LinkedIn: never automated (C12)
    # deadline reminder
    with module_db.begin() as c:
        c.execute(text("""INSERT INTO responses (user_id, class, priority, deadline, gmail_id, received_at, keep_until)
                          VALUES (:u, 'test', 1, now() + interval '5 hours', 'gx', now(), now() + interval '7 days')"""),
                  {"u": uid})
    scheduler.job_mail()
    assert any(t.startswith("Reminder: test") for t in sent)
    n = len(sent)
    scheduler.job_mail()
    assert len([t for t in sent[n:] if t.startswith("Reminder")]) == 0  # reminded once


def test_digest_at_users_hour(module_db, monkeypatch):
    from app import scheduler
    from app.notify import telegram
    uid = seed_user(module_db, settings={"digest_hour": 8})
    with module_db.begin() as c:
        c.execute(text("UPDATE users SET telegram_chat_id = 4242, timezone = 'Asia/Kolkata' WHERE id = :u"), {"u": uid})
    sent = []
    monkeypatch.setattr(telegram, "get_notifier", lambda: (lambda u, t, buttons=None: sent.append((u, t))))
    seven_ist = datetime(2026, 10, 19, 1, 45, tzinfo=timezone.utc)
    eight_ist = datetime(2026, 10, 19, 2, 40, tzinfo=timezone.utc)
    assert scheduler.job_digest(seven_ist)["sent"] == 0
    assert scheduler.job_digest(eight_ist)["sent"] == 1
    assert scheduler.job_digest(eight_ist + timedelta(minutes=10))["sent"] == 0      # once per day
    assert "JobBot daily digest" in [t for u, t in sent if str(u) == str(uid)][0]


def test_hourly_and_nightly_jobs_run(module_db):
    from app import scheduler
    hourly = scheduler.job_hourly()
    assert hourly["state"] in ("ok", "tight", "pause") and "waiting_list" in hourly["deleted"]
    nightly = scheduler.job_nightly()
    assert nightly["state"] == "ok" and set(nightly["partitions"]) == {"jobs", "seen_jobs", "jd_signatures"}


def test_cli_run_and_seed(module_db, tmp_path, capsys):
    from app.cli import main
    csv = tmp_path / "companies.csv"
    csv.write_text("name,careers_url\nSeeded Co,https://boards.greenhouse.io/seededco\nOther Co,\n")
    assert main(["seed-companies", str(csv)]) == 0
    assert "seeded 2 companies" in capsys.readouterr().out
    with module_db.connect() as c:
        row = c.execute(text("SELECT ats_type, ats_slug, pinned FROM companies WHERE name = 'Seeded Co'")).first()
    assert tuple(row) == ("greenhouse", "seededco", True)
    assert main(["run", "hourly"]) == 0
    assert '"state"' in capsys.readouterr().out
