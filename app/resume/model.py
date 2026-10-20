"""One view model for every output format (LaTeX, Typst, DOCX, plain text).

Built from vault + recipe, so the PDF, its DOCX twin and the ATS check all
see exactly the same content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.resume.recipe import Recipe
from app.vault.schema import Vault

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
HEADINGS = {
    "summary": "Summary", "education": "Education", "experience": "Experience", "projects": "Projects",
    "skills": "Technical Skills", "achievements": "Achievements", "certifications": "Certifications",
}
_LEVEL_ORDER = {"phd": 0, "masters": 1, "bachelors": 2, "diploma": 3, "certificate": 4, "other": 5,
                "class_xii": 6, "class_x": 7}
_EMPLOYMENT = {"internship": "Internship", "part_time": "Part-time", "contract": "Contract", "freelance": "Freelance"}


def fmt_month(ym: str | None, present: bool = False) -> str:
    if not ym:
        return "Present" if present else ""
    y, m = ym.split("-")
    return f"{_MONTHS[int(m) - 1]} {y}"


def date_range(start: str | None, end: str | None, current_ok: bool = True) -> str:
    if not start and not end:
        return ""
    s = fmt_month(start)
    e = fmt_month(end, present=current_ok)
    return f"{s} -- {e}" if s else e


def display_url(url: str) -> str:
    for prefix in ("https://", "http://", "www."):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url.rstrip("/")


def _safe_href(url: str | None) -> str | None:
    """Only plain http(s)/mailto/tel targets become links; anything with characters
    that could break out of \\href{} is shown as text only."""
    if not url or not re.match(r"^(https?://|mailto:|tel:)[^\s{}\\]+$", url):
        return None
    return url


@dataclass
class Entry:
    heading: str                    # bold part
    sub: str = ""                   # after the heading, same line
    right: str = ""                 # right-aligned (dates)
    second: str = ""                # optional second line
    bullets: list[str] = field(default_factory=list)
    second_href: str | None = None  # makes `second` a link (project URL)


@dataclass
class Section:
    key: str
    title: str
    entries: list[Entry] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)   # (label, text) for skills
    items: list[str] = field(default_factory=list)                # simple bullet list
    paragraph: str = ""                                           # summary


@dataclass
class ResumeView:
    name: str
    contact: list[str]
    sections: list[Section]
    contact_links: list[tuple[str, str | None]] = field(default_factory=list)   # (text, href) per contact item

    def bullet_texts(self) -> list[str]:
        return [b for s in self.sections for e in s.entries for b in e.bullets] + \
               [i for s in self.sections for i in s.items]


def _education(v: Vault) -> list[Entry]:
    entries = []
    shown = [e for e in v.education if e.show_on_resume]
    for ed in sorted(shown, key=lambda e: (_LEVEL_ORDER.get(e.level or "other", 5), -e.graduation_year)):
        if ed.level in ("class_x", "class_xii"):
            degree = {"class_x": "Class X", "class_xii": "Class XII"}[ed.level]
            if ed.board:
                degree += f" ({ed.board})"
            if ed.field_of_study:
                degree += f", {ed.field_of_study}"
        else:
            degree = ed.degree + (f", {ed.field_of_study}" if ed.field_of_study else "")
        second = "; ".join(x for x in (
            ed.score_text() or "",
            ("Coursework: " + ", ".join(ed.coursework)) if ed.coursework else "") if x)
        right = date_range(ed.start, ed.end, current_ok=False) or str(ed.graduation_year)
        entries.append(Entry(heading=ed.institution, sub=degree, right=right, second=second))
    return entries


def build_view(v: Vault, r: Recipe) -> ResumeView:
    p = v.personal
    contact_links: list[tuple[str, str | None]] = [
        (p.phone, _safe_href("tel:" + re.sub(r"[^\d+]", "", p.phone))),
        (p.email, _safe_href(f"mailto:{p.email}")),
        (f"{p.location.city}, {p.location.country}", None),
    ]
    contact_links += [(display_url(link.url), _safe_href(link.url)) for link in p.links]
    contact = [text for text, _ in contact_links]

    btext = {b.id: b.text for o in list(v.experience) + list(v.projects) for b in o.bullets}
    exp_by_id = {e.id: e for e in v.experience}
    proj_by_id = {pr.id: pr for pr in v.projects}
    ach_by_id = {a.id: a.text for a in v.achievements}

    sections: list[Section] = []
    for key in r.order:
        title = HEADINGS[key]
        if key == "summary" and r.summary:
            sections.append(Section(key, title, paragraph=r.summary))
        elif key == "education" and any(e.show_on_resume for e in v.education):
            sections.append(Section(key, title, entries=_education(v)))
        elif key == "experience" and r.experience:
            entries = []
            for sec in r.experience:
                e = exp_by_id[sec.id]
                kind = _EMPLOYMENT.get(e.employment_type)
                sub = e.company + (f", {e.location}" if e.location else "") + (f" ({kind})" if kind and kind.lower()[:6] not in e.title.lower() else "")
                entries.append(Entry(heading=e.title, sub=sub, right=date_range(e.start, e.end),
                                     bullets=[b.text or btext[b.id] for b in sec.bullets]))
            sections.append(Section(key, title, entries=entries))
        elif key == "projects" and r.projects:
            entries = []
            for sec in r.projects:
                pr = proj_by_id[sec.id]
                link = pr.url or pr.repo
                entries.append(Entry(
                    heading=pr.name, sub=", ".join(pr.tech[:6]),
                    right=date_range(pr.start, pr.end) if pr.start else "",
                    second=display_url(link) if link else "", second_href=_safe_href(link),
                    bullets=[b.text or btext[b.id] for b in sec.bullets]))
            sections.append(Section(key, title, entries=entries))
        elif key == "skills" and r.skills:
            sections.append(Section(key, title, lines=[(g.category, ", ".join(g.items)) for g in r.skills]))
        elif key == "achievements" and r.achievements:
            sections.append(Section(key, title, items=[ach_by_id[a] for a in r.achievements if a in ach_by_id]))
        elif key == "certifications" and v.certifications:
            items = [", ".join(x for x in (c.name, c.issuer or "", fmt_month(c.date)) if x) for c in v.certifications]
            sections.append(Section(key, title, items=items))
    return ResumeView(name=p.full_name, contact=contact, sections=sections, contact_links=contact_links)
