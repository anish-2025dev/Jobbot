"""Generic LLM-guided adapter (P5) for custom career sites.

Reaching the form: follow an obvious Apply button/link; failing that, the
cheap LLM picks which visible control leads to the application. Filling uses
the same truthful filler. Success: the usual phrases, or the LLM judges the
page after Submit. Anything uncertain falls back to Assisted/Manual."""
from __future__ import annotations

import re

from app.apply.adapters.base import Adapter, StepResult
from app.llm.client import LLMError, LLMUnavailable

APPLY = re.compile(r"^\s*(apply( now| for this (job|role|position))?|start (your )?application|i'?m interested)\s*$", re.I)


class GenericAdapter(Adapter):
    name = "generic"
    url_pattern = None

    def __init__(self, llm=None):
        self.llm = llm

    def matches(self, url: str) -> bool:
        return False                                  # only used when explicitly chosen

    def open(self, page, url: str) -> StepResult:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        if self._has_form(page):
            return StepResult(True, "open")
        for role in ("link", "button"):
            ctl = page.get_by_role(role, name=APPLY)
            if ctl.count():
                ctl.first.click()
                page.wait_for_load_state("domcontentloaded")
                if self._has_form(page):
                    return StepResult(True, "open")
        if self.llm is not None and self.llm.available and self._llm_navigate(page):
            return StepResult(True, "open")
        return StepResult(False, "open", "could not find an application form")

    @staticmethod
    def _has_form(page) -> bool:
        return page.locator("form input[type=file], form input[type=email], form textarea").count() > 0

    def _llm_navigate(self, page) -> bool:
        ctls = page.locator("a, button")
        items = []
        for i in range(min(ctls.count(), 60)):
            t = (ctls.nth(i).inner_text() or "").strip()
            if t and len(t) < 60:
                items.append((i, t))
        if not items:
            return False
        listing = "\n".join(f"{i}: {t}" for i, t in items)
        try:
            data = self.llm.complete_json(
                f"Which control opens the job application form? Reply {{\"index\": n}} or {{\"index\": null}}.\n{listing}",
                tier="cheap", max_tokens=40)
        except (LLMError, LLMUnavailable):
            return False
        idx = data.get("index") if isinstance(data, dict) else None
        if not isinstance(idx, int) or idx not in {i for i, _ in items}:
            return False
        ctls.nth(idx).click()
        page.wait_for_load_state("domcontentloaded")
        return self._has_form(page)

    def confirmed(self, page, timeout_ms: int = 30_000) -> bool:
        if super().confirmed(page, timeout_ms=min(timeout_ms, 15_000)):
            return True
        if self.llm is None or not self.llm.available:
            return False
        body = page.inner_text("body")[:3000]
        try:
            data = self.llm.complete_json(
                "After clicking Submit on a job application, the page shows the text below. Was the application "
                f'accepted? Reply {{"submitted": true|false, "confidence": 0-1}}.\n\n{body}', tier="cheap", max_tokens=40)
        except (LLMError, LLMUnavailable):
            return False
        return isinstance(data, dict) and data.get("submitted") is True and float(data.get("confidence") or 0) >= 0.85
