"""Vault: the only source of truth for resume content (C11).

Everything the resume engine may say must exist here. Facts never get
defaults: an unknown answer stays None and routes the form to Assisted mode
instead of the bot guessing.

Schema v2 (v1 vaults still validate) adds the application profile that
forms ask for (name parts, date of birth, addresses, family details,
languages, Class X/XII scores, backlogs, bond/shift/laptop answers) and
target roles: one vault, one resume per job, built for the role the job
title matches. Identity documents (PAN, Aadhaar, passport) are NOT here:
they live encrypted in app/vault/identity.py and never reach the LLM.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (AfterValidator, BaseModel, ConfigDict, Field,
                      StringConstraints, field_validator, model_validator)

MAX_VAULT_BYTES = 64 * 1024   # spec budgets ~50 KB/user
MAX_PROJECTS = 10
MAX_SKILLS = 200              # the whole pool; a resume shows the ~25 that fit the job
MAX_TARGET_ROLES = 10


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
Date = Annotated[str, StringConstraints(pattern=r"^(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")]
Url = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^https?://\S+$", max_length=300)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Prose = Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=600)]
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


class Address(_Model):
    line1: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    line2: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None
    city: Short
    district: Short | None = None
    state: Short
    pincode: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9 -]{3,10}$")]
    country: Short


class Personal(_Model):
    """Required: full_name, email, phone, location. The rest answers the forms that ask
    for it (first/last name split, date of birth, addresses...)."""
    full_name: Short
    first_name: Name | None = None          # derived from full_name when left empty
    middle_name: Name | None = None
    last_name: Name | None = None
    preferred_name: Name | None = None
    email: Email
    alternate_email: Email | None = None
    phone: Phone
    alternate_phone: Phone | None = None
    whatsapp_phone: Phone | None = None
    date_of_birth: Date | None = None
    gender: Literal["male", "female", "non_binary", "other"] | None = None
    pronouns: Annotated[str, StringConstraints(max_length=30)] | None = None
    nationality: Short | None = None
    marital_status: Literal["single", "married", "divorced", "widowed", "other"] | None = None
    location: Location
    current_address: Address | None = None
    permanent_address: Address | None = None   # None = same as current
    links: list[Link] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def split_name(self) -> "Personal":
        parts = self.full_name.split()
        if self.first_name is None and parts:
            self.first_name = parts[0]
        if self.last_name is None and len(parts) > 1:
            self.last_name = parts[-1]
        if self.middle_name is None and len(parts) > 2:
            self.middle_name = " ".join(parts[1:-1])
        return self

    def age_on(self, today) -> int | None:
        if not self.date_of_birth:
            return None
        y, m, d = (int(x) for x in self.date_of_birth.split("-"))
        return today.year - y - ((today.month, today.day) < (m, d))


class Language(_Model):
    name: Short
    proficiency: Literal["native", "fluent", "professional", "intermediate", "basic"] = "professional"
    read: bool = True
    write: bool = True
    speak: bool = True


class Background(_Model):
    """Questions Indian (and some US) application forms ask. All optional; the
    self-identification answers (gender, category, disability, veteran) are only
    given to forms when eeo_disclosure is "share"; otherwise the bot declines."""
    father_name: Short | None = None
    mother_name: Short | None = None
    category: Literal["general", "obc", "sc", "st", "ews", "other"] | None = None
    person_with_disability: bool | None = None
    disability_details: Short | None = None
    veteran: bool | None = None
    languages: list[Language] = Field(default_factory=list, max_length=10)
    hobbies: TagList = Field(default_factory=list, max_length=10)
    eeo_disclosure: Literal["decline", "share"] = "decline"


class Bullet(_Model):
    id: Slug
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=300)]

    @field_validator("text")
    @classmethod
    def single_line(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("bullet must be a single line")
        return v


EducationLevel = Literal["class_x", "class_xii", "diploma", "bachelors", "masters", "phd", "certificate", "other"]


def guess_level(degree: str) -> str:
    d = degree.lower()
    if re.search(r"\b(xii|12|12th|hsc|higher secondary|senior secondary|intermediate|puc)\b", d):
        return "class_xii"
    if re.search(r"\b(x|10|10th|ssc|secondary|matric|matriculation)\b", d):
        return "class_x"
    if re.search(r"diploma|polytechnic", d):
        return "diploma"
    if re.search(r"ph\.?\s?d|doctor", d):
        return "phd"
    if re.search(r"\bm\.?\s?(tech|e|sc|s|ca|ba|com)\b|master|mba|pgdm|post ?graduate", d):
        return "masters"
    if re.search(r"\bb\.?\s?(tech|e|sc|s|ca|ba|com|ba)\b|bachelor|undergraduate|\bbs\b|\bba\b", d):
        return "bachelors"
    return "other"


class Education(_Model):
    level: EducationLevel | None = None       # None = guessed from the degree name
    institution: Short
    degree: Short
    field_of_study: Short | None = None
    start: YearMonth | None = None
    end: YearMonth | None = None
    graduation_year: int = Field(ge=1990, le=2100)
    grade: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    score: float | None = Field(default=None, ge=0, le=100)          # 8.4 or 91.2
    score_scale: Literal["cgpa_10", "cgpa_4", "percentage"] | None = None
    board: Short | None = None                                        # CBSE / ICSE / state board (Class X/XII)
    location: Short | None = None
    active_backlogs: int | None = Field(default=None, ge=0, le=60)
    backlog_history: int | None = Field(default=None, ge=0, le=60)
    coursework: TagList = Field(default_factory=list, max_length=20)
    show_on_resume: bool = True

    @model_validator(mode="after")
    def dates(self) -> "Education":
        _check_range(self.start, self.end, self.institution)
        if self.level is None:
            self.level = guess_level(self.degree)  # type: ignore[assignment]
        if self.score_scale == "cgpa_10" and self.score is not None and self.score > 10:
            raise ValueError(f"{self.institution}: a CGPA out of 10 cannot be {self.score:g}")
        if self.score_scale == "cgpa_4" and self.score is not None and self.score > 4:
            raise ValueError(f"{self.institution}: a GPA out of 4 cannot be {self.score:g}")
        return self

    def score_text(self) -> str | None:
        """'CGPA 8.4/10', '91.2%', or the free-text grade."""
        if self.score is None:
            return self.grade
        return {"cgpa_10": f"CGPA {self.score:g}/10", "cgpa_4": f"GPA {self.score:g}/4",
                "percentage": f"{self.score:g}%"}.get(self.score_scale or "", self.grade or f"{self.score:g}")

    def percentage(self) -> float | None:
        if self.score is None:
            return None
        if self.score_scale == "percentage":
            return self.score
        if self.score_scale == "cgpa_10":
            return round(self.score * 9.5, 1)       # the common CBSE/AICTE conversion
        return None


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
    roles: list[Slug] = Field(default_factory=list, max_length=MAX_TARGET_ROLES)   # target roles it suits best

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
    roles: list[Slug] = Field(default_factory=list, max_length=MAX_TARGET_ROLES)   # target roles it suits best
    bullets: list[Bullet] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def dates(self) -> "Project":
        _check_range(self.start, self.end, self.id)
        return self


class SkillGroup(_Model):
    category: Short
    items: TagList = Field(min_length=1, max_length=80)


class Certification(_Model):
    name: Short
    issuer: Short | None = None
    date: YearMonth | None = None
    url: Url | None = None


class TargetRole(_Model):
    """A kind of job the user wants (SDE, Data Analyst, ...). Each job's resume is
    built for the role its title matches best: that role's summary, its skills
    first, its projects preferred. Everything still comes from this vault (C11)."""
    id: Slug
    name: Short                                                              # "Software Engineer"
    title_keywords: TagList = Field(default_factory=list, max_length=20)     # "sde", "backend developer"
    summary: Prose | None = None
    skills: TagList = Field(default_factory=list, max_length=60)             # from the pool, most important first
    projects: list[Slug] = Field(default_factory=list, max_length=MAX_PROJECTS)   # preferred, best first


class StandardAnswers(_Model):
    expected_ctc_lpa: float | None = Field(default=None, ge=0, le=500)
    expected_ctc_negotiable: bool | None = None
    current_ctc_lpa: float | None = Field(default=None, ge=0, le=500)
    notice_period_days: int | None = Field(default=None, ge=0, le=180)
    earliest_start: YearMonth | None = None
    currently_employed: bool | None = None
    currently_studying: bool | None = None
    willing_to_relocate: bool | None = None
    relocation_cities: list[Short] = Field(default_factory=list, max_length=20)
    work_authorization: Short | None = None
    requires_sponsorship: bool | None = None
    preferred_work_mode: Literal["onsite", "hybrid", "remote", "any"] | None = None
    willing_to_travel: bool | None = None
    willing_night_shifts: bool | None = None
    willing_to_sign_bond: bool | None = None
    max_bond_months: int | None = Field(default=None, ge=0, le=60)
    has_laptop: bool | None = None
    has_reliable_internet: bool | None = None
    education_gap_years: int | None = Field(default=None, ge=0, le=20)
    gap_reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
    criminal_record: bool | None = None
    background_check_consent: bool | None = None
    heard_about_us: Short | None = None              # default answer to "how did you hear about us"
    custom: dict[Annotated[str, StringConstraints(min_length=3, max_length=200)],
                 Annotated[str, StringConstraints(max_length=1000)]] = Field(default_factory=dict, max_length=50)


class Expectations(_Model):
    locations: TagList = Field(min_length=1, max_length=20)
    remote_types: list[Literal["onsite", "hybrid", "remote"]] = Field(min_length=1)
    role_types: TagList = Field(default_factory=list, max_length=20)     # plus every target role's name
    min_ctc_lpa: float | None = Field(default=None, ge=0, le=500)
    experience_level: Literal["fresher", "junior", "mid"]
    blacklisted_companies: TagList = Field(default_factory=list, max_length=200)


class Vault(_Model):
    schema_version: Literal[1, 2] = 2
    personal: Personal
    background: Background = Field(default_factory=Background)
    summary: Prose | None = None                       # used when the matched role has none
    target_roles: list[TargetRole] = Field(default_factory=list, max_length=MAX_TARGET_ROLES)
    education: list[Education] = Field(min_length=1, max_length=8)
    experience: list[Experience] = Field(default_factory=list, max_length=15)
    projects: list[Project] = Field(min_length=1, max_length=MAX_PROJECTS)
    skills: list[SkillGroup] = Field(min_length=1, max_length=15)
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
        total = len({i.casefold() for g in self.skills for i in g.items})
        if total > MAX_SKILLS:
            raise ValueError(f"{total} skills in the pool; limit is {MAX_SKILLS}")
        role_ids = [r.id for r in self.target_roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("target role ids must be unique")
        project_ids = {p.id for p in self.projects}
        for r in self.target_roles:
            unknown = [p for p in r.projects if p not in project_ids]
            if unknown:
                raise ValueError(f"target role '{r.id}' lists unknown projects: {unknown}")
        for owner in list(self.projects) + list(self.experience):
            unknown = [x for x in owner.roles if x not in role_ids]
            if unknown:
                raise ValueError(f"'{owner.id}' refers to unknown target roles: {unknown}")
        if not self.expectations.role_types and not self.target_roles:
            raise ValueError("add at least one target role (or expectations.role_types)")
        self.schema_version = 2            # v1 vaults are upgraded on load; every v1 field still means the same
        size = len(self.model_dump_json(exclude_none=True).encode())
        if size > MAX_VAULT_BYTES:
            raise ValueError(f"vault is {size} bytes; limit is {MAX_VAULT_BYTES}")
        return self

    @property
    def graduation_year(self) -> int:
        """Latest graduation year: drives the batch-eligibility hard filter."""
        return max(e.graduation_year for e in self.higher_education())

    def higher_education(self) -> list[Education]:
        """Degrees and diplomas; Class X/XII only count when nothing else exists."""
        higher = [e for e in self.education if e.level not in ("class_x", "class_xii")]
        return higher or list(self.education)

    def school(self, level: str) -> Education | None:
        return next((e for e in self.education if e.level == level), None)

    @property
    def all_role_types(self) -> list[str]:
        return list(dict.fromkeys([*self.expectations.role_types, *(r.name for r in self.target_roles)]))

    def role(self, role_id: str | None) -> TargetRole | None:
        return next((r for r in self.target_roles if r.id == role_id), None)

    def project(self, project_id: str) -> Project | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def to_storage(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)
