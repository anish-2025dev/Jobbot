"""Adapter interface. An adapter knows one ATS's page flow; the shared
label-driven filler decides values."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.apply.browser import apply_decision, page_fields


@dataclass
class StepResult:
    ok: bool
    step: str = ""
    reason: str = ""


class Adapter:
    name = "base"
    url_pattern: re.Pattern | None = None
    form_scope: str | None = None                      # CSS selector of the form, if known
    submit_selectors = ("button[type=submit]", "input[type=submit]")
    submit_text = re.compile(r"submit( application)?|apply( now)?|send application", re.I)
    success_text = re.compile(r"thank(s| you) for (applying|your application|your interest)|application (has been )?"
                              r"(submitted|received)|we('ve| have) received your application|successfully applied", re.I)
    upload_error_text = re.compile(r"(file|format|type).{0,40}(not (supported|allowed|accepted)|invalid)|"
                                   r"(only|please upload).{0,20}(docx?|word)", re.I)

    def matches(self, url: str) -> bool:
        return bool(self.url_pattern and self.url_pattern.search(url or ""))

    def open(self, page, url: str) -> StepResult:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        return StepResult(True, "open")

    def fields(self, page):
        return page_fields(page, self.form_scope)

    def apply(self, page, decision, field) -> None:
        """Put one decided value into the page (custom widgets override this)."""
        apply_decision(page, decision, field)

    def submit(self, page) -> None:
        for sel in self.submit_selectors:
            loc = page.locator(sel)
            if loc.count():
                loc.first.click()
                return
        page.get_by_role("button", name=self.submit_text).first.click()

    def confirmed(self, page, timeout_ms: int = 30_000) -> bool:
        try:
            page.wait_for_function(
                "re => new RegExp(re, 'i').test(document.body ? document.body.innerText : '')",
                arg=self.success_text.pattern, timeout=timeout_ms)
            return True
        except Exception:
            return False

    def upload_rejected(self, page) -> bool:
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
        return bool(self.upload_error_text.search(body or ""))

    # multi-page ATSs (Workday) override this
    def advance(self, page) -> bool:
        """Move to the next page of a multi-step form. False when on the last step."""
        return False
