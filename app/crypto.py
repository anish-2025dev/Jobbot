"""Per-user encryption of secrets at rest (section 13).

AES-256-GCM with the key from ENCRYPTION_KEY (env only, never in the DB).
The associated data binds each ciphertext to its owner and purpose, so a
row copied to another user (or reused for a different secret type) fails to
decrypt instead of leaking.

Format: b"v1" | 12-byte nonce | ciphertext+tag
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from functools import lru_cache
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

_VERSION = b"v1"


class CryptoError(Exception):
    pass


@lru_cache
def _key() -> bytes:
    raw = get_settings().encryption_key
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        key = b""
    if len(key) != 32:
        # Dev/test convenience: derive a key from a non-base64 secret, but
        # never silently accept an empty one.
        if not raw:
            raise CryptoError("ENCRYPTION_KEY is not set")
        key = hashlib.sha256(raw.encode()).digest()
    return key


def _aad(user_id: UUID | str, purpose: str) -> bytes:
    return f"{user_id}|{purpose}".encode()


def encrypt(plaintext: str | bytes, user_id: UUID | str, purpose: str) -> bytes:
    data = plaintext.encode() if isinstance(plaintext, str) else plaintext
    nonce = os.urandom(12)
    return _VERSION + nonce + AESGCM(_key()).encrypt(nonce, data, _aad(user_id, purpose))


def decrypt(blob: bytes, user_id: UUID | str, purpose: str) -> bytes:
    blob = bytes(blob)
    if not blob.startswith(_VERSION) or len(blob) < 2 + 12 + 16:
        raise CryptoError("unknown ciphertext format")
    nonce, ct = blob[2:14], blob[14:]
    try:
        return AESGCM(_key()).decrypt(nonce, ct, _aad(user_id, purpose))
    except Exception as e:  # InvalidTag
        raise CryptoError("decryption failed (wrong key, user or purpose)") from e


def encrypt_json(obj: dict, user_id: UUID | str, purpose: str) -> bytes:
    return encrypt(json.dumps(obj, separators=(",", ":")), user_id, purpose)


def decrypt_json(blob: bytes, user_id: UUID | str, purpose: str) -> dict:
    return json.loads(decrypt(blob, user_id, purpose))


def keyed_hash64(value: str) -> int:
    """HMAC-SHA256 of a normalized value, truncated to a signed bigint.

    Used for do-not-contact and outreach logs: the address itself is never
    stored, and without the key a dictionary of known emails can't be
    matched against the table."""
    digest = hmac.new(_key(), value.strip().lower().encode(), hashlib.sha256).digest()
    v = int.from_bytes(digest[:8], "big")
    return v - (1 << 64) if v >= 1 << 63 else v
