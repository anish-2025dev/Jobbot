import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
from pydantic import ValidationError  # noqa: E402

from app.vault.facts import vault_facts  # noqa: E402
from app.vault.lint import lint  # noqa: E402
from app.vault.schema import MAX_VAULT_BYTES, Vault  # noqa: E402

EXAMPLE = json.loads((Path(__file__).parents[2] / "examples/vault.example.json").read_text())


@pytest.fixture
def data():
    return copy.deepcopy(EXAMPLE)


def _invalid(d, fragment):
    with pytest.raises(ValidationError) as e:
        Vault.model_validate(d)
    assert fragment in str(e.value), str(e.value)


def test_example_is_valid_and_lint_clean(data):
    v = Vault.model_validate(data)
    assert v.graduation_year == 2027
    assert lint(v) == []


def test_roundtrip_storage(data):
    v = Vault.model_validate(data)
    assert Vault.model_validate(v.to_storage()) == v


def test_unknown_field_rejected(data):
    data["personal"]["nickname"] = "x"
    _invalid(data, "Extra inputs are not permitted")


def test_duplicate_ids_rejected(data):
    data["projects"][1]["bullets"][0]["id"] = "jb-1"
    _invalid(data, "ids must be unique")


def test_duplicate_project_ids_rejected(data):
    data["projects"][1]["id"] = "proj-job-bot"
    _invalid(data, "ids must be unique")


@pytest.mark.parametrize("bad", ["Proj One", "-x", "UPPER", "a" * 49])
def test_slug_rules(data, bad):
    data["projects"][0]["id"] = bad
    _invalid(data, "pattern")


def test_date_order(data):
    data["experience"][0]["end"] = "2026-01"
    _invalid(data, "before start")


@pytest.mark.parametrize("bad", ["2026-13", "2026-1", "26-01", "2026/01"])
def test_year_month_format(data, bad):
    data["experience"][0]["start"] = bad
    _invalid(data, "pattern")


def test_current_role_allowed(data):
    data["experience"][0].pop("end")
    assert Vault.model_validate(data).experience[0].end is None


def test_bullet_rules(data):
    d = copy.deepcopy(data)
    d["projects"][0]["bullets"][0]["text"] = "too short"
    _invalid(d, "at least 10")
    d = copy.deepcopy(data)
    d["projects"][0]["bullets"][0]["text"] = "line one is fine\nline two"
    _invalid(d, "single line")
    d = copy.deepcopy(data)
    d["projects"][0]["bullets"][0]["text"] = "x" * 301
    _invalid(d, "at most 300")


def test_required_sections(data):
    for key in ("personal", "education", "projects", "skills", "expectations"):
        d = copy.deepcopy(data)
        d.pop(key)
        _invalid(d, "Field required")
    d = copy.deepcopy(data)
    d["projects"] = []
    _invalid(d, "at least 1")


def test_facts_have_no_defaults(data):
    data.pop("standard_answers")
    v = Vault.model_validate(data)
    assert v.standard_answers.notice_period_days is None
    assert v.standard_answers.work_authorization is None


def test_graduation_year_bounds(data):
    data["education"][0]["graduation_year"] = 1980
    _invalid(data, "greater than or equal")


def test_skills_deduped_case_insensitive(data):
    data["skills"][0]["items"] = ["Python", "python", " PYTHON ", "Go"]
    assert Vault.model_validate(data).skills[0].items == ["Python", "Go"]


def test_skill_categories_unique(data):
    data["skills"][1]["category"] = "languages"
    _invalid(data, "skill categories must be unique")


def test_size_limit(data):
    big = [{"id": f"b{i}", "text": "x" * 290} for i in range(8)]
    data["target_roles"], data["expectations"]["role_types"] = [], ["SDE"]
    data["projects"] = [
        {"id": f"p{n}", "name": "P", "tech": ["Python"], "bullets": [dict(b, id=f"p{n}-{b['id']}") for b in big]}
        for n in range(10)
    ]
    data["experience"] = [
        {"id": f"e{n}", "company": "C", "title": "T", "employment_type": "internship", "start": "2025-01",
         "bullets": [dict(b, id=f"e{n}-{b['id']}") for b in big]}
        for n in range(15)
    ]
    _invalid(data, f"limit is {MAX_VAULT_BYTES}")


def test_email_phone_url_validation(data):
    for path, bad in [(("personal", "email"), "not-an-email"),
                      (("personal", "phone"), "call me"),
                      (("projects", 0, "repo"), "github.com/no-scheme")]:
        d = copy.deepcopy(data)
        node = d
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = bad
        _invalid(d, "pattern")


def test_json_schema_generates():
    schema = Vault.model_json_schema()
    assert "personal" in schema["properties"] and "$defs" in schema


# --- lint ----------------------------------------------------------------------
def test_lint_flags_missing_metric_and_bullet_count(data):
    data["projects"][0]["bullets"] = [{"id": "jb-x", "text": "Built a thing that works well"}]
    msgs = " ".join(w["message"] for w in lint(Vault.model_validate(data)))
    assert "no real metric" in msgs and "1 bullets" in msgs


def test_lint_flags_unlisted_tech(data):
    data["projects"][0]["tech"].append("Kafka")
    msgs = [w for w in lint(Vault.model_validate(data)) if w["path"] == "skills"]
    assert msgs and "Kafka" in msgs[0]["message"]


def test_lint_alias_counts_as_listed(data):
    data["projects"][0]["tech"].append("Postgres")      # PostgreSQL is listed
    assert not [w for w in lint(Vault.model_validate(data)) if w["path"] == "skills"]


def test_lint_flags_unanswered_standard_answers(data):
    data["standard_answers"].pop("notice_period_days")
    paths = [w["path"] for w in lint(Vault.model_validate(data))]
    assert "standard_answers.notice_period_days" in paths


# --- facts from a real vault ---------------------------------------------------------
def test_vault_facts(data):
    f = vault_facts(Vault.model_validate(data))
    assert {"python", "postgresql", "c#", "react", "data structures and algorithms"} <= f.skills
    assert {"40000", "45%", "82%", "0.91", "60%", "2027", "8.4"} <= f.numbers
    assert f.bullets["acme-2"].startswith("Cut p95")
    assert f.untraceable_numbers("cut latency by 70%") == ["70%"]
    assert f.untraceable_skills(["Kubernetes", "FastAPI"]) == ["Kubernetes"]
