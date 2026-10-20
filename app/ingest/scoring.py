"""Hard filters and the success score (section 6, steps 3-5).

Pure functions over a UserProfile and a NormalizedJob; the DB-backed
context (source quality, company velocity, learned conversion, stars) is
passed in, so all of this is unit-testable.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import get_settings
from app.embed import cosine, embed
from app.ingest.normalize import ROLE_FAMILIES, NormalizedJob, families
from app.util.hashing import company_key, h64, normalize_location
from app.util.skills import norm_skill
from app.vault.facts import vault_facts
from app.vault.schema import Vault

COMPONENTS = ("skills", "experience", "freshness", "source", "velocity", "learned")
DEFAULT_WEIGHTS = {"skills": 0.40, "experience": 0.20, "freshness": 0.15, "source": 0.10,
                   "velocity": 0.05, "learned": 0.10}
INDIA_HINTS = ("india", "bengaluru", "bangalore", "delhi", "gurugram", "gurgaon", "noida", "hyderabad",
               "pune", "mumbai", "chennai", "kolkata", "ahmedabad", "jaipur", "kochi", "indore",
               "chandigarh", "coimbatore", "trivandrum", "thiruvananthapuram", "nagpur", "remote - india")
FOREIGN_HINTS = ("united states", "usa", "us only", "u.s.", "canada", "united kingdom", "uk only", "london",
                 "germany", "berlin", "france", "paris", "netherlands", "amsterdam", "singapore", "australia",
                 "new york", "san francisco", "seattle", "austin", "toronto", "dublin", "europe", "emea",
                 "japan", "tokyo", "brazil", "mexico", "poland", "spain", "israel", "dubai")
SIM_SATURATION = 0.22
LANGUAGES = {"python", "java", "javascript", "typescript", "go", "rust", "c", "c++", "c#", "kotlin",
             "swift", "scala", "ruby", "php", "dart", "elixir", "haskell"}


@dataclass
class UserProfile:
    user_id: object
    vault_revision: int
    families: set[str]
    locations: list[str]                 # normalized
    remote_types: set[str]
    min_ctc_lpa: float | None
    level: str                           # fresher | junior | mid
    grad_year: int
    blacklist: set[int]                  # company_key_hash
    skills: set[str]                     # normalized vault skills
    terms: tuple[str, ...]               # vault spellings, for detection
    embedding: list[float]
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    min_score: float = 0.60
    starred: set[int] = field(default_factory=set)
    country: str = "india"
    settings: dict = field(default_factory=dict)          # users.settings (auto_apply, outreach, paused...)
    timezone: str = "Asia/Kolkata"


def profile_from_vault(user_id, v: Vault, revision: int, settings: dict | None = None,
                       starred: set[int] | None = None) -> UserProfile:
    from app.resume.select import vault_terms
    cfg = get_settings()
    settings = settings or {}
    fams: set[str] = set()
    for rt in v.all_role_types:
        fams |= families(rt) or {f for f, pat in ROLE_FAMILIES.items() if re.search(pat, rt.lower())}
    if not fams:
        fams = {"swe"}
    profile_text = " ".join([*(s for g in v.skills for s in g.items), *v.extra_keywords,
                             *(b.text for p in v.projects for b in p.bullets),
                             *(b.text for e in v.experience for b in e.bullets)])
    weights = dict(DEFAULT_WEIGHTS)
    tuned = settings.get("weights")
    if isinstance(tuned, dict) and set(tuned) == set(COMPONENTS):
        weights = {k: float(tuned[k]) for k in COMPONENTS}
    return UserProfile(
        user_id=user_id, vault_revision=revision, families=fams,
        locations=[normalize_location(l) for l in v.expectations.locations],
        remote_types=set(v.expectations.remote_types), min_ctc_lpa=v.expectations.min_ctc_lpa,
        level=v.expectations.experience_level, grad_year=v.graduation_year,
        blacklist={h64(company_key(c)) for c in v.expectations.blacklisted_companies},
        skills=vault_facts(v).skills, terms=vault_terms(v), embedding=embed(profile_text),
        weights=weights, min_score=float(settings.get("min_score", cfg.min_score)),
        starred=starred or set(), country=v.personal.location.country.lower(), settings=settings,
    )


# --- hard filters -------------------------------------------------------------------
def location_ok(p: UserProfile, job: NormalizedJob) -> bool:
    loc = job.location_norm
    wants_remote = "remote" in p.remote_types or any("remote" in l for l in p.locations)
    home = p.country
    if job.remote_type == "remote" and wants_remote:
        # remote, but restricted to another country?
        if any(h in loc for h in FOREIGN_HINTS) and home not in loc and not re.search(r"anywhere|worldwide|global|apac", loc):
            return False
        return True
    if not loc:
        return True                       # unknown location: let scoring decide
    for want in p.locations:
        if want and want != "remote" and want in loc:
            return True
    if home and home in p.locations and home in loc:
        return True
    return False


def hard_filter(p: UserProfile, job: NormalizedJob) -> str | None:
    """None if the job passes, else the reason it was rejected."""
    if not job.families or not (job.families & p.families):
        return "role type"
    if job.company_key_hash in p.blacklist:
        return "blacklisted company"
    if job.remote_type != "unknown" and job.remote_type not in p.remote_types:
        return f"work mode {job.remote_type}"
    if not location_ok(p, job):
        return "location"
    if p.min_ctc_lpa and job.salary_lpa_max is not None and job.salary_lpa_max < p.min_ctc_lpa:
        return "below min CTC"
    if job.grad_years and p.grad_year not in job.grad_years:
        return "graduation year"
    if p.level == "fresher":
        if job.senior or (job.exp_min is not None and job.exp_min >= 3):
            return "experience level"
    elif p.level == "junior":
        if re.search(r"\b(staff|principal|director|head of|vp)\b", job.title, re.I) or (job.exp_min or 0) >= 5:
            return "experience level"
    return None


# --- components ------------------------------------------------------------------
def skill_coverage(p: UserProfile, job: NormalizedJob) -> tuple[float, list[str]]:
    must = [norm_skill(s) for s in job.must][:12] or [norm_skill(s) for s in job.skills][:12]
    missing = [s for s in must if s not in p.skills]
    hits = (len(must) - len(missing)) / len(must) if must else 0.5
    sim = cosine(p.embedding, embed(f"{job.title}\n{job.text[:6000]}"))
    # Hashing embeddings put strong profile-JD matches near 0.2 and unrelated
    # roles near 0 (measured in tests/unit/test_scoring.py), so saturate at 0.22.
    sim_score = min(max(sim / SIM_SATURATION, 0.0), 1.0)
    return 0.7 * hits + 0.3 * sim_score, missing


def experience_fit(p: UserProfile, job: NormalizedJob) -> float:
    lo = job.exp_min
    if p.level == "mid":
        target = 3
        return 1.0 if lo is None else max(0.0, 1 - abs(lo - target) / 5)
    if job.junior:
        return 1.0
    if lo is None:
        return 0.55 if not job.senior else 0.1
    if p.level == "junior":
        return {0: 0.9, 1: 1.0, 2: 1.0, 3: 0.7, 4: 0.4}.get(lo, 0.1)
    return {0: 1.0, 1: 0.85, 2: 0.6}.get(lo, 0.05)          # fresher


def freshness(job: NormalizedJob, now: datetime) -> float:
    if job.posted_at is None:
        return 0.5
    hours = max(0.0, (now - job.posted_at).total_seconds() / 3600)
    if hours <= 48:
        return 1.0
    days = hours / 24
    return max(0.2, 1.0 - (days - 2) * (0.8 / 12))             # 0.2 floor at 14 days


def velocity(matching_jobs_7d: int) -> float:
    return min(1.0, math.log1p(matching_jobs_7d) / math.log(20))


def learned(rate: float | None) -> float:
    """Smoothed positive rate for similar jobs (source x bucket), as a 0-1
    score where 0.5 = average. None (no data yet) -> 0.5."""
    if rate is None:
        return 0.5
    return min(1.0, max(0.0, rate))


def bucket_of(score: float) -> int:
    cfg = get_settings()
    if score >= cfg.bucket_high:
        return 3
    if score >= cfg.bucket_medium:
        return 2
    if score >= cfg.min_score:
        return 1
    if score >= cfg.stretch_min:
        return 0
    return -1


BUCKET_NAMES = {3: "high", 2: "medium", 1: "low", 0: "stretch", -1: "below"}


@dataclass
class Scored:
    score: float
    bucket: int
    components: dict[str, float]
    missing: list[str]
    referral_worthy: bool = False

    def component_ints(self) -> list[int]:
        return [int(round(self.components[c] * 1000)) for c in COMPONENTS]


def score_job(p: UserProfile, job: NormalizedJob, *, source_quality: float, company_jobs_7d: int,
              learned_rate: float | None, now: datetime | None = None,
              referral_obtained: bool = False) -> Scored:
    now = now or datetime.now(timezone.utc)
    cov, missing = skill_coverage(p, job)
    comps = {
        "skills": cov,
        "experience": experience_fit(p, job),
        "freshness": freshness(job, now),
        "source": min(max(source_quality, 0.0), 1.0),
        "velocity": velocity(company_jobs_7d),
        "learned": learned(learned_rate),
    }
    total = sum(p.weights[k] * comps[k] for k in COMPONENTS) / max(sum(p.weights.values()), 1e-9)
    if referral_obtained:
        total = min(1.0, total * get_settings().referral_multiplier)
    total = round(total, 4)
    b = bucket_of(total)
    if b >= 1 and total < p.min_score:          # per-user MIN_SCORE override (C10)
        b = 0 if total >= get_settings().stretch_min else -1
    return Scored(score=total, bucket=b, components=comps, missing=missing)


def referral_worthy(scored: Scored, *, employee_count: int | None, starred: bool, referral_driven: bool,
                    open_jobs: int) -> bool:
    """Section 6.5: High bucket AND (size > 200 or starred or referral-driven).
    Without size data, >= 20 open postings is used as a size proxy."""
    if scored.bucket != 3:
        return False
    big = (employee_count or 0) > 200 or (employee_count is None and open_jobs >= 20)
    return big or starred or referral_driven


def prep_days(missing: list[str]) -> int:
    """Rough prep estimate for Stretch jobs: languages take longer than tools."""
    return sum(7 if s in LANGUAGES else 3 for s in missing)
