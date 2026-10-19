"""Ashby, SmartRecruiters and Workable: single-page forms the shared
label-driven filler handles; adapters only know how to reach the form and
recognize success."""
from __future__ import annotations

import re

from app.apply.adapters.base import Adapter, StepResult


class AshbyAdapter(Adapter):
    name = "ashby"
    url_pattern = re.compile(r"jobs\.ashbyhq\.com|/ashby/", re.I)
    form_scope = None
    submit_text = re.compile(r"submit application|submit", re.I)
    success_text = re.compile(r"thank(s| you) for applying|application (was |has been )?(submitted|received)|"
                              r"we('ve| have) received your application", re.I)

    def open(self, page, url: str) -> StepResult:
        target = url if url.rstrip("/").endswith("/application") else url.rstrip("/") + "/application"
        page.goto(target, wait_until="domcontentloaded", timeout=45_000)
        tab = page.get_by_role("tab", name=re.compile("application", re.I))
        if tab.count():
            tab.first.click()
        ok = page.locator("form, [class*='application-form']").count() > 0
        return StepResult(ok, "open", "" if ok else "no application form found")


class SmartRecruitersAdapter(Adapter):
    name = "smartrecruiters"
    url_pattern = re.compile(r"smartrecruiters\.com|/smartrecruiters/", re.I)
    submit_text = re.compile(r"submit|send application|apply", re.I)
    success_text = re.compile(r"thank(s| you) for (applying|your application)|application (has been )?(sent|submitted)|"
                              r"successfully applied", re.I)

    def open(self, page, url: str) -> StepResult:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        btn = page.get_by_role("button", name=re.compile(r"i'?m interested|apply( now)?", re.I))
        if btn.count():
            btn.first.click()
            page.wait_for_load_state("domcontentloaded")
        ok = page.locator("form, oc-oneclick-form, [data-test*='application']").count() > 0
        return StepResult(ok, "open", "" if ok else "no application form found")


class WorkableAdapter(Adapter):
    name = "workable"
    url_pattern = re.compile(r"apply\.workable\.com|/workable/", re.I)
    submit_text = re.compile(r"submit application|submit", re.I)

    def open(self, page, url: str) -> StepResult:
        target = url if url.rstrip("/").endswith("/apply") else url.rstrip("/") + "/apply/"
        page.goto(target, wait_until="domcontentloaded", timeout=45_000)
        ok = page.locator("form").count() > 0
        return StepResult(ok, "open", "" if ok else "no application form found")
