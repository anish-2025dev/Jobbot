"""Form fields that vault v2 and the identity documents answer (Indian and US forms)."""
import copy
import json
from datetime import date
from pathlib import Path

import pytest

from app.apply.filler import Field, FillContext, plan
from app.vault.identity import Identity
from app.vault.schema import Vault

EXAMPLE = json.loads((Path(__file__).parents[2] / "examples/vault.example.json").read_text(encoding="utf-8"))
IDENTITY = Identity.model_validate({"pan": "ABCDE1234F", "aadhaar": "2345 6789 0123",
                                    "passport": {"number": "Z1234567", "issue_date": "2022-01-10",
                                                 "expiry_date": "2032-01-09", "place_of_issue": "Delhi"}})


def _ctx(data=None, identity=IDENTITY):
    v = Vault.model_validate(data or copy.deepcopy(EXAMPLE))
    return FillContext(vault=v, company="Acme", title="SDE", resume_path="/tmp/r.pdf",
                       identity=identity.values(), today=date(2026, 10, 20))


def fill(*fields, ctx=None):
    p = plan(list(fields), ctx or _ctx())
    return {d.key: d.value for d in p.decisions}, {f.key for f in p.unresolved}


def test_indian_campus_form():
    vals, unresolved = fill(
        Field("fn", "First Name", "text", required=True),
        Field("ln", "Surname", "text", required=True),
        Field("dob", "Date of Birth", "text", required=True),
        Field("dob2", "DOB", "date"),
        Field("age", "Age", "number"),
        Field("father", "Father's Name", "text", required=True),
        Field("nat", "Nationality", "select", options=["Indian", "Other"]),
        Field("pan", "PAN Card Number", "text", required=True),
        Field("aad", "Aadhar Number", "text"),
        Field("x12", "12th Percentage", "number", required=True),
        Field("x12b", "Class XII Board", "select", options=["CBSE", "ICSE", "State Board"]),
        Field("x12y", "12th Passing Year", "text"),
        Field("x12s", "Senior Secondary School Name", "text"),
        Field("cg", "Graduation CGPA", "text", required=True),
        Field("bl", "Number of active backlogs", "number", required=True),
        Field("blh", "Any history of backlogs?", "radio", options=["Yes", "No"]),
        Field("gap", "Do you have any gap in education?", "radio", options=["Yes", "No"]),
        Field("bond", "Are you willing to sign a service agreement of 2 years?", "radio", options=["Yes", "No"]),
        Field("shift", "Are you comfortable with night shifts?", "radio", options=["Yes", "No"]),
        Field("lap", "Do you have your own laptop?", "radio", options=["Yes", "No"]),
        Field("pin", "Pincode", "text"),
        Field("state", "State", "select", options=["Delhi", "Haryana"]),
        Field("lang", "Languages Known", "text"),
    )
    assert not unresolved
    assert (vals["fn"], vals["ln"]) == ("Sample", "Candidate")
    assert vals["dob"] == "14/03/2005" and vals["dob2"] == "2005-03-14" and vals["age"] == "21"
    assert vals["father"] == "Sample Parent" and vals["nat"] == "Indian"
    assert vals["pan"] == "ABCDE1234F" and vals["aad"] == "234567890123"
    assert vals["x12"] == "91.2" and vals["x12b"] == "CBSE" and vals["x12y"] == "2023"
    assert vals["x12s"] == "Example Senior Secondary School"
    assert vals["cg"] == "8.4/10" and vals["bl"] == "0" and vals["blh"] == "No"
    assert (vals["gap"], vals["bond"], vals["shift"], vals["lap"]) == ("No", "No", "Yes", "Yes")
    assert vals["pin"] == "110001" and vals["state"] == "Delhi" and vals["lang"] == "English, Hindi"


def test_passport_fields():
    vals, unresolved = fill(
        Field("has", "Do you have a valid passport?", "radio", options=["Yes", "No"]),
        Field("no", "Passport Number", "text"),
        Field("exp", "Passport Expiry Date", "date"),
        Field("place", "Passport place of issue", "text"))
    assert vals == {"has": "Yes", "no": "Z1234567", "exp": "2032-01-09", "place": "Delhi"}


def test_missing_identity_goes_to_assisted():
    vals, unresolved = fill(Field("pan", "PAN", "text", required=True),
                            Field("has", "Do you have a valid passport?", "radio", options=["Yes", "No"], required=True),
                            ctx=_ctx(identity=Identity()))
    assert unresolved == {"pan"} and vals["has"] == "No"


def test_pan_india_is_not_the_pan_number():
    vals, unresolved = fill(Field("loc", "Are you open to PAN India relocation?", "radio", options=["Yes", "No"]))
    assert vals.get("loc") != "ABCDE1234F"


def test_missing_class_x_is_unresolved_not_guessed():
    vals, unresolved = fill(Field("x10", "10th Percentage", "number", required=True))
    assert unresolved == {"x10"}


def test_self_identification_declines_by_default():
    vals, _ = fill(Field("g", "Gender", "select", options=["Male", "Female", "Prefer not to say"]),
                   Field("c", "Caste category", "select", options=["General", "OBC", "SC", "ST", "Prefer not to say"]))
    assert vals == {"g": "Prefer not to say", "c": "Prefer not to say"}


def test_self_identification_when_shared():
    data = copy.deepcopy(EXAMPLE)
    data["background"]["eeo_disclosure"] = "share"
    data["background"]["category"] = "general"
    vals, _ = fill(Field("g", "Gender", "select", options=["Male", "Female", "Prefer not to say"]),
                   Field("c", "Caste category", "select", options=["General", "OBC", "Prefer not to say"]),
                   ctx=_ctx(data))
    assert vals == {"g": "Male", "c": "General"}


@pytest.mark.parametrize("label", ["Expected CTC", "Current Location", "Email"])
def test_generic_rules_still_apply(label):
    vals, unresolved = fill(Field("x", label, "text", required=True))
    assert vals["x"] and not unresolved
