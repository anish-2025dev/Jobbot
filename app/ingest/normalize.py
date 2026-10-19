"""Normalize a RawJob (section 6, step 1).

Produces everything scoring, dedupe and storage need. The full description
text is kept on the object for scoring and then discarded (C3); only the
summary, skills and hashes are persisted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.ingest.models import RawJob
from app.util.hashing import (canonical_hash, company_key, fingerprint, h64,
                              normalize_location, simhash)
from app.util.skills import extract_skills, must_have_skills
from app.util.text import html_to_text, summarize

# Approximate INR per unit; only used to compare against min CTC (an
# estimate is fine for a hard filter that only rejects clear misses).
FX_TO_INR = {"INR": 1.0, "USD": 84.0, "EUR": 91.0, "GBP": 108.0, "SGD": 63.0, "AED": 23.0,
             "CAD": 61.0, "AUD": 55.0}

ROLE_FAMILIES = {
    "swe": r"software|sde\b|swe\b|developer|programmer|backend|back-end|back end|frontend|front-end|"
           r"front end|full[- ]?stack|application engineer|platform engineer|web engineer|"
           r"member of technical staff|mts\b|engineer (i|1|ii|2)\b|graduate engineer|engineering intern|"
           r"java|python|golang|\.net|node",
    "data": r"data engineer|analytics engineer|etl|big data|data platform|bi engineer|data analyst",
    "ml": r"machine learning|\bml\b|ai engineer|deep learning|nlp|computer vision|data scientist|applied scientist",
    "devops": r"devops|site reliability|\bsre\b|infrastructure|cloud engineer|platform ops|kubernetes",
    "security": r"security|soc analyst|penetration|appsec|cyber",
    "qa": r"\bqa\b|quality|sdet|test engineer|automation engineer",
    "mobile": r"android|ios\b|mobile|flutter|react native",
}
NON_TECH = re.compile(r"\b(sales|marketing|recruit|talent acquisition|hr\b|human resources|account manager|"
                      r"customer success|support specialist|finance|legal|counsel|designer|copywriter|"
                      r"business development|operations manager|office|executive assistant)\b", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|architect|vp|"
                    r"distinguished|fellow)\b", re.I)
JUNIOR = re.compile(r"\b(intern|internship|trainee|graduate|new grad|fresher|entry[- ]level|junior|"
                    r"associate|sde[- ]?(i|1)\b|engineer (i|1)\b|level 1|l1\b)\b", re.I)


@dataclass
class NormalizedJob:
    raw: RawJob
    text: str                                  # full JD text, in memory only (C3)
    company: str
    company_key: str
    company_key_hash: int
    canonical_hash: int
    fingerprint: int
    simhash: int
    title: str
    location: str | None
    location_norm: str
    remote_type: str                           # onsite | hybrid | remote | unknown
    exp_min: int | None
    exp_max: int | None
    grad_years: list[int]
    salary_min: int | None                     # annual, in salary_currency
    salary_max: int | None
    salary_currency: str | None
    salary_lpa_max: float | None               # annual max in INR lakhs (for min-CTC filter)
    skills: list[str]
    must: list[str]
    nice: list[str]
    summary: str
    families: set[str]
    posted_at: datetime | None
    senior: bool
    junior: bool
    extra: dict = field(default_factory=dict)


# --- salary -------------------------------------------------------------------------
_NUM = r"(\d+(?:[.,]\d+)*)"
_LPA = re.compile(rf"(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:-|–|to)\s*{_NUM}\s*(lpa|lakhs?|lacs?|l\b|cr|crores?)", re.I)
_LPA_ONE = re.compile(rf"(?:₹|rs\.?|inr)?\s*{_NUM}\s*(lpa|lakhs? per annum|lakhs?|lacs?)\b", re.I)
_CUR_RANGE = re.compile(rf"(₹|\$|€|£|inr|usd|eur|gbp)\s*{_NUM}\s*(k)?\s*(?:-|–|to)\s*(?:₹|\$|€|£|inr|usd|eur|gbp)?\s*{_NUM}\s*(k)?"
                        r"(?:\s*(?:per|/|a)\s*(year|annum|yr|month|mo|hour|hr))?", re.I)
_SYM = {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP", "inr": "INR", "usd": "USD", "eur": "EUR", "gbp": "GBP"}


def _f(x: str) -> float:
    return float(x.replace(",", ""))


def parse_salary(text: str) -> tuple[float | None, float | None, str | None, str | None]:
    """(min, max, currency, period) from free text; None if not found."""
    if not text:
        return None, None, None, None
    m = _LPA.search(text)
    if m:
        mult = 1e7 if m.group(3).lower().startswith("cr") else 1e5
        return _f(m.group(1)) * mult, _f(m.group(2)) * mult, "INR", "year"
    m = _CUR_RANGE.search(text)
    if m:
        cur = _SYM[m.group(1).lower()]
        lo, hi = _f(m.group(2)) * (1000 if m.group(3) else 1), _f(m.group(4)) * (1000 if m.group(5) else 1)
        per = (m.group(6) or "").lower()
        period = "month" if per.startswith("mo") else "hour" if per.startswith("h") else "year"
        if lo < 1000 and hi < 1000 and period == "year" and cur != "INR":
            return None, None, None, None      # "$10 - $20" without unit: too ambiguous
        return lo, hi, cur, period
    m = _LPA_ONE.search(text)
    if m:
        v = _f(m.group(1)) * 1e5
        return v, v, "INR", "year"
    return None, None, None, None


def annualize(value: float | None, period: str | None) -> float | None:
    if value is None:
        return None
    return value * {"month": 12, "hour": 2080, "week": 52, "day": 260}.get(period or "year", 1)


# --- experience / batch ------------------------------------------------------------
_EXP_RANGE = re.compile(r"(\d{1,2})\s*(?:\+\s*)?(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_EXP_PLUS = re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?)|(?:minimum|min\.?|at least)\s*(\d{1,2})\s*(?:years?|yrs?)"
                       r"|(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s+)?(?:\w+\s+){0,3}experience", re.I)
_FRESHER = re.compile(r"\b(fresher|freshers|new grad|recent graduate|entry[- ]level|no experience required|"
                      r"0\s*years?)\b", re.I)


def parse_experience(title: str, text: str, months: int | None = None) -> tuple[int | None, int | None]:
    if months is not None:
        y = int(months // 12)
        return y, None if y else 1
    blob = f"{title}\n{text}"
    m = _EXP_RANGE.search(blob)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= hi <= 30:
            return lo, hi
    m = _EXP_PLUS.search(blob)
    if m:
        v = int(next(g for g in m.groups() if g))
        if v <= 30:
            return v, None
    if _FRESHER.search(blob) or JUNIOR.search(title):
        return 0, 1
    return None, None


_YEAR = r"(20\d{2})"
_BATCH = re.compile(rf"(?:batch(?:es)?|class|graduat\w*|pass[- ]?outs?|passing out|graduating in|year of passing|yop)"
                    rf"[^.\n]{{0,40}}?({_YEAR}(?:\s*(?:/|,|&|and|or|-|–)\s*{_YEAR})*)"
                    rf"|({_YEAR}(?:\s*(?:/|,|&|and|or|-|–)\s*{_YEAR})*)\s*(?:batch|graduates|pass[- ]?outs?|grads)", re.I)


def parse_grad_years(text: str, today: datetime | None = None) -> list[int]:
    """Eligible graduation years ('2025/2026 batch', 'graduating in 2027')."""
    year = (today or datetime.now(timezone.utc)).year
    found: set[int] = set()
    for m in _BATCH.finditer(text or ""):
        span = m.group(1) or m.group(4) or ""
        ys = [int(y) for y in re.findall(_YEAR, span)]
        if len(ys) == 2 and re.search(r"\d\s*(?:-|–)\s*\d", span) and ys[1] - ys[0] <= 4:
            ys = list(range(ys[0], ys[1] + 1))
        found |= {y for y in ys if year - 4 <= y <= year + 3}
    return sorted(found)


# --- location / work mode -------------------------------------------------------------
def remote_type(raw: RawJob, text: str) -> str:
    if raw.workplace in ("remote", "hybrid", "onsite"):
        return raw.workplace
    blob = f"{raw.location or ''} {raw.title}".lower()
    if re.search(r"\bhybrid\b", blob):
        return "hybrid"
    if re.search(r"\bremote\b|work from home|\bwfh\b|anywhere", blob):
        return "remote"
    body = text[:3000].lower()
    if re.search(r"\bhybrid\b", body):
        return "hybrid"
    if re.search(r"\b(fully remote|100% remote|remote[- ]first|remote position|work from home)\b", body):
        return "remote"
    if re.search(r"\b(on-?site|in[- ]office|work from office|wfo)\b", body):
        return "onsite"
    return "unknown"


def families(title: str) -> set[str]:
    t = title.lower()
    if NON_TECH.search(t) and not re.search(r"engineer|developer", t):
        return set()
    return {fam for fam, pat in ROLE_FAMILIES.items() if re.search(pat, t)}


def normalize(raw: RawJob, *, today: datetime | None = None, vault_terms: tuple[str, ...] = ()) -> NormalizedJob:
    text = html_to_text(raw.description) if raw.description_is_html else (raw.description or "")
    ckey = company_key(raw.company)
    title = re.sub(r"\s+", " ", raw.title).strip()
    loc = raw.location or (raw.locations[0] if raw.locations else None)
    all_locs = "; ".join([loc or ""] + [l for l in raw.locations if l and l != loc])

    smin, smax, cur, period = raw.salary_min, raw.salary_max, raw.salary_currency, raw.salary_period
    if smin is None and smax is None:
        smin, smax, cur, period = parse_salary(f"{raw.salary_text or ''}\n{text[:4000]}")
    smin, smax = annualize(smin, period), annualize(smax, period)
    cur = (cur or "").upper() or None
    lpa = None
    if (smax or smin) and cur in FX_TO_INR:
        lpa = round((smax or smin) * FX_TO_INR[cur] / 1e5, 2)

    exp_min, exp_max = parse_experience(title, text, raw.experience_months)
    must, nice = must_have_skills(text, vault_terms)
    skills = extract_skills(f"{title}\n{text}", vault_terms)
    return NormalizedJob(
        raw=raw, text=text, company=raw.company.strip(), company_key=ckey, company_key_hash=h64(ckey),
        canonical_hash=canonical_hash(raw.ats, raw.company, raw.req_id, raw.url),
        fingerprint=fingerprint(raw.company, title, loc),
        simhash=simhash(text) if len(text) > 200 else simhash(f"{title} {raw.company} {text}"),
        title=title, location=all_locs.strip("; ") or None, location_norm=normalize_location(all_locs),
        remote_type=remote_type(raw, text), exp_min=exp_min, exp_max=exp_max,
        grad_years=parse_grad_years(f"{title}\n{text}", today),
        salary_min=int(smin) if smin else None, salary_max=int(smax) if smax else None,
        salary_currency=cur, salary_lpa_max=lpa, skills=skills, must=must, nice=nice,
        summary=summarize(text) or title, families=families(title),
        posted_at=raw.posted_at, senior=bool(SENIOR.search(title)), junior=bool(JUNIOR.search(title)),
    )
