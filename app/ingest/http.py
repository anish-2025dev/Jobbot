"""HTTP for fetchers: rate-limited per source, retries with exponential
backoff on 429/5xx, and source health bookkeeping (5 consecutive failures
pause a source for 24 h, section 5)."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from app.ratelimit import RateLimiter, get_limiter

log = logging.getLogger(__name__)
USER_AGENT = "JobBot/1.0 (personal job search; contact via repository)"


class SourceError(Exception):
    """Source-level failure (network, 5xx, 429 after retries): counts toward pausing."""


class NotFoundError(Exception):
    """404/410 for one company/slug: a registry problem, not a source outage."""


@dataclass
class Fetcher:
    client: httpx.Client
    limiter: RateLimiter
    max_retries: int = 4
    sleep: object = time.sleep
    calls: dict = field(default_factory=dict)

    def request(self, source: str, method: str, url: str, *, rate_key: str | None = None,
                **kw) -> httpx.Response:
        key = rate_key or source
        self.calls[source] = self.calls.get(source, 0) + 1
        last: Exception | None = None
        for attempt in range(self.max_retries):
            if not self.limiter.acquire(key, timeout=120, sleep=self.sleep):
                raise SourceError(f"{source}: rate limit wait exceeded")
            try:
                r = self.client.request(method, url, **kw)
            except httpx.HTTPError as e:
                last = e
            else:
                if r.status_code in (404, 410):
                    raise NotFoundError(f"{source}: {r.status_code} {url}")
                if r.status_code < 400:
                    return r
                if r.status_code in (429, 500, 502, 503, 504):
                    retry_after = r.headers.get("retry-after", "")
                    wait = float(retry_after) if retry_after.isdigit() else 2 ** attempt + random.random()
                    last = SourceError(f"{source}: HTTP {r.status_code}")
                    self.sleep(min(wait, 60))
                    continue
                raise SourceError(f"{source}: HTTP {r.status_code} {url}")
            self.sleep(min(2 ** attempt + random.random(), 30))
        raise SourceError(f"{source}: failed after {self.max_retries} attempts: {last}")

    def get_json(self, source: str, url: str, **kw):
        return _decode(source, url, self.request(source, "GET", url, **kw))

    def post_json(self, source: str, url: str, body: dict, **kw):
        return _decode(source, url, self.request(source, "POST", url, json=body, **kw))


def _decode(source: str, url: str, r: httpx.Response):
    """A 200 with an HTML body (bot challenge, maintenance page) is a source error, not a crash."""
    try:
        return r.json()
    except ValueError as e:
        raise SourceError(f"{source}: response from {url} is not JSON") from e


def make_fetcher(transport: httpx.BaseTransport | None = None, limiter: RateLimiter | None = None,
                 sleep=time.sleep) -> Fetcher:
    client = httpx.Client(timeout=30, transport=transport, follow_redirects=True,
                          headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    return Fetcher(client=client, limiter=limiter or get_limiter(), sleep=sleep)


# --- source health (sources table) ------------------------------------------------
def record_source_result(s, source_key: str, ok: bool, error: str | None = None) -> None:
    from sqlalchemy import text
    if ok:
        s.execute(text("""UPDATE sources SET consecutive_failures = 0, last_ok_at = now(),
                          status = 'active', paused_until = NULL WHERE key = :k"""), {"k": source_key})
    else:
        s.execute(text("""
            UPDATE sources SET consecutive_failures = consecutive_failures + 1, last_error = :e,
                status = CASE WHEN consecutive_failures + 1 >= 5 THEN 'paused'::health_status ELSE status END,
                paused_until = CASE WHEN consecutive_failures + 1 >= 5 THEN now() + interval '24 hours'
                                    ELSE paused_until END
            WHERE key = :k"""), {"k": source_key, "e": (error or "")[:500]})


def source_active(s, source_key: str) -> bool:
    from sqlalchemy import text
    row = s.execute(text("""
        UPDATE sources SET status = 'active', consecutive_failures = 0, paused_until = NULL
        WHERE key = :k AND status = 'paused' AND paused_until < now()
        RETURNING 1"""), {"k": source_key}).first()
    if row:
        return True
    status = s.execute(text("SELECT status FROM sources WHERE key = :k"), {"k": source_key}).scalar()
    return status != "paused"
