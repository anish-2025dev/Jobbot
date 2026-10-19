import json
import uuid

import httpx
import pytest

from app.embed import cosine, embed, to_pgvector
from app.llm.client import LLM, LLMError, LLMUnavailable, extract_json
from app.util.skills import extract_skills, must_have_skills, norm_skill
from app.util.text import content_words, html_links, html_to_text, jaccard, summarize


# --- crypto ---------------------------------------------------------------------
@pytest.fixture
def crypto(monkeypatch):
    import base64
    import os
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x/y")
    import app.config
    import app.crypto
    app.config.get_settings.cache_clear()
    app.crypto._key.cache_clear()
    yield app.crypto
    app.config.get_settings.cache_clear()
    app.crypto._key.cache_clear()


def test_crypto_roundtrip_and_binding(crypto):
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    blob = crypto.encrypt("hunter-key-123", u1, "hunter")
    assert b"hunter-key-123" not in blob
    assert crypto.decrypt(blob, u1, "hunter") == b"hunter-key-123"
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(blob, u2, "hunter")          # other user
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(blob, u1, "workday")         # other purpose
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]), u1, "hunter")  # tampered


def test_crypto_nonce_unique(crypto):
    u = uuid.uuid4()
    assert crypto.encrypt("x", u, "p") != crypto.encrypt("x", u, "p")


def test_crypto_json(crypto):
    u = uuid.uuid4()
    obj = {"refresh_token": "abc", "scopes": ["gmail.modify"]}
    assert crypto.decrypt_json(crypto.encrypt_json(obj, u, "gmail"), u, "gmail") == obj


def test_keyed_hash_normalizes_and_fits_bigint(crypto):
    a = crypto.keyed_hash64("Priya@Acme.com ")
    assert a == crypto.keyed_hash64("priya@acme.com")
    assert -(2**63) <= a < 2**63
    assert a != crypto.keyed_hash64("other@acme.com")


# --- text -----------------------------------------------------------------------
def test_html_to_text_escaped_greenhouse_content():
    raw = "&lt;p&gt;Build APIs&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;li&gt;Go&lt;/li&gt;&lt;/ul&gt;"
    assert html_to_text(raw) == "Build APIs\n\n- Python\n\n- Go"


def test_html_to_text_skips_scripts_and_keeps_links():
    raw = '<div>Hi<script>evil()</script> <a href="https://x.dev/j/1">Apply</a></div>'
    assert "evil" not in html_to_text(raw)
    assert html_links(raw) == [("https://x.dev/j/1", "Apply")]


def test_summarize_skips_boilerplate_and_caps():
    text = "About us\nWe are a company.\nYou will build high throughput payment APIs in Go.\n" + "x " * 400
    s = summarize(text, 120)
    assert s.startswith("You will build") and len(s) <= 120


def test_content_words_and_jaccard():
    a = content_words("Built 6 REST endpoints in ASP.NET Core")
    assert "rest" in a and "in" not in a and "6" not in a
    assert jaccard({"a"}, {"a"}) == 1.0 and jaccard({"a"}, {"b"}) == 0.0


# --- skills ---------------------------------------------------------------------
def test_ambiguous_words_need_tech_context():
    assert extract_skills("lets go home, spark joy, rust on the car, swift action, a c grade") == []
    assert set(extract_skills("Go, Rust, C/C++, Swift and Apache Spark")) >= {"go", "rust", "c", "c++", "swift", "spark"}


def test_longest_match_wins():
    skills = extract_skills("SQL Server and Spring Boot")
    assert "sql server" in skills and "sql" not in skills
    assert "spring boot" in skills


def test_aliases_fold():
    assert extract_skills("Postgres, K8s, NodeJS, React.js") == ["postgresql", "kubernetes", "node.js", "react"]
    assert norm_skill(" GCP ") == "google cloud"


def test_extra_terms_detected():
    assert "obscuredb" in extract_skills("We use ObscureDB daily", extra=("ObscureDB",))


def test_must_vs_nice():
    jd = "We build with Python and FastAPI.\nRequirements:\n- SQL\nNice to have:\n- Terraform\n- Python"
    must, nice = must_have_skills(jd)
    assert must == ["sql", "python", "fastapi"] and nice == ["terraform"]   # requirements section first


# --- embeddings -----------------------------------------------------------------
def test_embed_properties():
    a = embed("Python FastAPI Postgres backend APIs")
    assert len(a) == 256 and abs(sum(v * v for v in a) - 1) < 1e-6
    assert a == embed("Python  FastAPI Postgres backend APIs")      # deterministic
    assert cosine(a, embed("Backend APIs with Python, FastAPI and PostgreSQL")) > 0.6
    assert cosine(a, embed("Graphic designer, Figma, brand campaigns")) < 0.2
    assert to_pgvector([0.5, -0.25]) == "[0.50000,-0.25000]"


# --- LLM client -------------------------------------------------------------------
def test_extract_json_variants():
    assert extract_json('Sure! ```json\n{"a": 1}\n``` done') == {"a": 1}
    assert extract_json('prefix {"a": {"b": "x}y"}} suffix') == {"a": {"b": "x}y"}}
    assert extract_json("[1, 2]") == [1, 2]
    assert extract_json("no json here") is None


def _llm(provider, handler):
    return LLM(provider=provider, api_key="k", base_url="https://llm.test", cheap_model="c",
               strong_model="s", transport=httpx.MockTransport(handler))


def test_anthropic_wire_format():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), headers=req.headers, body=json.loads(req.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"ok": true}'}]})

    out = _llm("anthropic", handler).complete_json("hi", tier="strong")
    assert out == {"ok": True}
    assert seen["url"] == "https://llm.test/v1/messages"
    assert seen["headers"]["x-api-key"] == "k" and seen["headers"]["anthropic-version"]
    assert seen["body"]["model"] == "s" and "JSON" in seen["body"]["system"]


def test_openai_compatible_wire_format():
    seen = {}

    def handler(req):
        seen.update(url=str(req.url), headers=req.headers, body=json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    assert _llm("openai", handler).complete("hi", system="be brief") == "hello"
    assert seen["url"] == "https://llm.test/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer k"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["body"]["model"] == "c"


def test_retries_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    assert _llm("openai", handler).complete("x") == "ok" and calls["n"] == 3


def test_non_retryable_error_raises():
    with pytest.raises(LLMError):
        _llm("openai", lambda r: httpx.Response(401, text="bad key")).complete("x")


def test_json_corrective_retry():
    replies = iter(["not json", '{"fixed": 1}'])
    handler = lambda r: httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})
    assert _llm("openai", handler).complete_json("x") == {"fixed": 1}


def test_none_provider_unavailable():
    llm = LLM(provider="none", api_key="", base_url="", cheap_model="", strong_model="")
    assert not llm.available
    with pytest.raises(LLMUnavailable):
        llm.complete("x")


def test_summarize_drops_boilerplate_sentences():
    text = "We build payment APIs in Go for merchants across India. We are an equal opportunity employer.\n" \
           "Benefits include health insurance and a learning budget for everyone.\nRef 4411-9 internal."
    assert summarize(text) == "We build payment APIs in Go for merchants across India."
