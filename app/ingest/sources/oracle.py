"""Oracle Recruiting Cloud (Oracle HCM Candidate Experience), public and keyless.
Used by many large employers (JPMorgan Chase, Oracle, ...). Slug = "{host}/{siteNumber}",
e.g. "jpmc.fa.oraclecloud.com/CX_1001".

1. One facet call maps the user's country to the tenant's location id (cached per run).
2. The newest requisitions in that location, 100 at a time, at most MAX_PAGES pages.
Only a short description is public, which is enough for title/skill scoring."""
from __future__ import annotations

from app.ingest.http import Fetcher
from app.ingest.models import RawJob
from app.ingest.sources.common import TitleFilter, keep, parse_iso

API = "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
JOB = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{id}"
PAGE = 100
MAX_PAGES = 2


def _split(slug: str) -> tuple[str, str]:
    host, _, site = slug.partition("/")
    if not host.endswith(".oraclecloud.com") or not site:
        raise ValueError(f"bad oracle slug {slug!r}")
    return host, site


def _items(data) -> dict:
    items = (data or {}).get("items") or []
    return items[0] if items else {}


_LOCATION_IDS: dict[tuple[str, str, str], int | None] = {}


def _location_id(f: Fetcher, host: str, site: str, country: str) -> int | None:
    key = (host, site, country.lower())
    if key not in _LOCATION_IDS:
        _LOCATION_IDS[key] = _lookup_location(f, host, site, country)
    return _LOCATION_IDS[key]


def _lookup_location(f: Fetcher, host: str, site: str, country: str) -> int | None:
    data = f.get_json("oracle", API.format(host=host), params={
        "onlyData": "true", "expand": "locationsFacet",
        "finder": f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit=1"})
    for loc in _items(data).get("locationsFacet") or []:
        if (loc.get("Name") or "").strip().lower() == country.lower():
            return loc.get("Id")
    return None


def fetch(f: Fetcher, slug: str, company: str, title_filter: TitleFilter = None, country: str = "India") -> list[RawJob]:
    host, site = _split(slug)
    loc_id = _location_id(f, host, site, country)
    where = f",locationId={loc_id}" if loc_id else ""
    out = []
    for page in range(MAX_PAGES):
        data = f.get_json("oracle", API.format(host=host), params={
            "onlyData": "true", "expand": "requisitionList.secondaryLocations",
            "finder": f"findReqs;siteNumber={site},limit={PAGE},offset={page * PAGE}{where},sortBy=POSTING_DATES_DESC"})
        block = _items(data)
        reqs = block.get("requisitionList") or []
        for j in reqs:
            title = (j.get("Title") or "").strip()
            if not title or not keep(title, title_filter):
                continue
            secondary = [x.get("Name") for x in j.get("secondaryLocations") or [] if x.get("Name")]
            workplace = (j.get("WorkplaceType") or "").lower()
            out.append(RawJob(
                source="oracle", ats="oracle", company=company, company_slug=slug, title=title,
                url=JOB.format(host=host, site=site, id=j.get("Id")), req_id=str(j.get("Id")),
                description=" ".join(x for x in (j.get("ShortDescriptionStr"), j.get("ExternalQualificationsStr"),
                                                  j.get("ExternalResponsibilitiesStr")) if x),
                description_is_html=False, location=j.get("PrimaryLocation"),
                locations=[x for x in [j.get("PrimaryLocation"), *secondary] if x],
                posted_at=parse_iso(j.get("PostedDate")),
                workplace="remote" if "remote" in workplace else "hybrid" if "hybrid" in workplace else
                          "onsite" if "site" in workplace else None,
            ))
        if len(reqs) < PAGE or (page + 1) * PAGE >= int(block.get("TotalJobsCount") or 0):
            break
    return out


def probe(f: Fetcher, slug: str) -> bool:
    host, site = _split(slug)
    data = f.get_json("oracle", API.format(host=host), params={
        "onlyData": "true", "finder": f"findReqs;siteNumber={site},limit=1"})
    return "TotalJobsCount" in _items(data)
