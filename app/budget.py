"""C1 storage-budget state, kept pure so it is unit-testable."""
from typing import Literal

State = Literal["ok", "tight", "pause"]


def budget_state(used_mb: float, budget_mb: int, warn_pct: int, pause_pct: int) -> tuple[float, State]:
    """Return (percent used, state). tight -> shorten jobs retention to 3 days;
    pause -> stop ingestion and alert."""
    if budget_mb <= 0:
        raise ValueError("budget_mb must be positive")
    pct = 100.0 * used_mb / budget_mb
    if pct >= pause_pct:
        return pct, "pause"
    if pct >= warn_pct:
        return pct, "tight"
    return pct, "ok"
