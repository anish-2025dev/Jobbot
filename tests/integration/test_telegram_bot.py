"""Telegram bot loop: chat linking and inline buttons acting on the
linked user's data only."""
import json

import httpx
import pytest
from sqlalchemy import text

from tests.fakes.mail import FakeMailbox, make_msg
from tests.integration.conftest import seed_user
from tests.integration.test_mail_flow import make_app, run

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_redis():
    from app.redis_client import get_redis
    get_redis.cache_clear()
    r = get_redis()
    if r is None:
        pytest.skip("redis not available")
    r.flushdb()
    yield r
    r.flushdb()


def test_telegram_link_and_done_button(module_db):
    from app import db
    from app.notify import bot_loop
    from app.notify.telegram import TelegramBot, new_link_code
    uid = seed_user(module_db)
    make_app(module_db, uid, company="Proseware", title="SDE")
    box = FakeMailbox()
    m = box.add(make_msg("hr@proseware.com", "Interview", "Let's schedule your interview."))
    run(module_db, uid, box)
    with db.worker_session() as s:
        code = new_link_code(s, uid)
    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append((str(req.url).rsplit("/", 1)[-1], body))
        return httpx.Response(200, json={"ok": True, "result": True})

    bot = TelegramBot("t", transport=httpx.MockTransport(handler))
    bot_loop.handle_update(bot, {"update_id": 1, "message": {"chat": {"id": 777}, "text": f"/start {code}"}})
    assert sent[-1][0] == "sendMessage" and "Linked" in sent[-1][1]["text"]
    bot_loop.handle_update(bot, {"update_id": 2, "callback_query": {
        "id": "cb1", "data": f"done:{m.id}", "message": {"chat": {"id": 777}, "message_id": 5}}})
    assert sent[-1][0] == "answerCallbackQuery" and "done" in sent[-1][1]["text"].lower()
    with module_db.connect() as c:
        assert c.execute(text("SELECT user_done_at IS NOT NULL FROM responses WHERE gmail_id = :g"), {"g": m.id}).scalar()
    # an unlinked chat can't act on anyone's data
    bot_loop.handle_update(bot, {"update_id": 3, "callback_query": {
        "id": "cb2", "data": f"done:{m.id}", "message": {"chat": {"id": 999}, "message_id": 6}}})
    assert sent[-1][1]["text"] == "Not linked."
