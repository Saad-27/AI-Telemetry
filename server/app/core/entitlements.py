"""What each plan allows (brief §8.2, §20.7). Limits are read from here, never hard-coded.

The numbers are placeholders until there is real usage (brief §7.2, D7). They are sized so
free accounts fit a 0.25 CU Neon database (ADR 0017).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Entitlements:
    projects: int
    retention_days: int
    monthly_calls: int  # calls measured: the sum of the summaries' `count` (brief §19.4)
    # Protective limits per account, counted in records stored (summaries plus samples). The
    # SDK sends at most two records per call, so the cap is reached only by abuse.
    monthly_records: int
    records_per_s: float
    records_burst: int


FREE = Entitlements(
    projects=1,
    retention_days=7,
    monthly_calls=1_000_000,
    monthly_records=2_000_000,
    records_per_s=20,
    records_burst=3_000,
)

_PLANS = {"free": FREE}


def entitlements(plan: str) -> Entitlements:
    """An unknown plan gets the free plan's limits."""
    return _PLANS.get(plan, FREE)
