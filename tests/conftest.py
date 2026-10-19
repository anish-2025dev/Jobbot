"""Test-only defaults so modules that build settings at import time can be
collected. Integration fixtures override DATABASE_URL with a throwaway DB."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused@localhost:1/unused")
os.environ.setdefault("ENCRYPTION_KEY", "test-only")
os.environ.setdefault("SESSION_SECRET", "test-only-session-secret")
