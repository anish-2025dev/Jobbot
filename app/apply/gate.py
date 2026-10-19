"""Pre-submit gate (section 8.2) and the apply bookkeeping transaction.

claim() runs immediately before Submit, under a per-(user, company)
advisory lock, and checks in order:
  1. canonical id / fingerprint not in applied_ledger            (C4)
  2. not under an active referral hold, not already referred      (C15)
  3. < 3 applications to the company in the rolling 30 days,
     counting reserved referral slots                             (C5)
  4. last application to the company >= 3 h ago, else requeue
     with not_before = last + 3 h                                 (C6)
  5. adapter not paused
If all pass, the claim itself writes the ledger row and the cooldown
timestamp, so no parallel worker can slip past any check while this one is
mid-submit, and a crash mid-submit errs toward never applying twice.

finalize() marks success: status applied, company eviction score, adapter
health. release() undoes a claim whose Submit provably did not happen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingest import registry
from app.util.hashing import h64

VIA = {"normal": 0, "referral": 1, "manual": 2}
ACTIVE_HOLD = ("finding", "awaiting_approval", "scheduled", "sent")


@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    not_before: datetime | None = None


def _lock(s: Session, user_id: UUID, company_key_hash: int) -> None:
    s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": h64(f"apply:{user_id}:{company_key_hash}")})


def cooldown_state(s: Session, user_id: UUID, company_key_hash: int, for_update: bool = False) -> tuple[int, int, datetime | None]:
    """(applications in the rolling 30 days, reserved slots, last applied)."""
    row = s.execute(text(f"""
        SELECT (SELECT count(*) FROM unnest(applied_ats) t WHERE t > now() - interval '30 days') AS n,
               reserved_slots, last_applied_at
        FROM company_cooldown WHERE user_id = :u AND company_key_hash = :k
        {"FOR UPDATE" if for_update else ""}"""), {"u": user_id, "k": company_key_hash}).first()
    return (row.n, row.reserved_slots, row.last_applied_at) if row else (0, 0, None)


def check(s: Session, app: dict, *, via: str = "normal", now: datetime | None = None,
          adapter: str | None = None) -> GateResult:
    """Read-only version of the gate (for showing waiting-list entries and
    'ready at' times). claim() repeats it under the lock."""
    cfg = get_settings()
    now = now or datetime.now(timezone.utc)
    uid, ck = app["user_id"], app["company_key_hash"]
    if s.execute(text("""SELECT 1 FROM applied_ledger WHERE user_id = :u
                         AND (canonical_hash = :c OR fingerprint = :f) LIMIT 1"""),
                 {"u": uid, "c": app["canonical_hash"], "f": app["fingerprint"]}).first():
        return GateResult(False, "already applied (C4)")
    if via != "referral":
        rq = s.execute(text("""SELECT status, apply_at, referral FROM referral_queue
                               WHERE user_id = :u AND canonical_hash = :c"""),
                       {"u": uid, "c": app["canonical_hash"]}).first()
        if rq is not None:
            if rq.status in ACTIVE_HOLD and (rq.apply_at is None or rq.apply_at > now):
                return GateResult(False, "referral hold (C15)", rq.apply_at)
            if rq.status == "referred" and (rq.referral or {}).get("submitted_by_employee"):
                return GateResult(False, "referred by an employee already")
    n, reserved, last = cooldown_state(s, uid, ck)
    own_reservation = 1 if app.get("uses_reserved_slot") else 0
    if n + reserved - own_reservation >= cfg.company_cap_per_30d:
        return GateResult(False, "company cap: 3 in 30 days (C5)")
    gap = timedelta(hours=cfg.company_gap_hours)
    if last is not None and now - last < gap:
        return GateResult(False, "3-hour company gap (C6)", last + gap)
    if adapter:
        paused = s.execute(text("""SELECT 1 FROM adapter_health WHERE adapter = :a AND status = 'paused'
                                   AND (paused_until IS NULL OR paused_until > now())"""), {"a": adapter}).first()
        if paused:
            return GateResult(False, f"adapter {adapter} paused")
    return GateResult(True)


def claim(s: Session, application_id: int, *, via: str = "normal", adapter: str | None = None,
          now: datetime | None = None) -> GateResult:
    app = s.execute(text("SELECT * FROM applications WHERE id = :i FOR UPDATE"), {"i": application_id}).mappings().first()
    if app is None:
        return GateResult(False, "no such application")
    app = dict(app)
    _lock(s, app["user_id"], app["company_key_hash"])
    res = check(s, app, via=via, now=now, adapter=adapter)
    if not res.ok:
        if res.not_before is not None:
            s.execute(text("UPDATE applications SET not_before = :t, last_status_at = now() WHERE id = :i"),
                      {"t": res.not_before, "i": application_id})
        elif "C4" in res.reason:
            s.execute(text("UPDATE applications SET status = 'closed', failure_reason = :r, last_status_at = now() WHERE id = :i"),
                      {"r": res.reason, "i": application_id})
        return res
    uid, ck = app["user_id"], app["company_key_hash"]
    _, _, prev_last = cooldown_state(s, uid, ck, for_update=True)
    claimed_at = s.execute(text("SELECT now()")).scalar_one()
    s.execute(text("""INSERT INTO applied_ledger (user_id, canonical_hash, fingerprint, company_key_hash, applied_at, via)
                      VALUES (:u, :c, :f, :k, :t, :v)"""),
              {"u": uid, "c": app["canonical_hash"], "f": app["fingerprint"], "k": ck, "t": claimed_at, "v": VIA[via]})
    s.execute(text("""
        INSERT INTO company_cooldown (user_id, company_key_hash, applied_ats, reserved_slots, last_applied_at)
        VALUES (:u, :k, ARRAY[CAST(:t AS timestamptz)], 0, :t)
        ON CONFLICT (user_id, company_key_hash) DO UPDATE SET
            applied_ats = ARRAY(SELECT x FROM unnest(company_cooldown.applied_ats) x
                                WHERE x > now() - interval '30 days') || CAST(:t AS timestamptz),
            reserved_slots = company_cooldown.reserved_slots - :own,
            last_applied_at = :t"""),
              {"u": uid, "k": ck, "t": claimed_at, "own": 1 if app.get("uses_reserved_slot") else 0})
    s.execute(text("""UPDATE applications SET status = 'applying', claimed_at = :t, claim_prev_last_at = :p,
                      attempts = attempts + 1, last_status_at = now() WHERE id = :i"""),
              {"t": claimed_at, "p": prev_last, "i": application_id})
    return GateResult(True)


def finalize(s: Session, application_id: int, *, adapter: str | None = None, status: str = "applied",
             screenshot_url: str | None = None) -> None:
    """Success bookkeeping in one transaction (section 8.2)."""
    app = s.execute(text("""UPDATE applications SET status = CAST(:st AS application_status), applied_at = claimed_at,
                            screenshot_url = COALESCE(:shot, screenshot_url), not_before = NULL,
                            failure_step = NULL, failure_reason = NULL, last_status_at = now()
                            WHERE id = :i RETURNING user_id, company_key_hash"""),
                    {"i": application_id, "st": status, "shot": screenshot_url}).first()
    if app is None:
        return
    registry.bump_on_application(s, app.company_key_hash)
    if adapter:
        from app.apply.health import record
        record(s, adapter, True)
    s.execute(text("DELETE FROM waiting_list WHERE application_id = :i"), {"i": application_id})


def release(s: Session, application_id: int, *, reason: str, step: str | None = None,
            adapter: str | None = None, adapter_failed: bool = True, next_status: str = "queued") -> None:
    """Undo a claim whose Submit did NOT happen. Never call this when the
    click may have gone through; use mark_unconfirmed() instead."""
    app = s.execute(text("SELECT * FROM applications WHERE id = :i FOR UPDATE"), {"i": application_id}).mappings().first()
    if app is None or app["claimed_at"] is None:
        return
    _lock(s, app["user_id"], app["company_key_hash"])
    s.execute(text("""DELETE FROM applied_ledger WHERE user_id = :u AND canonical_hash = :c AND applied_at = :t"""),
              {"u": app["user_id"], "c": app["canonical_hash"], "t": app["claimed_at"]})
    s.execute(text("""UPDATE company_cooldown SET applied_ats = array_remove(applied_ats, CAST(:t AS timestamptz)),
                      last_applied_at = :prev, reserved_slots = reserved_slots + :own
                      WHERE user_id = :u AND company_key_hash = :k"""),
              {"t": app["claimed_at"], "prev": app["claim_prev_last_at"], "u": app["user_id"],
               "k": app["company_key_hash"], "own": 1 if app["uses_reserved_slot"] else 0})
    s.execute(text("""UPDATE applications SET status = CAST(:st AS application_status), claimed_at = NULL,
                      claim_prev_last_at = NULL, failure_step = :step, failure_reason = :r, last_status_at = now()
                      WHERE id = :i"""), {"st": next_status, "step": step, "r": reason[:500], "i": application_id})
    if adapter and adapter_failed:
        from app.apply.health import record
        record(s, adapter, False)


def mark_unconfirmed(s: Session, application_id: int, *, reason: str) -> None:
    """Submit was clicked but no confirmation was seen. Keep the claim (C4
    fail-safe: never risk a second application) and ask the user to check."""
    s.execute(text("""UPDATE applications SET status = 'applied', applied_at = claimed_at,
                      failure_step = 'confirm', failure_reason = :r, last_status_at = now() WHERE id = :i"""),
              {"r": reason[:500], "i": application_id})


def manual_applied(s: Session, application_id: int) -> GateResult:
    """User says they applied (waiting list / ranked list): same gate, same
    bookkeeping, via=manual."""
    res = claim(s, application_id, via="manual")
    if res.ok:
        finalize(s, application_id)
    return res


def reserve_referral_slot(s: Session, user_id: UUID, company_key_hash: int) -> bool:
    """Reserve one C5 slot for a referral-queue job (section 10.4, T0)."""
    _lock(s, user_id, company_key_hash)
    n, reserved, _ = cooldown_state(s, user_id, company_key_hash, for_update=True)
    if n + reserved >= get_settings().company_cap_per_30d:
        return False
    s.execute(text("""INSERT INTO company_cooldown (user_id, company_key_hash, reserved_slots) VALUES (:u, :k, 1)
                      ON CONFLICT (user_id, company_key_hash) DO UPDATE SET reserved_slots = company_cooldown.reserved_slots + 1"""),
              {"u": user_id, "k": company_key_hash})
    return True


def release_referral_slot(s: Session, user_id: UUID, company_key_hash: int) -> None:
    s.execute(text("""UPDATE company_cooldown SET reserved_slots = GREATEST(reserved_slots - 1, 0)
                      WHERE user_id = :u AND company_key_hash = :k"""), {"u": user_id, "k": company_key_hash})
