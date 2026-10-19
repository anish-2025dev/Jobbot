import pytest

pytestmark = pytest.mark.integration


def _budget(monkeypatch, mb):
    import app.config
    monkeypatch.setenv("DB_BUDGET_MB", str(mb))
    app.config.get_settings.cache_clear()


def test_health_ok(api, monkeypatch):
    _budget(monkeypatch, 100000)
    body = api.get("/health").json()
    assert body["state"] == "ok" and body["db_mb"] > 0 and body["budget_mb"] == 100000


def test_health_pause_when_over_budget(api, monkeypatch):
    _budget(monkeypatch, 1)   # a migrated DB is ~8+ MB
    assert api.get("/health").json()["state"] == "pause"


def test_health_needs_no_api_key(api):
    assert api.get("/health", headers={"X-API-Key": ""}).status_code == 200
