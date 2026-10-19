from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.integration.conftest import seed_user

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]


def mk_app(eng, uid, n, bucket=2, salary=None, status="waiting", company=None, posted_hours=1):
    with eng.begin() as c:
        return c.execute(text("""
            INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title,
                apply_url, status, bucket, salary_max, posted_at, score)
            VALUES (:u, :c, :c, :k, :cn, 'SDE', 'https://x/' || :c, CAST(:st AS application_status), :b, :s,
                    now() - make_interval(hours => :ph), 0.7) RETURNING id"""),
            {"u": uid, "c": n, "k": 1000 + n if company is None else company, "cn": f"Co{n}", "st": status,
             "b": bucket, "s": salary, "ph": posted_hours}).scalar_one()


def waiting_ids(eng, uid):
    with eng.connect() as c:
        return set(c.execute(text("SELECT application_id FROM waiting_list WHERE user_id = :u"), {"u": uid}).scalars())


def test_waiting_list_cap_and_displacement(module_db, monkeypatch):
    import app.config
    from app import db
    from app.apply.waiting import add_to_waiting
    monkeypatch.setenv("WAITING_LIST_MAX", "5")
    app.config.get_settings.cache_clear()
    uid = seed_user(module_db)
    ids = [mk_app(module_db, uid, 10_000 + i, bucket=2, salary=1_000_000 + i) for i in range(5)]
    with db.worker_session() as s:
        for a in ids:
            assert add_to_waiting(s, a)
    assert waiting_ids(module_db, uid) == set(ids)
    low = mk_app(module_db, uid, 10_100, bucket=1)                 # outranked by everything
    high = mk_app(module_db, uid, 10_101, bucket=3)                # outranks the lowest
    notified = []
    with db.worker_session() as s:
        assert not add_to_waiting(s, low)
        assert add_to_waiting(s, high, notify=lambda u, t, buttons=None: notified.append(t))
    w = waiting_ids(module_db, uid)
    assert high in w and low not in w and ids[0] not in w and len(w) == 5   # lowest (smallest salary) displaced
    assert notified and "High-match" in notified[0]                       # High bucket push
    with module_db.connect() as c:                                         # displaced/rejected rows removed
        assert c.execute(text("SELECT count(*) FROM applications WHERE id = ANY(:i)"), {"i": [low, ids[0]]}).scalar() == 0
        exp = c.execute(text("SELECT expires_at - entered_at FROM waiting_list WHERE application_id = :a"), {"a": high}).scalar()
        assert exp == timedelta(hours=24)
    app.config.get_settings.cache_clear()


def test_pin_listing_and_manual_apply(module_db):
    from app import db
    from app.apply.waiting import add_to_waiting, listing, mark_applied, pin
    uid = seed_user(module_db)
    a = mk_app(module_db, uid, 20_001, company=777)
    b = mk_app(module_db, uid, 20_002, company=777)
    c_ = mk_app(module_db, uid, 20_003, company=888)
    with db.worker_session() as s:
        for x in (a, b, c_):
            add_to_waiting(s, x)
        before = s.execute(text("SELECT expires_at FROM waiting_list WHERE application_id = :a"), {"a": a}).scalar()
        assert pin(s, uid, a) == before + timedelta(hours=24)
        assert mark_applied(s, uid, a).ok
        rows = {r["id"]: r for r in listing(s, uid)}
        assert a not in rows                                     # applied -> gone
        assert rows[b]["ready_at"] is not None                   # same company: 3 h gap shown as "ready at"
        assert rows[c_]["ready_at"] is None
        res = mark_applied(s, uid, b)
        assert not res.ok and "C6" in res.reason
        other = seed_user(module_db)
        assert not mark_applied(s, other, c_).ok                 # another user can't act on it
    with module_db.connect() as conn:
        assert conn.execute(text("SELECT via FROM applied_ledger WHERE user_id = :u"), {"u": uid}).scalar() == 2


def test_listing_hides_entries_that_break_caps(module_db):
    from app import db
    from app.apply.waiting import add_to_waiting, listing
    uid = seed_user(module_db)
    a = mk_app(module_db, uid, 30_001, company=999)
    with db.worker_session() as s:
        add_to_waiting(s, a)
        s.execute(text("""INSERT INTO company_cooldown (user_id, company_key_hash, applied_ats, last_applied_at)
                          VALUES (:u, 999, ARRAY[now() - interval '5 days', now() - interval '4 days', now() - interval '3 days'],
                                  now() - interval '3 days')"""), {"u": uid})
        assert [r["id"] for r in listing(s, uid)] == []          # C5 full: hidden


def test_router_lean_mode_vs_auto_apply(module_db):
    """Lean-first: auto_apply off -> ranked list only. On -> applications with
    a stored recipe; Easy Apply-only jobs -> waiting list."""
    from app.apply.routing import make_router
    from app.ingest.http import make_fetcher
    from app.ingest.models import RawJob
    from app.ingest.runner import run_round
    from app.ratelimit import RateLimiter, UnlimitedBuckets
    jd = (ROOT / "examples/jd.example.txt").read_text()
    now = datetime.now(timezone.utc)

    def raws(tag):
        return [RawJob(source="greenhouse", ats="greenhouse", company=f"Router Co {tag}", company_slug="rc",
                       title="Software Development Engineer I", req_id=f"R-{tag}-1",
                       url=f"https://boards.greenhouse.io/rc{tag}/jobs/1", description=jd, description_is_html=False,
                       location="Bengaluru, India", posted_at=now),
                RawJob(source="jsearch", ats="unknown", company=f"Easy Co {tag}", title="Backend Engineer - New Grad",
                       req_id=f"li-{tag}", url=f"https://www.linkedin.com/jobs/view/40{tag}99/", description=jd,
                       description_is_html=False, location="Pune, India", posted_at=now, easy_apply_only=True)]

    f = make_fetcher(None, RateLimiter(UnlimitedBuckets()))
    lean = seed_user(module_db)
    run_round(f, tier_a=False, tier_b=False, user_ids=[lean], extra_raws=raws("1"), router=make_router())
    auto = seed_user(module_db, settings={"auto_apply": True})
    run_round(f, tier_a=False, tier_b=False, user_ids=[auto], extra_raws=raws("2"), router=make_router())
    with module_db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM applications WHERE user_id = :u"), {"u": lean}).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM jobs WHERE user_id = :u"), {"u": lean}).scalar() == 2
        apps = {r.company_name: r for r in c.execute(text("""SELECT company_name, status, mode, recipe, vault_revision
                                                             FROM applications WHERE user_id = :u"""), {"u": auto})}
        gh = apps["Router Co 2"]
        assert gh.status == "queued" and gh.recipe["projects"] and gh.vault_revision == 1
        easy = apps["Easy Co 2"]
        assert easy.status == "waiting" and easy.mode == "manual"
        assert c.execute(text("""SELECT count(*) FROM waiting_list w JOIN applications a ON a.id = w.application_id
                                 WHERE a.user_id = :u"""), {"u": auto}).scalar() == 1


def test_worker_picks_by_rank_and_respects_gap(module_db, monkeypatch):
    from app import db
    from app.apply import timing, worker
    monkeypatch.setattr(timing, "in_daytime", lambda tz, now=None: True)
    with module_db.begin() as c:     # isolate from queued rows left by other tests in this module DB
        c.execute(text("UPDATE applications SET status = 'closed' WHERE status = 'queued'"))
    uid = seed_user(module_db, settings={"auto_apply": True})
    with module_db.begin() as c:
        for n, bucket, nb in ((40_001, 2, None), (40_002, 3, None), (40_003, 3, "now() + interval '2 hours'")):
            c.execute(text(f"""INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name,
                    title, apply_url, status, bucket, recipe, not_before)
                VALUES (:u, :n, :n, :n, 'W', 'SDE', 'https://boards.greenhouse.io/w/jobs/' || :n, 'queued', :b, '{{}}',
                        {nb or 'NULL'})"""), {"u": uid, "n": n, "b": bucket})
    with db.worker_session() as s:
        first = worker.next_application(s)
    assert first["id"] and first["user_id"] == uid
    with module_db.connect() as c:
        assert c.execute(text("SELECT canonical_hash FROM applications WHERE id = :i"), {"i": first["id"]}).scalar() == 40_002
    paused = seed_user(module_db, settings={"auto_apply": True, "paused": True})
    with module_db.begin() as c:
        c.execute(text("""INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name,
                title, apply_url, status, bucket, recipe) VALUES (:u, 1, 1, 1, 'P', 'SDE', 'https://x', 'queued', 3, '{}')"""),
                  {"u": paused})
    with db.worker_session() as s:
        picked = [worker.next_application(s) for _ in range(3)]
    assert all(p is None or p["user_id"] != paused for p in picked)
