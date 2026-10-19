"""One view model for every output format (LaTeX, Typst, DOCX, plain text).

Built from vault + recipe, so the PDF, its DOCX twin and the ATS check all
see exactly the same content.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.resume.recipe import Recipe
from app.vault.schema import Vault

_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
HEADINGS = {
    "education": "Education", "experience": "Experience", "projects": "Projects",
    "skills": "Skills", "achievements": "Achievements", "certifications": "Certifications",
}


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


@dataclass
class Entry:
    heading: str                    # bold part
    sub: str = ""                   # after the heading, same line
    right: str = ""                 # right-aligned (dates)
    second: str = ""                # optional second line
    bullets: list[str] = field(default_factory=list)


@dataclass
class Section:
    key: str
    title: str
    entries: list[Entry] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)   # (label, text) for skills
    items: list[str] = field(default_factory=list)                # simple bullet list


@dataclass
class ResumeView:
    name: str
    contact: list[str]
    sections: list[Section]

    def bullet_texts(self) -> list[str]:
        return [b for s in self.sections for e in s.entries for b in e.bullets] + \
               [i for s in self.sections for i in s.items]


def build_view(v: Vault, r: Recipe) -> ResumeView:
    p = v.personal
    contact = [p.email, p.phone, f"{p.location.city}, {p.location.country}"]
    contact += [display_url(l.url) for l in p.links]

    btext = {b.id: b.text for o in list(v.experience) + list(v.projects) for b in o.bullets}
    exp_by_id = {e.id: e for e in v.experience}
    proj_by_id = {pr.id: pr for pr in v.projects}
    ach_by_id = {a.id: a.text for a in v.achievements}

    sections: list[Section] = []
    for key in r.order:
        title = HEADINGS[key]
        if key == "education" and v.education:
            entries = []
            for ed in sorted(v.education, key=lambda e: -e.graduation_year):
                degree = ed.degree + (f", {ed.field_of_study}" if ed.field_of_study else "")
                second = "; ".join(x for x in (
                    ed.grade or "",
                    ("Coursework: " + ", ".join(ed.coursework)) if ed.coursework else "") if x)
                right = date_range(ed.start, ed.end, current_ok=False) or str(ed.graduation_year)
                entries.append(Entry(heading=ed.institution, sub=degree, right=right, second=second))
            sections.append(Section(key, title, entries=entries))
        elif key == "experience" and r.experience:
            entries = []
            for sec in r.experience:
                e = exp_by_id[sec.id]
                sub = e.company + (f", {e.location}" if e.location else "")
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
                    second=display_url(link) if link else "",
                    bullets=[b.text or btext[b.id] for b in sec.bullets]))
            sections.append(Section(key, title, entries=entries))
        elif key == "skills" and r.skills:
            sections.append(Section(key, title, lines=[(g.category, ", ".join(g.items)) for g in r.skills]))
        elif key == "achievements" and r.achievements:
            sections.append(Section(key, title, items=[ach_by_id[a] for a in r.achievements if a in ach_by_id]))
        elif key == "certifications" and v.certifications:
            items = [", ".join(x for x in (c.name, c.issuer or "", fmt_month(c.date)) if x) for c in v.certifications]
            sections.append(Section(key, title, items=items))
    return ResumeView(name=p.full_name, contact=contact, sections=sections)
