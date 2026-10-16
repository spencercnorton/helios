"""Usage stats reach the toolbar for every provider and survive a switch."""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.main_window import MainWindow  # noqa: E402
from helios.widgets.chat_toolbar import usage_caption  # noqa: E402


def test_caption_shows_fill_and_the_tightest_limit():
    limits = {
        "five_hour": {"usedPercent": 55, "status": "allowed"},
        "seven_day": {"usedPercent": 30, "status": "allowed"},
        "codex_primary": {"label": "ChatGPT Pro · 5-hour usage", "usedPercent": 12},
    }
    assert usage_caption(84_200, 200_000, limits) == "84.2k / 200k · 5-hour 55%"
    # A window the provider never published is not guessed.
    assert usage_caption(13_096, 0, {}) == "13.1k used"
    # A reached limit outranks any percentage, even one without a number.
    limits["openrouter"] = {"status": "blocked"}
    assert usage_caption(0, 200_000, limits) == "OpenRouter rate limit reached"
    assert usage_caption(0, 200_000, {}) == ""


def test_background_usage_is_remembered_for_the_switch_back():
    shown = []
    window = types.SimpleNamespace(
        _destroyed=False,
        _drv_is_current=lambda _drv: False,
        _chat_toolbar=types.SimpleNamespace(set_context_usage=lambda *a: shown.append(a)),
    )
    drv = types.SimpleNamespace()
    MainWindow._on_usage_updated(window, drv, 42_000, 256_000)
    assert shown == []
    assert drv._helios_last_usage == (42_000, 256_000)


def test_account_limits_from_a_background_session_still_reach_the_popover():
    rows = []
    window = types.SimpleNamespace(
        _destroyed=False,
        _drv_is_current=lambda _drv: False,
        _chat_toolbar=types.SimpleNamespace(update_rate_limit=rows.append),
    )
    row = {"provider": "muse", "rateLimitType": "muse_window", "usedPercent": 10.0}
    MainWindow._on_rate_limit_updated(window, object(), row)
    assert rows == [row]


def test_popover_keeps_each_providers_rows_across_a_switch():
    from gi.repository import Gtk

    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    from helios.widgets.chat_toolbar import _ContextPopover

    popover = _ContextPopover()
    popover.set_model("gpt-5.5")
    popover.update_rate_limit({"provider": "openai", "rateLimitType": "codex_primary", "usedPercent": 20})
    popover.set_model("muse-wonder-1.0")
    assert popover._rate_limits == {}
    # Arrives while another provider is shown: filed, not displayed, not lost.
    popover.update_rate_limit({"provider": "openai", "rateLimitType": "codex_secondary", "usedPercent": 5})
    assert popover._rate_limits == {}
    popover.set_model("gpt-5.5")
    assert set(popover._rate_limits) == {"codex_primary", "codex_secondary"}
