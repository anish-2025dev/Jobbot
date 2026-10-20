"""Identity documents: masked API, merge-patch edits, and serialized concurrent edits."""
import threading
import time
import uuid

import pytest


@pytest.fixture
def uid(api):
    r = api.post("/users", json={"email": f"id{uuid.uuid4().hex[:8]}@test.dev"})
    return r.json()["id"]


def test_masked_roundtrip_and_removal(api, uid):
    r = api.patch(f"/users/{uid}/identity", json={"pan": "abcde1234f", "aadhaar": "2345 6789 0123"})
    assert r.status_code == 200 and r.json()["data"]["pan"].endswith("234F") and "ABCDE" not in r.text
    r = api.patch(f"/users/{uid}/identity", json={"aadhaar": None})
    assert "aadhaar" not in r.json()["data"] and "pan" in r.json()["data"]
    bad = api.patch(f"/users/{uid}/identity", json={"pan": "NOTAPAN99"})
    assert bad.status_code == 422 and "NOTAPAN99" not in bad.text


def test_concurrent_edit_waits_for_the_lock(api, uid):
    """A second edit can't read the documents while another edit holds the lock, so it
    can't merge onto a stale snapshot and undo a removal."""
    from uuid import UUID

    from app import db
    from app.vault import identity as ident
    api.patch(f"/users/{uid}/identity", json={"pan": "ABCDE1234F", "uan": "123456789012"})
    held, done = threading.Event(), threading.Event()

    def remover():
        with db.user_session(UUID(uid)) as s:
            ident.lock(s, UUID(uid))
            cur = ident.load(s, UUID(uid))
            held.set()
            time.sleep(1.0)                                    # slow removal in flight
            ident.save(s, UUID(uid), cur.model_copy(update={"pan": None}))
        done.set()

    t = threading.Thread(target=remover)
    t.start()
    held.wait(5)
    r = api.patch(f"/users/{uid}/identity", json={"uan": "999999999999"})   # starts mid-removal
    assert done.is_set(), "the edit ran before the removal committed"
    t.join()
    assert "pan" not in r.json()["data"] and r.json()["data"]["uan"].endswith("9999")
