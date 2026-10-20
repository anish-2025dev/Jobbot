"""Find more company boards from links in live job feeds: the last 3 HN 'Who is hiring?'
threads and the keyless boards. Writes harvested.json = [[ats, slug, company], ...]."""
import json
import re
import sys

import httpx

sys.path.insert(0, "../..")
from app.ingest.detect import detect  # noqa: E402
from app.ingest.http import make_fetcher  # noqa: E402
from app.ingest.sources.feeds import FEEDS  # noqa: E402
from app.util.text import html_links, html_to_text  # noqa: E402


class L:
    def acquire(self, *a, **k):
        return True


f = make_fetcher(limiter=L())
found: dict[tuple[str, str], str] = {}


def add(url, company):
    d = detect(url)
    if d and d.slug and d.ats in ("greenhouse", "lever", "ashby", "smartrecruiters", "recruitee", "rippling", "breezy"):
        found.setdefault((d.ats, d.slug), (company or d.slug).strip()[:80])


for key, feed in FEEDS.items():
    try:
        for j in feed.fetch(f, None):
            add(j.apply_url or j.url, j.company)
    except Exception as e:  # noqa: BLE001
        print(key, "failed", e)

c = httpx.Client(timeout=30)
threads = c.get("https://hn.algolia.com/api/v1/search_by_date",
                params={"tags": "story,author_whoishiring", "hitsPerPage": 40}).json()["hits"]
for t in [h for h in threads if "who is hiring" in (h.get("title") or "").lower()][:12]:
    item = c.get(f"https://hn.algolia.com/api/v1/items/{t['objectID']}").json()
    for com in item.get("children") or []:
        html = com.get("text") or ""
        first = html_to_text(html).split("\n", 1)[0]
        company = re.sub(r"\(.*?\)", "", first.split("|")[0]).strip()
        for href, _ in html_links(html):
            add(href, company)
json.dump([[a, s, n] for (a, s), n in found.items()], open("harvested.json", "w"), indent=0)
print(len(found), "boards harvested")
