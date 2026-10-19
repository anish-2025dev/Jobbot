"""Outcome-based weight tuning (section 6, P6).

Per user, weekly: logistic regression of "got a positive response" on the
six score components of every application with a known outcome
(training_examples). The learned per-component importance becomes the
scoring weights, shrunk toward the spec defaults by n / (n + PRIOR_N), so a
handful of outcomes can't swing scoring. Needs MIN_EXAMPLES rows with both
outcomes present; otherwise the defaults stay.

Pure Python (6 features, at most a few thousand rows).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ingest.scoring import COMPONENTS, DEFAULT_WEIGHTS

MIN_EXAMPLES = 30
MIN_POSITIVES = 3
PRIOR_N = 60
FLOOR = 0.02                      # no component ever drops to zero


@dataclass
class TuneResult:
    n: int
    positives: int
    weights: dict[str, float] | None
    reason: str = ""


def fit_logistic(X: list[list[float]], y: list[int], *, l2: float = 1.0, iters: int = 400, lr: float = 0.5) -> list[float]:
    """Batch gradient descent, features standardized; returns coefficients
    (no intercept) on the standardized scale."""
    d = len(X[0])
    mu = [sum(r[j] for r in X) / len(X) for j in range(d)]
    sd = [max(1e-6, math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / len(X))) for j in range(d)]
    Z = [[(r[j] - mu[j]) / sd[j] for j in range(d)] for r in X]
    w = [0.0] * d
    b = math.log((sum(y) + 0.5) / (len(y) - sum(y) + 0.5))
    n = len(Z)
    for _ in range(iters):
        gw = [l2 * wj / n for wj in w]
        gb = 0.0
        for z, t in zip(Z, y):
            s = b + sum(wj * zj for wj, zj in zip(w, z))
            p = 1 / (1 + math.exp(-max(min(s, 30), -30)))
            err = p - t
            gb += err / n
            for j in range(d):
                gw[j] += err * z[j] / n
        w = [wj - lr * g for wj, g in zip(w, gw)]
        b -= lr * gb
    return w


def weights_from_coefficients(coef: list[float], n: int) -> dict[str, float]:
    pos = [max(c, 0.0) for c in coef]
    total = sum(pos)
    learned = {k: (p / total if total else DEFAULT_WEIGHTS[k]) for k, p in zip(COMPONENTS, pos)}
    alpha = n / (n + PRIOR_N)
    blended = {k: (1 - alpha) * DEFAULT_WEIGHTS[k] + alpha * learned[k] for k in COMPONENTS}
    blended = {k: max(v, FLOOR) for k, v in blended.items()}
    z = sum(blended.values())
    return {k: round(v / z, 4) for k, v in blended.items()}


def tune_user(s: Session, user_id: UUID, apply: bool = True) -> TuneResult:
    rows = s.execute(text("""SELECT components, label FROM training_examples WHERE user_id = :u
                             AND cardinality(components) = 6"""), {"u": user_id}).all()
    X = [[c / 1000 for c in r.components] for r in rows]
    y = [int(r.label) for r in rows]
    n, pos = len(y), sum(y)
    if n < MIN_EXAMPLES:
        return TuneResult(n, pos, None, f"need {MIN_EXAMPLES} outcomes, have {n}")
    if pos < MIN_POSITIVES or n - pos < MIN_POSITIVES:
        return TuneResult(n, pos, None, "need both positive and negative outcomes")
    w = weights_from_coefficients(fit_logistic(X, y), n)
    if apply:
        s.execute(text("""UPDATE users SET settings = coalesce(settings, '{}') || jsonb_build_object('weights', CAST(:w AS jsonb),
                          'weights_n', CAST(:n AS integer)) WHERE id = :u"""), {"w": json.dumps(w), "n": n, "u": user_id})
    return TuneResult(n, pos, w)


def tune_all(s_factory) -> dict[str, TuneResult]:
    with s_factory() as s:
        ids = s.execute(text("""SELECT DISTINCT user_id FROM training_examples""")).scalars().all()
    out = {}
    for uid in ids:
        with s_factory() as s:
            out[str(uid)] = tune_user(s, uid)
    return out
