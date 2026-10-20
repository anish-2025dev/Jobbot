"""Everything the truth validator (C11) may accept, derived from the vault.

Numbers are compared in canonical form so "1.2k", "1,200" and "1200" are the
same fact, while "1500" is not. Skills compare after alias folding so
"Postgres" in a JD matches "PostgreSQL" in the vault, but nothing new is
ever invented.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from app.vault.schema import Vault

from app.util.skills import ALIASES as SKILL_ALIASES  # noqa: E402,F401
from app.util.skills import norm_skill  # noqa: E402,F401

_NUM = re.compile(
    r"(?<![\w.])[₹$€£]?"
    r"(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?"
    r"(?:\s?(%|[kKmMbB](?![a-zA-Z])|[xX](?![a-zA-Z])"
    r"|(?:million|mn|billion|bn|thousand|lakhs?|lacs?|crores?|cr)\b))?",
    re.I,
)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mn": 1e6, "b": 1e9, "billion": 1e9,
         "bn": 1e9, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "crore": 1e7, "crores": 1e7,
         "cr": 1e7}


def extract_numbers(text: str) -> list[str]:
    """Canonical numeric facts in text: '40%', '10x', '1200', '2.5', and
    magnitude words folded in ('5 million' -> '5000000', '2 lakh' -> '200000')."""
    out = []
    for whole, frac, unit in _NUM.findall(text or ""):
        value = float(whole.replace(",", "") + (f".{frac}" if frac else ""))
        unit = (unit or "").lower()
        if unit in _MULT:
            value *= _MULT[unit]
            unit = ""
        text_value = str(int(value)) if value == int(value) else f"{value:g}"
        out.append(f"{text_value}{unit}")
    return out


@dataclass
class VaultFacts:
    skills: set[str] = field(default_factory=set)      # normalized
    numbers: set[str] = field(default_factory=set)     # canonical
    bullets: dict[str, str] = field(default_factory=dict)

    def untraceable_numbers(self, text: str) -> list[str]:
        return [n for n in extract_numbers(text) if n not in self.numbers]

    def untraceable_skills(self, skills: Iterable[str]) -> list[str]:
        return [s for s in skills if norm_skill(s) not in self.skills]


def vault_facts(v: "Vault") -> VaultFacts:
    f = VaultFacts()
    skill_lists = [g.items for g in v.skills] + [v.extra_keywords]
    skill_lists += [e.tech for e in v.experience] + [p.tech for p in v.projects]
    skill_lists += [ed.coursework for ed in v.education]
    for lst in skill_lists:
        f.skills.update(norm_skill(s) for s in lst)
    f.skills.update(norm_skill(c.name) for c in v.certifications)

    texts: list[str] = []
    for owner in list(v.experience) + list(v.projects):
        for b in owner.bullets:
            f.bullets[b.id] = b.text
            texts.append(b.text)
    for a in v.achievements:
        f.bullets[a.id] = a.text
        texts.append(a.text)
    texts += [ed.grade or "" for ed in v.education]
    texts += [ed.score_text() or "" for ed in v.education]
    texts += [r.summary or "" for r in v.target_roles] + [v.summary or ""]
    texts += [str(ed.graduation_year) for ed in v.education]
    sa = v.standard_answers
    texts += [str(x) for x in (sa.expected_ctc_lpa, sa.current_ctc_lpa, sa.notice_period_days) if x is not None]
    if sa.earliest_start:
        texts.append(sa.earliest_start.split("-")[0])
    texts += list(sa.custom.values())
    for t in texts:
        f.numbers.update(extract_numbers(t))
    return f
