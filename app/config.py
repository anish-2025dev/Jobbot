"""All tunables from the spec. Override any of them via environment / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infra
    database_url: str                      # postgresql+psycopg://user:pass@host:5432/db
    redis_url: str = "redis://localhost:6379/0"
    encryption_key: str                    # base64 32-byte AES-GCM key, env only
    telegram_bot_token: str | None = None

    # C1 storage budget
    db_budget_mb: int = 300
    db_warn_pct: int = 70                  # shorten jobs retention to 3 days
    db_pause_pct: int = 90                 # pause ingestion + alert

    # Retention (days)
    jobs_retention_days: int = 7
    jobs_retention_days_tight: int = 3
    seen_jobs_retention_days: int = 7
    jd_signature_retention_days: int = 60
    recipe_cache_days: int = 7
    mail_actions_days: int = 30
    no_reply_delete_days: int = 45
    ledger_min_days: int = 90              # then dropped only once posting verified closed

    # Caps (never relaxed for volume)
    company_cap_per_30d: int = 3           # C5
    company_gap_hours: int = 3             # C6
    max_companies: int = 1000              # C7
    waiting_list_max: int = 30             # C8
    waiting_list_ttl_hours: int = 24

    # Scoring (C10)
    min_score: float = 0.60
    stretch_min: float = 0.50
    bucket_high: float = 0.80
    bucket_medium: float = 0.65
    referral_multiplier: float = 1.5
    repost_hamming_max: int = 6
    repost_window_days: int = 60

    # Resume engine
    recipe_reuse_similarity: float = 0.92
    embedding_dim: int = 256               # must match halfvec(256) in migration 0001
    max_one_page_loops: int = 5

    # Referrals / mail
    referral_hold_hours: int = 48
    positive_mail_min_days: int = 7
    mail_delete_mode: str = "trash"        # or "permanent" (needs full mail scope)


@lru_cache
def get_settings() -> Settings:
    return Settings()
