"""What every fetcher returns, before normalization."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawJob:
    source: str                         # sources.key, e.g. "greenhouse", "jsearch"
    ats: str                            # ats_type enum value ("unknown" for aggregators)
    company: str
    title: str
    url: str                            # posting page
    description: str = ""               # HTML or text (see description_is_html)
    description_is_html: bool = True
    apply_url: str | None = None
    req_id: str | None = None           # ATS requisition / posting id
    company_slug: str | None = None
    location: str | None = None
    locations: list[str] = field(default_factory=list)
    posted_at: datetime | None = None
    workplace: str | None = None        # remote | hybrid | onsite (when the source says)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None    # year | month | hour
    salary_text: str | None = None
    experience_months: int | None = None
    employment_type: str | None = None
    company_domain: str | None = None
    easy_apply_only: bool = False       # LinkedIn/Naukri Easy Apply etc. -> manual only (C12)
