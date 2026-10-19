"""ATS check (section 7, step 6): extract text the way a parser would and
verify reading order, completeness, page count and keyword coverage."""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from app.resume.model import ResumeView
from app.util.skills import extract_skills
from app.util.text import squash


@dataclass
class AtsReport:
    pages: int
    text_chars: int
    keyword_coverage: float
    issues: list[str] = field(default_factory=list)

    @property
    def one_page(self) -> bool:
        return self.pages == 1

    @property
    def ok(self) -> bool:
        return self.one_page and not self.issues

    def as_dict(self) -> dict:
        return {"pages": self.pages, "one_page": self.one_page, "ok": self.ok,
                "keyword_coverage": round(self.keyword_coverage, 3), "issues": self.issues,
                "text_chars": self.text_chars}


def page_count(pdf: bytes) -> int:
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(pdf)).pages)


def pdf_text(pdf: bytes) -> str:
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(pdf)
            f.flush()
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", f.name, "-"],
                                 capture_output=True, timeout=30)
            if out.returncode == 0:
                return out.stdout.decode("utf-8", "replace")
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages)


def _norm(text: str) -> str:
    text = text.replace("-\n", "").replace("­", "")
    return squash(text).lower()


def check(pdf: bytes, view: ResumeView, keywords: list[str]) -> AtsReport:
    raw = pdf_text(pdf)
    text = _norm(raw)
    issues: list[str] = []

    first = next((l.strip() for l in raw.splitlines() if l.strip()), "")
    if view.name.lower() not in first.lower():
        issues.append(f"name is not the first line (got {first[:40]!r})")
    for bad, label in (("(cid:", "unmapped glyphs"), ("�", "replacement characters"),
                       ("ﬁ", "fi ligature"), ("ﬂ", "fl ligature"), ("ﬀ", "ff ligature")):
        if bad in raw:
            issues.append(f"text contains {label}")

    # headings present and in order
    pos = -1
    for s in view.sections:
        i = text.find(s.title.lower(), pos + 1)
        if i == -1:
            issues.append(f"heading {s.title!r} missing")
        elif i < pos:
            issues.append(f"heading {s.title!r} out of order")
        else:
            pos = i

    # each bullet readable: its first content words appear in order nearby
    for b in view.bullet_texts():
        probe = [w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 2][:5]
        if probe and not re.search(r".{0,40}?".join(map(re.escape, probe)), text):
            issues.append(f"bullet not extractable: {b[:50]!r}")

    contact = view.contact[0].lower() if view.contact else ""
    if contact and contact not in text.replace(" ", "") and contact not in text:
        issues.append("email not extractable")

    found = set(extract_skills(raw))
    cov = (sum(1 for k in keywords if k in found) / len(keywords)) if keywords else 1.0
    return AtsReport(pages=page_count(pdf), text_chars=len(text), keyword_coverage=cov, issues=issues)
