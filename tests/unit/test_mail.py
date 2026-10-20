import base64
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.mail.alerts_parse import parse_alert
from app.mail.classify import classify, extract_deadline, extract_otp, is_job_alert
from app.mail.gmail import GmailAuthError, GmailClient
from app.mail.message import from_gmail, from_rfc822
from app.notify.telegram import TelegramBot
from tests.fakes.mail import make_msg

NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)


def c(from_addr, subject, body="", context=None, **kw):
    return classify(make_msg(from_addr, subject, body, **kw), context=context, now=NOW)


@pytest.mark.parametrize("from_addr,subject,body,context,expected", [
    ("no-reply@greenhouse.io", "Thank you for applying to Northwind", "We have received your application.", "application", "confirmation"),
    ("careers@northwind.com", "Your application", "Unfortunately, we have decided to move forward with other candidates.", "application", "rejection"),
    ("recruiting@northwind.com", "Interview invitation - SDE I", "We'd like to schedule a call. Please share your availability.", "application", "interview"),
    ("no-reply@hackerrank.com", "Northwind coding assessment", "Complete the HackerRank test within 72 hours.", "application", "test"),
    ("hr@northwind.com", "Offer letter", "Congratulations! We are pleased to offer you the position.", "application", "offer"),
    ("hr@northwind.com", "Next steps", "Could you please share your transcripts and a copy of your ID?", "application", "info_request"),
    ("priya@gmail.com", "Re: Referral for SDE I", "Done! I've referred you, here is the referral link: https://x.co/r/1", "outreach", "referral_given"),
    ("priya@gmail.com", "Re: Referral for SDE I", "Sorry, I'm not able to refer for this team. Please don't contact me again.", "outreach", "outreach_decline"),
    ("priya@gmail.com", "Re: Referral for SDE I", "Sure, can you send me your resume and the job ID?", "outreach", "outreach_question"),
    ("noreply@workday.com", "Your verification code", "Your verification code is 483920. It expires in 10 minutes.", None, "otp"),
    ("noreply@myworkday.com", "Verify your email", "Please verify your email address to activate your account.", None, "verification"),
    ("jobalerts-noreply@linkedin.com", "30+ new jobs for software engineer", "View jobs", None, "job_alert"),
    ("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)", "Address not found", None, "bounce"),
    ("talent@startup.io", "Opportunity", "I came across your profile and wanted to reach out about a backend role.", None, "recruiter"),
])
def test_classify(from_addr, subject, body, context, expected):
    assert c(from_addr, subject, body, context).cls == expected


def test_we_offer_benefits_is_not_an_offer():
    r = c("careers@acme.com", "SDE role at Acme", "We offer competitive pay and health insurance. Apply today!")
    assert r.cls != "offer"


def test_rejection_mentioning_interview_is_still_rejection():
    r = c("careers@acme.com", "Update on your interview",
          "Thank you for interviewing. Unfortunately we will not be moving forward with your application.", "application")
    assert r.cls == "rejection"


def test_weak_rejection_is_never_trashable():
    r = c("careers@acme.com", "Interview reschedule",
          "Unfortunately the panel is unavailable. Can we reschedule your interview to Friday?", "application")
    assert r.cls == "interview"
    r = c("careers@acme.com", "Your application", "Unfortunately, the role details changed. We'll be in touch.", "application")
    assert r.cls == "unclear"          # kept and alerted, never trashed


def test_decline_words_outside_outreach_are_not_decline():
    r = c("news@acme.com", "Newsletter", "Not interested? Unsubscribe here.")
    assert r.cls not in ("outreach_decline", "rejection")


def test_unknown_mail_is_unclear_not_trashable():
    r = c("friend@gmail.com", "Dinner?", "Are we still on for Saturday?")
    assert r.cls == "unclear"


def test_bounce_extracts_recipient():
    r = c("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)",
          "Your message wasn't delivered to priya@acme.com because the address couldn't be found.")
    assert r.bounced_recipient == "priya@acme.com"


def test_otp_extraction():
    assert extract_otp("Your code is 483920") == "483920"
    assert extract_otp("Copyright 2026. Code: 7731") == "7731"
    assert extract_otp("Your OTP is AB12CD") == "AB12CD"
    assert extract_otp("Welcome to the 2026 batch") is None


def test_deadline_extraction():
    assert extract_deadline("Please complete it by 25 October 2026", NOW) == datetime(2026, 10, 25, 23, 59, tzinfo=timezone.utc)
    assert extract_deadline("complete within 72 hours", NOW).day == 21
    assert extract_deadline("deadline: Oct 30", NOW).month == 10
    assert extract_deadline("no date here", NOW) is None


def test_interview_link_and_deadline():
    r = c("recruiting@acme.com", "Interview", "Book a slot by October 22: https://calendly.com/acme/sde", "application")
    assert r.link == "https://calendly.com/acme/sde" and r.deadline.day == 22


# --- Tier C alert parsing -------------------------------------------------------------
def test_hand_selected_job_recommendation_is_parsed():
    msg = make_msg("jobs@cutshort.io", "Hand-selected jobs for you",
                   "Backend Engineer\nAcme\nBengaluru", html=(
                       '<a href="https://cutshort.io/job/Backend-Engineer-Acme-xYz12AbC">Backend Engineer</a>'))
    assert is_job_alert(msg.from_addr, msg.subject)
    assert classify(msg).cls == "job_alert"
    jobs = parse_alert(msg)
    assert len(jobs) == 1
    assert (jobs[0].title, jobs[0].company, jobs[0].req_id) == ("Backend Engineer", "Acme", "xYz12AbC")


LINKEDIN_HTML = """<table><tr><td>
<a href="https://www.linkedin.com/comm/jobs/view/4011112222/?trackingId=abc">Software Engineer I</a></td></tr>
<tr><td>Northwind Payments · Bengaluru, Karnataka, India</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4011112222/?trackingId=abc">View job</a></td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/4033334444/">Backend Developer</a></td></tr>
<tr><td>Contoso · Pune, Maharashtra, India</td></tr>
<tr><td><a href="https://www.linkedin.com/comm/jobs/unsubscribe">Unsubscribe</a></td></tr></table>"""


def test_parse_linkedin_alert():
    msg = make_msg("jobalerts-noreply@linkedin.com", "2 new jobs for software engineer", html=LINKEDIN_HTML)
    jobs = parse_alert(msg)
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("Software Engineer I", "Northwind Payments", "Bengaluru, Karnataka, India"),
        ("Backend Developer", "Contoso", "Pune, Maharashtra, India")]
    assert all(j.easy_apply_only and j.source == "alert_linkedin" for j in jobs)
    assert jobs[0].req_id == "4011112222" and "?" not in jobs[0].url


def test_parse_naukri_alert():
    html = """<a href="https://www.naukri.com/job-listings-sde-1-contoso-pune-0-to-2-years-181024500123?src=alert">SDE 1</a>
    <p>Contoso Technologies</p><p>0-2 Yrs</p><p>Pune</p>"""
    jobs = parse_alert(make_msg("naukrialerts@naukri.com", "Jobs for you", html=html))
    assert [(j.title, j.company, j.location) for j in jobs] == [("SDE 1", "Contoso Technologies", "Pune")]


# --- message parsing ----------------------------------------------------------------
def test_from_gmail_multipart():
    b64 = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
    msg = from_gmail({"id": "1", "threadId": "t1", "internalDate": "1760781600000", "labelIds": ["INBOX"],
                      "payload": {"mimeType": "multipart/alternative", "headers": [
                          {"name": "From", "value": "Acme HR <HR@Acme.com>"}, {"name": "Subject", "value": "Hi"},
                          {"name": "In-Reply-To", "value": "<abc@mail>"}],
                          "parts": [{"mimeType": "text/plain", "body": {"data": b64("plain body")}},
                                    {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}}]}})
    assert (msg.from_addr, msg.from_name, msg.domain, msg.text, msg.in_reply_to) == (
        "hr@acme.com", "Acme HR", "acme.com", "plain body", "<abc@mail>")


def test_from_rfc822():
    raw = b"From: Bot <noreply@x.io>\r\nTo: me@y.com\r\nSubject: Code\r\nDate: Sun, 18 Oct 2026 10:00:00 +0000\r\n\r\nYour code is 1234\r\n"
    msg = from_rfc822(raw, "42")
    assert msg.subject == "Code" and "1234" in msg.text and msg.date.year == 2026


# --- Gmail client wire format --------------------------------------------------------
def test_gmail_refresh_list_label_send():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url)))
        url = str(req.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(req.content.decode())
            assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["rt"]
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer at"
        if "/messages?" in url:
            q = parse_qs(urlparse(url).query)
            if "pageToken" in q:
                return httpx.Response(200, json={"messages": [{"id": "3", "threadId": "c"}]})
            return httpx.Response(200, json={"messages": [{"id": "1", "threadId": "a"}, {"id": "2", "threadId": "b"}],
                                             "nextPageToken": "p2"})
        if url.endswith("/labels") and req.method == "GET":
            return httpx.Response(200, json={"labels": [{"id": "L1", "name": "Existing"}]})
        if url.endswith("/labels") and req.method == "POST":
            return httpx.Response(200, json={"id": "L2", "name": json.loads(req.content)["name"]})
        if url.endswith("/modify"):
            body = json.loads(req.content)
            assert body == {"addLabelIds": ["STARRED", "L2"], "removeLabelIds": []}
            return httpx.Response(200, json={})
        if url.endswith("/send"):
            raw = json.loads(req.content)["raw"]
            assert base64.urlsafe_b64decode(raw + "==").startswith(b"From:")
            return httpx.Response(200, json={"id": "s1", "threadId": "ts1"})
        return httpx.Response(404)

    g = GmailClient("cid", "secret", "rt", transport=httpx.MockTransport(handler))
    assert g.list_ids("newer_than:2d") == ["1", "2", "3"]
    g.modify("1", add=["STARRED", "JobBot/Action"])
    assert g.send(b"From: me\r\n\r\nhi") == {"id": "s1", "threadId": "ts1"}
    assert sum(1 for m, u in seen if "oauth2" in u) == 1     # token cached


def test_gmail_revoked_token():
    g = GmailClient("cid", "s", "rt", transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    with pytest.raises(GmailAuthError):
        g.list_ids("x")


# --- OAuth state ----------------------------------------------------------------------
def test_oauth_state_roundtrip_and_tamper(monkeypatch):
    import uuid

    import app.config
    from app.mail.oauth import OAuthError, consent_url, make_state, read_state
    monkeypatch.setenv("SESSION_SECRET", "s3cret")
    app.config.get_settings.cache_clear()
    uid = uuid.uuid4()
    assert read_state(make_state(uid, "n1"), "n1") == uid
    bad = make_state(uid, "n1")[:-3] + "AAA"
    with pytest.raises(OAuthError):
        read_state(bad, "n1")
    with pytest.raises(OAuthError):                  # started in another browser (no / wrong nonce cookie)
        read_state(make_state(uid, "n1"), "other")
    with pytest.raises(OAuthError):
        read_state(make_state(uid, "n1"), None)
    url = consent_url(uid, "cid.apps.googleusercontent.com", "http://localhost:8000/oauth/google/callback", "n1")
    q = parse_qs(urlparse(url).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert "gmail.modify" in q["scope"][0] and "gmail.send" in q["scope"][0]
    app.config.get_settings.cache_clear()


# --- Telegram ------------------------------------------------------------------------
def test_telegram_send_with_buttons():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    TelegramBot("123:abc", transport=httpx.MockTransport(handler)).send(42, "hi", [[("Done", "done:m1")]])
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert seen["body"]["reply_markup"] == {"inline_keyboard": [[{"text": "Done", "callback_data": "done:m1"}]]}
