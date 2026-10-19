"""A job description prepared for selection, scoring and ATS checks.

The full text lives only in memory during processing (C3); what persists is
the summary, skills, hash and embedding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property

from app.embed import embed
from app.util.hashing import h64
from app.util.skills import extract_skills, must_have_skills
from app.util.text import squash


@dataclass
class JobDescription:
    text: str
    title: str = ""
    company: str = ""
    location: str = ""
    extra_terms: tuple[str, ...] = field(default_factory=tuple)   # vault skills, for detection

    @cached_property
    def jd_hash(self) -> int:
        return h64(squash(f"{self.title}\n{self.text}").lower())

    @cached_property
    def skills(self) -> list[str]:
        return extract_skills(f"{self.title}\n{self.text}", self.extra_terms)

    @cached_property
    def must_nice(self) -> tuple[list[str], list[str]]:
        return must_have_skills(self.text, self.extra_terms)

    @property
    def must(self) -> list[str]:
        return self.must_nice[0]

    @property
    def nice(self) -> list[str]:
        return self.must_nice[1]

    @cached_property
    def embedding(self) -> list[float]:
        return embed(f"{self.title}\n{self.text}")
