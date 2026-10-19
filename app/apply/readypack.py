"""Ready Pack (section 9): everything to finish an application in about a
minute. ~10 KB JSON stored on the waiting-list row."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings
from app.resume.cover import standard_answers
from app.resume.recipe import Recipe
from app.util.skills import norm_skill
from app.vault.schema import Vault

# Related skills: a missing must-have can be honestly addressed by pointing
# at real vault work in an adjacent tool (never by claiming the skill).
RELATED = {
    "kubernetes": ["docker", "helm"], "aws": ["google cloud", "azure"], "google cloud": ["aws", "azure"],
    "azure": ["aws", "google cloud"], "kafka": ["rabbitmq", "redis", "celery"], "rabbitmq": ["kafka", "redis"],
    "react": ["vue", "angular", "next.js"], "vue": ["react", "angular"], "angular": ["react", "vue"],
    "java": ["c#", "kotlin", "scala"], "c#": ["java", ".net"], "kotlin": ["java"], "go": ["python", "rust", "c++"],
    "mysql": ["postgresql", "sql server", "sql"], "postgresql": ["mysql", "sql server", "sql"],
    "mongodb": ["dynamodb", "redis"], "spark": ["pandas", "dbt", "airflow"], "terraform": ["ansible", "docker"],
    "typescript": ["javascript"], "node.js": ["express", "javascript"], "django": ["flask", "fastapi"],
    "flask": ["fastapi", "django"], "fastapi": ["flask", "django"], "spring boot": ["asp.net core", "django"],
    "graphql": ["rest apis", "grpc"], "grpc": ["rest apis"], "pytorch": ["tensorflow", "scikit-learn"],
    "tensorflow": ["pytorch", "scikit-learn"], "elasticsearch": ["opensearch"], "opensearch": ["elasticsearch"],
}


def gap_notes(vault: Vault, missing: list[str]) -> list[dict]:
    texts = [(f"{o.name if hasattr(o, 'name') else o.company}", b.text, {norm_skill(t) for t in o.tech})
             for o in list(vault.projects) + list(vault.experience) for b in o.bullets]
    out = []
    for skill in missing[:8]:
        related = RELATED.get(skill, [])
        evidence = []
        for owner, text_, tech in texts:
            hit = [r for r in related if r in tech or r in text_.lower()]
            if hit:
                evidence.append({"from": owner, "related": hit[0], "text": text_})
            if len(evidence) >= 2:
                break
        out.append({"skill": skill, "related_evidence": evidence})
    return out


def build(app: dict, vault: Vault, recipe: Recipe | None, *, expires_at: datetime | None = None,
          referral: dict | None = None, extra: dict | None = None) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    uid, aid = app["user_id"], app["id"]
    answers = dict(standard_answers(vault))
    if recipe:
        answers.update(recipe.answers)
    links = {l.label: l.url for l in vault.personal.links}
    pack = {
        "title": app["title"], "company": app["company_name"], "apply_url": app["apply_url"],
        "location": app.get("location"), "score": app.get("score"), "bucket": app.get("bucket"),
        "salary_max": app.get("salary_max"),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "resume_pdf": f"{base}/users/{uid}/applications/{aid}/resume.pdf",
        "resume_docx": f"{base}/users/{uid}/applications/{aid}/resume.docx",
        "continue_url": f"{base}/app/#/apply/{aid}",
        "cover_letter": recipe.cover_letter if recipe else "",
        "copy": {"Full name": vault.personal.full_name, "Email": vault.personal.email, "Phone": vault.personal.phone,
                 "Location": f"{vault.personal.location.city}, {vault.personal.location.country}", **links, **answers},
        "failure": {"step": app.get("failure_step"), "reason": app.get("failure_reason"),
                    "screenshot": app.get("screenshot_url")},
        "gaps": gap_notes(vault, list(app.get("missing_skills") or [])),
        "referral": referral,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        pack.update(extra)
    return pack
