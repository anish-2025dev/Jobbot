"""Resume recipe: the ~2 KB JSON stored instead of a PDF (C2).

A recipe references vault items by id. `text` on a bullet is set only when
the tailoring step rephrased it (and the truth validator accepted it);
otherwise the vault text is used verbatim at render time. Re-rendering a
recipe against the same vault revision always produces the same resume.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SectionName = Literal["summary", "education", "experience", "projects", "skills", "achievements", "certifications"]
# The fixed layout: header, summary (when the vault has one), experience, skills, projects,
# then education and the short lists. Empty sections are left out.
DEFAULT_ORDER: list[SectionName] = ["summary", "experience", "skills", "projects", "education",
                                    "certifications", "achievements"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RBullet(_M):
    id: str
    text: str | None = None       # None = vault text verbatim
    rank: float = 0.0             # higher = more relevant; lowest dropped first


class RSection(_M):
    id: str                        # experience or project id
    bullets: list[RBullet]


class RSkillGroup(_M):
    category: str
    items: list[str]


class ValidatorReport(_M):
    stripped_skills: list[str] = Field(default_factory=list)
    reverted_bullets: list[str] = Field(default_factory=list)
    dropped_ids: list[str] = Field(default_factory=list)
    removed_sentences: list[str] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.stripped_skills or self.reverted_bullets or self.dropped_ids or self.removed_sentences)


class Recipe(_M):
    v: Literal[1] = 1
    vault_revision: int
    jd_hash: int = 0
    title: str = ""
    company: str = ""
    bucket: Literal["high", "medium", "low", "stretch", "base"] = "base"
    tailoring: Literal["llm", "heuristic", "cached"] = "heuristic"
    order: list[SectionName] = Field(default_factory=lambda: list(DEFAULT_ORDER))
    role_id: str | None = None                                  # the target role this resume was built for
    summary: str | None = None                                  # vault text verbatim (role's, else the general one)
    experience: list[RSection] = Field(default_factory=list)
    projects: list[RSection] = Field(default_factory=list)
    skills: list[RSkillGroup] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)       # achievement bullet ids
    keywords: list[str] = Field(default_factory=list)           # JD keywords the vault supports
    missing_skills: list[str] = Field(default_factory=list)     # JD must-haves the vault lacks
    cover_letter: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    validator: ValidatorReport = Field(default_factory=ValidatorReport)

    def all_bullets(self) -> list[tuple[str, RBullet]]:
        out = []
        for sec in self.experience + self.projects:
            out += [(sec.id, b) for b in sec.bullets]
        return out

    def size_bytes(self) -> int:
        return len(self.model_dump_json(exclude_defaults=True).encode())

    def to_storage(self) -> dict:
        return self.model_dump(mode="json", exclude_defaults=True)
