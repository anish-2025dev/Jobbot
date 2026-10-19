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

    sa = v.standard_answers
    for name in ("expected_ctc_lpa", "notice_period_days", "willing_to_relocate",
                 "work_authorization", "requires_sponsorship", "earliest_start"):
        if getattr(sa, name) is None:
            warn(f"standard_answers.{name}", "Unanswered: forms asking this will go to Assisted mode.")
    return w
