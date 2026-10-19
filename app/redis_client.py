"""Shared Redis client: OTPs, rate-limit buckets, scheduler locks, seen-mail
sets. Nothing in Redis is durable state; losing it is always safe."""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings


@lru_cache
def get_redis():
    url = get_settings().redis_url
    if not url:
        return None
    import redis
    client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=3)
    try:
        client.ping()
    except Exception:
        return None
    return client
