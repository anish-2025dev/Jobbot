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
from datetime import date

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
    identity: dict[str, str] = field(default_factory=dict)      # decrypted PAN/passport/... (app.vault.identity)
    today: date | None = None


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
    first, last = p.first_name or _first_last(p.full_name)[0], p.last_name or _first_last(p.full_name)[1]
    edu = max(v.higher_education(), key=lambda e: e.graduation_year)
    latest = max(v.experience, key=lambda e: (e.end or "9999-99", e.start)) if v.experience else None
    cc, local_phone = _phone_parts(p.phone)

    def d(val, conf=0.97, src="vault"):
        return Decision(f.key, val, conf, src) if val not in (None, "") else None

    if f.type != "file":
        prof = profile_value(f, ctx, label, d)
        if prof is not False:
            return prof
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
        (r"preferred name", lambda: d(p.preferred_name or first)),
        (r"(full |legal )?name\b(?!.*(company|employer|school|university|referr|recruit|manager))", lambda: d(p.full_name)),
        (r"e ?mail", lambda: d(p.email) if "referr" not in label else d(ctx.referrer_email, 0.95, "referral")),
        (r"country code|dialing code", lambda: d(cc or "+91")),
        (r"phone|mobile|contact number|cell", lambda: d(p.phone if f.type != "number" else local_phone)),
        (r"linkedin", lambda: d(_link(v, "linkedin"))),
        (r"github", lambda: d(_link(v, "github"))),
        (r"portfolio|website|personal (site|url)|blog", lambda: d(_link(v, "portfolio") or _link(v, "github"), 0.85)),
        (r"how did you (hear|find|learn)|source of application|where did you (hear|find)", lambda:
            d(pick_option(f.options, "Employee referral") if ctx.referrer and f.options else
              ("Employee referral" if ctx.referrer else
               (pick_option(f.options, sa.heard_about_us) if f.options else sa.heard_about_us) if sa.heard_about_us else
               (pick_option(f.options, "Company website") or
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
        (r"\bgpa\b|cgpa|grade|percentage|marks", lambda: _degree_score(f, edu, label, d)),
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
        (r"gender|race|ethnicity|veteran|disability|sexual orientation|pronoun|hispanic|caste", lambda: _self_id(f, ctx, label)),
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


def _fmt_date(iso: str | None, f: Field) -> str | None:
    """Date inputs take ISO; text fields get the DD/MM/YYYY that Indian forms expect."""
    if not iso:
        return None
    if f.type == "date":
        return iso
    y, m, dd = iso.split("-")
    return f"{dd}/{m}/{y}"


def _yn(f: Field, flag: bool | None, conf: float = 0.95) -> Decision | None:
    if flag is None:
        return None
    if f.type == "checkbox":
        return Decision(f.key, flag, conf, "standard")
    val = pick_option(f.options, "Yes" if flag else "No") if f.options else ("Yes" if flag else "No")
    return Decision(f.key, val, conf, "standard") if val else None


_SCHOOL = {  # XII first: "senior secondary school" must not read as Class X
    "class_xii": r"(12th|\bxii\b|class 12|std 12|\bhsc\b|higher secondary|senior secondary|intermediate|\bpuc\b)",
    "class_x": r"(10th|class 10|class x\b|std x\b|std 10|\bssc\b|matric|secondary school)"}
_SCHOOL_PART = [("score", r"percent|marks|score|cgpa|gpa|grade|aggregate|result"),
                ("board", r"board"), ("year", r"year|passing|completion"),
                ("institution", r"school|institut|college|name of")]


def profile_value(f: Field, ctx: FillContext, label: str, d) -> Decision | None | bool:
    """Profile fields from vault v2 and the encrypted identity documents. Returns False
    when the label isn't one of these, so the generic rules get their turn."""
    v = ctx.vault
    p, sa, bg = v.personal, v.standard_answers, v.background
    ident = ctx.identity
    home = p.current_address
    perm = p.permanent_address or home

    # Class X / XII before the generic school / grade / year rules
    for level, pat in _SCHOOL.items():
        if re.search(pat, label):
            school = v.school(level)
            for part, ppat in _SCHOOL_PART:
                if re.search(ppat, label):
                    if school is None:
                        return None
                    if part == "score":
                        if f.type == "number":
                            pct = school.percentage() if "percent" in label else school.score
                            return d(f"{pct:g}" if pct is not None else None)
                        return d(school.score_text())
                    if part == "board":
                        return d(pick_option(f.options, school.board) if f.options and school.board else school.board)
                    if part == "year":
                        return d(str(school.graduation_year))
                    return d(school.institution)
            break

    def addr(a, part: str):
        if a is None:
            return None
        return {"line1": a.line1, "line2": a.line2, "full": ", ".join(x for x in (a.line1, a.line2, a.city, a.state, a.pincode) if x),
                "city": a.city, "district": a.district, "state": pick_option(f.options, a.state) if f.options else a.state,
                "pincode": a.pincode, "country": pick_option(f.options, a.country) if f.options else a.country}[part]

    which = perm if re.search(r"permanent", label) else home
    rules: list[tuple[str, object]] = [
        (r"middle name", lambda: d(p.middle_name) if p.middle_name else Decision(f.key, None, 0.9, "skip-optional")),
        (r"alternat\w* (e ?mail)|secondary e ?mail", lambda: d(p.alternate_email)),
        (r"alternat\w* (phone|mobile|contact|number)|secondary (phone|mobile)", lambda: d(p.alternate_phone)),
        (r"whats ?app", lambda: d(p.whatsapp_phone or p.phone, 0.9)),
        (r"date of birth|\bdob\b|birth ?date|born on", lambda: d(_fmt_date(p.date_of_birth, f))),
        (r"^(your )?(current )?age$|^age (in years)?$", lambda: d(str(p.age_on(ctx.today or date.today())) if p.date_of_birth and not f.options else None)),
        (r"nationality|citizenship", lambda: d(pick_option(f.options, p.nationality) if f.options and p.nationality else p.nationality)),
        (r"marital", lambda: d(pick_option(f.options, p.marital_status) if f.options and p.marital_status
                               else (p.marital_status or "").capitalize() or None)),
        (r"father s name|father name|name of (your )?father|father.{0,3}guardian.{0,3}name", lambda: d(bg.father_name)),
        (r"mother s name|mother name|name of (your )?mother", lambda: d(bg.mother_name)),
        (r"\bpan\b(?!\s*india)|permanent account number", lambda: d(ident.get("pan"), 0.98, "identity")),
        (r"aa?dha+r",lambda: d(ident.get("aadhaar"), 0.98, "identity")),
        (r"(do|does) you (hold|have|possess) a (valid )?passport|valid passport\b(?!.*(number|no))", lambda:
            _yn(f, "passport_number" in ident)),
        (r"passport.*(expir|valid (till|until|upto))|(expir|validity).*passport", lambda: d(_fmt_date(ident.get("passport_expiry_date"), f), 0.98, "identity")),
        (r"passport.*(issue|issuing) date|passport.*date of issue", lambda: d(_fmt_date(ident.get("passport_issue_date"), f), 0.98, "identity")),
        (r"passport.*place|place of issue.*passport", lambda: d(ident.get("passport_place_of_issue"), 0.98, "identity")),
        (r"passport", lambda: d(ident.get("passport_number"), 0.98, "identity")),
        (r"driving licen[cs]e|\bdl (no|number)", lambda: d(ident.get("driving_licence"), 0.98, "identity")),
        (r"voter", lambda: d(ident.get("voter_id"), 0.98, "identity")),
        (r"\buan\b|pf (account )?number|provident fund", lambda: d(ident.get("uan"), 0.98, "identity")),
        (r"address line ?2|street address 2|address 2", lambda: d(addr(which, "line2")) or Decision(f.key, None, 0.9, "skip-optional")),
        (r"address line ?1|street address|address 1|house (no|number)|flat (no|number)", lambda: d(addr(which, "line1"))),
        (r"pin ?code|postal|zip", lambda: d(addr(which, "pincode"))),
        (r"district", lambda: d(addr(which, "district"))),
        (r"^(current |permanent |present )?(state|province)( name)?$|\bstate\b(?=.*(address|residence|domicile))", lambda: d(addr(which, "state"))),
        (r"(permanent|current|present|residential|mailing|correspondence) address|^address$", lambda: d(addr(which, "full"))),
        (r"(permanent|current|present|residential) (city|country)", lambda: d(addr(which, "city" if "city" in label else "country"))),
        (r"languages? (known|spoken|proficien)|which languages|^languages?$", lambda:
            d(", ".join(lang.name for lang in bg.languages) if bg.languages else None, 0.9)),
        (r"active backlog|current backlog|pending backlog|backlogs? (currently|pending)|arrears?", lambda:
            _backlogs(f, v, "active_backlogs")),
        (r"backlog", lambda: _backlogs(f, v, "backlog_history")),
        (r"reason for (the )?gap|explain.*gap", lambda: d(sa.gap_reason or ("No gap" if sa.education_gap_years == 0 else None), 0.9)),
        (r"gap (year|in (your )?(education|studies|career))|education(al)? gap|year gap|break in (education|studies)", lambda:
            _yn(f, bool(sa.education_gap_years)) if f.type in ("radio", "select", "checkbox") and not any(re.search(r"\d", o) for o in f.options)
            else d(str(sa.education_gap_years) if sa.education_gap_years is not None else None, 0.95, "standard")),
        (r"\bbond\b|service agreement", lambda: _yn(f, sa.willing_to_sign_bond)),
        (r"night shift|rotational shift|work in shifts|shift timings|(us|uk) shift", lambda: _yn(f, sa.willing_night_shifts)),
        (r"willing to travel|travel (requirement|required)|open to travel", lambda: _yn(f, sa.willing_to_travel)),
        (r"laptop|own (a )?(computer|pc|desktop)", lambda: _yn(f, sa.has_laptop)),
        (r"internet|broadband|wi-?fi", lambda: _yn(f, sa.has_reliable_internet)),
        (r"(currently|presently) (employed|working)|are you (currently )?(employed|working)", lambda: _yn(f, sa.currently_employed)),
        (r"(currently|presently) (pursuing|studying|enrolled)|are you (a )?(current )?student", lambda: _yn(f, sa.currently_studying)),
        (r"negotiable", lambda: _yn(f, sa.expected_ctc_negotiable)),
        (r"convicted|criminal (record|offen|charge|case|proceeding)", lambda: _yn(f, sa.criminal_record)),
        (r"background (check|verification)|\bbgv\b", lambda: _yn(f, sa.background_check_consent)),
        (r"preferred (work )?(mode|model|arrangement)|work arrangement|(remote|onsite|hybrid) preference", lambda:
            d(pick_option(f.options, sa.preferred_work_mode) if f.options and sa.preferred_work_mode
              else (sa.preferred_work_mode or "").capitalize() or None, 0.9, "standard")),
        (r"preferred (job )?location|location preference|preferred cit", lambda:
            d((pick_option(f.options, v.expectations.locations[0]) if f.options else
               ", ".join(v.expectations.locations[:5])), 0.85, "standard")),
    ]
    for pat, fn in rules:
        if re.search(pat, label):
            return fn()
    return False


def _degree_score(f: Field, edu, label: str, d) -> Decision | None:
    """'8.4/10' for text, '8.4' for a number field, the percentage when the form asks for one."""
    if edu.score is not None:
        if "percent" in label and edu.score_scale != "percentage":
            pct = edu.percentage()
            return d(f"{pct:g}" if pct is not None else None, 0.85)
        if f.type == "number":
            return d(f"{edu.score:g}", 0.95)
        return d({"cgpa_10": f"{edu.score:g}/10", "cgpa_4": f"{edu.score:g}/4",
                  "percentage": f"{edu.score:g}%"}.get(edu.score_scale or "", f"{edu.score:g}"), 0.95)
    return d(re.sub(r"^\s*c?gpa\s*", "", edu.grade or "", flags=re.I) or None, 0.9)


def _backlogs(f: Field, v: Vault, attr: str) -> Decision | None:
    vals = [getattr(e, attr) for e in v.higher_education()]
    if any(x is None for x in vals):
        return None
    n = sum(vals)
    if f.type in ("radio", "select", "checkbox") and f.options and not any(re.search(r"\d", o) for o in f.options):
        return _yn(f, n > 0)
    return Decision(f.key, str(n), 0.95, "standard")


def _self_id(f: Field, ctx: FillContext, label: str) -> Decision | None:
    """Gender, category, disability, veteran: answered only when the user chose to share
    (background.eeo_disclosure = "share"); otherwise the 'decline' option."""
    v = ctx.vault
    bg = v.background
    if bg.eeo_disclosure == "share":
        value = None
        if re.search(r"gender|\bsex\b", label) and v.personal.gender:
            value = {"male": "Male", "female": "Female", "non_binary": "Non-binary", "other": "Other"}[v.personal.gender]
        elif re.search(r"caste|category", label) and bg.category:
            value = {"general": "General", "obc": "OBC", "sc": "SC", "st": "ST", "ews": "EWS", "other": "Other"}[bg.category]
        elif re.search(r"disab", label) and bg.person_with_disability is not None:
            return _yn(f, bg.person_with_disability)
        elif re.search(r"veteran", label) and bg.veteran is not None:
            if f.options:
                opt = next((o for o in f.options if re.search(r"not a (protected )?veteran|^no", o, re.I)), None) \
                    if not bg.veteran else pick_option(f.options, "veteran")
                return Decision(f.key, opt, 0.9, "standard") if opt else None
            return _yn(f, bg.veteran)
        if value is not None:
            if f.options:
                opt = pick_option(f.options, value)
                return Decision(f.key, opt, 0.95, "standard") if opt else None
            return Decision(f.key, value, 0.95, "standard")
    return _decline(f)


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
