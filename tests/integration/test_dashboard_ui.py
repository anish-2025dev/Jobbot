"""The dashboard renders and works in a real browser against a live server."""
import json
import os
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
VAULT = json.loads((Path(__file__).parents[2] / "examples/vault.example.json").read_text())


@pytest.fixture(scope="module")
def server(module_db):
    import importlib

    import uvicorn
    os.environ.setdefault("ENCRYPTION_KEY", "ui-test")
    import app.config
    app.config.get_settings.cache_clear()
    for m in ("app.auth", "app.api.account", "app.api.dashboard", "app.api.onboarding", "app.vault.api",
              "app.resume.service", "app.resume.api", "app.main"):
        importlib.reload(importlib.import_module(m))
    import app.main
    srv = uvicorn.Server(uvicorn.Config(app.main.app, host="127.0.0.1", port=0, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"no Chromium: {e}")
        yield b
        b.close()


def test_signup_and_every_view_renders(server, browser):
    import httpx
    from app.redis_client import get_redis
    get_redis.cache_clear()
    if get_redis():
        get_redis().flushdb()
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server + "/app/")
    page.get_by_role("button", name="New here? Create an account").click()
    page.fill("#nm", "Sample Candidate")
    page.fill("#em", "ui-user@example.com")
    page.fill("#pw", "correct horse battery")
    page.get_by_role("button", name="Create account").click()
    page.wait_for_selector("h1:has-text('Today')")
    page.wait_for_selector("text=Finish setting up")
    sess = page.evaluate("JSON.parse(localStorage.getItem('jobbot'))")
    httpx.put(f"{server}/users/{sess['user_id']}/vault", json=VAULT,
              headers={"Authorization": f"Bearer {sess['token']}"}).raise_for_status()
    for name, heading in [("Ranked jobs", "Ranked jobs"), ("Waiting", "Waiting for you"), ("Referrals", "Referral outreach"),
                          ("Applications", "Applications"), ("Profile", "Profile"), ("Settings", "Settings")]:
        page.get_by_role("button", name=name, exact=True).click()
        page.wait_for_selector(f"h1:has-text('{heading}')")
    # profile form loads the saved vault, edits a field, saves with If-Match
    page.get_by_role("button", name="Profile", exact=True).click()
    page.wait_for_function("document.querySelector('#f-personal-full_name')?.value === 'Sample Candidate'")
    page.get_by_role("button", name="Target roles").click()
    page.wait_for_function("document.querySelector('#f-target_roles-1-name')?.value === 'Data Engineer'")
    page.fill("#f-target_roles-1-name", "Data and Analytics Engineer")
    page.get_by_role("button", name="Save profile").click()
    page.wait_for_selector("text=Saved, revision 2")
    saved = httpx.get(f"{server}/users/{sess['user_id']}/vault", headers={"Authorization": f"Bearer {sess['token']}"}).json()
    assert saved["data"]["target_roles"][1]["name"] == "Data and Analytics Engineer"
    assert saved["gaps"] and all({"path", "label", "asked"} <= set(g) for g in saved["gaps"])
    # settings toggle round-trips to the API
    page.get_by_role("button", name="Settings", exact=True).click()
    page.check("#auto_apply")
    page.wait_for_selector(".toast:has-text('Settings saved.')")
    st = httpx.get(f"{server}/users/{sess['user_id']}/settings", headers={"Authorization": f"Bearer {sess['token']}"}).json()
    assert st["auto_apply"] is True
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_selector("text=Sign in")
    assert errors == []
