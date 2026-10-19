import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ingest.models import RawJob
from app.ingest.normalize import families, normalize, parse_experience, parse_grad_years, parse_salary
from app.ingest.scoring import (DEFAULT_WEIGHTS, bucket_of, experience_fit, freshness, hard_filter,
                                location_ok, prep_days, profile_from_vault, referral_worthy, score_job)
from app.util.hashing import company_key, h64
from app.vault.schema import Vault

ROOT = Path(__file__).parents[2]
EXAMPLE = json.loads((ROOT / "examples/vault.example.json").read_text())
JD = (ROOT / "examples/jd.example.txt").read_text()
NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


@pytest.fixture
def profile():
    v = Vault.model_validate(copy.deepcopy(EXAMPLE))
    return profile_from_vault(uuid.uuid4(), v, 1)


def raw(**kw) -> RawJob:
    base = dict(source="greenhouse", ats="greenhouse", company="Northwind Payments",
                title="Software Development Engineer I", url="https://boards.greenhouse.io/nw/jobs/1",
                req_id="R1", description=JD, description_is_html=False, location="Bengaluru, India",
                posted_at=NOW - timedelta(hours=5))
    base.update(kw)
    return RawJob(**base)


def job(**kw):
    return normalize(raw(**kw), today=NOW)


# --- normalization ----------------------------------------------------------------
def test_normalize_core_fields():
    j = job()
    assert j.company_key == "northwind payments" and j.families == {"swe"}
    assert j.grad_years == [2026, 2027]
    assert j.remote_type == "hybrid"
    assert {"python", "fastapi", "postgresql"} <= set(j.skills)
    assert "kafka" in j.nice and "python" in j.must
    assert 0 < len(j.summary) <= 600 and "Northwind" in j.summary
    assert j.exp_min == 0 and j.junior


def test_canonical_matches_across_portals():
    direct = job()
    via_jsearch = job(source="jsearch", ats="greenhouse", url="https://boards.greenhouse.io/nw/jobs/1?gh_src=li")
    assert direct.canonical_hash == via_jsearch.canonical_hash


def test_fingerprint_catches_unlinked_duplicate():
    a = job()
    b = job(source="adzuna", ats="unknown", req_id="adz-9", url="https://adzuna.in/x/9",
            title="Software Development Engineer I (JR12345)", company="Northwind Payments Pvt Ltd",
            location="Bangalore, India")
    assert a.canonical_hash != b.canonical_hash and a.fingerprint == b.fingerprint


def test_salary_normalized_to_lpa():
    assert job(description="Pay: ₹12-18 LPA").salary_lpa_max == 18.0
    usd = job(salary_min=100000, salary_max=120000, salary_currency="usd", salary_period="year")
    assert usd.salary_currency == "USD" and usd.salary_lpa_max == round(120000 * 84 / 1e5, 2)
    monthly = job(description="Stipend ₹50,000 - ₹70,000 per month")
    assert monthly.salary_max == 840000


def test_parsers_edge_cases():
    assert parse_salary("$10-$20") == (None, None, None, None)
    assert parse_experience("Staff Engineer", "10+ years of experience") == (10, None)
    assert parse_experience("x", "", months=0) == (0, 1)
    assert parse_grad_years("We were founded in 2015 and raised in 2024") == []
    assert families("Account Executive") == set() and families("Sales Engineer") == set()


# --- hard filters -----------------------------------------------------------------
@pytest.mark.parametrize("kw,reason", [
    (dict(title="Senior Software Engineer"), "experience level"),
    (dict(description=JD.replace("graduating in 2026 or 2027", "5+ years of experience")), "experience level"),
    (dict(title="Account Executive"), "role type"),
    (dict(location="San Francisco, CA", description="Build Python APIs onsite"), "location"),
    (dict(location="Remote - US only", workplace="remote"), "location"),
    (dict(description=JD.replace("2026 or 2027", "2024 or 2025")), "graduation year"),
    (dict(description=JD + "\nCTC: 3-4 LPA"), "below min CTC"),
])
def test_hard_filter_rejects(profile, kw, reason):
    assert hard_filter(profile, job(**kw)) == reason


def test_hard_filter_passes_and_blacklist(profile):
    assert hard_filter(profile, job()) is None
    assert hard_filter(profile, job(location="Remote - India", workplace="remote")) is None
    profile.blacklist = {h64(company_key("Northwind Payments"))}
    assert hard_filter(profile, job()) == "blacklisted company"


def test_unknown_location_passes(profile):
    assert location_ok(profile, job(location=None))


def test_work_mode_filter(profile):
    profile.remote_types = {"remote"}
    assert hard_filter(profile, job()) == "work mode hybrid"


# --- components / buckets ---------------------------------------------------------
def test_strong_match_is_high(profile):
    sc = score_job(profile, job(), source_quality=0.9, company_jobs_7d=3, learned_rate=None, now=NOW)
    assert sc.bucket == 3, sc
    assert sc.components["skills"] > 0.8 and sc.components["freshness"] == 1.0
    assert len(sc.component_ints()) == 6 and all(0 <= c <= 1000 for c in sc.component_ints())


def test_skill_gap_lowers_score_and_lists_missing(profile):
    rust = ("Requirements:\n- Rust\n- Kubernetes\n- Terraform\n- Scala\n- Haskell\nYou will build infra in Rust. "
            "0-2 years of experience.")
    sc = score_job(profile, job(description=rust), source_quality=0.9, company_jobs_7d=0,
                   learned_rate=None, now=NOW)
    assert sc.score < 0.65 and {"rust", "kubernetes"} <= set(sc.missing)
    assert prep_days(["rust", "kubernetes"]) == 10


def test_freshness_curve():
    assert freshness(job(posted_at=NOW - timedelta(hours=47)), NOW) == 1.0
    assert freshness(job(posted_at=NOW - timedelta(days=8)), NOW) == pytest.approx(0.6, abs=0.01)
    assert freshness(job(posted_at=NOW - timedelta(days=40)), NOW) == 0.2
    assert freshness(job(posted_at=None), NOW) == 0.5


def test_experience_fit_fresher(profile):
    assert experience_fit(profile, job()) == 1.0
    assert experience_fit(profile, job(title="Backend Engineer", description="2 years of experience in Go")) == 0.6


def test_aggregator_scores_below_company_site(profile):
    a = score_job(profile, job(), source_quality=0.9, company_jobs_7d=0, learned_rate=None, now=NOW)
    b = score_job(profile, job(), source_quality=0.5, company_jobs_7d=0, learned_rate=None, now=NOW)
    assert a.score - b.score == pytest.approx(0.04, abs=1e-3)


def test_referral_multiplier_caps(profile):
    sc = score_job(profile, job(), source_quality=0.9, company_jobs_7d=3, learned_rate=1.0, now=NOW,
                   referral_obtained=True)
    assert sc.score == 1.0


def test_buckets_and_min_score_override(profile):
    assert [bucket_of(x) for x in (0.85, 0.7, 0.62, 0.55, 0.3)] == [3, 2, 1, 0, -1]
    profile.min_score = 0.9
    sc = score_job(profile, job(), source_quality=0.9, company_jobs_7d=3, learned_rate=None, now=NOW)
    assert sc.bucket == 0       # below the user's own MIN_SCORE -> Stretch only


def test_tuned_weights_used():
    v = Vault.model_validate(copy.deepcopy(EXAMPLE))
    w = dict(DEFAULT_WEIGHTS, skills=0.2, freshness=0.35)
    p = profile_from_vault(uuid.uuid4(), v, 1, {"weights": w})
    assert p.weights["freshness"] == 0.35
    bad = profile_from_vault(uuid.uuid4(), v, 1, {"weights": {"skills": 1}})
    assert bad.weights == DEFAULT_WEIGHTS


def test_referral_worthiness(profile):
    sc = score_job(profile, job(), source_quality=0.9, company_jobs_7d=3, learned_rate=None, now=NOW)
    assert referral_worthy(sc, employee_count=5000, starred=False, referral_driven=False, open_jobs=0)
    assert referral_worthy(sc, employee_count=None, starred=False, referral_driven=False, open_jobs=25)
    assert not referral_worthy(sc, employee_count=50, starred=False, referral_driven=False, open_jobs=25)
    assert referral_worthy(sc, employee_count=50, starred=True, referral_driven=False, open_jobs=0)
    sc.bucket = 2
    assert not referral_worthy(sc, employee_count=5000, starred=True, referral_driven=True, open_jobs=99)


def test_similarity_calibration(profile):
    """Pins the measured cosine range that SIM_SATURATION is based on."""
    from app.embed import cosine, embed
    strong = cosine(profile.embedding, embed(JD))
    unrelated = cosine(profile.embedding, embed("Account executive growing an enterprise sales pipeline, CRM, quota"))
    assert 0.15 < strong < 0.35 and unrelated < 0.08
