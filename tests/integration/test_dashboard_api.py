"""P6 + dashboard API: accounts, token isolation (app and RLS), ranked list
with manual apply through the gate, waiting list, positives, referral
drafts, settings, write-only credentials, onboarding import, tuning."""
import json
from pathlib import Path

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]
VAULT = json.loads((ROOT / "examples/vault.example.json").read_text())
JD = (ROOT / "examples/jd.example.txt").read_text()
NOKEY = {"X-API-Key": ""}


@pytest.fixture(autouse=True)
def fresh_limits():
    from app.ratelimit import get_limiter
    from app.redis_client import get_redis
    get_redis.cache_clear()
    get_limiter.cache_clear()
    r = get_redis()
    if r:
        r.flushdb()


def signup(api, email, password="correct horse battery"):
    r = api.post("/auth/signup", json={"email": email, "password": password}, headers=NOKEY)
    assert r.status_code == 201, r.text
    d = r.json()
    return d["user_id"], {**NOKEY, "Authorization": f"Bearer {d['token']}"}


def with_vault(api, email):
    uid, h = signup(api, email)
    assert api.put(f"/users/{uid}/vault", json=VAULT, headers=h).status_code == 201
    return uid, h


# --- accounts -----------------------------------------------------------------------
def test_signup_login_me_and_password_change(api):
    uid, h = signup(api, "anish.test@example.com")
    assert api.get("/me", headers=h).json()["email"] == "anish.test@example.com"
    assert api.post("/auth/signup", json={"email": "ANISH.test@example.com", "password": "another long one"},
                    headers=NOKEY).status_code == 409
    bad = api.post("/auth/login", json={"email": "anish.test@example.com", "password": "wrong password!"}, headers=NOKEY)
    assert bad.status_code == 401
    ok = api.post("/auth/login", json={"email": "anish.test@example.com", "password": "correct horse battery"}, headers=NOKEY)
    assert ok.status_code == 200
    # password change revokes every older token
    r = api.post("/auth/password", json={"current_password": "correct horse battery", "new_password": "a brand new secret"},
                 headers=h)
    assert r.status_code == 200
    assert api.get("/me", headers=h).status_code == 401
    assert api.get("/me", headers={**NOKEY, "Authorization": f"Bearer {r.json()['token']}"}).status_code == 200


def test_weak_password_rejected(api):
    r = api.post("/auth/signup", json={"email": "weak@example.com", "password": "short"}, headers=NOKEY)
    assert r.status_code == 422 and "10 characters" in json.dumps(r.json())


def test_login_is_rate_limited(api):
    signup(api, "rl@example.com")
    codes = [api.post("/auth/login", json={"email": "rl@example.com", "password": "nope nope nope"}, headers=NOKEY).status_code
             for _ in range(12)]
    assert codes.count(429) >= 1 and codes[0] == 401


def test_tampered_or_missing_token(api):
    uid, h = signup(api, "tamper@example.com")
    tok = h["Authorization"].split()[1]
    forged = tok[:-2] + ("A" if tok[-2] != "A" else "B") + tok[-1]
    assert api.get("/me", headers={**NOKEY, "Authorization": f"Bearer {forged}"}).status_code == 401
    assert api.get(f"/users/{uid}/vault", headers=NOKEY).status_code == 401


def test_users_cannot_reach_each_other(api):
    a, ha = with_vault(api, "alice@example.com")
    b, hb = with_vault(api, "bob@example.com")
    for path in ("/vault", "/jobs", "/waiting", "/applications", "/settings", "/credentials", "/summary"):
        assert api.get(f"/users/{b}{path}", headers=ha).status_code == 404, path
    assert api.put(f"/users/{b}/vault", json=VAULT, headers=ha).status_code == 404
    assert api.post("/users", json={"email": "x@example.com"}, headers=ha).status_code == 401   # admin only
    assert api.get(f"/users/{a}/vault", headers=ha).status_code == 200


def test_rls_enforced_under_api_role(api, engine):
    """Even a query that forgets the WHERE clause only sees the caller's rows."""
    from app import db
    a, _ = with_vault(api, "rls-a@example.com")
    b, _ = with_vault(api, "rls-b@example.com")
    with db.user_session(a) as s:
        rows = s.execute(text("SELECT user_id FROM vaults")).scalars().all()
        role = s.execute(text("SELECT current_user")).scalar()
    assert role == "jobbot_api" and set(map(str, rows)) == {a}


def test_delete_account_cascades(api, engine):
    uid, h = with_vault(api, "delete-me@example.com")
    assert api.delete(f"/users/{uid}", headers=h).json() == {"deleted": True}
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM vaults WHERE user_id = :u"), {"u": uid}).scalar() == 0


# --- settings / credentials -----------------------------------------------------------
def test_settings_patch_and_outreach_review_week(api):
    uid, h = signup(api, "settings@example.com")
    r = api.patch(f"/users/{uid}/settings", json={"auto_apply": True, "outreach": True, "min_score": 0.7,
                                                   "timezone": "Asia/Kolkata"}, headers=h).json()
    assert r["auto_apply"] and r["min_score"] == 0.7 and r["outreach_enabled_at"]
    assert api.patch(f"/users/{uid}/settings", json={"timezone": "Mars/Base"}, headers=h).status_code == 422
    assert api.patch(f"/users/{uid}/settings", json={"min_score": 0.2}, headers=h).status_code == 422


def test_credentials_are_write_only_and_encrypted(api, engine):
    uid, h = signup(api, "creds@example.com")
    assert api.put(f"/users/{uid}/credentials/hunter", json={"secret": {"api_key": "hk-supersecret"}}, headers=h).status_code == 200
    assert api.put(f"/users/{uid}/credentials/imap", json={"username": "nope", "secret": {"password": "x"}}, headers=h).status_code == 422
    listed = api.get(f"/users/{uid}/credentials", headers=h).json()
    assert [c["host"] for c in listed] == ["hunter"] and "hk-supersecret" not in json.dumps(listed)
    with engine.connect() as c:
        blob = bytes(c.execute(text("SELECT secret_enc FROM credentials WHERE user_id = :u"), {"u": uid}).scalar())
    assert b"hk-supersecret" not in blob


def test_telegram_link_and_gmail_connect(api):
    uid, h = signup(api, "links@example.com")
    code = api.post(f"/users/{uid}/telegram/link", headers=h).json()["code"]
    assert len(code) == 8
    assert api.get(f"/users/{uid}/gmail/connect", headers=h).status_code == 409
    api.put(f"/users/{uid}/credentials/gmail_client", json={"secret": {"client_id": "cid.apps", "client_secret": "s"}}, headers=h)
    url = api.get(f"/users/{uid}/gmail/connect", headers=h).json()["url"]
    assert url.startswith("https://accounts.google.com/") and "gmail.send" in url and "state=" in url
    assert api.get("/oauth/google/callback?state=forged&code=x", headers=NOKEY).status_code == 400


# --- ranked list, manual apply ----------------------------------------------------------
def seed_jobs(engine, uid, n=3, company="Northwind Payments"):
    from app.util.hashing import company_key, h64
    ids = []
    with engine.begin() as c:
        c.execute(text("SELECT jobbot_ensure_daily_partitions('jobs', current_date, current_date)"))
        src = c.execute(text("SELECT id FROM sources WHERE key = 'greenhouse'")).scalar()
        for i in range(n):
            ids.append(c.execute(text("""
                INSERT INTO jobs (user_id, canonical_hash, fingerprint, company_key_hash, company_name, source_id, title,
                    location, skills, summary, url, posted_at, score, bucket, salary_max, ats, components, missing_skills)
                VALUES (:u, :c, :c, :k, :cn, :src, :t, 'Bengaluru, India', '{python,fastapi,postgresql,redis,docker}',
                    'Build payment APIs with Python, FastAPI and PostgreSQL.', :url, now() - interval '3 hours',
                    :score, :b, :sal, 'greenhouse', '{900,1000,1000,900,300,500}', :miss) RETURNING id"""),
                {"u": uid, "c": 7_000_000 + i + abs(hash(uid)) % 10**6, "k": h64(company_key(company)), "cn": company,
                 "src": src, "t": ["SDE I", "Backend Engineer", "Platform Engineer"][i % 3],
                 "url": f"https://boards.greenhouse.io/nw/jobs/{i}", "score": [0.86, 0.71, 0.55][i % 3],
                 "b": [3, 2, 0][i % 3], "sal": [1800000, None, 1200000][i % 3],
                 "miss": [[], [], ["kubernetes", "go"]][i % 3]}).scalar_one())
    return ids


def test_ranked_list_sort_filter_and_manual_apply(api, engine):
    uid, h = with_vault(api, "ranked@example.com")
    ids = seed_jobs(engine, uid)
    data = api.get(f"/users/{uid}/jobs", headers=h).json()
    assert [j["bucket"] for j in data["items"]] == [3, 2, 0]
    assert data["items"][2]["prep_days"] == 10 and data["counts"]["high"] == 1
    assert len(api.get(f"/users/{uid}/jobs?bucket=stretch", headers=h).json()["items"]) == 1
    pdf = api.get(f"/users/{uid}/jobs/{ids[0]}/resume.pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert api.post(f"/users/{uid}/jobs/{ids[0]}/applied", headers=h).json()["applied"] is True
    # gone from the ranked list (C4), and the 3-hour company gap now blocks the next one (C6)
    assert ids[0] not in [j["id"] for j in api.get(f"/users/{uid}/jobs", headers=h).json()["items"]]
    r = api.post(f"/users/{uid}/jobs/{ids[1]}/applied", headers=h)
    assert r.status_code == 409 and "C6" in r.json()["detail"]["reason"] and r.json()["detail"]["ready_at"]
    apps = api.get(f"/users/{uid}/applications?status=applied", headers=h).json()
    assert len(apps) == 1
    resume = api.get(f"/users/{uid}/applications/{apps[0]['id']}/resume.docx", headers=h)
    assert resume.status_code == 200 and resume.content[:2] == b"PK"


def test_queue_for_auto_apply(api, engine):
    uid, h = with_vault(api, "queue@example.com")
    ids = seed_jobs(engine, uid, n=1, company="Queue Co")
    aid = api.post(f"/users/{uid}/jobs/{ids[0]}/queue", headers=h).json()["application_id"]
    with engine.connect() as c:
        row = c.execute(text("SELECT status, recipe IS NOT NULL AS has_recipe FROM applications WHERE id = :a"), {"a": aid}).first()
    assert row.status == "queued" and row.has_recipe


# --- waiting list, positives, referrals -------------------------------------------------
def test_waiting_list_answers_pin_dismiss(api, engine):
    from app import db
    from app.apply.waiting import add_to_waiting
    uid, h = with_vault(api, "waiting@example.com")
    ids = seed_jobs(engine, uid, n=2, company="Waiting Co")
    a1 = api.post(f"/users/{uid}/jobs/{ids[0]}/queue", headers=h).json()["application_id"]
    a2 = api.post(f"/users/{uid}/jobs/{ids[1]}/queue", headers=h).json()["application_id"]
    with db.worker_session() as s:
        for a in (a1, a2):
            s.execute(text("UPDATE applications SET status = 'waiting', failure_reason = 'Needs your answer' WHERE id = :a"), {"a": a})
        add_to_waiting(s, a1, extra={"unresolved_questions": ["Why do you want to join Waiting Co?"]})
        add_to_waiting(s, a2)
    items = api.get(f"/users/{uid}/waiting", headers=h).json()
    assert [i["id"] for i in items] == [a1, a2]
    assert items[0]["ready_pack"]["copy"]["Email"] == "sample.candidate@example.com"
    assert api.post(f"/users/{uid}/waiting/{a2}/pin", headers=h).status_code == 200
    r = api.post(f"/users/{uid}/waiting/{a1}/answers", json={"answers": {"Why do you want to join Waiting Co?": "Payments infra."}},
                 headers=h).json()
    assert r == {"saved": 1, "requeued": True}
    assert api.get(f"/users/{uid}/answers", headers=h).json()[0]["answer"] == "Payments infra."
    assert api.delete(f"/users/{uid}/waiting/{a2}", headers=h).json() == {"dismissed": True}
    assert api.get(f"/users/{uid}/waiting", headers=h).json() == []


def test_responses_done(api, engine):
    uid, h = with_vault(api, "resp@example.com")
    with engine.begin() as c:
        rid = c.execute(text("""INSERT INTO responses (user_id, class, priority, gmail_id, gmail_thread_id, received_at, keep_until)
                                VALUES (:u, 'interview', 1, 'g-1', 'thr-1', now(), now() + interval '7 days') RETURNING id"""),
                        {"u": uid}).scalar()
    items = api.get(f"/users/{uid}/responses", headers=h).json()
    assert items[0]["gmail_url"].endswith("thr-1")
    assert api.post(f"/users/{uid}/responses/{rid}/done", headers=h).json()["done"]
    assert api.get(f"/users/{uid}/responses", headers=h).json() == []


def test_referral_draft_edit_must_keep_opt_out_and_limit(api, engine):
    uid, h = with_vault(api, "drafts@example.com")
    ids = seed_jobs(engine, uid, n=1, company="Draft Co")
    aid = api.post(f"/users/{uid}/jobs/{ids[0]}/queue", headers=h).json()["application_id"]
    contacts = [{"contact_id": 1, "role": "peer", "name": "Priya", "email": "p@d.co", "title": "SDE II", "subject": "s",
                 "body": "b", "status": "drafted"}]
    with engine.begin() as c:
        rq = c.execute(text("""INSERT INTO referral_queue (user_id, application_id, canonical_hash, company_key_hash, job, contacts, status)
                               SELECT user_id, id, canonical_hash, company_key_hash, '{"title": "SDE I", "company": "Draft Co"}',
                                      CAST(:c AS jsonb), 'awaiting_approval' FROM applications WHERE id = :a RETURNING id"""),
                       {"c": json.dumps(contacts), "a": aid}).scalar()
    long = "word " * 130
    assert api.post(f"/users/{uid}/referrals/{rq}/contacts/0", json={"approve": True, "body": long}, headers=h).status_code == 409
    ok = api.post(f"/users/{uid}/referrals/{rq}/contacts/0", json={"approve": True, "body": "Hi Priya, short ask."}, headers=h)
    assert ok.status_code == 200
    got = api.get(f"/users/{uid}/referrals", headers=h).json()[0]
    assert "won't reach out again" in got["contacts"][0]["body"] and got["status"] == "scheduled"


def test_star_and_blacklist(api, engine):
    uid, h = with_vault(api, "stars@example.com")
    assert api.put(f"/users/{uid}/stars/Northwind Payments Pvt Ltd", headers=h).json() == {"starred": "northwind payments"}
    assert api.put(f"/users/{uid}/blacklist/Spammy Corp", headers=h).status_code == 200
    v = api.get(f"/users/{uid}/vault", headers=h).json()
    assert v["data"]["expectations"]["blacklisted_companies"] == ["Spammy Corp"] and v["revision"] == 2


def test_summary(api, engine):
    uid, h = with_vault(api, "summary@example.com")
    seed_jobs(engine, uid)
    s = api.get(f"/users/{uid}/summary", headers=h).json()
    assert s["ranked_jobs"] == 2 and "JobBot daily digest" in s["digest"]


# --- onboarding ---------------------------------------------------------------------------
RESUME_TXT = """Anish Example
anish.example@gmail.com | +91 98100 00000 | github.com/anish-example | linkedin.com/in/anish-example

EDUCATION
Delhi Technological University
B.Tech in Computer Science and Engineering, CGPA 8.4/10    Aug 2023 - May 2027

EXPERIENCE
Software Trainee Intern | FinThrive Global    Jul 2026 - Oct 2026
• Built 6 REST endpoints in ASP.NET Core for a clearing house service handling 40k requests/day
• Cut p95 latency of the claim-status API from 900 ms to 350 ms with Redis caching

PROJECTS
Proctorly - React, Node.js, MongoDB
• Built browser-based proctored exams with camera and fullscreen checks for 200 concurrent candidates
• Blocked copy-paste and tab switching, flagging 3 violation types for admin review

SKILLS
Languages: Python, C#, JavaScript, SQL
Frameworks: ASP.NET Core, React, FastAPI
"""


def test_onboarding_import_text_and_pdf(api):
    uid, h = signup(api, "onboard@example.com")
    r = api.post(f"/users/{uid}/onboarding/import", data={"text": RESUME_TXT}, headers=h).json()
    d = r["draft"]
    assert d["personal"]["full_name"] == "Anish Example" and d["personal"]["email"] == "anish.example@gmail.com"
    assert d["education"][0]["graduation_year"] == 2027 and "8.4" in d["education"][0]["grade"]
    exp = d["experience"][0]
    assert exp["start"] == "2026-07" and exp["end"] == "2026-10" and len(exp["bullets"]) == 2
    assert d["projects"][0]["name"] == "Proctorly" and "react" in d["projects"][0]["tech"]
    assert d["skills"][0] == {"category": "Languages", "items": ["Python", "C#", "JavaScript", "SQL"]}
    # nothing saved; missing facts (location, expectations) are reported, never guessed
    assert api.get(f"/users/{uid}/vault", headers=h).status_code == 404
    assert any("location" in json.dumps(e["loc"]) for e in r["errors"])
    # PDF upload goes through the same path
    from app.resume.engine import build_recipe, prepare_jd, render_pdf
    from app.vault.schema import Vault
    v = Vault.model_validate(VAULT)
    pdf = render_pdf(v, build_recipe(v, prepare_jd(v, JD, "SDE", "X"), vault_revision=1)).pdf
    r2 = api.post(f"/users/{uid}/onboarding/import", files={"file": ("resume.pdf", pdf, "application/pdf")}, headers=h).json()
    assert r2["draft"]["personal"]["email"] == "sample.candidate@example.com"
    assert len(r2["draft"]["projects"]) >= 2
    checklist = api.get(f"/users/{uid}/onboarding", headers=h).json()
    assert [c["step"] for c in checklist if not c["done"]][0] == "vault"


def test_onboarding_llm_draft_drops_invented_bullets():
    import httpx

    from app.llm.client import LLM
    from app.onboarding import import_resume
    invented = {"personal": {"full_name": "Anish Example", "email": "anish.example@gmail.com", "phone": "+91 98100 00000",
                             "location": {"city": "Delhi", "country": "India"}},
                "projects": [{"id": "p1", "name": "Proctorly", "tech": ["React"], "bullets": [
                    {"id": "b1", "text": "Built browser-based proctored exams with camera and fullscreen checks for 200 concurrent candidates"},
                    {"id": "b2", "text": "Scaled the platform to 50000 daily users across 12 universities"}]}]}
    llm = LLM("anthropic", "k", "https://x", "c", "s", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(invented)}]})))
    out = import_resume(RESUME_TXT, llm)
    assert out["method"] == "llm"
    texts = [b["text"] for b in out["draft"]["projects"][0]["bullets"]]
    assert texts == [invented["projects"][0]["bullets"][0]["text"]]


# --- tuning -------------------------------------------------------------------------------
def test_weight_tuning_learns_and_shrinks(api, engine):
    import random

    from app import db
    from app.ingest.scoring import DEFAULT_WEIGHTS
    from app.tuning import tune_user
    uid, h = with_vault(api, "tune@example.com")
    rnd = random.Random(3)
    rows = []
    for _ in range(300):
        comps = [rnd.randint(0, 1000) for _ in range(6)]
        # outcomes driven by experience fit (index 1) only
        label = 1 if comps[1] + rnd.gauss(0, 120) > 750 else 0
        rows.append({"u": uid, "c": comps, "l": label})
    with engine.begin() as c:
        c.execute(text("INSERT INTO training_examples (user_id, components, label) VALUES (:u, :c, :l)"), rows[:10])
    with db.worker_session() as s:
        assert tune_user(s, uid).weights is None                       # too few outcomes: defaults stay
    with engine.begin() as c:
        c.execute(text("INSERT INTO training_examples (user_id, components, label) VALUES (:u, :c, :l)"), rows[10:])
    with db.worker_session() as s:
        res = tune_user(s, uid)
    w = res.weights
    assert abs(sum(w.values()) - 1) < 1e-3 and min(w.values()) >= 0.019
    assert w["experience"] > DEFAULT_WEIGHTS["experience"] + 0.2           # learned the real driver
    assert w["skills"] < DEFAULT_WEIGHTS["skills"]
    st = api.get(f"/users/{uid}/settings", headers=h).json()
    assert st["weights"] == w
    # scoring picks the tuned weights up
    from app.ingest.runner import load_profiles
    with db.worker_session() as s:
        p = next(p for p in load_profiles(s) if str(p.user_id) == uid)
    assert p.weights == w


# --- UI is served ----------------------------------------------------------------------------
def test_dashboard_static_files(api):
    page = api.get("/app/", headers=NOKEY)
    assert page.status_code == 200 and "importmap" in page.text
    for f in ("app.js", "style.css", "vendor/preact.module.js", "vendor/hooks.module.js", "vendor/htm.mjs", "example-vault.json"):
        assert api.get(f"/app/{f}", headers=NOKEY).status_code == 200, f
    assert api.get("/", headers=NOKEY, follow_redirects=False).headers["location"] == "/app/"


def test_totp_secret_generates_codes(api):
    import pyotp

    from app import db
    from app.mail.otp import totp_code
    uid, h = signup(api, "totp@example.com")
    secret = pyotp.random_base32()
    assert api.put(f"/users/{uid}/credentials/totp", json={"username": "www.MyWorkday.com", "secret": {"secret": secret}},
                   headers=h).status_code == 200
    assert api.put(f"/users/{uid}/credentials/totp", json={"username": "x.com", "secret": {"secret": "not base32!!"}},
                   headers=h).status_code == 422
    with db.worker_session() as s:
        assert totp_code(s, uid, "myworkday.com") == pyotp.TOTP(secret).now()
        assert totp_code(s, uid, "other.com") is None


def test_sms_webhook_feeds_pending_signin(api):
    from app.mail import otp
    from app.redis_client import get_redis
    uid, h = signup(api, "sms@example.com")
    cfg = api.get(f"/users/{uid}/webhooks/sms", headers=h).json()
    path = f"/webhooks/sms/{uid}"
    assert api.post(path, json={"text": "Your OTP is 445566"}, headers={**NOKEY, "X-Webhook-Token": "wrong"}).status_code == 401
    assert api.post(path, json={"text": "Your OTP is 445566"}, headers={**NOKEY, "X-Webhook-Token": cfg["token"]}).json()["accepted"] is False
    otp.register_waiter(uid, "myworkday.com")
    r = api.post(path, json={"text": "445566 is your verification code"}, headers={**NOKEY, "X-Webhook-Token": cfg["token"]})
    assert r.json() == {"accepted": True, "waiting_sites": 1}
    assert otp.take_code(uid, "myworkday.com") == "445566"
    assert all("445566" not in k for k in get_redis().keys("*"))
