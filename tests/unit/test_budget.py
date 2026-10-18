import pytest

from app.budget import budget_state


@pytest.mark.parametrize("used,state", [
    (0, "ok"), (209.9, "ok"), (210, "tight"), (269.9, "tight"), (270, "pause"), (400, "pause"),
])
def test_thresholds_at_300mb(used, state):
    # C1: 70% of 300 = 210 MB -> tight, 90% = 270 MB -> pause
    assert budget_state(used, 300, 70, 90)[1] == state


def test_percent_value():
    pct, _ = budget_state(150, 300, 70, 90)
    assert pct == pytest.approx(50.0)


def test_rejects_zero_budget():
    with pytest.raises(ValueError):
        budget_state(1, 0, 70, 90)
