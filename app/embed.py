"""256-dim embeddings for JD similarity (recipe cache) and skill matching.

Default: deterministic feature hashing. No model download, no API cost,
identical across machines, so cache hits are reproducible. Words and
bigrams are hashed into 256 signed buckets with sublinear term frequency,
skills are folded through the alias table first, then L2-normalized.

Optional: EMBEDDING_PROVIDER=ollama uses nomic-embed-text v1.5, which is
Matryoshka-trained, so truncating to 256 dims keeps it meaningful. Changing
provider invalidates recipe_cache rows (stored with the provider name).
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

import xxhash

from app.config import get_settings
from app.util.skills import extract_skills
from app.util.text import STOPWORDS, words

DIM = 256


def _hashing(text: str) -> list[float]:
    toks = [w for w in words(text) if w not in STOPWORDS and not w.isdigit()]
    toks += [f"skill:{s}" for s in extract_skills(text)] * 2  # skills weigh more
    feats: dict[str, int] = {}
    for t in toks:
        feats[t] = feats.get(t, 0) + 1
    for a, b in zip(toks, toks[1:]):
        if not a.startswith("skill:") and not b.startswith("skill:"):
            k = f"{a} {b}"
            feats[k] = feats.get(k, 0) + 1
    vec = [0.0] * DIM
    for f, tf in feats.items():
        h = xxhash.xxh64_intdigest(f.encode())
        sign = 1.0 if (h >> 63) & 1 else -1.0
        vec[h % DIM] += sign * (1.0 + math.log(tf))
    return _normalize(vec)


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec] if n else vec


def _ollama(text: str) -> list[float]:
    import httpx

    s = get_settings()
    r = httpx.post(f"{s.ollama_url.rstrip('/')}/api/embeddings",
                   json={"model": s.ollama_embed_model, "prompt": f"search_document: {text[:8000]}"},
                   timeout=60)
    r.raise_for_status()
    full = r.json()["embedding"]
    return _normalize([float(x) for x in full[:DIM]])


def provider_name() -> str:
    return get_settings().embedding_provider


def embed(text: str) -> list[float]:
    if provider_name() == "ollama":
        return _ollama(text)
    return _cached_hashing(re.sub(r"\s+", " ", text or "").strip())


@lru_cache(maxsize=2048)
def _cached_hashing(text: str) -> list[float]:
    return _hashing(text)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.5f}" for v in vec) + "]"
