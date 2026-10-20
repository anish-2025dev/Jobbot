"""Decide what goes in every form field (section 8.3).

Pure logic: given the fields an adapter found on the page, produce a fill
plan from the vault, standard answers, the recipe (cover letter, custom
answers), learned form_answers, and, only above a confidence threshold, the
cheap LLM. Every value is the user's own information; anything unknown and
required makes the application Assisted instead of guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.llm.client import LLM, LLMError, LLMUnavailable
from app.resume.recipe import Recipe
from app.resume.validator import experience_years
from app.util.hashing import h64
from app.vault.schema import Vault

LLM_MIN_CONFIDENCE = 0.8


@dataclass
class Field:
    key: str                         # adapter handle (e.g. data-jobbot-idx)
    label: str
    type: str                        # text | email | tel | textarea | select | radio | checkbox | file | number | url | date
    name: str = ""
    required: bool = False
    options: list[str] = field(default_factory=list)
    multiple: bool = False


@dataclass
class Decision:
    key: str
    value: str | bool | None
    confidence: float
    source: str                      # vault | standard | recipe | learned | llm | default | resume


@dataclass
class FillPlan:
    decisions: list[Decision] = field(default_factory=list)
    unresolved: list[Field] = field(default_factory=list)      # required but unknown -> Assisted
    skipped: list[Field] = field(default_factory=list)          # optional and unknown -> left blank

    @property
    def complete(self) -> bool:
        return not self.unresolved


@dataclass
class FillContext:
    vault: Vault
    recipe: Recipe | None = None
    company: str = ""
    title: str = ""
    job_country: str = "india"
    referrer: str | None = None
    referrer_email: str | None = None
    learned: dict[int, str] = field(default_factory=dict)       # question_hash -> answer
    llm: LLM | None = None
    resume_path: str | None = None


def question_hash(label: str) -> int:
    return h64(re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", label.lower())).strip())


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def _yes_no(flag: bool | None) -> str | None:
    return None if flag is None else ("Yes" if flag else "No")


def pick_option(options: list[str], value: str) -> str | None:
    """Best option for a value: exact, then yes/no semantics, then containment."""
    if not options or value is None:
        return None
    v = _norm(str(value))
    opts = [(o, _norm(o)) for o in options if _norm(o) and not _norm(o).startswith("select")]
    for o, n in opts:
        if n == v:
            return o
    if v in ("yes", "no"):
        for o, n in opts:
            if n.split()[:1] == [v] or (v == "yes" and n.startswith(("i am", "i do", "true"))) \
                    or (v == "no" and n.startswith(("i am not", "i do not", "false", "none"))):
                return o
    for o, n in opts:
        if v and (v in n or n in v):
            return o
    return None


DECLINE = re.compile(r"decline|prefer not|don.?t wish|do not wish|not to (say|answer|disclose)|rather not|choose not", re.I)


def _first_last(full: str) -> tuple[str, str]:
    parts = full.split()
    return (parts[0], " ".join(parts[1:]) if len(parts) > 1 else "")


def _link(v: Vault, word: str) -> str | None:
    for l in v.personal.links:
        if word in l.label.lower() or word in l.url.lower():
            return l.url
    return None


def _phone_parts(phone: str) -> tuple[str, str]:
    digits = re.sub(r"[^\d+]", "", phone)
    if digits.startswith("+91"):
        return "+91", digits[3:]
    if digits.startswith("+"):
        return digits[:3], digits[3:]
    return "", digits


def standard_value(f: Field, ctx: FillContext) -> Decision | None:
    """Known, deterministic answers. Order matters: specific before generic."""
    v = ctx.vault
    p, sa = v.personal, v.standard_answers
    label = _norm(f"{f.label} {f.name}")
    first, last = _first_last(p.full_name)
    edu = max(v.higher_education(), key=lambda e: e.graduation_year)
    latest = max(v.experience, key=lambda e: (e.end or "9999-99", e.start)) if v.experience else None
    cc, local_phone = _phone_parts(p.phone)

    def d(val, conf=0.97, src="vault"):
        return Decision(f.key, val, conf, src) if val not in (None, "") else None

    if f.type == "file":
        if re.search(r"cover", label):
            return Decision(f.key, None, 0.9, "skip-cover-file")      # cover letter goes in the text field if any
        return d(ctx.resume_path, 0.99, "resume") if re.search(r"resume|cv|curriculum", label) or f.required else None
    rules: list[tuple[str, object]] = [
        # referral fields first: "Referred by (employee name)" must not match the name rules
        (r"(referred|referral|referrer)( by| name|s name)?(?!.*how did)|employee who referred", lambda:
            d(ctx.referrer, 0.95, "referral") if ctx.referrer and "mail" not in label
            else d(ctx.referrer_email, 0.95, "referral") if ctx.referrer_email and "mail" in label
            else Decision(f.key, None, 0.9, "no-referral")),
        (r"\bfirst name|given name|\bfname\b|firstname", lambda: d(first)),
        (r"\blast name|family name|surname|\blname\b|lastname", lambda: d(last)),
        (r"preferred name", lambda: d(first)),
        (r"(full |legal )?name\b(?!.*(company|employer|school|university|referr|recruit|manager))", lambda: d(p.full_name)),
        (r"e ?mail", lambda: d(p.email) if "referr" not in label else d(ctx.referrer_email, 0.95, "referral")),
        (r"country code|dialing code", lambda: d(cc or "+91")),
        (r"phone|mobile|contact number|cell", lambda: d(p.phone if f.type != "number" else local_phone)),
        (r"linkedin", lambda: d(_link(v, "linkedin"))),
        (r"github", lambda: d(_link(v, "github"))),
        (r"portfolio|website|personal (site|url)|blog", lambda: d(_link(v, "portfolio") or _link(v, "github"), 0.85)),
        (r"how did you (hear|find|learn)|source of application|where did you (hear|find)", lambda:
            d(pick_option(f.options, "Employee referral") if ctx.referrer and f.options else
              ("Employee referral" if ctx.referrer else (pick_option(f.options, "Company website") or
               pick_option(f.options, "Careers page") or pick_option(f.options, "Job board") or
               ("Company careers page" if not f.options else None))), 0.85, "default")),
        (r"current (company|employer)|most recent (company|employer)|employer name", lambda:
            d(latest.company if latest else "N/A (student)", 0.9)),
        (r"current (title|role|designation)|job title|most recent title", lambda:
            d(latest.title if latest else "Student", 0.9)),
        (r"school|university|college|institution", lambda: d(edu.institution)),
        (r"degree|qualification", lambda: d(pick_option(f.options, edu.degree) if f.options else edu.degree, 0.9)),
        (r"discipline|major|field of study|specialization|branch", lambda: d(edu.field_of_study, 0.9)),
        (r"graduation (year|date)|year of (graduation|passing)|passing year|batch", lambda: d(str(edu.graduation_year))),
        (r"\bgpa\b|cgpa|grade|percentage|marks", lambda: d(re.sub(r"^\s*c?gpa\s*", "", edu.grade or "", flags=re.I) or None, 0.9)),
        (r"years of (professional |relevant |total )?experience|total experience|experience in years", lambda:
            d(str(int(experience_years(v))), 0.9, "vault")),
        (r"expected (ctc|salary|compensation|pay)|salary expectation|desired salary", lambda:
            d(f"{sa.expected_ctc_lpa:g} LPA" if sa.expected_ctc_lpa is not None and f.type != "number"
              else (str(int(sa.expected_ctc_lpa * 100000)) if sa.expected_ctc_lpa is not None else None), 0.95, "standard")),
        (r"current (ctc|salary|compensation)", lambda:
            d(f"{sa.current_ctc_lpa:g} LPA" if sa.current_ctc_lpa is not None and f.type != "number"
              else (str(int(sa.current_ctc_lpa * 100000)) if sa.current_ctc_lpa is not None else None), 0.95, "standard")),
        (r"notice period", lambda: d(("Immediate" if sa.notice_period_days == 0 else f"{sa.notice_period_days} days")
                                     if sa.notice_period_days is not None and f.type != "number"
                                     else (str(sa.notice_period_days) if sa.notice_period_days is not None else None),
                                     0.95, "standard")),
        (r"(earliest|available|availability) (start|to start|date)|start date|when can you (start|join)", lambda:
            d(sa.earliest_start, 0.95, "standard")),
        (r"relocat", lambda: d(_yes_no(sa.willing_to_relocate), 0.95, "standard")),
        (r"sponsor", lambda: d(_yes_no(sa.requires_sponsorship), 0.95, "standard")),
        (r"(legally )?(authori[sz]ed|eligible|permitted) to work|work (authori[sz]ation|permit)|right to work", lambda:
            _work_auth(f, ctx)),
        (r"\bcity\b|current location|where are you (based|located)|location", lambda:
            d(f"{p.location.city}, {p.location.country}" if not f.options else
              pick_option(f.options, p.location.city) or pick_option(f.options, p.location.country), 0.9)),
        (r"\bcountry\b", lambda: d(pick_option(f.options, p.location.country) if f.options else p.location.country)),
        (r"cover letter", lambda: d(ctx.recipe.cover_letter if ctx.recipe else None, 0.9, "recipe")),
        (r"gender|race|ethnicity|veteran|disability|sexual orientation|pronoun|hispanic", lambda: _decline(f)),
        (r"privacy|consent|terms|acknowledge|agree|certify", lambda:
            Decision(f.key, True, 0.9, "consent") if f.type == "checkbox" and f.required else None),
    ]
    for pat, fn in rules:
        if re.search(pat, label):
            return fn()
    if ctx.recipe:
        for q, a in ctx.recipe.answers.items():
            if _norm(q) and _norm(q) in label:
                return Decision(f.key, a, 0.9, "recipe")
    return None


def _work_auth(f: Field, ctx: FillContext) -> Decision | None:
    auth = (ctx.vault.standard_answers.work_authorization or "").lower()
    if not auth:
        return None
    country = ctx.job_country.lower()
    yes = (country and country in auth) or ("citizen" in auth and country == ctx.vault.personal.location.country.lower())
    if not yes:
        return None              # unclear for another country: ask the user (Assisted)
    val = pick_option(f.options, "Yes") if f.options else "Yes"
    return Decision(f.key, val, 0.9, "standard") if val else None


def _decline(f: Field) -> Decision | None:
    """Voluntary self-identification: always the 'decline to answer' option."""
    for o in f.options:
        if DECLINE.search(o):
            return Decision(f.key, o, 0.95, "decline")
    return None if f.required else Decision(f.key, None, 0.9, "skip-optional")


def _apply_option(f: Field, dec: Decision) -> Decision | None:
    if f.type in ("select", "radio") and f.options and isinstance(dec.value, str):
        opt = pick_option(f.options, dec.value)
        if opt is None:
            return None
        return Decision(dec.key, opt, dec.confidence, dec.source)
    return dec


def _llm_answer(f: Field, ctx: FillContext) -> Decision | None:
    if ctx.llm is None or not ctx.llm.available:
        return None
    v = ctx.vault
    facts = {"name": v.personal.full_name, "location": f"{v.personal.location.city}, {v.personal.location.country}",
             "education": [f"{e.degree} {e.field_of_study or ''} at {e.institution}, {e.graduation_year}" for e in v.education],
             "experience": [f"{e.title} at {e.company} ({e.start} to {e.end or 'present'})" for e in v.experience],
             "projects": [p.name for p in v.projects], "skills": [s for g in v.skills for s in g.items],
             "standard_answers": v.standard_answers.model_dump(exclude_none=True)}
    prompt = (f"Job application form for {ctx.title} at {ctx.company}.\nQuestion: {f.label}\n"
              f"{'Options: ' + ' | '.join(f.options) if f.options else ''}\n"
              f"Candidate facts (the ONLY information you may use): {facts}\n"
              'Return JSON {"answer": "...", "confidence": 0-1}. If the facts do not answer it, confidence must be below 0.5. '
              "Never invent experience, numbers or preferences.")
    try:
        data = ctx.llm.complete_json(prompt, tier="cheap", max_tokens=300)
    except (LLMError, LLMUnavailable):
        return None
    if not isinstance(data, dict) or float(data.get("confidence") or 0) < LLM_MIN_CONFIDENCE or not data.get("answer"):
        return None
    # the same truth rules as the resume: untraceable numbers/skills are rejected
    from app.resume.validator import _clean_prose
    from app.resume.recipe import ValidatorReport
    from app.resume.select import vault_terms
    from app.vault.facts import vault_facts
    rep = ValidatorReport()
    cleaned = _clean_prose(str(data["answer"]), vault_facts(v), vault_terms(v), rep, max_years=experience_years(v))
    if rep.removed_sentences or not cleaned:
        return None
    return Decision(f.key, cleaned, float(data["confidence"]), "llm")


def plan(fields: list[Field], ctx: FillContext) -> FillPlan:
    out = FillPlan()
    for f in fields:
        dec = standard_value(f, ctx)
        if dec is not None and dec.value is None and dec.source in ("skip-optional", "skip-cover-file", "no-referral"):
            out.skipped.append(f)
            continue
        if dec is not None:
            dec = _apply_option(f, dec)
        if dec is None:
            learned = ctx.learned.get(question_hash(f.label))
            if learned:
                dec = _apply_option(f, Decision(f.key, learned, 0.95, "learned"))
        if dec is None and (f.required or f.type == "textarea"):
            dec = _llm_answer(f, ctx)
            if dec is not None:
                dec = _apply_option(f, dec)
        if dec is None or dec.value in (None, ""):
            (out.unresolved if f.required else out.skipped).append(f)
            continue
        out.decisions.append(dec)
    return out
