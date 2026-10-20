"""One application, end to end (section 8).

Auto:     standard ATS, every field mapped with confidence -> fill, screenshot,
          claim (gate) -> Submit -> confirm -> finalize.
Assisted: CAPTCHA or an unknown required question -> everything else filled,
          job goes to the waiting list with a Ready Pack, Telegram ping; the
          user answers in the dashboard (auto retry) or finishes in a headed
          browser (assist session) where THEY solve the CAPTCHA / press Submit.
Manual:   LinkedIn/Naukri Easy Apply-only, no adapter, paused adapter, or a
          failure -> waiting list with a Ready Pack.
"""
from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import UUID

from sqlalchemy import text

from app import db
from app.apply import gate, health
from app.apply.adapters import get_adapter
from app.apply.browser import has_captcha, visible_errors
from app.apply.filler import FillContext, plan, question_hash
from app.config import get_settings
from app.ingest.detect import detect, is_manual_portal
from app.resume.engine import render_docx, render_pdf
from app.resume.recipe import Recipe
from app.vault.schema import Vault

log = logging.getLogger(__name__)
Notify = Callable[..., None]
ASSIST_TIMEOUT_MS = 15 * 60 * 1000
MAX_FORM_STEPS = 12                     # Workday wizards run 5-8 steps; anything longer is a loop


@dataclass
class Outcome:
    status: str          # applied | unconfirmed | assisted | manual | requeued | closed | failed
    reason: str = ""
    step: str = ""


def _load(s, application_id: int) -> tuple[dict, Vault, Recipe | None, dict]:
    app = dict(s.execute(text("SELECT * FROM applications WHERE id = :i"), {"i": application_id}).mappings().one())
    vrow = s.execute(text("SELECT data, revision FROM vaults WHERE user_id = :u"), {"u": app["user_id"]}).one()
    vault = Vault.model_validate(vrow.data)
    recipe = Recipe.model_validate(app["recipe"]) if app.get("recipe") else None
    learned = {r.question_hash: r.answer for r in s.execute(text(
        "SELECT question_hash, answer FROM form_answers WHERE user_id = :u"), {"u": app["user_id"]})}
    return app, vault, recipe, learned


def _resume_files(vault: Vault, recipe: Recipe, tmp: Path) -> tuple[Path, Path | None]:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", vault.personal.full_name).strip("_") + "_Resume"
    pdf = tmp / f"{stem}.pdf"
    pdf.write_bytes(render_pdf(vault, recipe).pdf)
    return pdf, tmp / f"{stem}.docx"


def _identity(s, user_id) -> dict[str, str]:
    """Decrypted identity documents for this fill only; never logged or stored."""
    from app.vault import identity as ident_store
    try:
        return ident_store.load(s, user_id).values()
    except Exception:                   # missing key / undecryptable: forms asking get Assisted
        log.warning("identity documents unavailable for user %s", user_id)
        return {}


def _to_waiting(application_id: int, *, mode: str, step: str, reason: str, notify: Notify | None,
                unresolved: list[str] | None = None) -> Outcome:
    from app.apply.waiting import add_to_waiting
    with db.worker_session() as s:
        s.execute(text("""UPDATE applications SET status = 'waiting', mode = CAST(:m AS apply_mode),
                          failure_step = :st, failure_reason = :r, last_status_at = now() WHERE id = :i"""),
                  {"m": mode, "st": step, "r": reason[:500], "i": application_id})
        added = add_to_waiting(s, application_id, extra={"unresolved_questions": unresolved or []}, notify=notify)
    return Outcome(mode if added else "closed", reason, step)


def _auth_context(app: dict, vault: Vault, mail_client_for, timeout_s: float = 300, sleep=None):
    """Credentials + verification-link plumbing for adapters with accounts (Workday)."""
    import time as _time

    from app.apply.adapters.workday import AuthContext
    from app.mail import otp
    from app.mail.accounts import load_credential, mail_address, save_credential
    uid = app["user_id"]
    with db.worker_session() as s:
        email_addr = mail_address(s, uid) or vault.personal.email

    def load(host):
        with db.worker_session() as s:
            c = load_credential(s, uid, host)
        return (c[0], c[1]["password"]) if c else None

    def save(host, username, password):
        with db.worker_session() as s:
            save_credential(s, uid, "ats", username, {"password": password}, host=host)

    def wait_link(host):
        from app.mail.processor import process_mailbox
        domains = [host, "myworkday.com", "myworkdayjobs.com"]
        for d in domains:
            otp.register_waiter(uid, d)
        with db.worker_session() as s:
            client = mail_client_for(s, uid)
        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            if client is not None:
                process_mailbox(client, db.worker_session, uid, query="newer_than:1h")
            for d in domains:
                link = otp.take_link(uid, d)
                if link:
                    return link
            (sleep or _time.sleep)(3)
        return None

    return AuthContext(user_id=uid, email=email_addr, load=load, save=save, wait_link=wait_link)


def run_application(application_id: int, context, *, notify: Notify | None = None, llm=None,
                    assisted: bool = False, submit: bool = True, mail_client_for=None,
                    keep_open: bool = False) -> Outcome:
    """`context` is a Playwright BrowserContext (headed for assisted runs)."""
    if mail_client_for is None:
        from app.mail.accounts import mail_client as mail_client_for
    with db.worker_session() as s:
        app, vault, recipe, learned = _load(s, application_id)
        pre = gate.check(s, app, via="referral" if app["via_referral"] else "normal")
    if not pre.ok:
        with db.worker_session() as s:
            if pre.not_before is not None:
                s.execute(text("UPDATE applications SET not_before = :t, status = 'queued' WHERE id = :i"),
                          {"t": pre.not_before, "i": application_id})
                return Outcome("requeued", pre.reason, "gate")
            s.execute(text("UPDATE applications SET status = 'closed', failure_reason = :r WHERE id = :i"),
                      {"r": pre.reason, "i": application_id})
        return Outcome("closed", pre.reason, "gate")

    url = app["apply_url"]
    det = detect(url)
    adapter = get_adapter(app["ats"], url)
    if adapter is None and not is_manual_portal(det) and app["ats"] in ("jsonld", "generic", "unknown") \
            and get_settings().generic_adapter_enabled:
        from app.apply.adapters.generic import GenericAdapter
        adapter = GenericAdapter(llm)
    if is_manual_portal(det) or adapter is None:
        return _to_waiting(application_id, mode="manual", step="route", notify=notify,
                           reason="Easy Apply / unsupported site: apply manually with the Ready Pack")
    with db.worker_session() as s:
        if health.is_paused(s, adapter.name):
            return _to_waiting(application_id, mode="manual", step="route", notify=notify,
                               reason=f"{adapter.name} adapter paused after repeated failures")
    if recipe is None:
        return _to_waiting(application_id, mode="manual", step="resume", notify=notify,
                           reason="no resume recipe for this job")

    with tempfile.TemporaryDirectory(prefix="jobbot-apply-") as tmpd:   # resume files deleted after use (C2)
        tmp = Path(tmpd)
        pdf, docx_path = _resume_files(vault, recipe, tmp)
        with db.worker_session() as s:
            identity = _identity(s, app["user_id"])
        ctx = FillContext(vault=vault, recipe=recipe, company=app["company_name"], title=app["title"],
                          job_country=_country(app.get("location"), vault), referrer=app.get("referrer"),
                          learned=learned, llm=llm, resume_path=str(pdf), identity=identity)
        page = context.new_page()
        try:
            opened = adapter.open(page, url)
            if not opened.ok:
                with db.worker_session() as s:
                    health.record(s, adapter.name, False)
                return _to_waiting(application_id, mode="manual", step="open", reason=opened.reason, notify=notify)
            if hasattr(adapter, "authenticate"):
                auth = adapter.authenticate(page, _auth_context(app, vault, mail_client_for))
                if not auth.ok:
                    return _to_waiting(application_id, mode="assisted", step=auth.step, reason=auth.reason,
                                       notify=notify)
            if has_captcha(page) and not assisted:
                return _to_waiting(application_id, mode="assisted", step="captcha", notify=notify,
                                   reason="CAPTCHA on the form: finish it in an assisted session")
            unresolved: list[str] = []
            steps = 0
            while True:                                              # one pass per page of a multi-step form
                fields = adapter.fields(page)
                fp = plan(fields, ctx)
                by_key = {f.key: f for f in fields}
                for d in fp.decisions:
                    adapter.apply(page, d, by_key[d.key])
                if adapter.upload_rejected(page) and docx_path is not None:
                    docx_path.write_bytes(render_docx(vault, recipe))
                    for f in fields:
                        if f.type == "file" and any(d.key == f.key for d in fp.decisions):
                            page.locator(f'[data-jobbot-idx="{f.key}"]').first.set_input_files(str(docx_path))
                unresolved += [f.label for f in fp.unresolved]
                if unresolved and not assisted:
                    break
                if not adapter.advance(page):
                    break
                steps += 1
                step_errors = visible_errors(page)
                if step_errors:            # "Next" didn't move on: stop instead of refilling the same step forever
                    return _to_waiting(application_id, mode="assisted", step="form", notify=notify,
                                       reason="form step not accepted: " + "; ".join(step_errors)[:300])
                if steps >= MAX_FORM_STEPS:
                    return _to_waiting(application_id, mode="assisted", step="form", notify=notify,
                                       reason=f"form still going after {MAX_FORM_STEPS} steps: finish it yourself")
            if unresolved and not assisted:
                return _to_waiting(application_id, mode="assisted", step="questions", notify=notify,
                                   unresolved=unresolved,
                                   reason="Needs your answer: " + "; ".join(q[:80] for q in unresolved[:5]))
            if not submit:
                return Outcome("dry-run", "filled but not submitted", "dry-run")

            with db.worker_session() as s:
                claimed = gate.claim(s, application_id, via="referral" if app["via_referral"] else "normal",
                                     adapter=adapter.name)
            if not claimed.ok:
                return Outcome("requeued" if claimed.not_before else "closed", claimed.reason, "gate")

            if assisted:
                if notify:
                    notify(app["user_id"], f"Assisted session ready: {app['company_name']} - {app['title']}.\n"
                                           "Solve any CAPTCHA and press Submit in the opened browser.")
                ok = adapter.confirmed(page, timeout_ms=ASSIST_TIMEOUT_MS)
            else:
                adapter.submit(page)
                ok = adapter.confirmed(page)
            with db.worker_session() as s:
                if ok:
                    gate.finalize(s, application_id, adapter=adapter.name)
                    return Outcome("applied", "", "submit")
                errors = visible_errors(page)
                if has_captcha(page):
                    gate.release(s, application_id, reason="CAPTCHA challenge on submit", step="captcha",
                                 adapter=adapter.name, adapter_failed=False, next_status="waiting")
                elif errors or (assisted and page.locator("form").count()):
                    gate.release(s, application_id, reason="form not accepted: " + "; ".join(errors)[:300],
                                 step="submit", adapter=adapter.name, next_status="waiting")
                else:
                    gate.mark_unconfirmed(s, application_id, reason="submitted but no confirmation seen; please check")
                    return Outcome("unconfirmed", "no confirmation page", "confirm")
            reason = "CAPTCHA challenge on submit" if has_captcha(page) else f"form not accepted: {'; '.join(errors)[:300]}"
            mode = "assisted" if "CAPTCHA" in reason else "manual"
            return _to_waiting(application_id, mode=mode, step="submit", reason=reason, notify=notify)
        except Exception as e:                       # adapter crash before claim: never submitted
            log.exception("application %s failed", application_id)
            with db.worker_session() as s:
                row = s.execute(text("SELECT claimed_at, status FROM applications WHERE id = :i"),
                                {"i": application_id}).first()
                if row and row.claimed_at is not None and row.status == "applying":
                    gate.mark_unconfirmed(s, application_id, reason=f"crashed after claim: {e}"[:300])
                    return Outcome("unconfirmed", str(e), "crash")
                health.record(s, adapter.name, False)
            return _to_waiting(application_id, mode="manual", step="error", reason=str(e)[:300], notify=notify)
        finally:
            if not keep_open:
                page.close()


def _country(location: str | None, vault: Vault) -> str:
    loc = (location or "").lower()
    for c in ("india", "united states", "united kingdom", "singapore", "germany", "canada", "australia", "uae"):
        if c in loc:
            return c
    if any(x in loc for x in ("bengaluru", "bangalore", "delhi", "gurugram", "gurgaon", "noida", "hyderabad",
                              "pune", "mumbai", "chennai", "kolkata")):
        return "india"
    return vault.personal.location.country.lower() if not loc else ""


def save_answers(user_id: UUID, answers: dict[str, str]) -> int:
    """User's answers to unknown questions -> form_answers (permanent)."""
    with db.worker_session() as s:
        for q, a in answers.items():
            s.execute(text("""INSERT INTO form_answers (user_id, question_hash, question_text, answer, answered_by)
                              VALUES (:u, :h, :q, :a, 0)
                              ON CONFLICT (user_id, question_hash) DO UPDATE SET answer = EXCLUDED.answer,
                                  updated_at = now(), answered_by = 0"""),
                      {"u": user_id, "h": question_hash(q), "q": q[:500], "a": a[:2000]})
    return len(answers)
