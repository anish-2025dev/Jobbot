"""Real compiles with every engine installed here (skipped when none)."""
import copy
import json
from pathlib import Path

import pytest

from app.resume import ats
from app.resume.compile import CompileError, available_engines, compile_view
from app.resume.engine import build_recipe, prepare_jd, render_pdf
from app.resume.model import build_view
from app.vault.schema import Vault

ROOT = Path(__file__).parents[2]
EXAMPLE = json.loads((ROOT / "examples/vault.example.json").read_text())
JD = (ROOT / "examples/jd.example.txt").read_text()
ENGINES = available_engines()
pytestmark = pytest.mark.skipif(not ENGINES, reason="no resume engine installed")


def _engine(monkeypatch, name):
    import app.config
    monkeypatch.setenv("RESUME_ENGINE", name)
    app.config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset():
    yield
    import app.config
    app.config.get_settings.cache_clear()


def _usable(engine):
    """Tectonic needs its bundle (network on first run); skip if it can't fetch."""
    if engine != "tectonic":
        return True
    import subprocess, tempfile
    with tempfile.TemporaryDirectory() as d:
        Path(d, "t.tex").write_text(r"\documentclass{article}\begin{document}x\end{document}")
        return subprocess.run(["tectonic", "t.tex"], cwd=d, capture_output=True, timeout=120).returncode == 0


@pytest.mark.parametrize("engine", ENGINES)
def test_one_page_ats_clean(engine, monkeypatch):
    if not _usable(engine):
        pytest.skip("tectonic bundle unavailable offline")
    _engine(monkeypatch, engine)
    v = Vault.model_validate(copy.deepcopy(EXAMPLE))
    jd = prepare_jd(v, JD, "Software Development Engineer I", "Northwind Payments")
    out = render_pdf(v, build_recipe(v, jd, vault_revision=1, bucket="high"))
    assert out.engine == engine
    assert out.report.ok, out.report.as_dict()
    assert out.report.keyword_coverage == 1.0
    text = ats.pdf_text(out.pdf)
    assert "Sample Candidate" in text.splitlines()[0]
    for bad in ("ﬁ", "ﬂ", "(cid:"):
        assert bad not in text


def _bloated() -> Vault:
    d = copy.deepcopy(EXAMPLE)
    long = ("Implemented and documented a resilient ingestion service in Python with FastAPI and "
            "PostgreSQL, adding retries, idempotency keys and structured logging for {n} pipelines")
    for p in d["projects"]:
        p["bullets"] = [{"id": f"{p['id']}-b{i}", "text": long.format(n=i + 2)} for i in range(6)]
    d["projects"] += [dict(d["projects"][0], id=f"proj-extra-{k}", name=f"Extra Project {k}",
                           bullets=[{"id": f"x{k}-{i}", "text": long.format(n=i + 9)} for i in range(6)])
                      for k in range(4)]
    d["experience"][0]["bullets"] = [{"id": f"acme-{i}", "text": long.format(n=i + 20)} for i in range(6)]
    d["achievements"] = [{"id": f"ach-{i}", "text": f"Won hackathon number {i + 1} among many teams"} for i in range(6)]
    return Vault.model_validate(d)


def test_trim_loop_reaches_one_page(monkeypatch):
    _engine(monkeypatch, "pdflatex" if "pdflatex" in ENGINES else ENGINES[0])
    v = _bloated()
    jd = prepare_jd(v, JD, "SDE I", "Northwind")
    recipe = build_recipe(v, jd, vault_revision=1)
    # force overflow regardless of the line estimate
    for sec in recipe.projects:
        sec.bullets = [b for b in sec.bullets]
    from app.resume.recipe import RBullet
    for p in v.projects[:4]:
        sec = next((s for s in recipe.projects if s.id == p.id), None)
        if sec:
            have = {b.id for b in sec.bullets}
            sec.bullets += [RBullet(id=b.id, rank=0.01) for b in p.bullets if b.id not in have]
    out = render_pdf(v, recipe)
    assert out.loops >= 1
    assert out.report.one_page
    # stored recipe re-renders to the same page count without more loops
    again = render_pdf(v, out.recipe)
    assert again.loops == 0 and again.report.one_page


def test_injection_text_compiles_literally(monkeypatch):
    engine = "pdflatex" if "pdflatex" in ENGINES else ENGINES[0]
    _engine(monkeypatch, engine)
    d = copy.deepcopy(EXAMPLE)
    d["personal"]["full_name"] = r"Ana \& Bo_b {x} 50% $"
    d["projects"][0]["bullets"][0]["text"] = r"Tried \input{/etc/passwd} and \write18{touch /tmp/pwned} plus #hash ~tilde ^caret"
    v = Vault.model_validate(d)
    jd = prepare_jd(v, JD, "SDE", "X")
    recipe = build_recipe(v, jd, vault_revision=1)
    from app.resume.recipe import RBullet
    recipe.projects[0].bullets.insert(0, RBullet(id="jb-1", rank=99))
    out = render_pdf(v, recipe)
    text = ats.pdf_text(out.pdf)
    assert "root:" not in text                         # /etc/passwd was not read
    assert not Path("/tmp/pwned").exists()
    assert "\\input{/etc/passwd}" in text.replace(" ", "") or "input{/etc/passwd}" in text
    assert "Ana" in text and "50%" in text


def test_ats_flags_missing_heading():
    v = Vault.model_validate(copy.deepcopy(EXAMPLE))
    jd = prepare_jd(v, JD, "SDE", "X")
    recipe = build_recipe(v, jd, vault_revision=1)
    view = build_view(v, recipe)
    pdf = compile_view(view).pdf
    view.sections[1].title = "Volunteering"            # claims a heading the PDF lacks
    report = ats.check(pdf, view, [])
    assert any("Volunteering" in i for i in report.issues)


def test_compile_error_when_engine_missing(monkeypatch):
    _engine(monkeypatch, "nonexistent-engine")
    v = Vault.model_validate(copy.deepcopy(EXAMPLE))
    with pytest.raises(CompileError):
        compile_view(build_view(v, build_recipe(v, prepare_jd(v, JD, "a", "b"), vault_revision=1)))
