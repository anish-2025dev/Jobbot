"""Vault v2: application profile, target roles, role-aware resumes, identity documents."""
import copy
import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.resume.engine import build_recipe, prepare_jd
from app.resume.model import build_view
from app.resume.render import latex_href, to_latex, to_text
from app.resume.select import MAX_RESUME_SKILLS, match_role
from app.vault.identity import Identity, mask, masked
from app.vault.lint import lint, profile_gaps
from app.vault.schema import MAX_SKILLS, Vault, guess_level

EXAMPLE = json.loads((Path(__file__).parents[2] / "examples/vault.example.json").read_text(encoding="utf-8"))

DATA_JD = """Data Engineer (Fresher). Build ELT pipelines with Airflow and dbt on PostgreSQL.
Must have: SQL, Python, dbt, Airflow. Nice to have: Power BI, MLflow."""
SDE_JD = """Software Engineer I, Backend. Build REST APIs in Python with FastAPI and PostgreSQL.
Must have: Python, FastAPI, PostgreSQL, Redis. Nice to have: Docker, React."""


@pytest.fixture
def data():
    return copy.deepcopy(EXAMPLE)


@pytest.fixture
def v(data):
    return Vault.model_validate(data)


def _recipe(v, text, title):
    return build_recipe(v, prepare_jd(v, text, title, "Acme"), vault_revision=1)


# --- schema ---------------------------------------------------------------------------
def test_v1_vault_still_loads_and_upgrades(data):
    for key in ("background", "summary", "target_roles"):
        data.pop(key)
    data["schema_version"] = 1
    data["expectations"]["role_types"] = ["Software Development Engineer"]
    for p in data["projects"]:
        p.pop("roles", None)
    data["education"] = data["education"][:1]
    for k in ("score", "score_scale", "active_backlogs", "backlog_history"):
        data["education"][0].pop(k)
    v = Vault.model_validate(data)
    assert v.schema_version == 2 and v.all_role_types == ["Software Development Engineer"]


def test_name_parts_derived(data):
    data["personal"]["full_name"] = "Anish Kumar Gupta"
    p = Vault.model_validate(data).personal
    assert (p.first_name, p.middle_name, p.last_name) == ("Anish", "Kumar", "Gupta")
    data["personal"]["first_name"] = "Ani"
    assert Vault.model_validate(data).personal.first_name == "Ani"


def test_age(v):
    assert v.personal.age_on(date(2026, 3, 13)) == 20 and v.personal.age_on(date(2026, 3, 14)) == 21


@pytest.mark.parametrize("degree,level", [
    ("Class XII", "class_xii"), ("12th (HSC)", "class_xii"), ("Senior Secondary", "class_xii"),
    ("Class X", "class_x"), ("SSC", "class_x"), ("B.Tech", "bachelors"), ("BSc Computer Science", "bachelors"),
    ("M.Tech", "masters"), ("MBA", "masters"), ("Diploma in CS", "diploma"), ("Ph.D", "phd")])
def test_education_level_guess(degree, level):
    assert guess_level(degree) == level


def test_class_xii_does_not_set_graduation_year(v):
    assert v.graduation_year == 2027          # the B.Tech, not the 2023 Class XII


def test_cgpa_bounds(data):
    data["education"][0]["score"] = 84
    with pytest.raises(ValidationError, match="CGPA out of 10"):
        Vault.model_validate(data)


def test_project_and_skill_limits(data):
    proj = data["projects"][0]
    data["target_roles"][0]["projects"] = []
    data["target_roles"][1]["projects"] = []
    data["projects"] = [dict(copy.deepcopy(proj), id=f"p{i}", roles=[],
                             bullets=[{"id": f"p{i}-b", "text": "Built a thing with 3 parts"}]) for i in range(11)]
    with pytest.raises(ValidationError, match="at most 10"):
        Vault.model_validate(data)
    data = copy.deepcopy(EXAMPLE)
    data["skills"] = [{"category": f"G{g}", "items": [f"skill{g}-{i}" for i in range(50)]} for g in range(5)]
    with pytest.raises(ValidationError, match=f"limit is {MAX_SKILLS}"):
        Vault.model_validate(data)


def test_role_references_checked(data):
    data["projects"][1]["roles"] = ["nope"]
    with pytest.raises(ValidationError, match="unknown target roles"):
        Vault.model_validate(data)
    data = copy.deepcopy(EXAMPLE)
    data["target_roles"][0]["projects"] = ["proj-missing"]
    with pytest.raises(ValidationError, match="unknown projects"):
        Vault.model_validate(data)


def test_a_role_is_required(data):
    data["target_roles"], data["expectations"]["role_types"] = [], []
    for p in data["projects"]:
        p["roles"] = []
    with pytest.raises(ValidationError, match="at least one target role"):
        Vault.model_validate(data)


def test_lint_flags_role_skill_outside_pool(data):
    data["target_roles"][0]["skills"].append("Kubernetes")
    msgs = [w for w in lint(Vault.model_validate(data)) if w["path"] == "target_roles[0].skills"]
    assert msgs and "Kubernetes" in msgs[0]["message"]


def test_profile_gaps(v):
    gaps = {g["path"] for g in profile_gaps(v)}
    assert "personal.date_of_birth" not in gaps and "education.class_xii" not in gaps
    assert {"education.class_x", "identity.pan", "identity.passport"} <= gaps
    assert "identity.pan" not in {g["path"] for g in profile_gaps(v, {"pan"})}
    order = [g["asked"] for g in profile_gaps(v)]
    assert order == sorted(order, key=["often", "sometimes", "rarely"].index)


# --- role-aware resumes ------------------------------------------------------------------
def test_match_role_by_title(v):
    assert match_role(v, "Data Analyst - Fresher")[0].id == "data"
    assert match_role(v, "SDE 1 (Backend)")[0].id == "sde"
    role, strong = match_role(v, "Graduate Engineer Trainee")
    assert role.id == "sde" and strong


def test_resume_follows_the_role(v):
    sde = _recipe(v, SDE_JD, "Software Engineer I")
    data = _recipe(v, DATA_JD, "Data Engineer")
    assert sde.role_id == "sde" and data.role_id == "data"
    assert sde.summary.startswith("Backend-focused") and data.summary.startswith("Data engineer")
    assert sde.projects[0].id == "proj-job-bot"
    assert data.projects[0].id == "proj-retail"
    sde_skills = [i for g in sde.skills for i in g.items]
    data_skills = [i for g in data.skills for i in g.items]
    assert {"FastAPI", "PostgreSQL", "Redis"} <= set(sde_skills)
    assert {"dbt", "Airflow", "SQL"} <= set(data_skills)
    assert data_skills.index("dbt") < len(data_skills) // 2 + 1
    assert len(sde_skills) <= MAX_RESUME_SKILLS + 4


def test_skills_come_only_from_the_pool(v):
    r = _recipe(v, SDE_JD + " Also Kubernetes and Terraform.", "Backend Engineer")
    items = {i for g in r.skills for i in g.items}
    assert "Kubernetes" not in items and "Terraform" not in items
    assert "kubernetes" in r.missing_skills or "Kubernetes".lower() not in r.keywords


def test_no_roles_falls_back_to_general_summary(data):
    data["target_roles"], data["expectations"]["role_types"] = [], ["SDE"]
    for p in data["projects"]:
        p["roles"] = []
    v = Vault.model_validate(data)
    r = _recipe(v, SDE_JD, "Software Engineer")
    assert r.role_id is None and r.summary.startswith("Final-year")


def test_layout_order_and_hidden_school(v):
    r = _recipe(v, SDE_JD, "Software Engineer")
    txt = to_text(build_view(v, r))
    heads = [h for h in ("SUMMARY", "EXPERIENCE", "TECHNICAL SKILLS", "PROJECTS", "EDUCATION") if h in txt]
    assert heads == ["SUMMARY", "EXPERIENCE", "TECHNICAL SKILLS", "PROJECTS", "EDUCATION"]
    assert [txt.index(h) for h in heads] == sorted(txt.index(h) for h in heads)
    assert "CGPA 8.4/10" in txt
    assert "Senior Secondary" not in txt                      # show_on_resume = false


def test_latex_links(v):
    r = _recipe(v, SDE_JD, "Software Engineer")
    tex = to_latex(build_view(v, r))
    assert r"\href{mailto:sample.candidate@example.com}" in tex
    assert r"\href{https://github.com/sample-candidate}{github.com/sample-candidate}" in tex
    assert latex_href("https://x.dev/a%20b#c") == r"https://x.dev/a\%20b\#c"
    with pytest.raises(ValueError):
        latex_href("https://x.dev/}\\input{x}")


def test_bad_link_is_text_only(data):
    data["personal"]["links"].append({"label": "Odd", "url": "https://x.dev/a}b"})
    v = Vault.model_validate(data)
    view = build_view(v, _recipe(v, SDE_JD, "Software Engineer"))
    assert ("x.dev/a}b", None) in view.contact_links
    to_latex(view)                                            # renders, text escaped


# --- identity documents ----------------------------------------------------------------
def test_identity_validation_and_mask():
    ident = Identity.model_validate({"pan": "abcde1234f", "aadhaar": "2345 6789 0123",
                                     "passport": {"number": "z1234567", "issue_date": "2022-01-10",
                                                  "expiry_date": "2032-01-09"}})
    assert ident.pan == "ABCDE1234F" and ident.values()["aadhaar"] == "234567890123"
    assert ident.values()["passport_number"] == "Z1234567"
    m = masked(ident)
    assert m["pan"].endswith("234F") and "ABCDE" not in m["pan"]
    assert m["passport"]["number"] == mask("Z1234567") and m["passport"]["expiry_date"] == "2032-01-09"
    for bad in ({"pan": "ABCD1234F"}, {"aadhaar": "1234 5678 9012"},
                {"passport": {"number": "Z1234567", "issue_date": "2030-01-01", "expiry_date": "2020-01-01"}}):
        with pytest.raises(ValidationError):
            Identity.model_validate(bad)


def test_identity_never_in_vault(data):
    data["personal"]["pan"] = "ABCDE1234F"
    with pytest.raises(ValidationError, match="Extra inputs"):
        Vault.model_validate(data)


# --- resume import: education ----------------------------------------------------------
def test_import_education_pairs_degree_with_institution_not_location():
    """Regression: a two-column PDF used to turn 'New Delhi, India' into the institution and
    fold the school rows into one garbled 'Class XII' degree."""
    from app.onboarding import parse_education
    got = parse_education([
        "Indian Institute of Information Technology   New Delhi, India",
        "B.Tech in Computer Science and Engineering; CGPA: 8.52/10   Aug 2022 - May 2026",
        "Delhi Public School, R.K. Puram   New Delhi, India",
        "Class XII (CBSE): 92.4%   2022",
    ])
    assert [(e["level"], e["institution"], e["degree"]) for e in got] == [
        ("bachelors", "Indian Institute of Information Technology", "B.Tech"),
        ("class_xii", "Delhi Public School, R.K. Puram", "Class XII")]
    assert got[0]["field_of_study"] == "Computer Science and Engineering" and got[0]["score"] == 8.52 and got[0]["end"] == "2026-05"
    assert got[1]["board"] == "CBSE" and got[1]["score_scale"] == "percentage" and got[1]["show_on_resume"] is False


def test_import_education_degree_first_layout():
    from app.onboarding import parse_education
    got = parse_education(["B.Tech, Computer Science   2022 - 2026", "Example Institute of Technology, New Delhi",
                           "CGPA 8.4", "Senior Secondary (XII), CBSE  2022", "Kendriya Vidyalaya, Delhi", "91%"])
    assert [(e["level"], e["institution"]) for e in got] == [("bachelors", "Example Institute of Technology"),
                                                             ("class_xii", "Kendriya Vidyalaya")]
    assert got[0]["score"] == 8.4 and got[1]["score"] == 91.0


def test_import_school_without_institution_is_left_for_the_user():
    from app.onboarding import parse_education
    got = parse_education(["Class X (CBSE): 95%   2020"])
    assert got[0]["institution"] == "" and got[0]["degree"] == "Class X"
