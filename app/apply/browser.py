"""Playwright helpers shared by all adapters.

Fields are discovered by their accessible label (what a person reads), not
by brittle per-site CSS, so one filler works across ATSs. CAPTCHAs are only
ever detected, never solved: detection routes the application to Assisted
mode where the user solves it (C12).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from app.apply.filler import Decision, Field

FIELDS_JS = r"""
(scopeSel) => {
  const scope = (scopeSel && document.querySelector(scopeSel)) || document;
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  // depth-first over the light DOM and every open shadow root (SmartRecruiters
  // and other web-component forms render inputs inside shadow DOM)
  const deep = (root, sel) => {
    const out = [];
    const visit = r => { for (const el of r.querySelectorAll('*')) { if (el.matches(sel)) out.push(el);
                                                                     if (el.shadowRoot) visit(el.shadowRoot); } };
    visit(root); return out;
  };
  const visible = el => {
    if (el.type === 'file') return true;                    // file inputs are often visually hidden
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const labelFor = el => {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const lb = el.getAttribute('aria-labelledby');
    if (lb) return lb.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
    if (el.id) { const root = el.getRootNode(); const l = (root.querySelector ? root : document).querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText; }
    const wrap = el.closest('label'); if (wrap) return wrap.innerText;
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return lg.innerText; }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const t = p.querySelector('label, legend, .label, .application-label, .text, [class*="label"], [class*="question"]');
      if (t && t !== el && clean(t.innerText)) return t.innerText;
    }
    return el.placeholder || el.name || el.id || '';
  };
  const out = []; const groups = {}; let idx = 0;
  for (const el of deep(scope, 'input, select, textarea')) {
    const type = (el.tagName === 'SELECT') ? 'select' : (el.tagName === 'TEXTAREA') ? 'textarea' : (el.type || 'text').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (!visible(el) || el.disabled) continue;
    const label = clean(labelFor(el));
    const required = el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label) || /\*/.test(label.slice(-3));
    if (type === 'radio' || (type === 'checkbox' && el.name && deep(scope, `input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) {
      const g = el.name || `g${idx}`;
      const optLabel = clean((el.id && el.getRootNode().querySelector && el.getRootNode().querySelector(`label[for="${CSS.escape(el.id)}"]`) || el.closest('label') || {}).innerText || el.value);
      if (!groups[g]) {
        const fs = el.closest('fieldset'); const legend = fs && fs.querySelector('legend');
        let q = clean(legend ? legend.innerText : '');
        const prio = ['.application-label', '[class*="question-label"]', '[class*="label"]', '[class*="question"]',
                      'label:not([for])', 'p', 'span'];
        let p = el.parentElement;
        for (let i = 0; i < 5 && p && !q; i++, p = p.parentElement) {
          for (const sel of prio) {
            const t = Array.from(p.querySelectorAll(sel)).find(t => !t.contains(el) && !t.querySelector('input')
                       && clean(t.innerText) && clean(t.innerText) !== optLabel);
            if (t) { q = clean(t.innerText); break; }
          }
        }
        groups[g] = {key: `g:${g}`, label: q || g, type, name: g, required, options: [], multiple: type === 'checkbox'};
        out.push(groups[g]);
      }
      el.setAttribute('data-jobbot-opt', `${g}::${optLabel}`);
      groups[g].options.push(optLabel);
      groups[g].required = groups[g].required || required;
      continue;
    }
    el.setAttribute('data-jobbot-idx', String(idx));
    const options = type === 'select' ? Array.from(el.options).map(o => clean(o.text)) : [];
    out.push({key: String(idx), label, type, name: el.name || '', required, options, multiple: !!el.multiple});
    idx++;
  }
  return out;
}
"""

CAPTCHA_JS = r"""
() => {
  const sel = 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], ' +
              'iframe[src*="challenges.cloudflare.com"], iframe[title*="captcha" i], .g-recaptcha, .h-captcha, ' +
              '.cf-turnstile, [data-sitekey], #captcha, [id*="captcha" i]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    // invisible reCAPTCHA v3 badges are not a human challenge
    if (el.classList && el.classList.contains('grecaptcha-badge')) continue;
    if (r.width > 0 && r.height > 0) return true;
  }
  return false;
}
"""

ERRORS_JS = r"""
() => Array.from(document.querySelectorAll('[role=alert], .error, .errors, .field-error, .invalid-feedback, [class*="error-message"], [aria-invalid="true"]'))
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map(e => (e.innerText || e.getAttribute('aria-label') || e.name || '').trim()).filter(Boolean).slice(0, 10)
"""


def page_fields(page, scope: str | None = None) -> list[Field]:
    raw = page.evaluate(FIELDS_JS, scope)
    return [Field(key=r["key"], label=r["label"], type=r["type"], name=r["name"], required=bool(r["required"]),
                  options=[o for o in r["options"] if o], multiple=r["multiple"]) for r in raw]


def has_captcha(page) -> bool:
    return bool(page.evaluate(CAPTCHA_JS))


def visible_errors(page) -> list[str]:
    return page.evaluate(ERRORS_JS)


def apply_decision(page, d: Decision, field: Field) -> None:
    if field.key.startswith("g:"):
        group = field.key[2:]
        values = d.value if isinstance(d.value, list) else [d.value]
        for v in values:
            loc = page.locator(f'[data-jobbot-opt="{group}::{v}"]')
            if loc.count():
                loc.first.check(force=True)
        return
    loc = page.locator(f'[data-jobbot-idx="{field.key}"]').first
    if field.type == "file":
        loc.set_input_files(str(d.value))
    elif field.type == "select":
        try:
            loc.select_option(label=str(d.value))
        except Exception:
            loc.select_option(value=str(d.value))
    elif field.type == "checkbox":
        if d.value:
            loc.check(force=True)
    else:
        loc.fill(str(d.value))
        if loc.get_attribute("role") == "combobox":
            loc.press("Enter")


@contextmanager
def browser_session(headed: bool = False) -> Iterator:
    """A fresh browser context. Assisted mode uses a headed browser on the
    worker's display, or a remote Chromium (noVNC/Selenium) when
    SELENIUM_REMOTE_URL is set, so the user can solve a CAPTCHA and press
    Submit themselves."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-IN",
                                      timezone_id="Asia/Kolkata")
        try:
            yield context
        finally:
            context.close()
            browser.close()
