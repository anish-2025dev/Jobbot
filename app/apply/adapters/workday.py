"""Workday (section 8.3, P5): multi-step wizard behind a per-tenant account.

1. Apply -> "Apply Manually".
2. Sign in with stored credentials, or create an account with a generated
   site-compliant password (stored encrypted in `credentials`, host = the
   tenant's host), click the verification link from Gmail, then sign in.
3. Step through My Information / My Experience / Questions / Disclosures /
   Review with the shared filler; custom listboxes are handled here.
4. Submit on the Review step.
Pages are driven by Workday's stable data-automation-id attributes.
"""
from __future__ import annotations

import logging
import re
import secrets
import string
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from app.apply.adapters.base import Adapter, StepResult
from app.apply.filler import Field, pick_option

log = logging.getLogger(__name__)
A = lambda aid: f'[data-automation-id="{aid}"]'   # noqa: E731


def site_password(length: int = 16) -> str:
    """Upper, lower, digit and special, as Workday tenants require."""
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!@#$%^&*"]
    chars = [secrets.choice(p) for p in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


@dataclass
class AuthContext:
    user_id: object
    email: str
    load: Callable[[str], tuple[str, str] | None]        # host -> (username, password)
    save: Callable[[str, str, str], None]                 # host, username, password
    wait_link: Callable[[str], str | None]               # domain -> verification link (blocks)


class WorkdayAdapter(Adapter):
    name = "workday"
    url_pattern = re.compile(r"myworkdayjobs\.com|myworkday\.com|/workday/", re.I)
    success_text = re.compile(r"congratulations|application (has been |was )?submitted|thank(s| you) for applying|"
                              r"successfully submitted", re.I)

    def open(self, page, url: str) -> StepResult:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        for aid in ("adventureButton", "applyButton"):
            if page.locator(A(aid)).count():
                page.locator(A(aid)).first.click()
                break
        if page.locator(A("applyManually")).count():
            page.locator(A("applyManually")).first.click()
        page.wait_for_load_state("domcontentloaded")
        return StepResult(True, "open")

    # --- account -----------------------------------------------------------------
    def authenticate(self, page, auth: AuthContext) -> StepResult:
        host = urlparse(page.url).hostname or ""
        on_auth_page = page.locator(f'{A("email")}, {A("signInLink")}, {A("createAccountLink")}').count() > 0
        if not on_auth_page:
            return StepResult(True, "auth")
        cred = auth.load(host)
        if cred is None:
            if page.locator(A("createAccountLink")).count():
                page.locator(A("createAccountLink")).first.click()
            password = site_password()
            page.locator(A("email")).first.fill(auth.email)
            page.locator(A("password")).first.fill(password)
            page.locator(A("verifyPassword")).first.fill(password)
            if page.locator(A("createAccountCheckbox")).count():
                page.locator(A("createAccountCheckbox")).first.check(force=True)
            page.locator(A("createAccountSubmitButton")).first.click()
            page.wait_for_load_state("domcontentloaded")
            auth.save(host, auth.email, password)                 # encrypted at rest
            if re.search(r"verif", page.inner_text("body"), re.I):
                link = auth.wait_link(host)
                if not link:
                    return StepResult(False, "verify", "verification email did not arrive")
                page.goto(link, wait_until="domcontentloaded")
            cred = (auth.email, password)
        if page.locator(A("signInLink")).count() and not page.locator(A("password")).count():
            page.locator(A("signInLink")).first.click()
        if page.locator(A("password")).count():
            page.locator(A("email")).first.fill(cred[0])
            page.locator(A("password")).first.fill(cred[1])
            page.locator(A("signInSubmitButton")).first.click()
            page.wait_for_load_state("domcontentloaded")
            if re.search(r"invalid|incorrect|wrong (email|password)", page.inner_text("body"), re.I):
                return StepResult(False, "sign-in", "Workday sign-in failed; reset the password for this tenant")
        return StepResult(True, "auth")

    # --- custom listboxes ------------------------------------------------------------
    def fields(self, page) -> list[Field]:
        fields = super().fields(page)
        boxes = page.locator('button[aria-haspopup="listbox"]')
        for i in range(boxes.count()):
            b = boxes.nth(i)
            b.evaluate("(el, i) => el.setAttribute('data-jobbot-lb', String(i))", i)
            label = b.get_attribute("aria-label") or ""
            lid = b.get_attribute("aria-labelledby")
            if lid:
                label = page.locator(f"#{lid}").inner_text()
            b.click()
            opts = [o.strip() for o in page.locator('[role="option"]').all_inner_texts() if o.strip()]
            page.keyboard.press("Escape")
            required = (b.get_attribute("aria-required") == "true") or label.strip().endswith("*")
            fields.append(Field(key=f"wdlb:{i}", label=label.strip(), type="select", required=required, options=opts))
        return fields

    def apply(self, page, decision, field) -> None:
        if not field.key.startswith("wdlb:"):
            return super().apply(page, decision, field)
        b = page.locator(f'[data-jobbot-lb="{field.key[5:]}"]').first
        b.click()
        options = page.locator('[role="option"]')
        texts = [o.strip() for o in options.all_inner_texts()]
        choice = pick_option(texts, str(decision.value))
        # Click the option whose whole text equals the choice. A substring match would pick
        # "British Indian Ocean Territory" for "India" or "Female" for "Male".
        idx = next((i for i, t in enumerate(texts) if choice is not None and t == choice), None)
        if idx is None:
            page.keyboard.press("Escape")
            raise ValueError(f"no option in '{field.label[:60]}' matches {str(decision.value)[:60]!r}")
        options.nth(idx).click()

    # --- wizard ----------------------------------------------------------------------
    def advance(self, page) -> bool:
        btn = page.locator(A("bottom-navigation-next-button"))
        if not btn.count():
            return False
        if re.search(r"submit", btn.first.inner_text(), re.I):
            return False                                        # Review step: submit() clicks it
        btn.first.click()
        page.wait_for_load_state("domcontentloaded")
        return True

    def submit(self, page) -> None:
        page.locator(A("bottom-navigation-next-button")).first.click()
