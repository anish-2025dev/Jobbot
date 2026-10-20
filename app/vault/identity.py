"""Identity documents some application forms ask for (PAN, Aadhaar, passport...).

Kept out of the vault on purpose: the vault is plain JSONB, feeds the LLM and
is copied into Ready Packs. These values are encrypted at rest in
`credentials` (host = 'identity', AES-GCM bound to the user, section 13),
decrypted only inside the apply worker, never logged, never sent to an LLM,
and the API only ever returns them masked.
"""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, StringConstraints, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.vault.schema import Date, Short

HOST = "identity"


def _upper(value):
    return value.strip().upper() if isinstance(value, str) else value


def _id(pattern: str):
    return Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=pattern)]


class Passport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    number: _id(r"^[A-Z0-9]{6,9}$")
    issue_date: Date | None = None
    expiry_date: Date | None = None
    place_of_issue: Short | None = None
    issuing_country: Short = "India"

    @model_validator(mode="after")
    def dates(self) -> "Passport":
        if self.issue_date and self.expiry_date and self.expiry_date <= self.issue_date:
            raise ValueError("passport expiry must be after its issue date")
        return self


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    pan: _id(r"^[A-Z]{5}[0-9]{4}[A-Z]$") | None = None
    aadhaar: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[2-9][0-9]{3} ?[0-9]{4} ?[0-9]{4}$")] | None = None
    passport: Passport | None = None
    driving_licence: _id(r"^[A-Z0-9 -]{8,20}$") | None = None
    voter_id: _id(r"^[A-Z0-9]{8,12}$") | None = None
    uan: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[0-9]{12}$")] | None = None   # EPF UAN

    def values(self) -> dict[str, str]:
        """Flat field -> value, as the form filler uses them."""
        out = {k: getattr(self, k) for k in ("pan", "driving_licence", "voter_id", "uan") if getattr(self, k)}
        if self.aadhaar:
            out["aadhaar"] = self.aadhaar.replace(" ", "")
        if self.passport:
            p = self.passport
            out.update({k: v for k, v in {
                "passport_number": p.number, "passport_issue_date": p.issue_date,
                "passport_expiry_date": p.expiry_date, "passport_place_of_issue": p.place_of_issue,
                "passport_country": p.issuing_country}.items() if v})
        return out


def mask(value: str) -> str:
    v = value.replace(" ", "")
    return "•" * max(0, len(v) - 4) + v[-4:]


def masked(ident: Identity) -> dict:
    """What the API may show: last 4 characters of each number, dates in full."""
    out: dict = {}
    for k in ("pan", "aadhaar", "driving_licence", "voter_id", "uan"):
        if getattr(ident, k):
            out[k] = mask(getattr(ident, k))
    if ident.passport:
        out["passport"] = {**ident.passport.model_dump(exclude_none=True), "number": mask(ident.passport.number)}
    return out


def load(s: Session, user_id: UUID) -> Identity:
    from app.mail.accounts import load_credential
    found = load_credential(s, user_id, HOST)
    return Identity.model_validate(found[1]) if found else Identity()


def save(s: Session, user_id: UUID, ident: Identity) -> None:
    from app.mail.accounts import save_credential
    data = ident.model_dump(mode="json", exclude_none=True)
    if not data:
        s.execute(text("DELETE FROM credentials WHERE user_id = :u AND host = :h"), {"u": user_id, "h": HOST})
        return
    save_credential(s, user_id, "identity", "identity", data, {}, host=HOST)
