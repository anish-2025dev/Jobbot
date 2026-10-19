"""Text helpers shared by ingestion, the resume engine and mail parsing."""
from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "table", "section", "article", "header", "footer", "blockquote", "pre",
}
_SKIP_TAGS = {"script", "style", "noscript", "head", "svg"}

STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have he her his
how i if in into is it its may me more most my no not of on or our out over she so some such
than that the their them then there these they this to too under up us very was we were what
when where which who will with would you your also about after all any both each few further
here just much must only other own same should through until while within without able across
etc using use used via per new well work working team teams role roles join help strong good
including include includes experience years year plus ability skills skill knowledge looking
""".split())


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._link_text).split())))
            self._href = None

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
            if self._href is not None:
                self._link_text.append(data)


def html_to_text(raw: str | None) -> str:
    """HTML (possibly entity-escaped, as Greenhouse returns it) to plain text
    with one paragraph or list item per line."""
    if not raw:
        return ""
    if "&lt;" in raw and "<" not in raw:
        raw = html.unescape(raw)
    p = _Stripper()
    p.feed(raw)
    p.close()
    return clean_lines("".join(p.parts))


def html_links(raw: str | None) -> list[tuple[str, str]]:
    """All (href, link text) pairs in an HTML fragment."""
    if not raw:
        return []
    p = _Stripper()
    p.feed(raw)
    p.close()
    return p.links


def clean_lines(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\xa0", " ").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    out: list[str] = []
    for l in lines:
        if l or (out and out[-1]):
            out.append(l)
    return "\n".join(out).strip()


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


_WORD = re.compile(r"[a-z0-9][a-z0-9+#.\-/]*[a-z0-9+#]|[a-z0-9]")


def words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def content_words(text: str) -> set[str]:
    return {w for w in words(text) if w not in STOPWORDS and not w.isdigit() and len(w) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


_BOILERPLATE = re.compile(
    r"equal opportunity|\beeo\b|affirmative action|regardless of (race|gender|religion)|"
    r"reasonable accommodation|privacy (policy|notice)|by applying,? you|background check|"
    r"\bbenefits\b|\bperks\b|health insurance|paid time off|learning budget|"
    r"\bref(erence)?\s*(no\.?|number|id)?\s*[:#]?\s*[\w-]*\d", re.I)


def summarize(text: str, limit: int = 560) -> str:
    """Deterministic summary for jobs.summary (C3): the first meaningful
    sentences, skipping section headings and boilerplate (EEO, benefits,
    privacy, reference codes), cut at a sentence boundary under `limit`."""
    skip_line = re.compile(r"^(about (us|the company)|who we are|equal opportunity|eeo|benefits|perks)", re.I)
    out = ""
    for line in (text or "").split("\n"):
        line = line.strip("-• ").strip()
        if len(line) < 25 or skip_line.match(line):
            continue
        for sent in sentences(line) or [line]:
            if _BOILERPLATE.search(sent):
                continue
            candidate = f"{out} {sent}".strip()
            if len(candidate) > limit:
                if not out:
                    cut = sent[:limit]
                    out = cut[: cut.rfind(" ")] if " " in cut else cut
                return out[:limit]
            out = candidate
    return out[:limit]


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text or "") if s.strip()]
