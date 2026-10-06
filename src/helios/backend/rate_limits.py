"""GTK-free normalization of native provider quota measurements."""

from __future__ import annotations

import copy
import math


def measured_percent(value: object, *, scale: float = 1) -> float | None:
    """Missing or invalid usage is unknown, never a measured zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        percent = float(value) * scale
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(percent) or percent < 0:
        return None
    return min(100.0, percent)


def claude_rate_limit(info: object) -> dict | None:
    """Adapt Claude's fractional utilization to the shared percentage view."""
    if not isinstance(info, dict) or not isinstance(info.get("rateLimitType"), str):
        return None
    if not info["rateLimitType"]:
        return None
    row = copy.deepcopy(info)
    row["provider"] = "anthropic"
    row["usedPercent"] = measured_percent(info.get("utilization"), scale=100)
    return row


def merge_rate_limit_snapshot(current: dict, update: dict) -> dict:
    """Merge sparse metadata while honoring explicit quota recovery/removal.

    Nullable identity/metadata fields in older servers do not erase known
    labels. An explicit null reached verdict clears a prior rejection, and
    null windows remove obsolete rows; omission leaves them untouched.
    """
    merged = copy.deepcopy(current)
    for key, value in update.items():
        if value is None:
            if key in {"rateLimitReachedType", "primary", "secondary"}:
                merged[key] = None
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_rate_limit_snapshot(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged
