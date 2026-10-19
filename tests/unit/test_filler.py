import copy
import json
from pathlib import Path

import httpx
import pytest

from app.apply.filler import Field, FillContext, pick_option, plan, question_hash
from app.llm.client import LLM
from app.resume.recipe import Recipe
from app.vault.schema import Vault

EXAMPLE = json.loads((Path(__file__).parents[2] / "examples/vault.example.json").read_text())


@pytest.fixture
def ctx():
    v = Vault.model_validate(copy.deepcopy(EXAMPLE))
    r = Recipe(vault_revision=1, title="SDE I", company="Northwind", cover_letter="Dear team, ...",
               answers={"Expected CTC": "12 LPA"})
    return FillContext(vault=v, recipe=r, company="Northwind", title="SDE I", job_country="india",
                       resume_path="/tmp/Sample_Candidate_Resume.pdf")


def values(p):
    return {d.key: d.value for d in p.decisions}


def test_greenhouse_style_form(ctx):
    fields = [
        Field("1", "First Name", "text", required=True), Field("2", "Last Name", "text", required=True),
        Field("3", "Email", "email", required=True), Field("4", "Phone", "tel"),
        Field("5", "Resume/CV", "file", required=True), Field("6", "Cover Letter", "file"),
        Field("7", "LinkedIn Profile", "url"), Field("8", "Website", "url"),
        Field("9", "Are you legally authorized to work in India?", "select", required=True, options=["Select...", "Yes", "No"]),
        Field("10", "Will you now or in the future require visa sponsorship?", "select", required=True, options=["Yes", "No"]),
        Field("11", "How did you hear about this job?", "text"),
        Field("12", "Gender", "select", options=["Male", "Female", "Decline To Self Identify"]),
        Field("13", "Veteran Status", "select", options=["I am a veteran", "I am not a veteran", "I don't wish to answer"]),
        Field("14", "What is your notice period?", "text", required=True),
        Field("15", "Expected CTC (LPA)", "text", required=True),
    ]
    p = plan(fields, ctx)
    v = values(p)
    assert p.complete, [f.label for f in p.unresolved]
    assert (v["1"], v["2"], v["3"]) == ("Sample", "Candidate", "sample.candidate@example.com")
    assert v["5"] == "/tmp/Sample_Candidate_Resume.pdf" and "6" not in v
    assert v["7"].startswith("https://www.linkedin.com") and v["8"].startswith("https://github.com")
    assert (v["9"], v["10"]) == ("Yes", "No")
    assert v["12"] == "Decline To Self Identify" and v["13"] == "I don't wish to answer"
    assert v["14"] == "Immediate" and v["15"] == "12 LPA"


def test_lever_style_form(ctx):
    fields = [Field("n", "Full name", "text", name="name", required=True), Field("o", "Current company", "text", name="org"),
              Field("g", "GitHub URL", "url", name="urls[GitHub]"), Field("c", "Additional information", "textarea", name="comments")]
    v = values(plan(fields, ctx))
    assert v["n"] == "Sample Candidate" and v["o"] == "Acme Health Tech" and v["g"].endswith("sample-candidate")


def test_unknown_required_question_goes_assisted(ctx):
    p = plan([Field("q", "Describe a time you disagreed with your manager", "textarea", required=True)], ctx)
    assert not p.complete and p.unresolved[0].key == "q"


def test_learned_answer_is_reused(ctx):
    label = "Describe a time you disagreed with your manager"
    ctx.learned = {question_hash(label + " "): "During my internship I proposed..."}
    assert values(plan([Field("q", label, "textarea", required=True)], ctx))["q"].startswith("During my internship")


def test_work_auth_for_other_country_is_not_guessed(ctx):
    ctx.job_country = "united states"
    p = plan([Field("w", "Are you legally authorized to work in the United States?", "select",
                    required=True, options=["Yes", "No"])], ctx)
    assert not p.complete


def test_referral_fields(ctx):
    fields = [Field("r", "Referred by (employee name)", "text"), Field("h", "How did you hear about us?", "select",
              options=["LinkedIn", "Employee Referral", "Company Website"])]
    v = values(plan(fields, ctx))
    assert "r" not in v and v["h"] == "Company Website"
    ctx.referrer = "Priya Sharma"
    v = values(plan(fields, ctx))
    assert v["r"] == "Priya Sharma" and v["h"] == "Employee Referral"


def test_unanswered_standard_question_is_unresolved(ctx):
    ctx.vault = Vault.model_validate({**copy.deepcopy(EXAMPLE), "standard_answers": {}})
    assert not plan([Field("n", "Notice period (days)", "number", required=True)], ctx).complete


def test_llm_answer_used_only_when_confident_and_truthful(ctx):
    def llm(reply):
        body = {"content": [{"type": "text", "text": json.dumps(reply)}]}
        return LLM("anthropic", "k", "https://x", "c", "s", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    q = Field("q", "Which database have you used the most?", "text", required=True)
    ctx.llm = llm({"answer": "PostgreSQL, in several projects.", "confidence": 0.9})
    assert values(plan([q], ctx))["q"].startswith("PostgreSQL")
    ctx.llm = llm({"answer": "Oracle for 5 years.", "confidence": 0.95})      # untraceable skill + years
    assert not plan([q], ctx).complete
    ctx.llm = llm({"answer": "PostgreSQL", "confidence": 0.5})                 # not confident
    assert not plan([q], ctx).complete


def test_pick_option():
    assert pick_option(["Select...", "Yes", "No"], "yes") == "Yes"
    assert pick_option(["I am authorized", "I am not authorized"], "No") == "I am not authorized"
    assert pick_option(["B.Tech", "M.Tech"], "B.Tech") == "B.Tech"
    assert pick_option(["Red", "Blue"], "Green") is None
