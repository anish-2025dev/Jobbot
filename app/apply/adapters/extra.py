"""P5 adapters, registered after Greenhouse and Lever.

The generic LLM adapter is deliberately NOT registered here: the runner and the router build it
only when GENERIC_ADAPTER_ENABLED is on, and with the user's LLM (see runner.run_application)."""
from __future__ import annotations


def register_all(register) -> None:
    from app.apply.adapters.simple import AshbyAdapter, SmartRecruitersAdapter, WorkableAdapter
    from app.apply.adapters.workday import WorkdayAdapter
    register(AshbyAdapter())
    register(SmartRecruitersAdapter())
    register(WorkableAdapter())
    register(WorkdayAdapter())
