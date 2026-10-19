"""Small-scale simulated week (the full 1000-company run is
`python -m scripts.simulate`; results in README)."""
import pytest

pytestmark = pytest.mark.integration


def test_simulated_week_stays_under_budget(module_db):
    from scripts.simulate import run
    out = run(companies=60, days=4, jobs_per_company=12, users=2, churn=0.2, apps_per_day=100)
    days = out["days"]
    assert all(d["polled"] == 60 and d["errors"] == 0 for d in days)
    assert days[0]["stored"] > 0                              # ranked jobs appear
    assert all(d["pre_skipped"] > 0 for d in days[1:])        # repeat postings skipped before normalization
    assert all(d["state"] == "ok" for d in days)
    assert out["db_mb"] < 300
    tables = {t["table"]: t for t in out["tables"]}
    assert tables["jobs"]["rows"] > 0 and tables["jobs"]["bytes_per_row"] < 3000
