"""Dollar accounting for interactive OpenRouter Work requests (GTK-free)."""

from __future__ import annotations

import math

WORK_COST_LIMIT_MICRO_USD = 5_000_000


class SpendLimitReached(Exception):
    """A request's reservation would exceed the Work's remaining dollars."""


def micro_usd(value: float) -> int:
    """Round charges/reservations upwards; invalid prices never mean free."""
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError("OpenRouter returned an invalid dollar amount")
    return math.ceil(value * 1_000_000)
