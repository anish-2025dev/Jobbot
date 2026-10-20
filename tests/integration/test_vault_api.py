import copy
import json
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

EXAMPLE = json.loads((Path(__file__).parents[2] / "examples/vault.example.json").read_text())


@pytest.fixture
def uid(api):
    r = api.post("/users", json={"email": f"u{uuid.uuid4().hex[:8]}@test.dev"})
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture
def saved(api, uid):
    r = api.put(f"/users/{uid}/vault", json=EXAMPLE)
    assert r.status_code == 201, r.text
    return uid


def _etag(r):
    return r.headers["ETag"]


# --- auth ----------------------------------------------------------------------
def test_api_key_required(api, uid):
    for key in ("", "wrong"):
        assert api.get(f"/users/{uid}/vault", headers={"X-API-Key": key}).status_code == 401


def test_fails_closed_without_configured_key(api, uid, monkeypatch):
    import app.config
    monkeypatch.setenv("ADMIN_API_KEY", "")
    app.config.get_settings.cache_clear()
    assert api.get(f"/users/{uid}/vault").status_code == 401


# --- users ---------------------------------------------------------------------
def test_create_user_duplicate_email(api):
    email = f"dup{uuid.uuid4().hex[:6]}@test.dev"
    assert api.post("/users", json={"email": email}).status_code == 201
    assert api.post("/users", json={"email": email.upper()}).status_code == 409


def test_create_user_bad_email(api):
    assert api.post("/users", json={"email": "nope"}).status_code == 422


# --- create / read -------------------------------------------------------------
def test_get_before_create_is_404(api, uid):
    assert api.get(f"/users/{uid}/vault").status_code == 404


def test_unknown_user_404(api):
    assert api.get(f"/users/{uuid.uuid4()}/vault").status_code == 404
    assert api.put(f"/users/{uuid.uuid4()}/vault", json=EXAMPLE).status_code == 404


def test_create_then_get(api, saved):
    r = api.get(f"/users/{saved}/vault")
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 1 and _etag(r) == '"1"'
    assert body["data"]["personal"]["full_name"] == "Sample Candidate"
    assert body["warnings"] == []
    assert "updated_at" in body


@pytest.mark.parametrize("patch", [{"pan": "ABCDE1234F"}, {"pan": None}])
def test_undecryptable_identity_can_be_replaced_or_deleted(api, saved, patch):
    from sqlalchemy import text
    from app import db

    assert api.patch(f"/users/{saved}/identity", json={"pan": "ZYXWV9876A"}).status_code == 200
    with db.worker_session() as s:
        s.execute(text("UPDATE credentials SET secret_enc = :bad WHERE user_id = :u AND host = 'identity'"),
                  {"bad": b"undecryptable", "u": saved})
    assert api.get(f"/users/{saved}/identity").json() == {"data": {}}
    assert api.get(f"/users/{saved}/vault").status_code == 200
    response = api.patch(f"/users/{saved}/identity", json=patch)
    assert response.status_code == 200
    expected = {"data": {"pan": "••••••234F"}} if patch["pan"] else {"data": {}}
    assert response.json() == expected
    assert api.get(f"/users/{saved}/identity").json() == expected
    with db.worker_session() as s:
        count = s.execute(text("SELECT count(*) FROM credentials WHERE user_id = :u AND host = 'identity'"), {"u": saved}).scalar()
        assert count == (1 if patch["pan"] else 0)


def test_invalid_vault_rejected_and_not_saved(api, uid):
    bad = copy.deepcopy(EXAMPLE)
    bad["projects"][0]["bullets"][0]["text"] = "short"
    r = api.put(f"/users/{uid}/vault", json=bad)
    assert r.status_code == 422
    assert any("bullets" in json.dumps(e["loc"]) for e in r.json()["detail"])
    assert api.get(f"/users/{uid}/vault").status_code == 404


# --- concurrency -----------------------------------------------------------------
def test_overwrite_needs_if_match(api, saved):
    assert api.put(f"/users/{saved}/vault", json=EXAMPLE).status_code == 428


def test_put_with_if_match_bumps_revision(api, saved):
    data = copy.deepcopy(EXAMPLE)
    data["personal"]["location"]["city"] = "Gurugram"
    r = api.put(f"/users/{saved}/vault", json=data, headers={"If-Match": '"1"'})
    assert r.status_code == 200 and r.json()["revision"] == 2 and _etag(r) == '"2"'


def test_stale_if_match_conflicts(api, saved):
    api.put(f"/users/{saved}/vault", json=EXAMPLE, headers={"If-Match": "1"})
    r = api.put(f"/users/{saved}/vault", json=EXAMPLE, headers={"If-Match": "1"})
    assert r.status_code == 409
    assert r.json()["detail"]["current_revision"] == 2


def test_bad_if_match_header(api, saved):
    assert api.put(f"/users/{saved}/vault", json=EXAMPLE, headers={"If-Match": "abc"}).status_code == 400


def test_weak_etag_accepted(api, saved):
    assert api.put(f"/users/{saved}/vault", json=EXAMPLE, headers={"If-Match": 'W/"1"'}).status_code == 200


# --- patch -----------------------------------------------------------------------
def test_merge_patch_updates_one_field(api, saved):
    r = api.patch(f"/users/{saved}/vault", json={"standard_answers": {"notice_period_days": 30}},
                  headers={"If-Match": '"1"'})
    assert r.status_code == 200, r.text
    sa = r.json()["data"]["standard_answers"]
    assert sa["notice_period_days"] == 30 and sa["work_authorization"] == "Indian citizen"


def test_merge_patch_null_removes_and_warns(api, saved):
    r = api.patch(f"/users/{saved}/vault", json={"standard_answers": {"notice_period_days": None}},
                  headers={"If-Match": '"1"'})
    assert r.status_code == 200
    assert "standard_answers.notice_period_days" in [w["path"] for w in r.json()["warnings"]]


def test_patch_that_breaks_schema_is_rejected(api, saved):
    r = api.patch(f"/users/{saved}/vault", json={"projects": []}, headers={"If-Match": '"1"'})
    assert r.status_code == 422
    assert api.get(f"/users/{saved}/vault").json()["revision"] == 1


def test_patch_requires_if_match(api, saved):
    assert api.patch(f"/users/{saved}/vault", json={}).status_code == 428


# --- validate (dry run) ----------------------------------------------------------
def test_validate_never_saves(api, uid):
    r = api.post(f"/users/{uid}/vault/validate", json=EXAMPLE)
    assert r.json() == {"valid": True, "errors": [], "warnings": []}
    assert api.get(f"/users/{uid}/vault").status_code == 404


def test_validate_reports_errors(api, uid):
    bad = copy.deepcopy(EXAMPLE)
    bad["projects"][1]["id"] = "proj-job-bot"
    body = api.post(f"/users/{uid}/vault/validate", json=bad).json()
    assert body["valid"] is False and "unique" in json.dumps(body["errors"])


# --- projects --------------------------------------------------------------------
NEW_PROJECT = {
    "name": "Mini SIEM",
    "tech": ["Python", "FastAPI"],
    "bullets": [{"id": "siem-1", "text": "Ingested 5k log events per second into OpenSearch"}],
}


def test_add_project(api, saved):
    r = api.put(f"/users/{saved}/vault/projects/proj-siem", json=NEW_PROJECT, headers={"If-Match": '"1"'})
    assert r.status_code == 200, r.text
    ids = [p["id"] for p in r.json()["data"]["projects"]]
    assert ids[-1] == "proj-siem" and len(ids) == 5


def test_replace_project_keeps_position(api, saved):
    proj = dict(NEW_PROJECT, name="Renamed")
    r = api.put(f"/users/{saved}/vault/projects/proj-proctor", json=proj, headers={"If-Match": '"1"'})
    projects = r.json()["data"]["projects"]
    assert projects[1]["id"] == "proj-proctor" and projects[1]["name"] == "Renamed"


def test_project_id_mismatch(api, saved):
    r = api.put(f"/users/{saved}/vault/projects/a", json=dict(NEW_PROJECT, id="b"), headers={"If-Match": '"1"'})
    assert r.status_code == 400


def test_project_bullet_id_clash_rejected(api, saved):
    proj = dict(NEW_PROJECT, bullets=[{"id": "jb-1", "text": "Duplicate bullet id here"}])
    r = api.put(f"/users/{saved}/vault/projects/proj-siem", json=proj, headers={"If-Match": '"1"'})
    assert r.status_code == 422


def test_delete_project(api, saved):
    r = api.delete(f"/users/{saved}/vault/projects/proj-retail", headers={"If-Match": '"1"'})
    assert r.status_code == 200
    assert "proj-retail" not in [p["id"] for p in r.json()["data"]["projects"]]
    assert all("proj-retail" not in t.get("projects", []) for t in r.json()["data"]["target_roles"])
    assert api.delete(f"/users/{saved}/vault/projects/proj-retail",
                      headers={"If-Match": '"2"'}).status_code == 404


def test_cannot_delete_last_project(api, uid):
    one = copy.deepcopy(EXAMPLE)
    one["projects"] = one["projects"][:1]
    for r in one["target_roles"]:
        r["projects"] = [p for p in r["projects"] if p == "proj-job-bot"]
    api.put(f"/users/{uid}/vault", json=one)
    r = api.delete(f"/users/{uid}/vault/projects/proj-job-bot", headers={"If-Match": '"1"'})
    assert r.status_code == 422


# --- isolation & storage -----------------------------------------------------------
def test_users_are_isolated(api, saved):
    other = api.post("/users", json={"email": f"o{uuid.uuid4().hex[:8]}@test.dev"}).json()["id"]
    assert api.get(f"/users/{other}/vault").status_code == 404


def test_stored_size_within_budget(api, saved, engine):
    from sqlalchemy import text
    with engine.connect() as c:
        size = c.execute(text("SELECT pg_column_size(data) FROM vaults WHERE user_id = :u"), {"u": saved}).scalar()
    assert size < 50 * 1024      # spec: ~50 KB/user


def test_schema_endpoint(api):
    schema = api.get("/vault/schema").json()
    assert {"personal", "projects", "standard_answers", "expectations"} <= set(schema["properties"])
