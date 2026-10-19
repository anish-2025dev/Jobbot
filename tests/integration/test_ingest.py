"""P1 end to end on real Postgres: registry -> fetch -> normalize -> dedupe ->
filter -> score -> store, plus registry admission/eviction rules."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.ingest import registry
from app.ingest.http import make_fetcher
from app.ingest.models import RawJob
from app.ingest.runner import run_round
from app.ratelimit import RateLimiter, UnlimitedBuckets
from tests.fakes.ats import FakeUniverse
from tests.integration.conftest import seed_user

pytestmark = pytest.mark.integration
NOW = datetime.now(timezone.utc)


def fetcher(uni):
    return make_fetcher(uni.transport(), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)


def add_registry(eng, name, ats, slug, host=None, **kw):
    from app import db
    with db.worker_session() as s:
        cid = registry.admit(s, registry.CompanyRef(name=name, ats=ats, slug=slug, host=host))
        for k, v in kw.items():
            s.execute(text(f"UPDATE companies SET {k} = :v WHERE id = :id"), {"v": v, "id": cid})
    return cid


def jobs_for(eng, uid):
    with eng.connect() as c:
        return c.execute(text("SELECT * FROM jobs WHERE user_id = :u ORDER BY bucket DESC, score DESC"),
                         {"u": uid}).mappings().all()


@pytest.fixture(scope="module")
def world(module_db):
    uni = FakeUniverse(seed=11, now=NOW)
    uni.add_company("Northwind Payments", "greenhouse", "northwind", n_jobs=0)
    nw = uni.companies[("greenhouse", "northwind")]
    nw.jobs = [uni.make_job(title="Software Development Engineer I", location="Bengaluru, India", junior=True,
                            stack=["Python", "FastAPI", "PostgreSQL", "Redis", "Docker"], age_hours=5,
                            batch="2026/2027"),
               uni.make_job(title="Senior Software Engineer", location="Bengaluru, India", junior=False),
               uni.make_job(title="Account Executive", location="Pune, India")]
    uni.add_company("Lever Labs", "lever", "leverlabs", n_jobs=4, junior=True, location="Pune, India")
    uni.add_company("Ashby Co", "ashby", "ashbyco", n_jobs=0)
    uni.companies[("ashby", "ashbyco")].jobs = [
        uni.make_job(title="SDE 1 (Python)", location="London, UK", junior=True),
        uni.make_job(title="Account Executive", location="London, UK")]
    uni.add_company("WD Corp", "workday", "wdcorp", n_jobs=3, junior=True, location="Hyderabad, India")
    add_registry(module_db, "Northwind Payments", "greenhouse", "northwind")
    add_registry(module_db, "Lever Labs", "lever", "leverlabs")
    add_registry(module_db, "Ashby Co", "ashby", "ashbyco")
    add_registry(module_db, "WD Corp", "workday", "wdcorp/careers", host="wdcorp.wd5.myworkdayjobs.com")
    uid = seed_user(module_db)
    return uni, uid


def test_first_round_ranks_and_filters(module_db, world):
    uni, uid = world
    res = run_round(fetcher(uni), tier_b=False, user_ids=[uid], now=NOW)
    assert res.skipped_reason is None and res.companies_polled == 4, res
    stats = res.per_user[str(uid)]
    # Off-target titles (Account Executive, Data Engineer for an SDE profile)
    # are dropped at fetch time, before any detail calls; on-target ones
    # reach the hard filters.
    assert stats["filtered"].get("location", 0) >= 1            # London SDE
    assert stats["filtered"].get("experience level", 0) >= 1    # Senior SWE
    rows = jobs_for(module_db, uid)
    assert all(r["title"] != "Account Executive" for r in rows)
    assert rows, stats
    top = next(r for r in rows if r["company_name"] == "Northwind Payments")
    assert top["bucket"] == 3, dict(top)
    # sorted bucket desc, then score: every High job precedes every Medium one
    buckets = [r["bucket"] for r in rows]
    assert buckets == sorted(buckets, reverse=True)
    assert top["title"] == "Software Development Engineer I"
    assert top["grad_years"] == [2026, 2027] and top["ats"] == "greenhouse"
    assert len(top["summary"]) <= 600 and len(top["components"]) == 6
    assert all(r["title"] != "Senior Software Engineer" for r in rows)
    # C3: summary only (capped), no boilerplate, nowhere near the full JD
    for r in rows:
        assert len(r["summary"]) <= 600 and "equal opportunity" not in r["summary"].lower()


def test_second_round_is_deduped(module_db, world):
    uni, uid = world
    before = len(jobs_for(module_db, uid))
    with module_db.begin() as c:                     # make every company due again
        c.execute(text("UPDATE companies SET last_polled_at = NULL"))
    res = run_round(fetcher(uni), tier_b=False, user_ids=[uid], now=NOW + timedelta(hours=3))
    stats = res.per_user[str(uid)]
    assert len(jobs_for(module_db, uid)) == before
    assert sum(stats["stored"].values()) == 0
    assert res.pre_skipped + stats["seen_before"] >= before     # skipped before normalization


def test_applied_opening_never_resurfaces(module_db, world):
    """C4 across portals: applied on Greenhouse -> a JSearch listing linking
    to the same posting is skipped, even after seen_jobs would expire."""
    uni, uid = world
    top = next(r for r in jobs_for(module_db, uid) if r["company_name"] == "Northwind Payments")
    with module_db.begin() as c:
        c.execute(text("""INSERT INTO applied_ledger (user_id, canonical_hash, fingerprint, company_key_hash)
                          VALUES (:u, :c, :f, :k)"""),
                  {"u": uid, "c": top["canonical_hash"], "f": top["fingerprint"], "k": top["company_key_hash"]})
        c.execute(text("DELETE FROM seen_jobs WHERE user_id = :u"), {"u": uid})
    nw = uni.companies[("greenhouse", "northwind")].jobs[0]
    agg = RawJob(source="jsearch", ats="greenhouse", company="Northwind Payments Pvt Ltd",
                 company_slug="northwind", title=nw.title, req_id=nw.req,
                 url=f"https://boards.greenhouse.io/northwind/jobs/{nw.id}", description=nw.html,
                 location="Bangalore, India", posted_at=NOW)
    res = run_round(fetcher(uni), tier_a=False, tier_b=False, user_ids=[uid], now=NOW, extra_raws=[agg])
    assert res.per_user[str(uid)]["applied_before"] == 1


def test_repost_is_skipped(module_db, world):
    uni, uid = world
    nw = uni.companies[("greenhouse", "northwind")].jobs[0]
    repost = RawJob(source="greenhouse", ats="greenhouse", company="Northwind Payments", company_slug="northwind",
                    title=nw.title + " - Platform", req_id="R-REPOST-1",
                    url="https://boards.greenhouse.io/northwind/jobs/999999", description=nw.html,
                    location="Bengaluru, India", posted_at=NOW)
    res = run_round(fetcher(uni), tier_a=False, tier_b=False, user_ids=[uid], now=NOW, extra_raws=[repost])
    assert res.per_user[str(uid)]["reposts"] == 1


def test_two_users_both_get_the_same_new_job(module_db, world):
    """Signatures are global; a second user must not see the first user's
    copy of the same opening as a repost."""
    uni, _ = world
    u2 = seed_user(module_db)
    j = uni.make_job(title="SDE 1 (Python)", location="Pune, India", junior=True,
                     stack=["Python", "FastAPI", "PostgreSQL", "Redis", "Docker"], age_hours=2)
    uni.companies[("lever", "leverlabs")].jobs.append(j)
    u3 = seed_user(module_db)
    with module_db.begin() as c:
        c.execute(text("UPDATE companies SET last_polled_at = NULL"))
    res = run_round(fetcher(uni), tier_b=False, user_ids=[u2, u3], now=NOW + timedelta(hours=4))
    titles2 = {r["title"] for r in jobs_for(module_db, u2)}
    titles3 = {r["title"] for r in jobs_for(module_db, u3)}
    assert "SDE 1 (Python)" in titles2 and "SDE 1 (Python)" in titles3, res.per_user


def test_slug_404_marks_company_for_reprobe(module_db, world):
    uni, uid = world
    cid = add_registry(module_db, "Gone Inc", "greenhouse", "gone-inc")
    run_round(fetcher(uni), tier_b=False, user_ids=[uid], now=NOW + timedelta(hours=6))
    with module_db.connect() as c:
        ats = c.execute(text("SELECT ats_type FROM companies WHERE id = :i"), {"i": cid}).scalar()
    assert ats == "unknown"


def test_source_paused_after_five_failures(module_db, world):
    from app import db
    from app.ingest.http import record_source_result, source_active
    with db.worker_session() as s:
        for _ in range(5):
            record_source_result(s, "ashby", False, "boom")
        assert not source_active(s, "ashby")
        s.execute(text("UPDATE sources SET paused_until = now() - interval '1 minute' WHERE key = 'ashby'"))
        assert source_active(s, "ashby")               # auto-resumes after 24 h


def test_aggregator_discovers_company(module_db, world):
    uni, uid = world
    uni.add_company("Fresh Startup", "greenhouse", "freshstartup", n_jobs=2, junior=True, location="Pune, India")
    agg = RawJob(source="jsearch", ats="greenhouse", company="Fresh Startup", company_slug="freshstartup",
                 title="Backend Engineer - New Grad", req_id="77",
                 url="https://boards.greenhouse.io/freshstartup/jobs/77", description="Python APIs",
                 location="Pune, India")
    res = run_round(fetcher(uni), tier_a=False, tier_b=False, user_ids=[uid], now=NOW, extra_raws=[agg])
    assert res.companies_admitted == 1
    from app import db
    with db.worker_session() as s:
        assert registry.find(s, "Fresh Startup")["ats_slug"] == "freshstartup"


def test_router_called_for_rankable_jobs(module_db, world):
    uni, _ = world
    u = seed_user(module_db)
    calls = []
    with module_db.begin() as c:
        c.execute(text("UPDATE companies SET last_polled_at = NULL"))
    run_round(fetcher(uni), tier_b=False, user_ids=[u], now=NOW + timedelta(hours=8),
              router=lambda s, p, j, sc, jid: calls.append((j.title, sc.bucket)))
    assert calls and all(b >= 1 for _, b in calls)


# --- registry rules ------------------------------------------------------------------
def test_registry_admission_and_eviction(module_db):
    from app import db
    with db.worker_session() as s:
        base = registry.count(s)
        cap = base + 2
        s.execute(text("UPDATE companies SET added_at = now() - interval '30 days'"))
        registry.admit(s, registry.CompanyRef(name="Evict Me A", ats="lever", slug="a"), cap)
        b = registry.admit(s, registry.CompanyRef(name="Evict Me B", ats="lever", slug="b"), cap)
        s.execute(text("UPDATE companies SET added_at = now() - interval '30 days', eviction_score = 0"))
        s.execute(text("UPDATE companies SET eviction_score = 5 WHERE id = :b"), {"b": b})
        # full: newcomer with 0 open jobs is rejected; with 3 it evicts the weakest evictable
        assert registry.admit(s, registry.CompanyRef(name="Newcomer", open_jobs=0, ats="lever", slug="n"), cap) is None
        new = registry.admit(s, registry.CompanyRef(name="Newcomer", open_jobs=3, ats="lever", slug="n"), cap)
        assert new is not None and registry.count(s) == cap


def test_protected_companies_never_evicted(module_db):
    from app import db
    uid = seed_user(module_db)
    with db.worker_session() as s:
        s.execute(text("UPDATE companies SET added_at = now() - interval '30 days', eviction_score = 0, pinned = false"))
        keep = registry.admit(s, registry.CompanyRef(name="Has Contact Co", ats="lever", slug="hc"))
        s.execute(text("UPDATE companies SET added_at = now() - interval '30 days' WHERE id = :i"), {"i": keep})
        key = s.execute(text("SELECT company_key_hash FROM companies WHERE id = :i"), {"i": keep}).scalar()
        s.execute(text("""INSERT INTO outreach_contacts (user_id, company_key_hash, name, title, role_type, email,
                          provider, verified_at) VALUES (:u, :k, 'P', 'SDE', 'peer', 'p@hc.dev', 'hunter', now())"""),
                  {"u": uid, "k": key})
        evicted = registry.evict_to_capacity(s, 1)
        assert evicted >= 1
        assert s.execute(text("SELECT 1 FROM companies WHERE id = :i"), {"i": keep}).scalar() == 1


def test_eviction_never_touches_cooldown(module_db):
    from app import db
    uid = seed_user(module_db)
    with db.worker_session() as s:
        cid = registry.admit(s, registry.CompanyRef(name="Cooldown Co", ats="lever", slug="cc"))
        key = s.execute(text("SELECT company_key_hash FROM companies WHERE id = :i"), {"i": cid}).scalar()
        s.execute(text("""INSERT INTO company_cooldown (user_id, company_key_hash, applied_ats, last_applied_at)
                          VALUES (:u, :k, ARRAY[now(), now()], now())"""), {"u": uid, "k": key})
        s.execute(text("DELETE FROM companies WHERE id = :i"), {"i": cid})
        assert s.execute(text("SELECT cardinality(applied_ats) FROM company_cooldown WHERE company_key_hash = :k"),
                         {"k": key}).scalar() == 2


def test_eviction_score_decay_and_bump(module_db):
    from app import db
    with db.worker_session() as s:
        cid = registry.admit(s, registry.CompanyRef(name="Decay Co", ats="lever", slug="dc"))
        key = s.execute(text("SELECT company_key_hash FROM companies WHERE id = :i"), {"i": cid}).scalar()
        s.execute(text("""UPDATE companies SET eviction_score = 2.0, score_updated_at = now() - interval '30 days'
                          WHERE id = :i"""), {"i": cid})
        registry.bump_on_application(s, key)
        score = s.execute(text("SELECT eviction_score FROM companies WHERE id = :i"), {"i": cid}).scalar()
    assert score == pytest.approx(2.0 * 0.36788 + 1, abs=0.01)     # 2*e^-1 + 1


def test_pause_threshold_skips_ingestion(module_db, world, monkeypatch):
    import app.config
    uni, uid = world
    monkeypatch.setenv("DB_BUDGET_MB", "1")
    app.config.get_settings.cache_clear()
    try:
        res = run_round(fetcher(uni), user_ids=[uid])
        assert res.skipped_reason and "C1" in res.skipped_reason and res.fetched == 0
    finally:
        monkeypatch.delenv("DB_BUDGET_MB")
        app.config.get_settings.cache_clear()
