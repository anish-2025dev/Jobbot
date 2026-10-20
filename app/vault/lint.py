"""Non-fatal advice for the vault editor. Errors block saving; warnings don't."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.vault.facts import extract_numbers, norm_skill

if TYPE_CHECKING:
    from app.vault.schema import Vault


def lint(v: "Vault") -> list[dict]:
    w: list[dict] = []

    def warn(path: str, msg: str) -> None:
        w.append({"path": path, "message": msg})

    if len(v.projects) < 3:
        warn("projects", "Add at least 3 projects; the resume engine picks 3-4 per job.")
    for i, p in enumerate(v.projects):
        n = len(p.bullets)
        if not 4 <= n <= 6:
            warn(f"projects[{i}].bullets", f"'{p.id}' has {n} bullets; 4-6 gives selection room.")
        if not any(extract_numbers(b.text) for b in p.bullets):
            warn(f"projects[{i}]", f"'{p.id}' has no real metric; numbers can't be added later (C11).")
    for i, e in enumerate(v.experience):
        if not any(extract_numbers(b.text) for b in e.bullets):
            warn(f"experience[{i}]", f"'{e.id}' has no real metric.")
    for path, bullets in [(f"projects[{i}]", p.bullets) for i, p in enumerate(v.projects)] + \
                         [(f"experience[{i}]", e.bullets) for i, e in enumerate(v.experience)]:
        for b in bullets:
            if len(b.text) > 220:
                warn(path, f"bullet '{b.id}' is {len(b.text)} chars and will likely wrap to 3 lines.")

    listed = {norm_skill(s) for g in v.skills for s in g.items} | {norm_skill(s) for s in v.extra_keywords}
    used = {norm_skill(s): s for owner in list(v.projects) + list(v.experience) for s in owner.tech}
    missing = sorted(orig for key, orig in used.items() if key not in listed)
    if missing:
        warn("skills", f"Used in projects/experience but not in skills: {', '.join(missing)}.")

    pool = listed | {norm_skill(s) for s in used}
    for i, r in enumerate(v.target_roles):
        off_pool = [s for s in r.skills if norm_skill(s) not in pool]
        if off_pool:
            warn(f"target_roles[{i}].skills", f"'{r.name}' lists skills that aren't in your skill pool, so they "
                 f"can never appear on a resume: {', '.join(off_pool)}.")
        if not r.title_keywords:
            warn(f"target_roles[{i}].title_keywords", f"'{r.name}' has no title keywords; jobs are matched to it "
                 "by its name only. Add the titles it covers (e.g. SDE, backend developer).")

    sa = v.standard_answers
    for name in ("expected_ctc_lpa", "notice_period_days", "willing_to_relocate",
                 "work_authorization", "requires_sponsorship", "earliest_start"):
        if getattr(sa, name) is None:
            warn(f"standard_answers.{name}", "Unanswered: forms asking this will go to Assisted mode.")
    return w


# Fields that application forms ask often, sometimes or rarely. Missing ones don't
# block saving; the filler sends such a form to Assisted mode instead of guessing.
PROFILE_FIELDS: list[tuple[str, str, str]] = [
    # (path, label, how often forms ask)
    ("personal.date_of_birth", "Date of birth", "often"),
    ("personal.gender", "Gender", "often"),
    ("personal.nationality", "Nationality", "often"),
    ("personal.current_address", "Current address with PIN code", "often"),
    ("standard_answers.expected_ctc_lpa", "Expected CTC", "often"),
    ("standard_answers.notice_period_days", "Notice period", "often"),
    ("standard_answers.willing_to_relocate", "Willing to relocate", "often"),
    ("standard_answers.work_authorization", "Work authorization", "often"),
    ("standard_answers.requires_sponsorship", "Needs visa sponsorship", "often"),
    ("standard_answers.earliest_start", "Earliest start date", "often"),
    ("education.class_xii", "Class XII school, board and score", "often"),
    ("education.class_x", "Class X school, board and score", "often"),
    ("education.backlogs", "Active backlogs on your degree", "often"),
    ("background.father_name", "Father's name", "sometimes"),
    ("background.languages", "Languages you know", "sometimes"),
    ("personal.permanent_address", "Permanent address (if different)", "sometimes"),
    ("standard_answers.education_gap_years", "Gap years in education", "sometimes"),
    ("standard_answers.willing_to_sign_bond", "Willing to sign a service bond", "sometimes"),
    ("standard_answers.willing_night_shifts", "Willing to work night shifts", "sometimes"),
    ("standard_answers.has_laptop", "Own a laptop (WFH roles)", "sometimes"),
    ("standard_answers.current_ctc_lpa", "Current CTC (0 if none)", "sometimes"),
    ("identity.pan", "PAN", "sometimes"),
    ("identity.aadhaar", "Aadhaar", "rarely"),
    ("identity.passport", "Passport", "rarely"),
    ("background.mother_name", "Mother's name", "rarely"),
    ("background.category", "Category (General/OBC/SC/ST/EWS)", "rarely"),
    ("personal.marital_status", "Marital status", "rarely"),
    ("standard_answers.criminal_record", "Criminal record (yes/no)", "rarely"),
    ("standard_answers.background_check_consent", "Consent to a background check", "rarely"),
]


def profile_gaps(v: "Vault", identity_fields: set[str] | None = None) -> list[dict]:
    """Application-form fields still empty, most commonly asked first."""
    identity_fields = identity_fields or set()

    def filled(path: str) -> bool:
        head, _, name = path.partition(".")
        if head == "identity":
            return name in identity_fields
        if head == "education":
            if name == "backlogs":
                return all(e.active_backlogs is not None for e in v.higher_education())
            school = v.school(name)
            return school is not None and (school.score is not None or bool(school.grade))
        if head == "background" and name == "languages":
            return bool(v.background.languages)
        if path == "personal.permanent_address":
            return v.personal.permanent_address is not None or v.personal.current_address is not None
        return getattr(getattr(v, head), name) is not None

    return [{"path": path, "label": label, "asked": asked}
            for path, label, asked in PROFILE_FIELDS if not filled(path)]
