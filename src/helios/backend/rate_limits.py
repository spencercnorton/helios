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


def humanize_window_mins(mins: object) -> str:
    """"300" -> "5-hour usage". Empty string when the CLI sent no duration.

    `RateLimitWindow.windowDurationMins` is the only self-describing field the
    App Server gives a window; `limitId` is an opaque backend id (schema says
    nullable string, not an enum), so keying a label table off it was always
    going to render a raw key the first time OpenAI changed one.
    """

    try:
        total = int(mins)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    for size, one, many in (
        (10080, "Weekly usage", "{n}-week usage"),
        (1440, "Daily usage", "{n}-day usage"),
        (60, "Hourly usage", "{n}-hour usage"),
    ):
        if total % size == 0:
            count = total // size
            return one if count == 1 else many.format(n=count)
    return f"{total}-minute usage"


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


def muse_rate_limit_rows(usage: object) -> list[dict]:
    """Adapt Muse's ``SubscriptionUsage`` (``usage/changed``) to toolbar rows.

    MSP reports the current window and the rolling week as verbatim integer
    percentages with epoch-millisecond resets; the toolbar keys on epoch
    seconds. A block that is missing or malformed yields no row, never 0%.
    """
    if not isinstance(usage, dict):
        return []
    rows = []
    for key, mins in (("window", None), ("weekly", 10080)):
        block = usage.get(key)
        if not isinstance(block, dict):
            continue
        percent = measured_percent(block.get("usedPercent"))
        if percent is None:
            continue
        span = mins or block.get("windowDurationMins")
        resets = block.get("resetsAtMs")
        rows.append({
            "provider": "muse",
            "rateLimitType": f"muse_{key}",
            "label": humanize_window_mins(span) or f"Muse {key} usage",
            "status": "exceeded" if percent >= 100 else "allowed",
            "usedPercent": percent,
            "resetsAt": resets // 1000 if isinstance(resets, int) and not isinstance(resets, bool) and resets > 0 else None,
        })
    return rows
