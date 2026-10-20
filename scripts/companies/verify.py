"""Verify candidate employers against the live public ATS APIs and count, per board:
open jobs, tech jobs, India jobs and entry-level (intern / graduate / junior) tech jobs.

Writes results.jsonl (one verified board per line). Polite: 12 threads, a short
timeout, one board per company (the first ATS that has postings).
"""
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

sys.path.insert(0, "../..")
from candidates import KNOWN, NAMES  # noqa: E402

UA = {"User-Agent": "JobBot board verification (polite; contact: repo owner)", "Accept": "application/json"}
client = httpx.Client(timeout=15, headers=UA, follow_redirects=True)
INDIA = re.compile(r"india|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|gurgaon|gurugram|noida|chennai|kolkata|"
                   r"ahmedabad|jaipur|kochi|coimbatore|trivandrum|thiruvananthapuram|indore|chandigarh|mysore|mysuru|"
                   r"vadodara|nagpur|bhubaneswar|\bin\b", re.I)
TECH = re.compile(r"engineer|developer|\bsde\b|software|data|analyst|devops|\bsre\b|\bqa\b|test|machine learning|\bml\b|"
                  r"\bai\b|cloud|security|backend|frontend|full ?stack|mobile|android|\bios\b|platform|infrastructure|"
                  r"scientist|architect|programmer|technical|\bit\b", re.I)
ENTRY = re.compile(r"\bintern(ship)?\b|graduate|new grad|fresher|trainee|apprentice|entry[- ]level|\bjunior\b|\bjr\.?\b|"
                   r"engineer[- ](i|1)\b|\bsde[- ]?(i|1)\b|developer[- ](i|1)\b|analyst[- ](i|1)\b|associate (software|engineer|"
                   r"developer|data|analyst|consultant)|campus|early career|university|\bget\b|graduate engineer", re.I)
lock = threading.Lock()


def slugs(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", name.lower().replace("&", "and").replace(".", " "))
    words = base.split()
    cands = ["".join(words), "-".join(words)]
    if len(words) > 1 and len(words[0]) >= 4:
        cands.append(words[0])
    return [c for c in dict.fromkeys(cands) if c]


def tally(rows):
    """rows: [(title, location_text)]"""
    tech = [(t, loc) for t, loc in rows if TECH.search(t or "")]
    return {"jobs": len(rows), "tech": len(tech), "india": sum(1 for _, loc in rows if INDIA.search(loc or "")),
            "india_tech": sum(1 for _, loc in tech if INDIA.search(loc or "")),
            "entry_tech": sum(1 for t, _ in tech if ENTRY.search(t or "")),
            "india_entry": sum(1 for t, loc in tech if ENTRY.search(t or "") and INDIA.search(loc or ""))}


def get(url, **kw):
    r = client.get(url, **kw)
    return r.json() if r.status_code == 200 and "json" in r.headers.get("content-type", "") else None


def greenhouse(slug):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not d or not d.get("jobs"):
        return None
    return [(j.get("title"), (j.get("location") or {}).get("name")) for j in d["jobs"]]


def lever(slug):
    d = get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if not isinstance(d, list) or not d:
        return None
    return [(j.get("text"), " ".join([(j.get("categories") or {}).get("location") or "",
                                      *((j.get("categories") or {}).get("allLocations") or [])])) for j in d]


def ashby(slug):
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not d or not d.get("jobs"):
        return None
    return [(j.get("title"), " ".join([j.get("location") or "", *[s.get("location", "") for s in j.get("secondaryLocations") or []]]))
            for j in d["jobs"]]


def smartrecruiters(slug):
    d = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100})
    if not d or not int(d.get("totalFound") or 0):
        return None
    rows = [(j.get("name"), " ".join(str(v) for v in (j.get("location") or {}).values())) for j in d.get("content") or []]
    ind = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100, "country": "in"}) or {}
    rows += [(j.get("name"), "India") for j in ind.get("content") or [] if j.get("id") not in {x.get("id") for x in d.get("content") or []}]
    return rows


def recruitee(slug):
    d = get(f"https://{slug}.recruitee.com/api/offers/")
    if not d or not d.get("offers"):
        return None
    return [(j.get("title"), f"{j.get('location')} {j.get('country')}") for j in d["offers"]]


def rippling(slug):
    d = get(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
    if not isinstance(d, list) or not d:
        return None
    return [(j.get("name"), (j.get("workLocation") or {}).get("label")) for j in d]


def breezy(slug):
    d = get(f"https://{slug}.breezy.hr/json")
    if not isinstance(d, list) or not d:
        return None
    return [(j.get("name"), json.dumps(j.get("location") or {})) for j in d]


PROBERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
           "recruitee": recruitee, "rippling": rippling, "breezy": breezy}


def workday(slug):
    host_tenant, _, site = slug.partition("/")
    tenant = host_tenant.split(".")[0]
    url = f"https://{host_tenant}/wday/cxs/{tenant}/{site}/jobs"
    rows = []
    seen = set()

    def append_postings(postings):
        for j in postings:
            key = j.get("externalPath") or (j.get("title"), j.get("locationsText") or "")
            if key not in seen:
                seen.add(key)
                rows.append((j.get("title"), j.get("locationsText") or ""))

    r = client.post(url, json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    if r.status_code != 200 or "jobPostings" not in r.text:
        return None
    total = r.json().get("total") or 0
    append_postings(r.json().get("jobPostings") or [])
    for text in ("India", "intern India", "graduate India", "software engineer India"):
        rr = client.post(url, json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": text})
        if rr.status_code == 200:
            append_postings(rr.json().get("jobPostings") or [])
    t = tally(rows)
    t["jobs"] = max(total, t["jobs"])
    return t


def oracle(slug):
    host, _, site = slug.partition("/")
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    d = get(api, params={"onlyData": "true", "expand": "locationsFacet", "finder": f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit=1"})
    if not d or not d.get("items"):
        return None
    item = d["items"][0]
    india = next((x for x in item.get("locationsFacet") or [] if x.get("Name") == "India"), None)
    rows = []
    if india:
        p = get(api, params={"onlyData": "true", "expand": "requisitionList",
                             "finder": f"findReqs;siteNumber={site},limit=100,locationId={india['Id']},sortBy=POSTING_DATES_DESC"})
        rows = [(j.get("Title"), j.get("PrimaryLocation")) for j in ((p or {}).get("items") or [{}])[0].get("requisitionList") or []]
    t = tally(rows)
    t["jobs"] = item.get("TotalJobsCount") or 0
    t["india"] = (india or {}).get("TotalCount") or 0
    return t


def careers_url(ats, slug):
    return {"greenhouse": f"https://job-boards.greenhouse.io/{slug}", "lever": f"https://jobs.lever.co/{slug}",
            "ashby": f"https://jobs.ashbyhq.com/{slug}", "smartrecruiters": f"https://jobs.smartrecruiters.com/{slug}",
            "recruitee": f"https://{slug}.recruitee.com/", "rippling": f"https://ats.rippling.com/{slug}/jobs",
            "breezy": f"https://{slug}.breezy.hr/", "workday": "https://" + slug.replace("/", "/en-US/", 1),
            "oracle": "https://" + slug.replace("/", "/hcmUI/CandidateExperience/en/sites/", 1)}[ats]


def emit(out, name, ats, slug, t, how):
    rec = {"name": name, "ats": ats, "slug": slug, "careers_url": careers_url(ats, slug), "how": how, **t}
    with lock:
        out.write(json.dumps(rec) + "\n")
        out.flush()


NAME_PROBERS = ("greenhouse", "lever", "ashby", "smartrecruiters", "rippling")   # no subdomain DNS guesses


def check_name(name):
    for ats in NAME_PROBERS:
        tries = slugs(name)
        if ats == "smartrecruiters":
            tries = [re.sub(r"[^A-Za-z0-9]", "", name)] + tries[:1]
        for slug in tries:
            try:
                rows = PROBERS[ats](slug)
            except Exception:  # noqa: BLE001
                rows = None
            if rows:
                return ats, slug, tally(rows)
    return None


def job(out, name, ats, slug, how):
    try:
        if how == "name":
            res = check_name(name)
            if res:
                emit(out, name, res[0], res[1], res[2], how)
            return
        if ats == "workday":
            t = workday(slug)
        elif ats == "oracle":
            t = oracle(slug)
        else:
            t = tally(PROBERS[ats](slug) or [])
        if t and t.get("jobs"):
            emit(out, name, ats, slug, t, how)
    except Exception as e:  # noqa: BLE001
        print("ERR", name, ats, slug, repr(e)[:100], flush=True)


def main():
    harvested = json.load(open("harvested.json")) if len(sys.argv) > 1 and sys.argv[1] == "--harvested" else None
    done = set()
    try:
        for line in open("results.jsonl"):
            r = json.loads(line)
            done.add(r["name"].lower())
            done.add((r["ats"], r["slug"].lower()))
    except FileNotFoundError:
        pass
    tasks = []
    if harvested is None:
        tasks += [(name, ats, slug, "known") for (ats, slug), name in KNOWN.items() if (ats, slug.lower()) not in done]
        tasks += [(name, None, None, "name") for name in NAMES if name.lower() not in done]
    else:
        tasks += [(name, ats, slug, "harvest") for ats, slug, name in harvested
                  if (ats, slug.lower()) not in done and ats in PROBERS]
    with open("results.jsonl", "a") as out, ThreadPoolExecutor(16) as pool:
        futs = [pool.submit(job, out, *t) for t in tasks]
        for i, _ in enumerate(as_completed(futs), 1):
            if i % 50 == 0:
                print(i, "/", len(futs), flush=True)


if __name__ == "__main__":
    main()
