"""Local HTTP server imitating Greenhouse and Lever application forms.

Each job has flags that switch on real-world complications:
captcha, unknown_question, reject_pdf (client-side file-type error),
server_error (validation error page on submit). Submissions are recorded,
including the uploaded resume's filename and bytes.
"""
from __future__ import annotations

import email
import email.policy
import html
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


@dataclass
class SiteJob:
    flags: set[str] = field(default_factory=set)


@dataclass
class Submission:
    path: str
    fields: dict[str, str]
    files: dict[str, tuple[str, int]]          # name -> (filename, size)


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict, dict]:
    msg = email.message_from_bytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body, policy=email.policy.default)
    fields, files = {}, {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = (filename, len(payload))
        elif name:
            prev = fields.get(name)
            val = payload.decode("utf-8", "replace")
            fields[name] = f"{prev},{val}" if prev else val
    return fields, files


GH_FORM = """<!doctype html><html><head><title>Software Development Engineer I - Acme</title></head><body>
<h1>Software Development Engineer I</h1><p>Bengaluru, India</p>
<form id="application-form" method="post" action="{action}" enctype="multipart/form-data">
 <div class="field"><label for="first_name">First Name *</label><input id="first_name" name="first_name" required></div>
 <div class="field"><label for="last_name">Last Name *</label><input id="last_name" name="last_name" required></div>
 <div class="field"><label for="email">Email *</label><input id="email" name="email" type="email" required></div>
 <div class="field"><label for="phone">Phone</label><input id="phone" name="phone" type="tel"></div>
 <div class="field"><label for="resume">Resume/CV *</label><input id="resume" name="resume" type="file" required
      accept=".pdf,.doc,.docx" onchange="chk(this)"><div id="upload-error" class="error" style="display:none"></div></div>
 <div class="field"><label for="cover_letter">Cover Letter</label><input id="cover_letter" name="cover_letter" type="file"></div>
 <div class="field"><label for="q_linkedin">LinkedIn Profile</label><input id="q_linkedin" name="q_linkedin"></div>
 <div class="field"><label for="q_website">Website</label><input id="q_website" name="q_website"></div>
 <div class="field"><label for="q_auth">Are you legally authorized to work in India? *</label>
   <select id="q_auth" name="q_auth" required><option value="">Select...</option><option>Yes</option><option>No</option></select></div>
 <div class="field"><label for="q_spons">Will you now or in the future require visa sponsorship? *</label>
   <select id="q_spons" name="q_spons" required><option value="">Select...</option><option>Yes</option><option>No</option></select></div>
 <div class="field"><label for="q_notice">What is your notice period? *</label><input id="q_notice" name="q_notice" required></div>
 <div class="field"><label for="q_hear">How did you hear about this job?</label><input id="q_hear" name="q_hear"></div>
 {extra}
 <fieldset><legend>Voluntary Self-Identification</legend>
  <label for="eeo_gender">Gender</label><select id="eeo_gender" name="eeo_gender"><option value="">Select...</option>
   <option>Male</option><option>Female</option><option>Decline To Self Identify</option></select>
  <label for="eeo_vet">Veteran Status</label><select id="eeo_vet" name="eeo_vet"><option value="">Select...</option>
   <option>I am a protected veteran</option><option>I am not a protected veteran</option><option>I don't wish to answer</option></select>
 </fieldset>
 {captcha}
 <button type="submit" id="submit_app">Submit Application</button>
</form>
<script>
function chk(i){{ const e=document.getElementById('upload-error');
  if (i.dataset.rejectPdf==='1' && i.files.length && i.files[0].name.toLowerCase().endsWith('.pdf')) {{
     e.style.display='block'; e.innerText='File type not supported. Please upload a .docx file.'; }}
  else {{ e.style.display='none'; e.innerText=''; }} }}
</script></body></html>"""

LEVER_FORM = """<!doctype html><html><body><h2>Backend Engineer - Lever Labs</h2>
<form id="application-form" method="post" action="{action}" enctype="multipart/form-data">
 <div class="application-question"><div class="application-label">Resume/CV ✱</div>
   <input type="file" name="resume" id="resume-upload-input" required></div>
 <div class="application-question"><label><div class="application-label">Full name ✱</div><input name="name" required></label></div>
 <div class="application-question"><label><div class="application-label">Email ✱</div><input name="email" type="email" required></label></div>
 <div class="application-question"><label><div class="application-label">Phone</div><input name="phone"></label></div>
 <div class="application-question"><label><div class="application-label">Current company</div><input name="org"></label></div>
 <div class="application-question"><label><div class="application-label">LinkedIn URL</div><input name="urls[LinkedIn]"></label></div>
 <div class="application-question"><label><div class="application-label">GitHub URL</div><input name="urls[GitHub]"></label></div>
 <div class="application-question"><div class="application-label">Are you willing to relocate? ✱</div>
   <label><input type="radio" name="cards[r1][field0]" value="Yes" required> Yes</label>
   <label><input type="radio" name="cards[r1][field0]" value="No"> No</label></div>
 <div class="application-question"><label><div class="application-label">Why do you want to work at Lever Labs? ✱</div>
   <textarea name="cards[c1][field0]" required></textarea></label></div>
 {captcha}
 <button type="submit" id="btn-submit">Submit application</button>
</form></body></html>"""

CAPTCHA = '<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001" style="width:300px;height:80px;border:1px solid #ccc"></div>'
UNKNOWN_Q = ('<div class="field"><label for="q_story">Describe a time you disagreed with your manager *</label>'
             '<textarea id="q_story" name="q_story" required></textarea></div>')


class FakeATSSite:
    def __init__(self):
        self.jobs: dict[str, SiteJob] = {}
        self.submissions: list[Submission] = []
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

    def add(self, path: str, *flags: str) -> str:
        self.jobs[path] = SiteJob(set(flags))
        return f"{self.base}{path}"

    def _handler(self):
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: str):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlparse(self.path).path.rstrip("/")
                if path.endswith("/apply") and path[:-6] in site.jobs:
                    path = path[:-6]
                job = site.jobs.get(path)
                if job is None:
                    return self._send(404, "not found")
                captcha = CAPTCHA if "captcha" in job.flags else ""
                if "/lever/" in path:
                    return self._send(200, LEVER_FORM.format(action=f"{path}/apply", captcha=captcha))
                page = GH_FORM.format(action=f"{path}/submit", captcha=captcha,
                                      extra=UNKNOWN_Q if "unknown_question" in job.flags else "")
                if "reject_pdf" in job.flags:
                    page = page.replace('id="resume" name="resume"', 'id="resume" name="resume" data-reject-pdf="1"')
                return self._send(200, page)

            def do_POST(self):
                path = urlparse(self.path).path
                base = path.rsplit("/", 1)[0]
                job = site.jobs.get(base)
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fields, files = _parse_multipart(self.headers.get("Content-Type", ""), body)
                if job is None:
                    return self._send(404, "not found")
                if "server_error" in job.flags:
                    return self._send(200, '<form><div role="alert" class="error">Phone number is invalid</div>'
                                           '<button type="submit">Submit Application</button></form>')
                site.submissions.append(Submission(base, fields, files))
                msg = "Application submitted!" if "/lever/" in base else "Thank you for applying to Acme!"
                return self._send(200, f"<html><body><h1>{html.escape(msg)}</h1></body></html>")

        return H
