"""Stored vault compatibility and application outcomes for invalid stored data."""
import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.integration.conftest import insert_application, seed_user

pytestmark = pytest.mark.integration
EXAMPLE = json.loads((Path(__file__).parents[2] / "examples/vault.example.json").read_text())


@pytest.fixture
def legacy():
    data = copy.deepcopy(EXAMPLE)
    while len(data["projects"]) < 11:
        project = copy.deepcopy(data["projects"][0])
        project["id"] = f"legacy-{len(data['projects'])}"
        for i, bullet in enumerate(project["bullets"]):
            bullet["id"] = f"{project['id']}-b{i}"
        data["projects"].append(project)
    return data


def test_stored_readers_preserve_all_legacy_projects(module_db, legacy):
    from app import db
    from app.apply.runner import _load
    from app.apply.waiting import add_to_waiting
    from app.ingest.runner import load_profiles
    from app.outreach.queue import _recipe_and_vault
    from app.onboarding import checklist
    from app.resume.service import load_vault

    uid = seed_user(module_db, vault=legacy)
    with db.worker_session() as s:
        aid = insert_application(s, uid, status="waiting")
        vault, rev = load_vault(s, uid)
        assert rev == 1 and len(vault.projects) == 11
        assert len(_load(s, aid)[1].projects) == 11
        assert len(_recipe_and_vault(s, {"application_id": aid, "user_id": uid})[1].projects) == 11
        assert len(load_profiles(s, [uid])) == 1
        assert next(step for step in checklist(s, uid) if step["step"] == "vault")["done"]
        assert add_to_waiting(s, aid)
        assert s.execute(text("SELECT count(*) FROM waiting_list WHERE application_id = :i"), {"i": aid}).scalar() == 1


@pytest.mark.parametrize("invalid", ["vault", "recipe"])
def test_invalid_stored_data_marks_application_failed(module_db, legacy, invalid):
    from app import db
    from app.apply.runner import run_application

    if invalid == "vault":
        legacy["personal"]["email"] = "invalid"
    uid = seed_user(module_db, vault=legacy)
    with db.worker_session() as s:
        aid = insert_application(s, uid, status="applying")
        if invalid == "recipe":
            s.execute(text("""UPDATE applications SET recipe = '{"summary": 123}'::jsonb WHERE id = :i"""), {"i": aid})
    outcome = run_application(aid, None)
    assert outcome.status == "failed" and outcome.step == "load"
    with db.worker_session() as s:
        row = s.execute(text("SELECT status, failure_step, failure_reason FROM applications WHERE id = :i"), {"i": aid}).one()
        assert row.status == "failed" and row.failure_step == "load"
        assert row.failure_reason == outcome.reason
