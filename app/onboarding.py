"""Onboarding (P6): turn an existing resume (PDF or pasted text) into a
DRAFT vault the user reviews in the editor. Nothing is saved automatically.

Heuristic parser by default; with an LLM configured, the cheap model
structures the text, and every bullet it returns must trace back to a line
of the original resume (same numbers, mostly the same words), so the draft
can't introduce claims the user never made (C11 starts here).
"""
from __future__ import annotations

import json
import re
from datetime import date

from pydantic import ValidationError

from app.llm.client import LLM, LLMError, LLMUnavailable
from app.util.skills import extract_skills
from app.util.text import content_words, jaccard
from app.vault.facts import extract_numbers

HEADINGS = {
    "education": r"education|academics?|academic background|qualifications",
    "experience": r"(work |professional )?experience|employment|internships?|work history",
    "projects": r"projects?|personal projects|academic projects|key projects",
    "skills": r"(technical )?skills|technologies|tech stack|tools",
    "achievements": r"achievements|awards|honou?rs|accomplishments|competitive programming",
    "certifications": r"certifications?|courses|licenses",
}
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_DATE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{4})|(\d{1,2})/(\d{4})|\b(20\d{2}|19\d{2})\b", re.I)
_RANGE = re.compile(rf"({_DATE.pattern})\s*(?:-|–|—|to)\s*(present|current|now|{_DATE.pattern})", re.I)
_BULLET = re.compile(r"^\s*([•\-\*▪●◦‣·]|\d+[.)])\s+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(\+?\d[\d\s()-]{8,}\d)")
URL = re.compile(r"(https?://)?(www\.)?(github\.com|linkedin\.com|gitlab\.com|leetcode\.com|[\w-]+\.(dev|io|me|in|com))/[\w./-]*", re.I)


def _ym(m: re.Match | None) -> str | None:
    if m is None:
        return None
    g = m.groups()
    if g[0]:
        return f"{int(g[1]):04d}-{_MONTHS[g[0][:3].lower()]:02d}"
    if g[2]:
        return f"{int(g[3]):04d}-{int(g[2]):02d}"
    return f"{int(g[4]):04d}-06" if g[4] else None


def _dates(line: str) -> tuple[str | None, str | None, bool]:
    """(start, end, current) from 'Jul 2026 - Oct 2026' / '2023 - Present'."""
    m = _RANGE.search(line)
    if not m:
        return None, None, False
    start = _ym(_DATE.search(m.group(1)))
    tail = line[m.start():m.end()].split(m.group(1), 1)[1]
    current = bool(re.search(r"present|current|now", tail, re.I))
    end = None if current else _ym(_DATE.search(tail))
    return start, end, current


def _slug(text_: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text_.lower()).strip("-")[:40] or "item"
    s, n = base, 2
    while s in used:
        s, n = f"{base}-{n}", n + 1
    used.add(s)
    return s


def split_sections(text_: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"header": []}
    cur = "header"
    for raw in text_.splitlines():
        line = raw.strip()
        if not line:
            continue
        key = next((k for k, pat in HEADINGS.items()
                    if len(line) < 40 and re.fullmatch(rf"\W*({pat})\W*", line, re.I)), None)
        if key:
            cur = key
            out.setdefault(cur, [])
            continue
        out.setdefault(cur, []).append(line)
    return out


_DANGLING = re.compile(r"(\b(of|and|or|the|to|with|using|for|in|on|by|a|an|via|from|into|across|at|as|including|"
                       r"while|than|per|over|under|between)|[,/(&+-])$", re.I)


def _looks_like_title(line: str) -> bool:
    """'Name | C#, Redis' or 'Name, Python, FastAPI, Redis': a project/job title line, not prose."""
    if "|" in line:
        return True
    parts = [p.strip() for p in line.split(",")]
    return len(parts) >= 3 and sum(len(p) for p in parts[1:]) / (len(parts) - 1) <= 22


def _continues(prev: str, line: str) -> bool:
    """Is `line` the wrapped rest of the bullet `prev` (PDF text breaks lines mid-sentence)?"""
    if _RANGE.search(line):
        return False                                   # a date range starts a new entry
    if line[:1].islower() or line[:1] in ",;:)&":
        return True                                    # sentences don't start lowercase
    tail = prev.rstrip()
    if _DANGLING.search(tail):
        return True                                    # "... separation of" / "... ~8,000 requests/second,"
    if _looks_like_title(line):
        return False
    # an unfinished bullet followed by a line that finishes a sentence
    return not tail.endswith((".", "!", "?", ";")) and line.rstrip().endswith((".", "!", "?"))


def _entries(lines: list[str]) -> list[tuple[str, list[str], list[str]]]:
    """[(header line, extra header lines, bullets)]: a non-bullet line starts an entry
    unless it is the wrapped continuation of the previous bullet."""
    out: list[tuple[str, list[str], list[str]]] = []
    for line in lines:
        if _BULLET.match(line):
            if out:
                out[-1][2].append(_BULLET.sub("", line).strip())
            else:
                out.append(("", [], [_BULLET.sub("", line).strip()]))
        elif out and out[-1][2] and _continues(out[-1][2][-1], line):
            out[-1][2][-1] += " " + line
        elif out and not out[-1][2] and len(out[-1][1]) < 3:
            out[-1][1].append(line)
        else:
            out.append((line, [], []))
    return out


_CITIES = ("delhi", "new delhi", "gurugram", "gurgaon", "noida", "faridabad", "ghaziabad", "bengaluru", "bangalore",
           "mumbai", "pune", "hyderabad", "chennai", "kolkata", "ahmedabad", "jaipur", "chandigarh", "lucknow",
           "indore", "kochi", "coimbatore", "remote")


def _home_city(lines: list[str]) -> str:
    for line in lines:
        low = line.lower()
        for c in _CITIES:
            if re.search(rf"\b{c}\b", low) and c != "remote":
                return c.title()
    return ""


_DEGREE = re.compile(r"b\.?\s?tech|\bb\.?\s?e\b|bachelor|m\.?\s?tech|master|\bb\.?\s?sc\b|\bm\.?\s?sc\b|\bbca\b|\bmca\b|"
                     r"\bmba\b|ph\.?\s?d|diploma|class\s*(x|xii|10|12)\b|\b(10|12)th\b|\bssc\b|\bhsc\b|senior secondary|"
                     r"higher secondary|matriculation|intermediate|\bpuc\b|\bb\.?\s?com\b|\bbba\b|\bba\b", re.I)
_INST = re.compile(r"universit|institut|college|school|vidyalaya|vidyapeeth|academy|\biit\b|\bnit\b|\biiit\b|\bbits\b|"
                   r"polytechnic|campus|kendriya|public school", re.I)
_BOARD = re.compile(r"\b(cbse|icse|isc|ib|igcse|nios|state board|[a-z]+ state board|[a-z]+ board)\b", re.I)
_SCORE = re.compile(r"(?:c?gpa|cpi|sgpa)\s*[:\-]?\s*(\d{1,2}(?:\.\d+)?)\s*(?:/\s*(10|4)(?:\.0+)?)?|(\d{2,3}(?:\.\d+)?)\s*%", re.I)


def _place_only(line: str) -> bool:
    """'New Delhi, India' / 'Bengaluru' / '2022 - 2026': a location or date column, not an institution."""
    rest = _RANGE.sub("", line)
    rest = re.sub(r"\b(19|20)\d{2}\b", "", rest).strip(" ,|-–—")
    if not rest:
        return True
    low = rest.lower()
    words = re.findall(r"[a-z]+", low)
    return len(words) <= 4 and not _INST.search(rest) and not _DEGREE.search(rest) and (
        "india" in low or any(re.search(rf"\b{c}\b", low) for c in _CITIES))


def _strip_columns(line: str) -> str:
    """Drop the right-hand columns pdftotext glues on: dates, 'City, India', scores."""
    line = _RANGE.sub("", line)
    parts = [p.strip(" ,|") for p in re.split(r"\s{2,}|\s\|\s|\t", line) if p.strip(" ,|")]
    keep = [p for p in parts if not _place_only(p)] or parts[:1]
    out = " ".join(keep)
    for c in sorted(_CITIES, key=len, reverse=True):          # "... Institute of X, New Delhi, India"
        out = re.sub(rf",?\s*\b{c}\b(,\s*india)?\s*$", "", out, flags=re.I)
    return re.sub(r",?\s*india\s*$", "", out, flags=re.I).strip(" ,|-–—")


def parse_education(raw: list[str]) -> list[dict]:
    """Pair every degree line (B.Tech, Class XII, ...) with its institution. Works whether a
    resume puts the institution above the degree or below it, and keeps school entries
    (Class X/XII) separate instead of folding them into the degree."""
    lines = [_BULLET.sub("", l).strip() for l in raw if l.strip()]
    deg_idx = [i for i, l in enumerate(lines) if _DEGREE.search(l)]
    inst_idx = [i for i, l in enumerate(lines) if i not in deg_idx and not _place_only(l)
                and (_INST.search(l) or not _SCORE.search(l))]
    if not deg_idx:
        return []
    inst_first = bool(inst_idx) and inst_idx[0] < deg_idx[0]
    used: set[int] = set()
    out = []
    for n, d in enumerate(deg_idx):
        nxt = deg_idx[n + 1] if n + 1 < len(deg_idx) else len(lines)
        prev = deg_idx[n - 1] if n else -1
        if inst_first:
            cands = [i for i in inst_idx if prev < i < d and i not in used]
            inst = cands[-1] if cands else None
            next_start = min([i for i in inst_idx if i > d] + [nxt])     # the next entry's institution line
            block = range(inst if inst is not None else d, next_start)
        else:
            cands = [i for i in inst_idx if d < i < nxt and i not in used]
            inst = cands[0] if cands else None
            block = range(d, nxt)
        if inst is not None:
            used.add(inst)
        text_ = " ".join(lines[i] for i in block if 0 <= i < len(lines)) or lines[d]
        if inst is not None and lines[inst] not in text_:
            text_ += " " + lines[inst]
        years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text_)]
        if not years:
            continue
        degree = _strip_columns(re.split(r"[;|]|\s{2,}", lines[d])[0])
        degree = re.sub(_SCORE, "", degree).strip(" ,:-–—()")
        institution = _strip_columns(lines[inst]) if inst is not None else ""
        if not institution and _INST.search(lines[d]):         # "B.Tech, Institute of X" on one line
            bits = [b.strip() for b in re.split(r",| - | – | at ", lines[d]) if b.strip()]
            institution = next((_strip_columns(b) for b in bits if _INST.search(b)), "")
            degree = next((b for b in bits if _DEGREE.search(b)), degree)
        from app.vault.schema import guess_level
        level = guess_level(degree)
        start, end, _ = _dates(text_)
        if level in ("class_x", "class_xii"):
            degree = {"class_x": "Class X", "class_xii": "Class XII"}[level]
        # A missing institution stays empty: the editor flags it instead of saving a placeholder.
        entry = {"level": level, "institution": institution[:120], "degree": (degree or "Degree")[:120],
                 "graduation_year": max(years), **({"start": start} if start else {}), **({"end": end} if end else {})}
        sm = _SCORE.search(text_)
        if sm:
            if sm.group(3):
                entry.update(score=float(sm.group(3)), score_scale="percentage")
            else:
                value, scale = float(sm.group(1)), sm.group(2) or ("10" if float(sm.group(1)) <= 10 else None)
                if scale in ("10", "4") and value <= float(scale):
                    entry.update(score=value, score_scale="cgpa_10" if scale == "10" else "cgpa_4")
        if level in ("class_x", "class_xii"):
            bm = _BOARD.search(text_)
            if bm:
                entry["board"] = bm.group(1).upper() if len(bm.group(1)) <= 5 else bm.group(1).title()
            entry["show_on_resume"] = False
        else:
            fm = re.search(r"\b(?:in|of)\s+([A-Z][A-Za-z &]+?(?:Engineering|Science|Technology|Applications|Commerce|Arts))", lines[d])
            if fm:
                entry["field_of_study"] = fm.group(1).strip()[:120]
                entry["degree"] = entry["degree"].split(" in ")[0].split(" of ")[0].strip()[:120] or entry["degree"]
        out.append(entry)
    return out


def heuristic_draft(text_: str, today: date | None = None) -> dict:
    today = today or date.today()
    sec = split_sections(text_)
    head = sec.get("header", [])
    blob = "\n".join(head[:8]) + "\n" + text_[:1500]
    name = next((l for l in head if not EMAIL.search(l) and not re.search(r"\d", l) and 2 <= len(l.split()) <= 5), "")
    email = (EMAIL.search(blob) or [None])[0] if EMAIL.search(blob) else None
    phone = PHONE.search(blob).group(1).strip() if PHONE.search(blob) else None
    links = []
    for m in URL.finditer(text_[:3000]):
        url = m.group(0)
        url = url if url.startswith("http") else "https://" + url
        label = "GitHub" if "github" in url else "LinkedIn" if "linkedin" in url else "Website"
        if url not in [l["url"] for l in links]:
            links.append({"label": label, "url": url})
    used: set[str] = set()
    city = _home_city(head[:10])

    education = parse_education(sec.get("education", []))

    experience = []
    for header, extra, bullets in _entries(sec.get("experience", [])):
        start, end, current = _dates(" ".join([header, *extra]))
        if not start or not bullets:
            continue
        parts = [p.strip() for p in re.split(r"\s+[|@–—-]\s+|,\s+|\s{2,}| at ", _RANGE.sub("", header)) if p.strip()]
        title = parts[0] if parts else header
        company = parts[1] if len(parts) > 1 else (extra[0] if extra else "Company")
        etype = "internship" if re.search(r"intern", header, re.I) else "full_time"
        experience.append({"id": _slug(f"exp-{company}", used), "company": company[:120], "title": title[:120],
                           "employment_type": etype, "start": start, **({"end": end} if end and not current else {}),
                           "bullets": [{"id": _slug(f"{company}-{i + 1}", used), "text": b[:300]}
                                       for i, b in enumerate(bullets[:8]) if len(b) >= 10],
                           "tech": [s for s in extract_skills(" ".join(bullets))][:30]})

    projects = []
    for header, extra, bullets in _entries(sec.get("projects", [])):
        if not bullets:
            continue
        name_ = re.split(r"\s+[|–—-]\s+|\s{2,}|:", header)[0].strip()
        if "," in name_ and _looks_like_title(name_):
            name_ = name_.split(",")[0].strip()        # "Name, Python, FastAPI, Redis": tech goes to `tech`
        name_ = name_[:120]
        tech_line = " ".join([header, *extra])
        tech = extract_skills(tech_line) or extract_skills(" ".join(bullets))
        projects.append({"id": _slug(f"proj-{name_}", used), "name": name_ or "Project",
                         "tech": tech[:30] or ["unspecified"],
                         "bullets": [{"id": _slug(f"{name_}-{i + 1}", used), "text": b[:300]}
                                     for i, b in enumerate(bullets[:8]) if len(b) >= 10]})

    skills = []
    for line in sec.get("skills", []):
        label, _, rest = line.partition(":")
        items = [x.strip() for x in re.split(r",|\||;|•|·", rest or label) if 1 < len(x.strip()) <= 40]
        if items:
            skills.append({"category": (label.strip() if rest else "Skills")[:120], "items": list(dict.fromkeys(items))[:40]})
    cats = set()
    skills = [g for g in skills if not (g["category"].lower() in cats or cats.add(g["category"].lower()))]
    achievements = [{"id": _slug(f"ach-{i + 1}", used), "text": _BULLET.sub("", l)[:300]}
                    for i, l in enumerate(sec.get("achievements", [])[:15]) if len(_BULLET.sub("", l)) >= 10]

    return {"schema_version": 2,
            "personal": {"full_name": name, "email": email or "", "phone": phone or "",
                         "location": {"city": city, "country": "India"}, "links": links[:8]},
            "target_roles": [{"id": "sde", "name": "Software Development Engineer",
                              "title_keywords": ["SDE", "Software Engineer", "Software Developer", "Backend Developer",
                                                 "Graduate Engineer Trainee"]}],
            "education": education, "experience": experience[:15], "projects": projects[:10], "skills": skills,
            "achievements": achievements, "standard_answers": {},
            # starts at the city on the resume (or India); the user edits it in the review step
            "expectations": {"locations": [city or "India"], "remote_types": ["onsite", "hybrid", "remote"],
                             "role_types": [], "experience_level": "fresher"}}


def _traceable(bullet: str, source_lines: list[str], source_numbers: set[str]) -> bool:
    if any(n not in source_numbers for n in extract_numbers(bullet)):
        return False
    words = content_words(bullet)
    return any(jaccard(words, content_words(l)) >= 0.6 or (words and len(words & content_words(l)) / len(words) >= 0.8)
               for l in source_lines)


def llm_draft(text_: str, llm: LLM, schema: dict) -> dict | None:
    prompt = ("Convert this resume into JSON matching the schema. Copy bullet text exactly as written (you may only "
              "fix line breaks). Do not invent anything; leave unknown fields out.\n\n"
              f"SCHEMA:\n{json.dumps(schema)[:6000]}\n\nRESUME:\n{text_[:12000]}")
    try:
        data = llm.complete_json(prompt, tier="cheap", max_tokens=6000)
    except (LLMError, LLMUnavailable):
        return None
    if not isinstance(data, dict):
        return None
    lines = [l.strip() for l in text_.splitlines() if l.strip()]
    joined = [" ".join(lines[i:i + 3]) for i in range(len(lines))]      # bullets may span wrapped lines
    nums = set(extract_numbers(text_))
    for key in ("experience", "projects"):
        for item in data.get(key) or []:
            item["bullets"] = [b for b in item.get("bullets") or []
                               if isinstance(b, dict) and _traceable(str(b.get("text", "")), joined, nums)]
    data["achievements"] = [a for a in data.get("achievements") or []
                            if isinstance(a, dict) and _traceable(str(a.get("text", "")), joined, nums)]
    return data


def import_resume(text_: str, llm: LLM | None = None) -> dict:
    """{'draft': vault-shaped dict, 'errors': [...], 'warnings': [...], 'method': ...}."""
    from app.vault.lint import lint
    from app.vault.schema import Vault
    draft, method = None, "heuristic"
    if llm is not None and llm.available:
        draft = llm_draft(text_, llm, Vault.model_json_schema())
        method = "llm" if draft else "heuristic"
    draft = draft or heuristic_draft(text_)
    try:
        v = Vault.model_validate(draft)
        return {"draft": v.to_storage(), "errors": [], "warnings": lint(v), "method": method}
    except ValidationError as e:
        return {"draft": draft, "errors": json.loads(e.json(include_url=False)), "warnings": [], "method": method}


def checklist(s, user_id) -> list[dict]:
    """What's left before the bot can run fully for this user."""
    from sqlalchemy import text
    from app.vault.lint import lint
    from app.vault.schema import Vault
    vault_row = s.execute(text("SELECT data FROM vaults WHERE user_id = :u"), {"u": user_id}).scalar()
    creds = set(s.execute(text("SELECT host FROM credentials WHERE user_id = :u"), {"u": user_id}).scalars())
    user = s.execute(text("SELECT telegram_chat_id, settings FROM users WHERE id = :u"), {"u": user_id}).first()
    warnings = lint(Vault.model_validate(vault_row)) if vault_row else []
    st = user.settings or {}
    return [
        {"step": "vault", "done": vault_row is not None, "detail": f"{len(warnings)} suggestions" if vault_row else "fill your vault"},
        {"step": "mail", "done": bool(creds & {"gmail", "imap"}), "detail": "Gmail (OAuth) or IMAP app password"},
        {"step": "telegram", "done": user.telegram_chat_id is not None, "detail": "alerts and one-tap approvals"},
        {"step": "email_finders", "done": bool(creds & {"apollo", "hunter", "snov"}), "detail": "optional: referral outreach"},
        {"step": "auto_apply", "done": bool(st.get("auto_apply")), "detail": "off = ranked list only (lean mode)"},
    ]
