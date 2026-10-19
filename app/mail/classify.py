"""Classify job-search mail (section 11).

Rule-based first (fast, explainable, free); the cheap LLM only breaks ties
when rules are unsure. Anything still unsure is "unclear", which the policy
treats as positive: alerted and never trashed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.llm.client import LLM, LLMError, LLMUnavailable
from app.mail.message import Message

POSITIVE = {"offer", "interview", "test", "recruiter", "info_request", "referral_given", "outreach_question"}
LOW_VALUE = {"confirmation", "otp", "verification", "account_created", "job_alert", "bounce"}
NEGATIVE = {"rejection", "outreach_decline"}
ALL = POSITIVE | LOW_VALUE | NEGATIVE | {"unclear"}

ALERT_SENDERS = re.compile(
    r"(jobalerts-noreply|jobs-noreply|jobs-listings|alerts)@linkedin\.com|@naukri\.com$|naukrialerts|"
    r"@(mail\.)?instahyre\.com$|@(hi\.|mail\.)?wellfound\.com$|@angel\.co$|@(mail\.)?indeed\.com$|"
    r"@(mail\.)?glassdoor\.(com|co\.in)$|@(mail\.)?foundit\.in$", re.I)
ATS_SENDERS = re.compile(
    r"greenhouse\.io|greenhouse-mail\.io|lever\.co|hire\.lever\.co|ashbyhq\.com|smartrecruiters\.com|"
    r"myworkday\.com|myworkdayjobs\.com|workablemail\.com|workable\.com|icims\.com|jobvite\.com|"
    r"taleo\.net|successfactors\.(com|eu)|darwinbox\.(in|com)|keka\.com|zohorecruit\.(com|in)", re.I)

_RULES: list[tuple[str, float, re.Pattern]] = [
    ("bounce", 0.95, re.compile(r"delivery status notification \(failure\)|undeliverable|mail delivery (failed|subsystem)|"
                                r"returned mail|address not found|message (was )?not delivered|delivery has failed", re.I)),
    ("otp", 0.9, re.compile(r"(verification|security|one[- ]time|login|sign[- ]in|confirmation) (code|passcode|pin)|"
                            r"\botp\b|your (code|passcode) is|use (this|the following) code", re.I)),
    ("verification", 0.9, re.compile(r"verify (your )?(email|e-mail|account)|confirm (your )?(email|e-mail|account|registration)|"
                                     r"activate (your )?account|email verification", re.I)),
    ("account_created", 0.85, re.compile(r"account (has been |was )?created|thanks for creating an account|"
                                         r"welcome to (the )?.{0,40}(careers|talent (community|network)|candidate portal)", re.I)),
    ("offer", 0.92, re.compile(r"(offer letter|job offer|offer of employment|extend(ing)? (you )?an offer|"
                               r"pleased to offer you|delighted to offer you|(congratulations|congrats).{0,80}\boffer\b)", re.I)),
    # Strong phrases are unambiguous; "unfortunately" alone is weak (it also
    # appears in "unfortunately the slot moved, can we reschedule?"). Only
    # strong rejections are ever trashed (section 11: high confidence).
    ("rejection", 0.95, re.compile(r"not (to )?(be )?mov(e|ing) forward|decided (to|not to) (move forward|proceed|progress) "
                                   r"with (other|another)|(other|another) candidates? (whose|who|that)|not been selected|"
                                   r"were not selected|regret to inform|position has (now )?been filled|"
                                   r"will not be progressing|no longer (being )?considered|we have decided to pursue other|"
                                   r"decided not to (proceed|move forward)|not (be )?proceeding with your application", re.I)),
    ("weak_rejection", 0.6, re.compile(r"unfortunately|after careful (consideration|review)", re.I)),
    ("referral_given", 0.88, re.compile(r"\b(i('ve| have)|just|happy to|gladly|already) (submitted )?(a |your )?referr(ed|al)|"
                                        r"referred you|submitted (a |your )?referral|referral link|use (this|my) (referral )?link|"
                                        r"(put|added) your (name|profile) (in|forward)", re.I)),
    ("outreach_decline", 0.85, re.compile(r"(not|n't) (be )?(able|in a position) to refer|(can't|cannot|can not|won't) refer|"
                                          r"please (don't|do not|stop) (contact|email|reach)|remove me|unsubscribe|"
                                          r"not interested|no longer (at|with|work)|(isn't|is not|not) relevant|stop emailing", re.I)),
    ("test", 0.85, re.compile(r"\bassessment\b|coding (test|challenge|round|exercise)|online test|take[- ]home|aptitude test|"
                              r"hackerrank|codility|hackerearth|codesignal|mettl|testgorilla|imocha|codeysignal|"
                              r"technical (test|exercise)", re.I)),
    ("interview", 0.85, re.compile(r"\binterview\b|phone screen|screening call|schedule (a|your|the) (call|chat|conversation|meeting)|"
                                   r"calendly\.com|your availability|available (for|to) (a )?(call|chat)|next (round|step)s?\b.{0,40}"
                                   r"(call|chat|discussion|round)|meet (with )?the (team|hiring manager)", re.I)),
    ("info_request", 0.75, re.compile(r"could you (please )?(share|send|provide|confirm|fill)|please (share|send|provide|submit|"
                                      r"confirm|complete)|need (some )?(more|additional|further) (information|details|documents)|"
                                      r"documents? (required|needed)|(fill|complete) (out |in )?(the|this) (form|questionnaire)", re.I)),
    ("outreach_question", 0.75, re.compile(r"send (me |over )?(your )?(resume|cv|details|profile)|which (role|position)|"
                                           r"what (role|position)|(job|req(uisition)?) (id|link)\?|can you (share|send)", re.I)),
    ("recruiter", 0.75, re.compile(r"came across your (profile|resume)|i('m| am) (a |the )?(recruiter|talent|hiring)|"
                                   r"reaching out (to you )?(about|regarding) (a|an|the) (role|position|opportunity)|"
                                   r"are you open to|would you be interested in (a|an|the) (role|position|opportunity)", re.I)),
    ("confirmation", 0.85, re.compile(r"thank(s| you) for (applying|your application|your interest|submitting)|"
                                      r"application (has been |was )?(received|submitted)|we('ve| have) received your application|"
                                      r"successfully (applied|submitted)|your application (to|for) .{0,80} (is|has been) (received|submitted)", re.I)),
    ("job_alert", 0.8, re.compile(r"job alert|jobs? (for|matching) you|new jobs? (matching|in|for)|recommended jobs|"
                                  r"jobs you (may|might) be interested in|\d+\+? new jobs", re.I)),
]

_OTP = re.compile(r"(?<![\d-])(\d{4,8})(?![\d-])")
_OTP_ALNUM = re.compile(r"\b(?i:code|otp|passcode)\s*(?i:is|:)?\s*([A-Z0-9]{4,8})\b")
_DATE_WORDS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass
class Classification:
    cls: str
    confidence: float
    deadline: datetime | None = None
    otp: str | None = None
    link: str | None = None
    bounced_recipient: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def positive(self) -> bool:
        return self.cls in POSITIVE or self.cls == "unclear"


def extract_otp(text: str) -> str | None:
    m = _OTP_ALNUM.search(text or "")
    if m and any(ch.isdigit() for ch in m.group(1)):
        return m.group(1)
    for m in _OTP.finditer(text or ""):
        code = m.group(1)
        if len(code) == 4 and 1990 <= int(code) <= 2100:
            continue                        # a year, not a code
        return code
    return None


def extract_deadline(text: str, now: datetime) -> datetime | None:
    t = (text or "").lower()
    m = re.search(r"within (\d{1,3}) (hours?|days?)", t)
    if m:
        n = int(m.group(1))
        return now + (timedelta(hours=n) if m.group(2).startswith("hour") else timedelta(days=n))
    m = re.search(r"(?:by|before|deadline:?|due:?|no later than|until)\s+(?:\w+day,?\s+)?"
                  r"(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*(?:,?\s+(\d{4}))?", t)
    m2 = None if m else re.search(r"(?:by|before|deadline:?|due:?|no later than|until)\s+(?:\w+day,?\s+)?"
                                  r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", t)
    m3 = None if (m or m2) else re.search(r"(?:by|before|deadline:?|due:?)\s+(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", t)
    try:
        if m:
            day, mon, year = int(m.group(1)), _DATE_WORDS[m.group(2)], int(m.group(3) or now.year)
        elif m2:
            mon, day, year = _DATE_WORDS[m2.group(1)], int(m2.group(2)), int(m2.group(3) or now.year)
        elif m3:
            day, mon, year = int(m3.group(1)), int(m3.group(2)), int(m3.group(3))
            year += 2000 if year < 100 else 0
        else:
            return None
        dt = datetime(year, mon, day, 23, 59, tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None
    if dt < now - timedelta(days=1) and not (m and m.group(3)) and not (m2 and m2.group(3)):
        dt = dt.replace(year=dt.year + 1)
    return dt


def _bounced_recipient(msg: Message) -> str | None:
    body = msg.body
    m = re.search(r"final-recipient:\s*rfc822;\s*([^\s>]+@[^\s>]+)", body, re.I) or \
        re.search(r"(?:wasn't delivered to|not delivered to|delivery to the following recipient[s]? failed[^:]*:)\s*<?([^\s<>]+@[^\s<>]+)", body, re.I)
    return m.group(1).strip(".,;").lower() if m else None


def classify(msg: Message, *, context: str | None = None, now: datetime | None = None,
             llm: LLM | None = None) -> Classification:
    """context: 'outreach' when the message is a reply in an outreach
    thread, 'application' when matched to an application, else None."""
    now = now or datetime.now(timezone.utc)
    subject, body = msg.subject or "", msg.body or msg.snippet
    blob = f"{subject}\n{body[:6000]}"
    sender = msg.from_addr

    if re.match(r"(mailer-daemon|postmaster)@", sender) or (_RULES[0][2].search(subject)):
        return Classification("bounce", 0.97, bounced_recipient=_bounced_recipient(msg), reasons=["bounce"])
    if ALERT_SENDERS.search(sender) and not re.search(r"interview|assessment|offer|recruiter", subject, re.I):
        return Classification("job_alert", 0.95, reasons=["alert sender"])

    hits: dict[str, float] = {}
    for cls, conf, pat in _RULES[1:]:
        if pat.search(subject):
            hits[cls] = max(hits.get(cls, 0), min(conf + 0.05, 0.99))
        elif pat.search(body[:6000]):
            hits[cls] = max(hits.get(cls, 0), conf)

    # Context changes meaning: in an outreach thread "not interested" is a
    # decline and "send your resume" a question; elsewhere they are not.
    if context != "outreach":
        hits.pop("outreach_decline", None)
        hits.pop("outreach_question", None)
    else:
        hits.pop("confirmation", None)
        hits.pop("job_alert", None)

    # precedence when several fire
    if "offer" in hits and "rejection" not in hits:
        cls = "offer"
    elif "rejection" in hits:
        cls = "rejection"                       # strong phrase: wins even over "interview" words
    elif "weak_rejection" in hits and not ({"interview", "test", "offer", "info_request"} & set(hits)):
        cls = "unclear"                         # possibly a rejection, but not confident enough to trash
    elif context == "outreach" and "referral_given" in hits:
        cls = "referral_given"
    elif context == "outreach" and "outreach_decline" in hits:
        cls = "outreach_decline"
    else:
        order = ["otp", "verification", "account_created", "test", "interview", "referral_given", "info_request",
                 "outreach_question", "recruiter", "confirmation", "job_alert"]
        cls = next((c for c in order if c in hits), "unclear")
    conf = hits.get(cls, 0.4)

    if cls == "otp":
        code = extract_otp(blob)
        if not code:
            cls, conf = ("verification", 0.7) if hits.get("verification") else ("unclear", 0.4)
    if cls == "unclear" and llm is not None and llm.available:
        cls, conf = _llm_classify(msg, context, llm) or (cls, conf)

    out = Classification(cls, conf, reasons=sorted(hits))
    if cls == "otp":
        out.otp = extract_otp(blob)
    if cls in ("verification", "referral_given", "test", "interview"):
        out.link = _best_link(msg, cls)
    if cls in POSITIVE:
        out.deadline = extract_deadline(blob, now)
    return out


def _best_link(msg: Message, cls: str) -> str | None:
    want = {"verification": r"verif|confirm|activate|token", "referral_given": r"refer|apply|job|career",
            "test": r"hackerrank|codility|hackerearth|codesignal|assess|test|mettl|testgorilla",
            "interview": r"calendly|schedule|meet|zoom|teams|interview"}[cls]
    for href, label in msg.links:
        if re.search(want, f"{href} {label}", re.I) and not re.search(r"unsubscribe|privacy|preferences", href, re.I):
            return href
    return None


def _llm_classify(msg: Message, context: str | None, llm: LLM) -> tuple[str, float] | None:
    labels = sorted(ALL - {"unclear"})
    prompt = (f"Classify this email received during a job search. Context: {context or 'unknown'}.\n"
              f"Labels: {', '.join(labels)}, unclear.\n"
              f"From: {msg.from_name} <{msg.from_addr}>\nSubject: {msg.subject}\n\n{msg.body[:3000]}\n\n"
              'Return JSON {"label": "...", "confidence": 0.0-1.0}. Use "unclear" if unsure.')
    try:
        data = llm.complete_json(prompt, tier="cheap", max_tokens=100)
    except (LLMError, LLMUnavailable):
        return None
    label = data.get("label") if isinstance(data, dict) else None
    if label not in ALL:
        return None
    conf = float(data.get("confidence") or 0.5)
    # A model that is unsure about a NEGATIVE label never gets to trash mail.
    if label in NEGATIVE | LOW_VALUE and conf < 0.8:
        return "unclear", conf
    return label, min(conf, 0.85)
