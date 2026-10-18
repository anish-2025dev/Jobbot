from fastapi import FastAPI

from app.budget import budget_state
from app.config import get_settings
from app.db import db_size_mb

app = FastAPI(title="JobBot", version="0.0.1")


@app.get("/health")
def health() -> dict:
    s = get_settings()
    used = db_size_mb()
    pct, state = budget_state(used, s.db_budget_mb, s.db_warn_pct, s.db_pause_pct)
    return {"db_mb": round(used, 1), "budget_mb": s.db_budget_mb, "pct": round(pct, 1), "state": state}
