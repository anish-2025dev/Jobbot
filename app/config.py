"""All tunables from the spec. Override any of them via environment / .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infra
    database_url: str                      # postgresql+psycopg://user:pass@host:5432/db
    redis_url: str = "redis://localhost:6379/0"
    db_api_role: str = "jobbot_api"        # migration 0003; empty disables SET ROLE
    encryption_key: str                    # base64 32-byte AES-GCM key, env only
    telegram_bot_token: str | None = None
    admin_api_key: str = ""                # admin API guard; empty = reject all admin-key calls
    allow_signup: bool = True              # anyone can create a profile (multi-user); set false for a private instance
    session_secret: str = ""               # signs login tokens and OAuth state (falls back to ENCRYPTION_KEY)
    public_base_url: str = "http://localhost:8000"   # for OAuth redirects and links in alerts

    # LLM (section 12): cheap model for extraction/scoring, strong for writing.
    llm_provider: str = "none"             # none | anthropic | openai (OpenAI-compatible, incl. Ollama)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_cheap_model: str = ""
    llm_strong_model: str = ""

    # Tier B aggregators (operator keys; free tiers are small, so queries are capped)
    jsearch_api_key: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    jooble_api_key: str = ""          # jooble.org/api/about (free)
    findwork_api_key: str = ""        # findwork.dev/developers (free)
    aggregator_queries_per_run: int = 4

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
    embedding_provider: str = "hashing"    # hashing | ollama
    ollama_url: str = "http://localhost:11434"
    ollama_embed_model: str = "nomic-embed-text:v1.5"
    max_one_page_loops: int = 5
    resume_engine: str = "auto"            # auto | tectonic | xelatex | pdflatex | typst
    resume_paper: str = "a4paper"          # a4paper | letterpaper

    # Application engine
    generic_adapter_enabled: bool = True   # LLM-guided adapter for custom career sites
    apply_workers: int = 1                 # parallel browser workers (5-8 on a capable VM)
    screenshot_store: str = "none"         # none | supabase (24 h lifecycle bucket)

    # Referral outreach (C14, C15, section 10)
    referral_hold_hours: int = 24
    outreach_daily_jobs: int = 10
    outreach_daily_emails: int = 30
    outreach_contacts_per_job: int = 3
    outreach_company_contacts_30d: int = 3
    outreach_review_days: int = 7          # review mode is forced for the first week
    outreach_approval_timeout_hours: int = 24
    bounce_pause_rate: float = 0.03
    outreach_pause_days: int = 7
    positive_mail_min_days: int = 7
    mail_delete_mode: str = "trash"        # or "permanent" (needs full mail scope)


@lru_cache
def get_settings() -> Settings:
    return Settings()
