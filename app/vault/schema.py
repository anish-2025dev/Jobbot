"""Vault: the only source of truth for resume content (C11).

Everything the resume engine may say must exist here. Facts never get
defaults: an unknown answer stays None and routes the form to Assisted mode
instead of the bot guessing.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field,
                      StringConstraints, field_validator, model_validator)

MAX_VAULT_BYTES = 64 * 1024   # spec budgets ~50 KB/user


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")]
YearMonth = Annotated[str, StringConstraints(pattern=r"^(19|20)\d{2}-(0[1-9]|1[0-2])$")]
Url = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^https?://\S+$", max_length=300)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
TagList = Annotated[list[Tag], AfterValidator(_dedupe)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)]
Phone = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\+?[0-9 ()-]{7,20}$")]


def _check_range(start: str | None, end: str | None, what: str) -> None:
    if start and end and end < start:          # YYYY-MM compares lexically
        raise ValueError(f"{what}: end {end} is before start {start}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Link(_Model):
    label: Short
    url: Url


class Location(_Model):
    city: Short
    country: Short


class Personal(_Model):
    full_name: Short
    email: Email
    phone: Phone
    location: Location
    links: list[Link] = Field(default_factory=list, max_length=8)


class Bullet(_Model):
    id: Slug
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=300)]

    @field_validator("text")
    @classmethod
    def single_line(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("bullet must be a single line")
        return v


class Education(_Model):
    institution: Short
    degree: Short
    field_of_study: Short | None = None
    start: YearMonth | None = None
    end: YearMonth | None = None
    graduation_year: int = Field(ge=1990, le=2100)
    grade: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    coursework: TagList = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def dates(self) -> "Education":
        _check_range(self.start, self.end, self.institution)
        return self


class Experience(_Model):
    id: Slug
    company: Short
    title: Short
    location: Short | None = None
    employment_type: Literal["full_time", "internship", "part_time", "contract", "freelance"]
    start: YearMonth
    end: YearMonth | None = None                  # None = current role
    bullets: list[Bullet] = Field(min_length=1, max_length=8)
    tech: TagList = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def dates(self) -> "Experience":
        _check_range(self.start, self.end, self.id)
        return self


class Project(_Model):
    id: Slug
    name: Short
    url: Url | None = None
    repo: Url | None = None
    start: YearMonth | None = None
    end: YearMonth | None = None
    tech: TagList = Field(min_length=1, max_length=30)
    tags: TagList = Field(default_factory=list, max_length=10)   # e.g. backend, ml, security
    bullets: list[Bullet] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def dates(self) -> "Project":
        _check_range(self.start, self.end, self.id)
        return self


class SkillGroup(_Model):
    category: Short
    items: TagList = Field(min_length=1, max_length=40)


class Certification(_Model):
    name: Short
    issuer: Short | None = None
    date: YearMonth | None = None
    url: Url | None = None


class StandardAnswers(_Model):
    expected_ctc_lpa: float | None = Field(default=None, ge=0, le=500)
    current_ctc_lpa: float | None = Field(default=None, ge=0, le=500)
    notice_period_days: int | None = Field(default=None, ge=0, le=180)
    earliest_start: YearMonth | None = None
    willing_to_relocate: bool | None = None
    relocation_cities: list[Short] = Field(default_factory=list, max_length=20)
    work_authorization: Short | None = None
    requires_sponsorship: bool | None = None
    custom: dict[Annotated[str, StringConstraints(min_length=3, max_length=200)],
                 Annotated[str, StringConstraints(max_length=1000)]] = Field(default_factory=dict, max_length=50)


class Expectations(_Model):
    locations: TagList = Field(min_length=1, max_length=20)
    remote_types: list[Literal["onsite", "hybrid", "remote"]] = Field(min_length=1)
    role_types: TagList = Field(min_length=1, max_length=20)
    min_ctc_lpa: float | None = Field(default=None, ge=0, le=500)
    experience_level: Literal["fresher", "junior", "mid"]
    blacklisted_companies: TagList = Field(default_factory=list, max_length=200)


class Vault(_Model):
    schema_version: Literal[1] = 1
    personal: Personal
    education: list[Education] = Field(min_length=1, max_length=6)
    experience: list[Experience] = Field(default_factory=list, max_length=15)
    projects: list[Project] = Field(min_length=1, max_length=40)
    skills: list[SkillGroup] = Field(min_length=1, max_length=12)
    extra_keywords: TagList = Field(default_factory=list, max_length=100)
    certifications: list[Certification] = Field(default_factory=list, max_length=20)
    achievements: list[Bullet] = Field(default_factory=list, max_length=15)
    standard_answers: StandardAnswers = Field(default_factory=StandardAnswers)
    expectations: Expectations

    @model_validator(mode="after")
    def integrity(self) -> "Vault":
        ids: list[str] = []
        for e in self.experience:
            ids += [e.id] + [b.id for b in e.bullets]
        for p in self.projects:
            ids += [p.id] + [b.id for b in p.bullets]
        ids += [a.id for a in self.achievements]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"ids must be unique across the vault: {dupes}")
        cats = [g.category.casefold() for g in self.skills]
        if len(cats) != len(set(cats)):
            raise ValueError("skill categories must be unique")
        size = len(self.model_dump_json(exclude_none=True).encode())
        if size > MAX_VAULT_BYTES:
            raise ValueError(f"vault is {size} bytes; limit is {MAX_VAULT_BYTES}")
        return self

    @property
    def graduation_year(self) -> int:
        """Latest graduation year: drives the batch-eligibility hard filter."""
        return max(e.graduation_year for e in self.education)

    def project(self, project_id: str) -> Project | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def to_storage(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)
