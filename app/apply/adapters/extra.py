"""P5 adapters, registered after Greenhouse and Lever."""
from __future__ import annotations


def register_all(register) -> None:
    from app.apply.adapters.generic import GenericAdapter
    from app.apply.adapters.simple import AshbyAdapter, SmartRecruitersAdapter, WorkableAdapter
    from app.apply.adapters.workday import WorkdayAdapter
    register(AshbyAdapter())
    register(SmartRecruitersAdapter())
    register(WorkableAdapter())
    register(WorkdayAdapter())
    register(GenericAdapter())
