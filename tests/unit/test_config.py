"""Defaults must match the spec's hard constraints."""
import pytest

pytest.importorskip("pydantic_settings")
from app.config import Settings  # noqa: E402


@pytest.fixture
def s():
    return Settings(_env_file=None, database_url="postgresql+psycopg://x/y", encryption_key="k")


def test_spec_constraints(s):
    assert s.db_budget_mb == 300 and s.db_warn_pct == 70 and s.db_pause_pct == 90   # C1
    assert s.company_cap_per_30d == 3                                               # C5
    assert s.company_gap_hours == 3                                                 # C6
    assert s.max_companies == 1000                                                  # C7
    assert s.waiting_list_max == 30 and s.waiting_list_ttl_hours == 24              # C8
    assert s.min_score == 0.60 and s.stretch_min == 0.50                            # C10
    assert s.positive_mail_min_days == 7                                            # C13
    assert s.referral_hold_hours == 24                                              # C15
    assert s.outreach_daily_jobs == 10 and s.outreach_daily_emails == 30            # C14
    assert s.outreach_contacts_per_job == 3 and s.outreach_company_contacts_30d == 3
    assert s.bounce_pause_rate == 0.03 and s.outreach_pause_days == 7               # section 13


def test_buckets_ordered(s):
    assert s.stretch_min < s.min_score < s.bucket_medium < s.bucket_high <= 1.0


def test_retention(s):
    assert s.jobs_retention_days == 7 and s.jobs_retention_days_tight == 3
    assert s.seen_jobs_retention_days == 7 and s.recipe_cache_days == 7
    assert s.no_reply_delete_days == 45 and s.ledger_min_days == 90
    assert s.mail_actions_days == 30 and s.repost_window_days == 60


def test_embedding_dim_matches_migration(s):
    from pathlib import Path
    sql = (Path(__file__).parents[2] / "migrations/versions/0001_initial_schema.py").read_text()
    assert f"halfvec({s.embedding_dim})" in sql


def test_env_override(monkeypatch):
    monkeypatch.setenv("MIN_SCORE", "0.7")
    monkeypatch.setenv("MAX_COMPANIES", "2500")
    s = Settings(_env_file=None, database_url="x", encryption_key="k")
    assert s.min_score == 0.7 and s.max_companies == 2500


def test_required_secrets(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        Settings(_env_file=None)
