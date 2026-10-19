"""Greenhouse application forms: boards.greenhouse.io, job-boards.greenhouse.io,
and company sites embedding the board (gh_jid / grnhse iframe)."""
from __future__ import annotations

import re

from app.apply.adapters.base import Adapter, StepResult


class GreenhouseAdapter(Adapter):
    name = "greenhouse"
    url_pattern = re.compile(r"greenhouse\.io|gh_jid=", re.I)
    form_scope = "#application-form, #application_form, form#application, form[action*='applications']"
    submit_selectors = ("#submit_app", "button[type=submit]", "input[type=submit]")

    def open(self, page, url: str) -> StepResult:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        # embedded boards render the form in an iframe; follow it
        frame = page.locator("iframe#grnhse_iframe")
        if frame.count():
            src = frame.first.get_attribute("src")
            if src:
                page.goto(src, wait_until="domcontentloaded", timeout=45_000)
        apply_btn = page.get_by_role("button", name=re.compile(r"^apply( for this job)?$", re.I))
        if apply_btn.count() and not page.locator(self.form_scope).count():
            apply_btn.first.click()
        return StepResult(page.locator(self.form_scope).count() > 0 or page.locator("form").count() > 0,
                          "open", "" if page.locator("form").count() else "no application form found")
