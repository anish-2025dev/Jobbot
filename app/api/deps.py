"""Shared API dependencies (see app/auth.py for the access rules)."""
from __future__ import annotations

from app.auth import Principal, principal, require_admin

require_api_key = require_admin          # backwards-compatible name

__all__ = ["Principal", "principal", "require_admin", "require_api_key"]
