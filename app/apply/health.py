"""Adapter health (section 8.6): success over the last 20 attempts; below
70% (with at least 5 attempts) the adapter pauses for 24 h, its jobs go to
the waiting list, and the user is alerted."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

WINDOW = 20
MIN_ATTEMPTS = 5
THRESHOLD = 0.70


def success_rate(bits: int, count: int) -> float:
    if count == 0:
        return 1.0
    return bin(bits & ((1 << count) - 1)).count("1") / count


def record(s: Session, adapter: str, ok: bool) -> tuple[float, bool]:
    """Returns (success rate, paused_now)."""
    row = s.execute(text("""
        INSERT INTO adapter_health (adapter, recent_bits, recent_count, updated_at)
        VALUES (:a, :b, 1, now())
        ON CONFLICT (adapter) DO UPDATE SET
            recent_bits = ((adapter_health.recent_bits << 1) | :b) & 1048575,
            recent_count = LEAST(adapter_health.recent_count + 1, 20),
            updated_at = now()
        RETURNING recent_bits, recent_count, status"""), {"a": adapter, "b": 1 if ok else 0}).first()
    rate = success_rate(row.recent_bits, row.recent_count)
    if row.status == "active" and row.recent_count >= MIN_ATTEMPTS and rate < THRESHOLD:
        s.execute(text("""UPDATE adapter_health SET status = 'paused', paused_until = now() + interval '24 hours'
                          WHERE adapter = :a"""), {"a": adapter})
        return rate, True
    return rate, False


def is_paused(s: Session, adapter: str) -> bool:
    row = s.execute(text("SELECT status, paused_until FROM adapter_health WHERE adapter = :a"), {"a": adapter}).first()
    if row is None or row.status == "active":
        return False
    if row.paused_until is not None:
        resumed = s.execute(text("""UPDATE adapter_health SET status = 'active', recent_bits = 0, recent_count = 0,
                                    paused_until = NULL WHERE adapter = :a AND paused_until < now() RETURNING 1"""),
                            {"a": adapter}).first()
        return resumed is None
    return True


def resume(s: Session, adapter: str) -> None:
    s.execute(text("""UPDATE adapter_health SET status = 'active', recent_bits = 0, recent_count = 0,
                      paused_until = NULL WHERE adapter = :a"""), {"a": adapter})
