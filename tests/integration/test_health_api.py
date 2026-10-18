import importlib

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def client(migrated_url, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", migrated_url)
    monkeypatch.setenv("ENCRYPTION_KEY", "test-only")
    import app.config
    app.config.get_settings.cache_clear()
    import app.db
    importlib.reload(app.db)
    import app.main
    importlib.reload(app.main)
    yield TestClient(app.main.app)
    app.config.get_settings.cache_clear()


def test_health_ok(client, monkeypatch):
    monkeypatch.setenv("DB_BUDGET_MB", "100000")
    import app.config
    app.config.get_settings.cache_clear()
    body = client.get("/health").json()
    assert body["state"] == "ok" and body["db_mb"] > 0 and body["budget_mb"] == 100000


def test_health_pause_when_over_budget(client, monkeypatch):
    monkeypatch.setenv("DB_BUDGET_MB", "1")   # a migrated DB is ~8+ MB
    import app.config
    app.config.get_settings.cache_clear()
    assert client.get("/health").json()["state"] == "pause"
