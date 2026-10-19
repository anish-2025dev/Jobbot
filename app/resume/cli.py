"""Offline resume generation: the P0 done-test without a database.

    python -m app.resume.cli --vault my_vault.json --jd jd.txt \\
        --title "SDE I" --company "Acme" --out resume.pdf [--docx] [--json recipe.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a tailored, ATS-checked one-page resume")
    ap.add_argument("--vault", required=True, type=Path)
    ap.add_argument("--jd", required=True, type=Path, help="job description text file")
    ap.add_argument("--title", required=True)
    ap.add_argument("--company", required=True)
    ap.add_argument("--bucket", default="high", choices=["high", "medium", "low", "base"])
    ap.add_argument("--out", type=Path, default=Path("resume.pdf"))
    ap.add_argument("--docx", action="store_true", help="also write the DOCX twin")
    ap.add_argument("--json", type=Path, help="write the recipe JSON here")
    ap.add_argument("--llm", action="store_true", help="use the configured LLM for tailoring")
    args = ap.parse_args(argv)

    from app.llm.client import get_llm
    from app.resume.engine import build_recipe, prepare_jd, render_docx, render_pdf
    from app.vault.lint import lint
    from app.vault.schema import Vault

    try:
        vault = Vault.model_validate(json.loads(args.vault.read_text()))
    except ValidationError as e:
        print(f"vault invalid:\n{e}", file=sys.stderr)
        return 2
    for w in lint(vault):
        print(f"warning: {w['path']}: {w['message']}", file=sys.stderr)

    jd = prepare_jd(vault, args.jd.read_text(), args.title, args.company)
    recipe = build_recipe(vault, jd, vault_revision=1, bucket=args.bucket,
                          llm=get_llm() if args.llm else None)
    out = render_pdf(vault, recipe)
    args.out.write_bytes(out.pdf)
    if args.docx:
        args.out.with_suffix(".docx").write_bytes(render_docx(vault, out.recipe))
    if args.json:
        args.json.write_text(json.dumps(out.recipe.to_storage(), indent=2))

    rep = out.report.as_dict()
    print(f"wrote {args.out} via {out.engine} ({rep['pages']} page, {out.loops} trim loops)")
    print(f"ATS: {'OK' if rep['ok'] else 'ISSUES'}  keyword coverage {rep['keyword_coverage']:.0%}")
    for issue in rep["issues"]:
        print(f"  issue: {issue}")
    if out.recipe.missing_skills:
        print(f"JD must-haves not in your vault: {', '.join(out.recipe.missing_skills)}")
    v = out.recipe.validator
    for line in v.reverted_bullets + [f"stripped skill: {x}" for x in v.stripped_skills]:
        print(f"validator: {line}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
