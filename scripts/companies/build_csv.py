"""results.jsonl -> jobbot/data/companies.csv, India- and fresher-friendly boards first."""
import csv
import json
from datetime import date

rows, seen = [], set()
for line in open("results.jsonl"):
    r = json.loads(line)
    key = (r["ats"], r["slug"].lower())
    if key in seen or not r.get("jobs"):
        continue
    seen.add(key)
    rows.append(r)

# A board found by guessing a slug from a name may belong to another company with the same
# slug: Greenhouse boards say whose they are, so use that name (cached in board_names.json).
try:
    names = json.load(open("board_names.json"))
except FileNotFoundError:
    names = {}
import httpx  # noqa: E402
for r in rows:
    if r["ats"] == "greenhouse" and r["slug"] not in names:
        try:
            names[r["slug"]] = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{r['slug']}", timeout=15).json().get("name")
        except Exception:  # noqa: BLE001
            names[r["slug"]] = None
    if r["ats"] == "greenhouse" and names.get(r["slug"]):
        r["name"] = names[r["slug"]].strip()
json.dump(names, open("board_names.json", "w"))

import re  # noqa: E402


def clean(name: str) -> str:
    name = re.sub(r"\[?\(?https?://\S+", "", name).lstrip("*• ")        # HN first lines carry links
    name = re.split(r"\s+[:|–—-]\s+|:\s|\s*,\s*(?:we|and|is|are)\b", name)[0]   # "Back Market: Senior ... position"
    name = re.sub(r"\s*\((?:[^)]*)\)\s*$", "", name)
    name = re.sub(r"[\s,-]*\b(careers?|job board|jobs|hiring|external careers?|software private limited|"
                  r"private limited|pvt\.? ltd\.?|inc\.?|llc|ltd\.?|gmbh|corp\.?|corporation)\s*$", "", name, flags=re.I)
    return name.strip(" ,.-") or name


for r in rows:
    r["name"] = clean(r["name"].split(",")[0])
    if len(r["name"].split()) > 5:              # a sentence, not a name: use the board slug
        r["name"] = r["slug"].split("/")[0].replace("-", " ").title()

# One row per company name (a company found twice keeps its bigger board).
best: dict[str, dict] = {}
for r in rows:
    k = r["name"].lower()
    if k not in best or r["jobs"] > best[k]["jobs"]:
        best[k] = r
rows = list(best.values())


def tier(r):
    if r.get("india_entry", 0) > 0:
        return "india-fresher"
    if r.get("india_tech", 0) > 0 or r.get("india", 0) > 0:
        return "india"
    if r.get("entry_tech", 0) > 0:
        return "global-fresher"
    return "global"


ORDER = {"india-fresher": 0, "india": 1, "global-fresher": 2, "global": 3}
rows.sort(key=lambda r: (ORDER[tier(r)], -r.get("india_entry", 0), -r.get("india_tech", 0), -r.get("entry_tech", 0), r["name"].lower()))
keep = [r for r in rows if r.get("tech", 0) > 0 or r["ats"] in ("workday", "oracle")]

with open("../../data/companies.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["name", "careers_url", "ats", "slug", "segment", "open_jobs", "tech_jobs", "india_jobs", "india_tech_jobs",
                "entry_tech_jobs", "india_entry_jobs", "verified"])
    for r in keep:
        w.writerow([r["name"], r["careers_url"], r["ats"], r["slug"] if r["ats"] != "workday" else r["slug"].split("/", 1)[0].split(".")[0] + "/" + r["slug"].split("/", 1)[1],
                    tier(r), r.get("jobs", 0), r.get("tech", 0), r.get("india", 0), r.get("india_tech", 0),
                    r.get("entry_tech", 0), r.get("india_entry", 0), date.today().isoformat()])
from collections import Counter  # noqa: E402
print(len(keep), "companies;", Counter(tier(r) for r in keep), Counter(r["ats"] for r in keep))
