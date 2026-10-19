import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"migrations/versions/{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


m2 = _load("0002_outreach_and_engine")
STMTS = m2.all_statements()


def test_new_tables_created_with_rls():
    created = {re.match(r"CREATE TABLE (\w+)", s).group(1) for s in STMTS if s.startswith("CREATE TABLE")}
    assert created == set(m2.NEW_USER_TABLES)
    joined = "\n".join(STMTS)
    for t in m2.NEW_USER_TABLES:
        assert f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;" in joined


def test_old_referral_tables_dropped():
    assert "DROP TABLE referral_requests;" in STMTS
    assert "DROP TABLE referral_contacts;" in STMTS


def test_no_percent_signs():
    for s in STMTS + m2.downgrade_statements():
        assert "%" not in s


def test_dnc_stores_no_address():
    stmt = next(s for s in STMTS if s.startswith("CREATE TABLE do_not_contact"))
    assert "email_hash" in stmt and not re.search(r"\bemail\s+text", stmt)


def test_outreach_contacts_privacy_fields_only():
    stmt = next(s for s in STMTS if s.startswith("CREATE TABLE outreach_contacts"))
    cols = {l.split()[0] for l in stmt.splitlines()[1:] if l.startswith("    ") and not l.strip().startswith(("UNIQUE", "PRIMARY"))}
    # section 10.5: name, title, company, verified email, provider, last contact (+ bookkeeping)
    assert cols <= {"id", "user_id", "company_key_hash", "name", "title", "role_type", "location",
                    "email", "provider", "verified_at", "last_contacted_at", "replied_positive", "created_at"}
