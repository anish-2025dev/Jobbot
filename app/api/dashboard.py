"""Dashboard API (section 12): ranked jobs, waiting list + Ready Packs,
applications, positives, referral queue, mail actions, stars, summary.

Every route is under /users/{user_id}/ and runs in user_session(), so
Postgres row-level security scopes it to that user (app/db.py)."""
from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app import db
from app.auth import principal
from app.ingest.scoring import BUCKET_NAMES, prep_days

router = APIRouter(dependencies=[Depends(principal)])
RANK = "bucket DESC, salary_max DESC NULLS LAST, posted_at DESC NULLS LAST, score DESC"


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _row(r) -> dict:
    return {k: _iso(v) for k, v in dict(r).items()}


# --- ranked jobs (the lean-first manual list) -----------------------------------------
@router.get("/users/{user_id}/jobs")
def ranked_jobs(user_id: UUID, bucket: Literal["all", "high", "medium", "low", "stretch"] = "all",
                q: str | None = Query(default=None, max_length=100), limit: int = Query(default=100, le=500),
                offset: int = Query(default=0, ge=0)) -> dict:
    where = ["j.user_id = :u", """NOT EXISTS (SELECT 1 FROM applied_ledger l WHERE l.user_id = j.user_id
              AND (l.canonical_hash = j.canonical_hash OR l.fingerprint = j.fingerprint))"""]
    params: dict = {"u": user_id, "lim": limit, "off": offset}
    if bucket != "all":
        where.append("j.bucket = :b")
        params["b"] = {v: k for k, v in BUCKET_NAMES.items()}[bucket]
    if q:
        where.append("(j.title ILIKE :q OR j.company_name ILIKE :q)")
        params["q"] = f"%{q}%"
    with db.user_session(user_id) as s:
        rows = s.execute(text(f"""
            SELECT DISTINCT ON (j.canonical_hash) j.id, j.title, j.company_name, j.location, j.remote_type, j.url,
                   j.score, j.bucket, j.salary_min, j.salary_max, j.salary_currency, j.posted_at, j.skills,
                   j.missing_skills, j.summary, j.referral_worthy, j.exp_min, j.exp_max, j.ats,
                   (SELECT status FROM applications a WHERE a.user_id = j.user_id AND a.canonical_hash = j.canonical_hash) AS app_status
            FROM jobs j WHERE {' AND '.join(where)}
            ORDER BY j.canonical_hash, j.ingested_on DESC"""), params).mappings().all()
        counts = dict(s.execute(text("SELECT bucket, count(DISTINCT canonical_hash) FROM jobs WHERE user_id = :u GROUP BY bucket"),
                                {"u": user_id}).all())
    # section 6 sort: bucket desc, salary desc (unknown last in its bucket), freshness
    items = sorted((_row(r) for r in rows), key=lambda r: (-r["bucket"],
                   -(r["salary_max"] if r["salary_max"] is not None else -1), -_ts(r["posted_at"]), -r["score"]))
    for r in items:
        r["bucket_name"] = BUCKET_NAMES[r["bucket"]]
        if r["bucket"] == 0:
            r["prep_days"] = prep_days(list(r["missing_skills"] or []))
    return {"counts": {BUCKET_NAMES[k]: v for k, v in counts.items()}, "items": items[offset:offset + limit]}


def _ts(v) -> float:
    from datetime import datetime
    if not v:
        return 0.0
    return datetime.fromisoformat(v).timestamp() if isinstance(v, str) else v.timestamp()


def _job(s, user_id: UUID, job_id: int) -> dict:
    row = s.execute(text("SELECT * FROM jobs WHERE id = :j AND user_id = :u ORDER BY ingested_on DESC LIMIT 1"),
                    {"j": job_id, "u": user_id}).mappings().first()
    if row is None:
        raise HTTPException(404, "job not found (jobs are kept 7 days)")
    return dict(row)


def _job_text(j: dict) -> str:
    """The full JD was discarded after scoring (C3); its summary and
    extracted skills are what the resume engine can use now."""
    skills = ", ".join(j["skills"] or [])
    return f"{j['title']}\n{j['summary'] or ''}\nRequirements:\n- {skills}\n"


def _ensure_application(s, user_id: UUID, j: dict) -> int:
    from app.resume.service import build_for_job
    aid = s.execute(text("SELECT id FROM applications WHERE user_id = :u AND canonical_hash = :c"),
                    {"u": user_id, "c": j["canonical_hash"]}).scalar()
    if aid:
        return aid
    built = build_for_job(s, user_id, jd_text=_job_text(j), title=j["title"], company=j["company_name"],
                          bucket=BUCKET_NAMES[max(j["bucket"], 1)] if j["bucket"] >= 1 else "low")
    return s.execute(text("""
        INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title, apply_url,
            source_id, score, bucket, salary_max, posted_at, status, mode, recipe, vault_revision, ats, req_id, location,
            components, missing_skills)
        VALUES (:u, :c, :f, :k, :cn, :t, :url, :src, :score, :b, :smax, :posted, 'waiting', 'manual', CAST(:r AS jsonb),
                :rev, CAST(:ats AS ats_type), :req, :loc, :comp, :missing) RETURNING id"""),
        {"u": user_id, "c": j["canonical_hash"], "f": j["fingerprint"], "k": j["company_key_hash"],
         "cn": j["company_name"], "t": j["title"], "url": j["url"], "src": j["source_id"], "score": j["score"],
         "b": j["bucket"], "smax": j["salary_max"], "posted": j["posted_at"],
         "r": json.dumps(built.recipe.to_storage()), "rev": built.revision, "ats": str(j["ats"]), "req": j["req_id"],
         "loc": j["location"], "comp": j["components"], "missing": j["missing_skills"]}).scalar_one()


@router.get("/users/{user_id}/jobs/{job_id}/resume.{fmt}")
def job_resume(user_id: UUID, job_id: int, fmt: Literal["pdf", "docx"]) -> Response:
    with db.user_session(user_id) as s:
        aid = _ensure_application(s, user_id, _job(s, user_id, job_id))
    return application_resume(user_id, aid, fmt)


@router.post("/users/{user_id}/jobs/{job_id}/applied")
def job_applied(user_id: UUID, job_id: int) -> dict:
    """'I applied to this' from the ranked list: same gate as the bot (C4-C6),
    same bookkeeping (ledger, cooldown, eviction score)."""
    from app.apply import gate
    with db.user_session(user_id) as s:
        aid = _ensure_application(s, user_id, _job(s, user_id, job_id))
    with db.worker_session() as s:          # gate needs the registry (owner connection)
        res = gate.manual_applied(s, aid)
    if not res.ok:
        raise HTTPException(409, {"reason": res.reason, "ready_at": _iso(res.not_before)})
    return {"applied": True, "application_id": aid}


@router.post("/users/{user_id}/jobs/{job_id}/queue")
def job_queue(user_id: UUID, job_id: int) -> dict:
    """Send one ranked job to the auto-apply queue."""
    with db.user_session(user_id) as s:
        aid = _ensure_application(s, user_id, _job(s, user_id, job_id))
        s.execute(text("UPDATE applications SET status = 'queued', mode = 'auto' WHERE id = :a AND applied_at IS NULL"),
                  {"a": aid})
    return {"queued": True, "application_id": aid}


# --- applications -----------------------------------------------------------------------
@router.get("/users/{user_id}/applications")
def applications(user_id: UUID, status: str | None = None, limit: int = Query(default=100, le=500)) -> list[dict]:
    q = """SELECT id, company_name, title, apply_url, status, mode, bucket, score, salary_max, applied_at, via_referral,
                  referrer, failure_step, failure_reason, not_before, last_status_at
           FROM applications WHERE user_id = :u AND deleted_at IS NULL"""
    params: dict = {"u": user_id, "lim": limit}
    if status:
        q += " AND status = CAST(:st AS application_status)"
        params["st"] = status
    q += " ORDER BY last_status_at DESC LIMIT :lim"
    with db.user_session(user_id) as s:
        return [_row(r) for r in s.execute(text(q), params).mappings()]


@router.get("/users/{user_id}/applications/{application_id}/resume.{fmt}")
def application_resume(user_id: UUID, application_id: int, fmt: Literal["pdf", "docx"]) -> Response:
    """Re-render the stored recipe (C2: PDFs are never stored)."""
    from app.resume.recipe import Recipe
    from app.resume.service import load_vault, render
    with db.user_session(user_id) as s:
        row = s.execute(text("SELECT recipe, company_name FROM applications WHERE id = :a AND user_id = :u"),
                        {"a": application_id, "u": user_id}).first()
        if row is None or row.recipe is None:
            raise HTTPException(404, "no stored resume for this application")
        vault, rev = load_vault(s, user_id)
    recipe = Recipe.model_validate(row.recipe)
    if recipe.vault_revision != rev:
        from app.resume.validator import validate
        recipe = validate(vault, recipe)          # vault changed since: re-check against current facts
    data, _ = render(vault, recipe, fmt)
    media = {"pdf": "application/pdf",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[fmt]
    name = "".join(c if c.isalnum() else "_" for c in f"{vault.personal.full_name}_{row.company_name}")[:80]
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})


# --- waiting list ----------------------------------------------------------------------
@router.get("/users/{user_id}/waiting")
def waiting(user_id: UUID) -> list[dict]:
    from app.apply.waiting import listing
    with db.worker_session() as s:          # gate checks read cooldown/ledger; owner connection, scoped by user_id
        rows = listing(s, user_id)
    keep = ("waiting_id", "id", "company_name", "title", "apply_url", "bucket", "score", "salary_max", "expires_at",
            "pin_count", "ready_at", "failure_step", "failure_reason", "mode", "ready_pack")
    return [{k: _iso(r.get(k)) for k in keep} for r in rows]


@router.post("/users/{user_id}/waiting/{application_id}/pin")
def waiting_pin(user_id: UUID, application_id: int) -> dict:
    from app.apply.waiting import pin
    with db.user_session(user_id) as s:
        t = pin(s, user_id, application_id)
    if t is None:
        raise HTTPException(404, "not on your waiting list")
    return {"expires_at": t.isoformat()}


@router.post("/users/{user_id}/waiting/{application_id}/applied")
def waiting_applied(user_id: UUID, application_id: int) -> dict:
    from app.apply.waiting import mark_applied
    with db.worker_session() as s:
        res = mark_applied(s, user_id, application_id)
    if not res.ok:
        raise HTTPException(409, {"reason": res.reason, "ready_at": _iso(res.not_before)})
    return {"applied": True}


@router.delete("/users/{user_id}/waiting/{application_id}")
def waiting_dismiss(user_id: UUID, application_id: int) -> dict:
    with db.user_session(user_id) as s:
        s.execute(text("DELETE FROM waiting_list WHERE user_id = :u AND application_id = :a"), {"u": user_id, "a": application_id})
        s.execute(text("UPDATE applications SET status = 'closed', failure_reason = 'dismissed by user' "
                       "WHERE id = :a AND user_id = :u AND applied_at IS NULL"), {"a": application_id, "u": user_id})
    return {"dismissed": True}


class Answers(BaseModel):
    answers: dict[str, str] = Field(min_length=1, max_length=30)
    retry: bool = True


@router.post("/users/{user_id}/waiting/{application_id}/answers")
def waiting_answers(user_id: UUID, application_id: int, body: Answers) -> dict:
    """Answer the questions that sent a job to Assisted mode. Answers are
    saved to form_answers (reused on every future form) and the job is
    retried automatically."""
    from app.apply.runner import save_answers
    save_answers(user_id, body.answers)
    if body.retry:
        with db.user_session(user_id) as s:
            n = s.execute(text("""UPDATE applications SET status = 'queued', mode = 'auto', failure_step = NULL,
                                  failure_reason = NULL WHERE id = :a AND user_id = :u AND applied_at IS NULL"""),
                          {"a": application_id, "u": user_id}).rowcount
            s.execute(text("DELETE FROM waiting_list WHERE application_id = :a AND user_id = :u"), {"a": application_id, "u": user_id})
        return {"saved": len(body.answers), "requeued": bool(n)}
    return {"saved": len(body.answers), "requeued": False}


@router.get("/users/{user_id}/answers")
def learned_answers(user_id: UUID) -> list[dict]:
    with db.user_session(user_id) as s:
        return [_row(r) for r in s.execute(text("""SELECT question_text, answer, answered_by, updated_at FROM form_answers
                                                   WHERE user_id = :u ORDER BY updated_at DESC"""), {"u": user_id}).mappings()]


# --- positives -----------------------------------------------------------------------
@router.get("/users/{user_id}/responses")
def responses(user_id: UUID, include_done: bool = False) -> list[dict]:
    q = """SELECT r.id, r.class AS kind, r.priority, r.deadline, r.snippet, r.received_at, r.keep_until, r.user_done_at,
                  r.gmail_thread_id, a.company_name, a.title
           FROM responses r LEFT JOIN applications a ON a.id = r.application_id
           WHERE r.user_id = :u AND r.deleted_at IS NULL"""
    if not include_done:
        q += " AND r.user_done_at IS NULL"
    q += " ORDER BY r.priority, r.deadline NULLS LAST, r.received_at DESC"
    with db.user_session(user_id) as s:
        out = [_row(r) for r in s.execute(text(q), {"u": user_id}).mappings()]
    for r in out:
        if r["gmail_thread_id"]:
            r["gmail_url"] = f"https://mail.google.com/mail/u/0/#all/{r['gmail_thread_id']}"
    return out


@router.post("/users/{user_id}/responses/{response_id}/done")
def response_done(user_id: UUID, response_id: int) -> dict:
    with db.user_session(user_id) as s:
        n = s.execute(text("UPDATE responses SET user_done_at = now() WHERE id = :r AND user_id = :u AND user_done_at IS NULL"),
                      {"r": response_id, "u": user_id}).rowcount
    return {"done": bool(n), "note": "kept at least 7 days after it arrived; offers are never auto-trashed"}


# --- referral queue ----------------------------------------------------------------------
@router.get("/users/{user_id}/referrals")
def referrals(user_id: UUID) -> list[dict]:
    with db.user_session(user_id) as s:
        rows = s.execute(text("""SELECT id, application_id, job, contacts, status, sent_at, apply_at, referral, created_at
                                 FROM referral_queue WHERE user_id = :u AND (closed_at IS NULL OR closed_at > now() - interval '7 days')
                                 ORDER BY created_at DESC"""), {"u": user_id}).mappings().all()
    return [_row(r) for r in rows]


class DraftDecision(BaseModel):
    approve: bool
    subject: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=2000)


@router.post("/users/{user_id}/referrals/{rq_id}/contacts/{index}")
def referral_decide(user_id: UUID, rq_id: int, index: int, body: DraftDecision) -> dict:
    from app.outreach.queue import decide
    with db.worker_session() as s:
        msg = decide(s, user_id, rq_id, index, body.approve, subject=body.subject, body=body.body)
    if msg.startswith(("This draft", "Unknown", "Edited")):
        raise HTTPException(409, msg)
    return {"result": msg}


# --- mail actions (undo) ------------------------------------------------------------------
@router.get("/users/{user_id}/mail-actions")
def mail_actions(user_id: UUID, limit: int = Query(default=100, le=500)) -> list[dict]:
    with db.user_session(user_id) as s:
        return [_row(r) for r in s.execute(text("""SELECT id, gmail_id, priority, action, acted_at, undone_at FROM mail_actions
                                                   WHERE user_id = :u ORDER BY acted_at DESC LIMIT :l"""),
                                           {"u": user_id, "l": limit}).mappings()]


@router.post("/users/{user_id}/mail-actions/{action_id}/undo")
def mail_undo(user_id: UUID, action_id: int) -> dict:
    from app.mail.accounts import mail_client
    from app.mail.processor import undo
    with db.user_session(user_id) as s:
        client = mail_client(s, user_id)
        if client is None:
            raise HTTPException(409, "mail is not connected")
        ok = undo(client, s, user_id, action_id)
    if not ok:
        raise HTTPException(409, "nothing to undo (already undone or older than 30 days)")
    return {"restored": True}


# --- companies --------------------------------------------------------------------------
@router.put("/users/{user_id}/stars/{company}")
def star(user_id: UUID, company: str) -> dict:
    from app.util.hashing import company_key, h64
    with db.user_session(user_id) as s:
        s.execute(text("INSERT INTO company_stars (user_id, company_key_hash) VALUES (:u, :k) ON CONFLICT DO NOTHING"),
                  {"u": user_id, "k": h64(company_key(company))})
    return {"starred": company_key(company)}


@router.delete("/users/{user_id}/stars/{company}")
def unstar(user_id: UUID, company: str) -> dict:
    from app.util.hashing import company_key, h64
    with db.user_session(user_id) as s:
        s.execute(text("DELETE FROM company_stars WHERE user_id = :u AND company_key_hash = :k"),
                  {"u": user_id, "k": h64(company_key(company))})
    return {"unstarred": company_key(company)}


@router.put("/users/{user_id}/blacklist/{company}")
def blacklist(user_id: UUID, company: str) -> dict:
    """Add an employer to expectations.blacklisted_companies (vault edit)."""
    from app.vault import repo
    from app.vault.schema import Vault
    with db.user_session(user_id) as s:
        stored = repo.get_vault(s, user_id, for_update=True)
        if stored is None:
            raise HTTPException(404, "no vault yet")
        data = dict(stored.data)
        bl = list(data["expectations"].get("blacklisted_companies", []))
        if company not in bl:
            bl.append(company)
        data["expectations"] = {**data["expectations"], "blacklisted_companies": bl}
        Vault.model_validate(data)
        repo.update_vault(s, user_id, data, stored.revision)
    return {"blacklisted": company}


# --- summary ------------------------------------------------------------------------------
@router.get("/users/{user_id}/summary")
def summary(user_id: UUID) -> dict:
    from app.notify.digest import compose
    with db.user_session(user_id) as s:
        apps = dict(s.execute(text("""SELECT status, count(*) FROM applications WHERE user_id = :u AND deleted_at IS NULL
                                      GROUP BY status"""), {"u": user_id}).all())
        week = s.execute(text("""SELECT count(*) FROM applied_ledger WHERE user_id = :u AND applied_at > now() - interval '7 days'"""),
                         {"u": user_id}).scalar()
        jobs = s.execute(text("SELECT count(DISTINCT canonical_hash) FROM jobs WHERE user_id = :u AND bucket >= 1"),
                         {"u": user_id}).scalar()
        waiting_n = s.execute(text("SELECT count(*) FROM waiting_list WHERE user_id = :u"), {"u": user_id}).scalar()
        positives = s.execute(text("SELECT count(*) FROM responses WHERE user_id = :u AND user_done_at IS NULL AND deleted_at IS NULL"),
                              {"u": user_id}).scalar()
    with db.worker_session() as s:
        digest = compose(s, user_id)
        paused = s.execute(text("SELECT adapter FROM adapter_health WHERE status = 'paused'")).scalars().all()
    return {"applications": apps, "applied_7d": week, "ranked_jobs": jobs, "waiting": waiting_n,
            "positives_open": positives, "paused_adapters": paused, "db_mb": round(db.db_size_mb(), 1),
            "digest": digest}
