"""Local imitations of Ashby, Workable, SmartRecruiters (shadow DOM), a custom
career site, and a multi-step Workday tenant with accounts, email
verification, sessions and custom listboxes."""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tests.fakes.ats_site import Submission, _parse_multipart

ASHBY = """<!doctype html><html><body><h1>Backend Engineer</h1>
<form method="post" action="{action}" enctype="multipart/form-data">
 <div><label for="_systemfield_name">Name *</label><input id="_systemfield_name" name="name" required></div>
 <div><label for="_systemfield_email">Email *</label><input id="_systemfield_email" name="email" type="email" required></div>
 <div><label for="phone">Phone</label><input id="phone" name="phone"></div>
 <div><label for="_systemfield_resume">Resume *</label><input id="_systemfield_resume" name="resume" type="file" required></div>
 <div><label for="li">LinkedIn</label><input id="li" name="linkedin"></div>
 <fieldset><legend>Will you now or in the future require visa sponsorship? *</legend>
   <label><input type="radio" name="spons" value="Yes" required> Yes</label>
   <label><input type="radio" name="spons" value="No"> No</label></fieldset>
 <button type="submit">Submit Application</button></form></body></html>"""

WORKABLE = """<!doctype html><html><body><h2>Full Stack Developer</h2>
<form method="post" action="{action}" enctype="multipart/form-data">
 <label for="firstname">First name *</label><input id="firstname" name="firstname" required>
 <label for="lastname">Last name *</label><input id="lastname" name="lastname" required>
 <label for="email">Email *</label><input id="email" name="email" required>
 <label for="headline">Headline</label><input id="headline" name="headline">
 <label for="phone">Phone *</label><input id="phone" name="phone" required>
 <label for="resume">Resume *</label><input id="resume" name="resume" type="file" required>
 <label for="cover">Cover letter</label><textarea id="cover" name="cover_letter"></textarea>
 <button type="submit">Submit application</button></form></body></html>"""

SR_LANDING = """<!doctype html><html><body><h1>Software Engineer</h1>
<button onclick="location.href='{apply}'">I'm interested</button></body></html>"""
SR_FORM = """<!doctype html><html><body><h1>Apply: Software Engineer</h1><sr-apply></sr-apply>
<script>
customElements.define('sr-apply', class extends HTMLElement {{
  connectedCallback() {{
    const root = this.attachShadow({{mode: 'open'}});
    root.innerHTML = `<form method="post" action="{action}" enctype="multipart/form-data">
      <label for="fn">First name *</label><input id="fn" name="firstName" required>
      <label for="ln">Last name *</label><input id="ln" name="lastName" required>
      <label for="em">Email *</label><input id="em" name="email" type="email" required>
      <label for="ph">Phone number *</label><input id="ph" name="phoneNumber" required>
      <label for="cv">Resume *</label><input id="cv" name="resume" type="file" required>
      <button type="submit">Send application</button></form>`;
  }}
}});
</script></body></html>"""

CUSTOM_JOB = """<!doctype html><html><body><h1>SDE - Contoso Careers</h1><p>Join our platform team.</p>
<a href="{form}">Apply now</a></body></html>"""
CUSTOM_FORM = """<!doctype html><html><body><h2>Application</h2>
<form method="post" action="{action}" enctype="multipart/form-data">
 <p><label for="a">Your full name *</label><input id="a" name="full_name" required></p>
 <p><label for="b">E-mail address *</label><input id="b" name="mail" type="email" required></p>
 <p><label for="c">Mobile number</label><input id="c" name="mobile"></p>
 <p><label for="d">Upload CV *</label><input id="d" name="cv" type="file" required></p>
 <p><label for="e">Expected salary (LPA) *</label><input id="e" name="expected" required></p>
 <button type="submit">Send application</button></form></body></html>"""

LISTBOX_JS = """<div id="popup" role="listbox" style="position:absolute;display:none;background:#fff;border:1px solid #999"></div>
<script>
document.querySelectorAll('button[aria-haspopup=listbox]').forEach(btn => {
  btn.addEventListener('click', e => {
    e.preventDefault();
    const pop = document.getElementById('popup');
    pop.innerHTML = '';
    JSON.parse(btn.dataset.options).forEach(opt => {
      const li = document.createElement('div'); li.setAttribute('role', 'option'); li.innerText = opt;
      li.onclick = () => { document.getElementById(btn.dataset.target).value = opt; btn.innerText = opt;
                           pop.style.display = 'none'; pop.innerHTML = ''; };
      pop.appendChild(li);
    });
    pop.style.display = 'block';
  });
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') { const p = document.getElementById('popup');
  p.style.display = 'none'; p.innerHTML = ''; } });
</script>"""


def wd_page(title: str, body: str, step_btn: str = "Save and Continue") -> str:
    return f"""<!doctype html><html><body><h2>{title}</h2>
<form method="post" enctype="multipart/form-data">{body}
<button type="submit" data-automation-id="bottom-navigation-next-button">{step_btn}</button></form>
{LISTBOX_JS}</body></html>"""


def listbox(label: str, target: str, options: list[str], required: bool = True) -> str:
    import json
    opts = json.dumps(options).replace('"', "&quot;")
    return (f'<label id="lb-{target}">{label}{" *" if required else ""}</label>'
            f'<button aria-haspopup="listbox" aria-labelledby="lb-{target}" aria-required="{str(required).lower()}" '
            f'data-target="{target}" data-options="{opts}">Select One</button>'
            f'<input type="hidden" name="{target}" id="{target}">')


WD_STEPS = {
    1: ("My Information", lambda email: f"""
        <label for="fn">First Name *</label><input id="fn" data-automation-id="legalNameSection_firstName" name="first" required>
        <label for="ln">Last Name *</label><input id="ln" data-automation-id="legalNameSection_lastName" name="last" required>
        <label for="ph">Phone Number *</label><input id="ph" data-automation-id="phone-number" name="phone" required>
        <label for="ci">City *</label><input id="ci" data-automation-id="addressSection_city" name="city" required>
        {listbox("How Did You Hear About Us?", "source", ["Company Website", "LinkedIn", "Employee Referral"])}"""),
    2: ("My Experience", lambda email: """
        <label for="cv">Resume/CV *</label><input id="cv" type="file" data-automation-id="file-upload-input-ref" name="resume" required>
        <label for="li">LinkedIn</label><input id="li" name="linkedin">"""),
    3: ("Application Questions", lambda email: """
        <label for="au">Are you legally authorized to work in India? *</label>
        <select id="au" name="auth" required><option value="">Select One</option><option>Yes</option><option>No</option></select>
        <label for="np">What is your notice period? *</label><textarea id="np" name="notice" required></textarea>"""),
    4: ("Voluntary Disclosures", lambda email: f"""
        {listbox("Gender", "gender", ["Male", "Female", "I do not wish to answer"], required=False)}
        <label for="tc">I agree to the terms and conditions *</label><input id="tc" type="checkbox" name="terms" value="yes" required>"""),
}


@dataclass
class WorkdayState:
    accounts: dict[str, dict] = field(default_factory=dict)        # email -> {password, verified}
    tokens: dict[str, str] = field(default_factory=dict)           # token -> email
    sessions: dict[str, dict] = field(default_factory=dict)        # sid -> {email, data, files}
    created: list[str] = field(default_factory=list)
    on_verification_email: object = None                           # callback(email, link)


class P5Site:
    def __init__(self):
        self.submissions: list[Submission] = []
        self.wd = WorkdayState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body="", headers=None):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _redirect(self, to, cookie=None):
                h = {"Location": to}
                if cookie:
                    h["Set-Cookie"] = cookie
                self._send(303, "", h)

            def _sid(self):
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "wdsess":
                        return v
                return None

            def _body(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("multipart/"):
                    return _parse_multipart(ctype, raw)
                return {k: v[0] for k, v in parse_qs(raw.decode()).items()}, {}

            def do_GET(self):
                u = urlparse(self.path)
                p = u.path.rstrip("/")
                if p.startswith("/ashby/") and p.endswith("/application"):
                    return self._send(200, ASHBY.format(action=p + "/submit"))
                if p.startswith("/workable/") and p.endswith("/apply"):
                    return self._send(200, WORKABLE.format(action=p + "/submit"))
                if p.startswith("/smartrecruiters/") and p.endswith("/apply"):
                    return self._send(200, SR_FORM.format(action=p + "/submit"))
                if p.startswith("/smartrecruiters/"):
                    return self._send(200, SR_LANDING.format(apply=p + "/apply"))
                if p.startswith("/custom/") and p.endswith("/form"):
                    return self._send(200, CUSTOM_FORM.format(action=p + "/submit"))
                if p.startswith("/custom/"):
                    return self._send(200, CUSTOM_JOB.format(form=p + "/form"))
                if p.startswith("/workday/"):
                    return self._wd_get(p, parse_qs(u.query))
                return self._send(404, "not found")

            def do_POST(self):
                p = urlparse(self.path).path.rstrip("/")
                if p.startswith("/workday/"):
                    return self._wd_post(p)
                fields, files = self._body()
                site.submissions.append(Submission(p.rsplit("/", 1)[0], fields, files))
                msg = {"/ashby/": "Thanks for applying!", "/workable/": "Thank you for your application!",
                       "/smartrecruiters/": "Thank you for applying!", "/custom/": "We received your application."}
                text_ = next((v for k, v in msg.items() if p.startswith(k)), "OK")
                if p.startswith("/custom/"):
                    text_ = "Your details reached our team. We'll get back to you within a week."
                return self._send(200, f"<html><body><h1>{text_}</h1></body></html>")

            # --- Workday ---------------------------------------------------------
            def _wd_get(self, p, q):
                wd = site.wd
                parts = p.split("/")                  # /workday/{tenant}/job/{id}[/apply...]
                base = "/".join(parts[:5])
                if p.endswith("/verify"):
                    email_ = wd.tokens.pop(q.get("token", [""])[0], None)
                    if email_:
                        wd.accounts[email_]["verified"] = True
                    return self._redirect(base + "/apply/manual")
                if len(parts) == 5:
                    return self._send(200, f"""<html><body><h1>Software Engineer</h1>
                        <button data-automation-id="adventureButton" onclick="document.getElementById('m').style.display='block'">Apply</button>
                        <div id="m" style="display:none"><a data-automation-id="applyManually" href="{base}/apply/manual">Apply Manually</a></div>
                        </body></html>""")
                sid = self._sid()
                sess = wd.sessions.get(sid or "")
                if p.endswith("/apply/manual") and not sess:
                    return self._send(200, f"""<html><body><h2>Sign In</h2><form method="post" action="{base}/signin">
                        <label for="e">Email Address</label><input id="e" data-automation-id="email" name="email">
                        <label for="pw">Password</label><input id="pw" type="password" data-automation-id="password" name="password">
                        <button type="submit" data-automation-id="signInSubmitButton">Sign In</button></form>
                        <a data-automation-id="createAccountLink" href="{base}/create">Create Account</a></body></html>""")
                if p.endswith("/create"):
                    return self._send(200, f"""<html><body><h2>Create Account</h2><form method="post" action="{base}/create">
                        <label for="e">Email Address</label><input id="e" data-automation-id="email" name="email">
                        <label for="pw">Password</label><input id="pw" type="password" data-automation-id="password" name="password">
                        <label for="vp">Verify New Password</label><input id="vp" type="password" data-automation-id="verifyPassword" name="verify">
                        <label><input type="checkbox" data-automation-id="createAccountCheckbox" name="agree" value="1"> I agree</label>
                        <button type="submit" data-automation-id="createAccountSubmitButton">Create Account</button></form></body></html>""")
                if not sess:
                    return self._redirect(base + "/apply/manual")
                if p.endswith("/apply/manual"):
                    return self._redirect(base + "/apply/step/1")
                step = int(p.rsplit("/", 1)[-1])
                if step in WD_STEPS:
                    title, body = WD_STEPS[step]
                    return self._send(200, wd_page(title, body(sess["email"])))
                return self._send(200, wd_page("Review", "<p>Review your application.</p>", "Submit"))

            def _wd_post(self, p):
                wd = site.wd
                parts = p.split("/")
                base = "/".join(parts[:5])
                fields, files = self._body()
                if p.endswith("/create"):
                    pw = fields.get("password", "")
                    strong = (len(pw) >= 8 and any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                              and any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw))
                    if not strong or pw != fields.get("verify") or fields.get("agree") != "1":
                        return self._send(200, '<div role="alert" class="error">Password does not meet requirements</div>')
                    wd.accounts[fields["email"]] = {"password": pw, "verified": False}
                    wd.created.append(fields["email"])
                    token = secrets.token_hex(8)
                    wd.tokens[token] = fields["email"]
                    link = f"{site.base}{base}/verify?token={token}"
                    if wd.on_verification_email:
                        wd.on_verification_email(fields["email"], link)
                    return self._send(200, "<html><body><h2>Verify your email</h2><p>We sent a verification link to "
                                           "your email address. Please verify your account to continue.</p></body></html>")
                if p.endswith("/signin"):
                    acct = wd.accounts.get(fields.get("email", ""))
                    if not acct or acct["password"] != fields.get("password") or not acct["verified"]:
                        return self._send(200, '<div role="alert">Invalid email or password</div>')
                    sid = secrets.token_hex(8)
                    wd.sessions[sid] = {"email": fields["email"], "data": {}, "files": {}}
                    return self._redirect(base + "/apply/step/1", cookie=f"wdsess={sid}; Path=/")
                sess = wd.sessions.get(self._sid() or "")
                if not sess:
                    return self._redirect(base + "/apply/manual")
                step = int(p.rsplit("/", 1)[-1])
                sess["data"].update(fields)
                sess["files"].update(files)
                if step <= 4:
                    return self._redirect(f"{base}/apply/step/{step + 1}")
                site.submissions.append(Submission(base, dict(sess["data"]), dict(sess["files"])))
                return self._send(200, "<html><body><h1>Congratulations!</h1><p>Your application has been submitted.</p></body></html>")

        return H
