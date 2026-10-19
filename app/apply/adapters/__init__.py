"""Adapter registry. Build order (section 8): Greenhouse, Lever (P3), then
Ashby, SmartRecruiters, Workable, Workday and the generic LLM adapter (P5)."""
from __future__ import annotations

from app.apply.adapters.base import Adapter

_REGISTRY: list[Adapter] = []


def register(adapter: Adapter) -> Adapter:
    _REGISTRY.append(adapter)
    return adapter


def get_adapter(ats: str | None, url: str | None) -> Adapter | None:
    _load()
    for a in _REGISTRY:
        if a.name == ats or a.matches(url or ""):
            return a
    return None


def names() -> list[str]:
    _load()
    return [a.name for a in _REGISTRY]


_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.apply.adapters.greenhouse import GreenhouseAdapter
    from app.apply.adapters.lever import LeverAdapter
    register(GreenhouseAdapter())
    register(LeverAdapter())
    try:                                   # P5 adapters
        from app.apply.adapters import extra
        extra.register_all(register)
    except ImportError:
        pass
