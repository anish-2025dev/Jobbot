from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.api.account import router as account_router
from app.api.dashboard import router as dashboard_router
from app.api.onboarding import router as onboarding_router
from app.api.webhooks import router as webhooks_router
from app.budget import budget_state
from app.config import get_settings
from app.resume.api import router as resume_router
from app.vault.api import router as vault_router

app = FastAPI(title="JobBot", version="1.0.0")
for r in (account_router, vault_router, resume_router, dashboard_router, onboarding_router, webhooks_router):
    app.include_router(r)

WEB = Path(__file__).parent / "web"
app.mount("/app", StaticFiles(directory=WEB, html=True), name="web")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/app/")


@app.get("/health")
def health() -> dict:
    s = get_settings()
    used = db.db_size_mb()
    pct, state = budget_state(used, s.db_budget_mb, s.db_warn_pct, s.db_pause_pct)
    return {"db_mb": round(used, 1), "budget_mb": s.db_budget_mb, "pct": round(pct, 1), "state": state}
