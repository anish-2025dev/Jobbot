from fastapi import FastAPI

from app import db
from app.budget import budget_state
from app.config import get_settings
from app.resume.api import router as resume_router
from app.vault.api import router as vault_router

app = FastAPI(title="JobBot", version="1.0.0")
for r in (vault_router, resume_router):
    app.include_router(r)


@app.get("/health")
def health() -> dict:
    s = get_settings()
    used = db.db_size_mb()
    pct, state = budget_state(used, s.db_budget_mb, s.db_warn_pct, s.db_pause_pct)
    return {"db_mb": round(used, 1), "budget_mb": s.db_budget_mb, "pct": round(pct, 1), "state": state}
