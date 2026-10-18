"""Checks on migration 0001 that need no database."""
import importlib.util
import re
from pathlib import Path

import pytest

PATH = Path(__file__).parents[2] / "migrations/versions/0001_initial_schema.py"
spec = importlib.util.spec_from_file_location("m0001", PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

SECTION4 = {
    "users", "vaults", "sources", "companies", "jobs", "seen_jobs",
    "applied_ledger", "company_cooldown", "applications", "waiting_list",
    "responses", "referral_contacts", "referral_requests", "outcomes_daily",
    "form_answers", "credentials", "recipe_cache", "adapter_health", "mail_actions",
}
STMTS = m.all_statements()
CREATED = {re.match(r"CREATE TABLE (\w+)", s).group(1) for s in STMTS if s.startswith("CREATE TABLE")}


def _strip_dollar(stmt: str) -> str:
    return re.sub(r"\$\$.*?\$\$", "$$", stmt, flags=re.S)


def _strip_comments(stmt: str) -> str:
    return "\n".join(l.split("--", 1)[0] for l in stmt.splitlines())


def test_every_section4_table_created():
    assert SECTION4 <= CREATED
    assert CREATED - SECTION4 == {"jd_signatures"}


def test_one_statement_per_execute():
    for s in STMTS:
        body = _strip_comments(_strip_dollar(s)).strip()
        assert body.endswith(";"), s[:80]
        assert body.count(";") == 1, s[:80]


def test_no_percent_or_bind_colons():
    # % breaks under pyformat drivers; ":name" would become a bind param.
    for s in STMTS:
        assert "%" not in s, s[:80]
        assert not re.search(r"(?<![:\w]):[A-Za-z_]", _strip_comments(s)), s[:80]


def test_partitioned_tables():
    for t in ("jobs", "seen_jobs", "jd_signatures"):
        stmt = next(s for s in STMTS if s.startswith(f"CREATE TABLE {t} "))
        assert "PARTITION BY RANGE" in stmt
        assert f"jobbot_ensure_daily_partitions('{t}'" in m.SCHEMA_SQL


def test_no_default_partition():
    code = _strip_comments(_strip_dollar(m.SCHEMA_SQL))
    assert "DEFAULT" not in re.findall(r"PARTITION OF \w+ (\w+)", code)
    assert "PARTITION OF" not in code   # only the helper function creates partitions


def test_rls_covers_every_user_table():
    with_user_id = {
        re.match(r"CREATE TABLE (\w+)", s).group(1)
        for s in STMTS if s.startswith("CREATE TABLE") and re.search(r"^\s+user_id\s", s, re.M)
    }
    assert with_user_id == set(m.USER_TABLES)
    joined = "\n".join(STMTS)
    for t in m.USER_TABLES + ["users"]:
        assert f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;" in joined
        assert re.search(rf"CREATE POLICY \w+ ON {t} ", joined)


def test_outcomes_daily_is_anonymous():
    stmt = next(s for s in STMTS if s.startswith("CREATE TABLE outcomes_daily"))
    assert "user_id" not in stmt


def test_c3_no_full_jd_column():
    stmt = next(s for s in STMTS if s.startswith("CREATE TABLE jobs "))
    assert "char_length(summary) <= 600" in stmt
    assert not re.search(r"\b(description|jd_text|full_jd)\b", stmt)


def test_c2_no_file_blobs():
    # Only encrypted secrets may be bytea; never PDFs/screenshots.
    for s in STMTS:
        if "bytea" in s:
            assert s.startswith("CREATE TABLE credentials"), s[:60]


def test_downgrade_drops_everything_created():
    src = PATH.read_text()
    block = src[src.index("def downgrade"):]
    for t in CREATED:
        assert f'"{t}"' in block, t
    assert "for name in ENUMS" in block


def test_enum_values_unique():
    for name, vals in m.ENUMS.items():
        assert len(vals) == len(set(vals)), name


def test_functions_revoked_from_public():
    assert "REVOKE EXECUTE ON FUNCTION jobbot_ensure_daily_partitions(text, date, date) FROM PUBLIC;" in STMTS
    assert "REVOKE EXECUTE ON FUNCTION jobbot_drop_daily_partitions(text, date) FROM PUBLIC;" in STMTS


@pytest.mark.parametrize("sql,n", [
    ("SELECT 1;", 1),
    ("SELECT 1; -- trailing\nSELECT 2;", 2),
    ("-- only a comment\n", 0),
    ("CREATE FUNCTION f() RETURNS int LANGUAGE plpgsql AS $$\nBEGIN\n  RETURN 1;\nEND $$;\nSELECT f();", 2),
])
def test_split_sql(sql, n):
    assert len(m.split_sql(sql)) == n


def test_split_sql_rejects_unterminated():
    with pytest.raises(ValueError):
        m.split_sql("CREATE FUNCTION f() AS $$\nBEGIN")
    with pytest.raises(ValueError):
        m.split_sql("SELECT 1")
