"""Provider quota regressions, runnable without GTK or either CLI."""

import pytest

from helios.backend.rate_limits import claude_rate_limit, measured_percent
from helios.backend.process.codex_app_contract import rate_limit_rows


@pytest.mark.parametrize("value", [None, True, False, "25", -1, float("nan"), float("inf"), 10**1000])
def test_invalid_usage_stays_unknown(value):
    assert measured_percent(value) is None
    assert rate_limit_rows({"primary": {"usedPercent": value}})[0]["usedPercent"] is None


@pytest.mark.parametrize("fraction,percent", [(0, 0), (0.375, 37.5), (1, 100), (1.2, 100)])
def test_claude_fractional_utilization_uses_shared_percent(fraction, percent):
    native = {"rateLimitType": "seven_day", "status": "allowed", "utilization": fraction, "resetsAt": 123}
    row = claude_rate_limit(native)
    assert row["usedPercent"] == percent
    assert row["provider"] == "anthropic"
    assert row["resetsAt"] == 123
    assert "usedPercent" not in native


def test_claude_old_record_without_utilization_keeps_verdict_without_inventing_usage():
    row = claude_rate_limit({"rateLimitType": "five_hour", "status": "rejected"})
    assert row["status"] == "rejected"
    assert row["usedPercent"] is None
    assert claude_rate_limit([]) is None
    assert claude_rate_limit({}) is None


def test_explicitly_removed_codex_window_produces_a_row_removal():
    assert rate_limit_rows({"limitId": "codex", "secondary": None}) == [
        {"provider": "openai", "rateLimitType": "codex_secondary", "removed": True}
    ]
