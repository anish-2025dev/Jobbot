"""Lever application forms: jobs.lever.co/{company}/{id}/apply."""
from __future__ import annotations

import re

from app.apply.adapters.base import Adapter, StepResult


class LeverAdapter(Adapter):
    name = "lever"
    url_pattern = re.compile(r"jobs\.lever\.co", re.I)
    form_scope = "form#application-form, form.application-form, form[action*='apply'], form"
    submit_selectors = ("#btn-submit", "button[type=submit]")
    success_text = re.compile(r"application submitted|thanks for applying|thank you for applying|"
                              r"we('ve| have) received your application", re.I)

    def open(self, page, url: str) -> StepResult:
        target = url if url.rstrip("/").endswith("/apply") else url.rstrip("/") + "/apply"
        page.goto(target, wait_until="domcontentloaded", timeout=45_000)
        ok = page.locator("form").count() > 0
        return StepResult(ok, "open", "" if ok else "no application form found")
