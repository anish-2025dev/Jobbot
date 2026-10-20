"""Compile resume source to PDF (section 7, step 5).

Engine order (RESUME_ENGINE=auto): Tectonic -> XeLaTeX -> pdfLaTeX -> Typst.
Tectonic is what the Docker image ships (self-contained, pre-warmed bundle);
the others are fallbacks for local machines. Every run happens in a fresh
temp directory that is deleted afterwards (PDFs are never kept, C2), with
shell escape disabled because the source contains user text.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.resume.model import ResumeView
from app.resume.render import to_latex, to_typst

ENGINES = ("tectonic", "xelatex", "pdflatex", "typst")
# Normal compiles take seconds. Tectonic's very first run also downloads its TeX bundle, which can
# take several minutes on a small VM: the Docker build's warm-up step raises this limit for that run.
TIMEOUT_S = int(os.environ.get("RESUME_COMPILE_TIMEOUT", "90"))


class CompileError(Exception):
    pass


@dataclass
class Compiled:
    pdf: bytes
    engine: str


def available_engines() -> list[str]:
    return [e for e in ENGINES if shutil.which(e)]


def _engine_order() -> list[str]:
    pref = get_settings().resume_engine
    avail = available_engines()
    if pref != "auto":
        return [pref] if pref in avail else []
    return avail


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S,
                          stdin=subprocess.DEVNULL)


def compile_view(view: ResumeView, paper: str | None = None) -> Compiled:
    paper = paper or get_settings().resume_paper
    errors = []
    for engine in _engine_order():
        with tempfile.TemporaryDirectory(prefix="jobbot-resume-") as tmp:
            d = Path(tmp)
            try:
                if engine == "typst":
                    (d / "resume.typ").write_text(to_typst(view, paper), encoding="utf-8")
                    proc = _run(["typst", "compile", "resume.typ", "resume.pdf"], d)
                else:
                    (d / "resume.tex").write_text(to_latex(view, paper), encoding="utf-8")
                    if engine == "tectonic":
                        cmd = ["tectonic", "--untrusted", "--chatter", "minimal", "--outdir", ".", "resume.tex"]
                    else:
                        cmd = [engine, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape",
                               "resume.tex"]
                    proc = _run(cmd, d)
            except subprocess.TimeoutExpired:
                errors.append(f"{engine}: timed out")
                continue
            pdf = d / "resume.pdf"
            if proc.returncode == 0 and pdf.exists() and pdf.stat().st_size > 0:
                return Compiled(pdf=pdf.read_bytes(), engine=engine)
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
            errors.append(f"{engine}: exit {proc.returncode}: {' | '.join(tail)}")
    if not errors:
        raise CompileError("no resume engine installed (tectonic, xelatex, pdflatex or typst)")
    raise CompileError("; ".join(errors))
