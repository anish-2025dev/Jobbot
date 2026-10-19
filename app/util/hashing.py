"""Stable 64-bit hashes stored as signed bigint, plus normalizers.

canonical_hash : identity of one opening on one ATS (req ID when available).
fingerprint    : cross-portal identity: normalized company + title + location.
company_key    : normalized employer name (caps and cooldowns key on this).
simhash        : near-duplicate JD signature for repost detection.
"""
from __future__ import annotations

import re
import unicodedata

import xxhash

_SIGN = 1 << 63
_LEGAL_SUFFIXES = re.compile(
    r"\b(private limited|pvt\.? ?ltd\.?|pvt|ltd\.?|limited|inc\.?|incorporated|"
    r"llc|llp|plc|corp\.?|corporation|co\.?|gmbh|s\.?a\.?|b\.?v\.?)$"
)
# Requisition codes: "(R123456)", "JR10234", "Req#4512", bare 5+ digit IDs.
# Never 4-digit years: "SDE Intern 2026" and "... 2027" are different openings.
_REQ_CODE = re.compile(
    r"[\(\[]\s*(?:req|r|jr|job)?\s*[-#:]?\s*\d{4,}\s*[\)\]]"
    r"|\b(?:req|jr|job)\s*[-#:]?\s*\d{3,}\b"
    r"|\br\d{4,}\b"
    r"|\b\d{5,}\b",
    re.I,
)
_PARENS = re.compile(r"\([^)]*\)")
# Country-subsidiary words dropped from company keys so caps (C5) apply to
# "Google India Pvt Ltd" and "Google" alike.
_COUNTRY_SUFFIX = re.compile(r"\b(india|usa|uk|emea|apac|global)$")
_CITY_ALIASES = {
    "bangalore": "bengaluru", "bombay": "mumbai", "gurgaon": "gurugram",
    "new delhi": "delhi", "ncr": "delhi", "calcutta": "kolkata",
    "madras": "chennai",
}


def h64(text: str) -> int:
    """xxh64 of UTF-8 text as a signed 64-bit int (fits Postgres bigint)."""
    v = xxhash.xxh64_intdigest(text.encode("utf-8"))
    return v - (1 << 64) if v >= _SIGN else v


def _basic(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def company_key(name: str) -> str:
    """'Razorpay Software Pvt. Ltd.' -> 'razorpay software'. Strips legal
    suffixes only; never words like 'technologies' that distinguish employers."""
    key = unicodedata.normalize("NFKC", name or "").lower()
    key = _PARENS.sub(" ", key).strip()
    for _ in range(3):  # 'xyz india pvt ltd' -> 'xyz india' -> 'xyz'
        key = _LEGAL_SUFFIXES.sub("", key).strip(" ,.-&")
        key = re.sub(r"\s+", " ", key)
        key = _COUNTRY_SUFFIX.sub("", key).strip(" ,.-&") if " " in key else key
    return _basic(key)


def normalize_title(title: str) -> str:
    return _basic(_REQ_CODE.sub(" ", title or ""))


def normalize_location(location: str | None) -> str:
    loc = _basic(location or "")
    for alias, city in _CITY_ALIASES.items():
        loc = re.sub(rf"\b{alias}\b", city, loc)
    return loc


def canonical_hash(ats: str, company: str, req_id: str | None, url: str) -> int:
    """Prefer the ATS requisition ID; fall back to the URL without query string."""
    ident = req_id.strip().lower() if req_id else re.sub(r"[?#].*$", "", url.strip().lower())
    return h64(f"{ats}|{company_key(company)}|{ident}")


def fingerprint(company: str, title: str, location: str | None) -> int:
    return h64(f"{company_key(company)}|{normalize_title(title)}|{normalize_location(location)}")


def simhash(text: str, shingle: int = 3) -> int:
    """64-bit SimHash over word 3-shingles. Reposts with the same JD differ by
    only a few bits; compare with hamming(a, b) <= REPOST_HAMMING_MAX."""
    words = _basic(text).split()
    if len(words) < shingle:
        words = words + [""] * (shingle - len(words))
    weights = [0] * 64
    shingles = {" ".join(words[i : i + shingle]) for i in range(len(words) - shingle + 1)}
    for sh in shingles:
        hv = xxhash.xxh64_intdigest(sh.encode("utf-8"))
        for bit in range(64):
            weights[bit] += 1 if (hv >> bit) & 1 else -1
    v = sum(1 << b for b in range(64) if weights[b] > 0)
    return v - (1 << 64) if v >= _SIGN else v


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()
