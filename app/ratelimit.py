"""Token buckets (section 8.5): one per source and per ATS domain.

Redis-backed (atomic Lua) so several worker processes share limits; an
in-memory bucket with the same interface is used in tests and when Redis is
not configured.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from functools import lru_cache

_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local want = tonumber(ARGV[4])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1]) or burst
local ts = tonumber(state[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
local ok = 0
if tokens >= want then tokens = tokens - want; ok = 1 end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 60)
return {ok, tostring(tokens)}
"""


@dataclass(frozen=True)
class Limit:
    rate_per_s: float      # sustained rate
    burst: int             # bucket size


DEFAULT_LIMITS = {
    # Public ATS APIs: polite (well under their documented/observed limits).
    "greenhouse": Limit(2.0, 10), "lever": Limit(2.0, 10), "ashby": Limit(1.0, 5),
    "smartrecruiters": Limit(1.0, 5), "workday": Limit(0.3, 3),
    # Keyed APIs: their free tiers are monthly; per-second limits keep bursts sane.
    "jsearch": Limit(0.2, 2), "adzuna": Limit(0.5, 3), "hn_whos_hiring": Limit(1.0, 5),
    "career_jsonld": Limit(0.5, 3), "career_generic": Limit(0.2, 2),
    "recruitee": Limit(1.0, 5), "rippling": Limit(1.0, 5), "breezy": Limit(1.0, 5), "oracle": Limit(0.5, 3),
    "jooble": Limit(0.5, 3), "findwork": Limit(0.5, 3),
    # Keyless boards: a handful of calls per poll, polled every few hours (feeds.py)
    "remotive": Limit(0.2, 3), "remoteok": Limit(0.2, 2), "arbeitnow": Limit(0.5, 2), "jobicy": Limit(0.2, 2),
    "himalayas": Limit(0.5, 5), "themuse": Limit(0.5, 3), "workingnomads": Limit(0.2, 2),
    # Email-finder providers and Gmail
    "apollo": Limit(0.5, 3), "hunter": Limit(0.5, 3), "snov": Limit(0.5, 3), "gmail": Limit(5.0, 20),
}
FALLBACK = Limit(0.5, 3)


class MemoryBuckets:
    def __init__(self) -> None:
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str, limit: Limit, want: float = 1.0, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            tokens, ts = self._state.get(key, (float(limit.burst), now))
            tokens = min(limit.burst, tokens + max(0.0, now - ts) * limit.rate_per_s)
            ok = tokens >= want
            if ok:
                tokens -= want
            self._state[key] = (tokens, now)
            return ok


class UnlimitedBuckets:
    """Never limits. Tests and offline simulations only."""

    def take(self, key: str, limit: Limit, want: float = 1.0, now: float | None = None) -> bool:
        return True


class RedisBuckets:
    def __init__(self, client) -> None:
        self.r = client
        self._script = client.register_script(_LUA)

    def take(self, key: str, limit: Limit, want: float = 1.0, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        ok, _ = self._script(keys=[f"rl:{key}"], args=[limit.rate_per_s, limit.burst, now, want])
        return bool(int(ok))


class RateLimiter:
    def __init__(self, backend) -> None:
        self.backend = backend

    def try_acquire(self, key: str, limit: Limit | None = None) -> bool:
        return self.backend.take(key, limit or DEFAULT_LIMITS.get(key.split(":")[0], FALLBACK))

    def acquire(self, key: str, limit: Limit | None = None, timeout: float = 60.0,
                sleep=time.sleep) -> bool:
        """Block until a token is available or `timeout` passes."""
        lim = limit or DEFAULT_LIMITS.get(key.split(":")[0], FALLBACK)
        deadline = time.monotonic() + timeout
        while True:
            if self.backend.take(key, lim):
                return True
            if time.monotonic() >= deadline:
                return False
            sleep(min(1.0 / max(lim.rate_per_s, 0.01), 1.0))


@lru_cache
def get_limiter() -> RateLimiter:
    from app.redis_client import get_redis
    r = get_redis()
    return RateLimiter(RedisBuckets(r) if r is not None else MemoryBuckets())
