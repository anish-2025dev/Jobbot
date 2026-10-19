import io
import json
import uuid
from pathlib import Path

import pytest

from app.resume.compile import available_engines

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not available_engines(), reason="no resume engine")]
ROOT = Path(__file__).parents[2]
EXAMPLE = json.loads((ROOT / "examples/vault.example.json").read_text())
JD = (ROOT / "examples/jd.example.txt").read_text()


@pytest.fixture
def uid(api):
    u = api.post("/users", json={"email": f"r{uuid.uuid4().hex[:8]}@test.dev"}).json()["id"]
    assert api.put(f"/users/{u}/vault", json=EXAMPLE).status_code == 201
    return u


def _req(**kw):
    return {"jd_text": JD, "title": "Software Development Engineer I", "company": "Northwind Payments", **kw}


def test_pdf_with_ats_header(api, uid):
    r = api.post(f"/users/{uid}/resume", json=_req())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF")
    report = json.loads(r.headers["x-ats-report"])
    assert report["ok"] and report["pages"] == 1
    assert "Sample_Candidate_Northwind_Payments.pdf" in r.headers["content-disposition"]


def test_docx(api, uid):
    from docx import Document
    r = api.post(f"/users/{uid}/resume", json=_req(format="docx"))
    assert r.status_code == 200
    assert Document(io.BytesIO(r.content)).paragraphs[0].text == "Sample Candidate"


def test_json_recipe_then_cache_hit(api, uid, engine):
    from sqlalchemy import text
    first = api.post(f"/users/{uid}/resume", json=_req(format="json")).json()
    assert first["cache_similarity"] is None
    assert first["recipe"]["vault_revision"] == 1
    # same JD, different company -> cache hit (similarity ~1.0), retargeted
    second = api.post(f"/users/{uid}/resume", json=_req(format="json", company="Contoso", bucket="low")).json()
    assert second["cache_similarity"] >= 0.92
    assert second["recipe"]["tailoring"] == "cached" and second["recipe"]["company"] == "Contoso"
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM recipe_cache WHERE user_id = :u"), {"u": uid}).scalar() == 1


def test_vault_change_invalidates_cache(api, uid):
    api.post(f"/users/{uid}/resume", json=_req(format="json"))
    api.patch(f"/users/{uid}/vault", json={"standard_answers": {"notice_period_days": 15}},
              headers={"If-Match": '"1"'})
    again = api.post(f"/users/{uid}/resume", json=_req(format="json")).json()
    assert again["cache_similarity"] is None and again["recipe"]["vault_revision"] == 2


def test_render_rechecks_client_recipe(api, uid):
    recipe = api.post(f"/users/{uid}/resume", json=_req(format="json")).json()["recipe"]
    recipe["experience"][0]["bullets"][0]["text"] = "Scaled the platform to 50 million users with Kubernetes"
    r = api.post(f"/users/{uid}/resume/render", json={"recipe": recipe})
    assert r.status_code == 200
    from app.resume.ats import pdf_text
    assert "50 million" not in pdf_text(r.content)


def test_render_rejects_stale_revision(api, uid):
    recipe = api.post(f"/users/{uid}/resume", json=_req(format="json")).json()["recipe"]
    recipe["vault_revision"] = 99
    assert api.post(f"/users/{uid}/resume/render", json={"recipe": recipe}).status_code == 409


def test_no_vault_404(api):
    u = api.post("/users", json={"email": f"n{uuid.uuid4().hex[:8]}@test.dev"}).json()["id"]
    assert api.post(f"/users/{u}/resume", json=_req()).status_code == 404


def test_short_jd_rejected(api, uid):
    assert api.post(f"/users/{uid}/resume", json=_req(jd_text="too short")).status_code == 422
