"""What each plan allows (brief §8.2, §20.7). Limits are read from here, never hard-coded.

The numbers are placeholders until there is real usage (brief §7.2, D7, ADR 0017).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Entitlements:
    projects: int
    retention_days: int
    monthly_calls: int  # calls measured: the sum of the summaries' `count` (brief §19.4)
    # Protective limit per ingest key, counted in records (summaries plus samples).
    records_per_s: float
    records_burst: int


FREE = Entitlements(
    projects=1,
    retention_days=7,
    monthly_calls=1_000_000,
    records_per_s=50,
    records_burst=3_000,
)

_PLANS = {"free": FREE}


def entitlements(plan: str) -> Entitlements:
    """An unknown plan gets the free plan's limits."""
    return _PLANS.get(plan, FREE)
