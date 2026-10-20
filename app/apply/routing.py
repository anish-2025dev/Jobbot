"""Route each ranked job (section 6.5): referral queue, auto-apply queue, or
waiting list. Runs inside ingestion while the full JD is still in memory,
so the tailored recipe is built now and stored; the JD is then discarded
(C3). Lean-first: with auto_apply off (the default) nothing is routed and
the ranked list in the dashboard is the manual-apply interface."""
from __future__ import annotations

import json
import logging
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.apply.adapters import get_adapter
from app.ingest.detect import detect, is_manual_portal
from app.ingest.normalize import NormalizedJob
from app.ingest.scoring import BUCKET_NAMES, Scored, UserProfile

log = logging.getLogger(__name__)


def make_router(notify: Callable | None = None, llm_enabled: bool = True):
    def route(s: Session, p: UserProfile, j: NormalizedJob, sc: Scored, job_id: int) -> None:
        st = p.settings or {}
        if st.get("paused") or not st.get("auto_apply"):
            return
        from app.resume.service import build_for_job
        try:
            built = build_for_job(s, p.user_id, jd_text=j.text or j.summary, title=j.title, company=j.company,
                                  bucket=BUCKET_NAMES[sc.bucket])
            recipe = built.recipe.to_storage()
            rev = built.revision
        except Exception as e:      # a recipe failure must not lose the job
            log.warning("recipe build failed for %s: %s", j.title, e)
            recipe, rev = None, None
        source_id = s.execute(text("SELECT id FROM sources WHERE key = :k"), {"k": j.raw.source}).scalar()
        app_id = s.execute(text("""
            INSERT INTO applications (user_id, canonical_hash, fingerprint, company_key_hash, company_name, title,
                apply_url, source_id, score, bucket, salary_max, posted_at, status, recipe, vault_revision,
                ats, req_id, location, components, missing_skills)
            VALUES (:u, :c, :f, :k, :cn, :t, :url, :src, :score, :b, :smax, :posted, 'queued',
                    CAST(:recipe AS jsonb), :rev, CAST(:ats AS ats_type), :req, :loc, :comp, :missing)
            ON CONFLICT (user_id, canonical_hash) DO NOTHING RETURNING id"""), {
            "u": p.user_id, "c": j.canonical_hash, "f": j.fingerprint, "k": j.company_key_hash,
            "cn": j.company[:200], "t": j.title[:300], "url": (j.raw.apply_url or j.raw.url)[:1000],
            "src": source_id, "score": sc.score, "b": sc.bucket, "smax": j.salary_max, "posted": j.posted_at,
            "recipe": json.dumps(recipe) if recipe else None, "rev": rev,
            "ats": j.raw.ats if j.raw.ats in _ATS else "unknown", "req": j.raw.req_id,
            "loc": (j.location or "")[:300] or None, "comp": sc.component_ints(), "missing": sc.missing[:20]}).scalar()
        if app_id is None:
            return
        from app.config import get_settings
        has_adapter = get_adapter(j.raw.ats, j.raw.apply_url or j.raw.url) is not None or (
            get_settings().generic_adapter_enabled and j.raw.ats in ("jsonld", "generic", "unknown"))
        manual = j.raw.easy_apply_only or is_manual_portal(detect(j.raw.apply_url or j.raw.url)) or not has_adapter
        if manual:
            from app.apply.waiting import add_to_waiting
            s.execute(text("""UPDATE applications SET status = 'waiting', mode = 'manual',
                              failure_reason = 'Easy Apply / unsupported site' WHERE id = :i"""), {"i": app_id})
            add_to_waiting(s, app_id, notify=notify)
            return
        if sc.referral_worthy and st.get("outreach"):
            from app.outreach.queue import try_enqueue
            if try_enqueue(s, p.user_id, app_id):
                return
        # otherwise: queued for the apply worker
    return route


_ATS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "workday", "recruitee", "rippling", "breezy",
        "oracle", "jsonld", "generic", "unknown"}
