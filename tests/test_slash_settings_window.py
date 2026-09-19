"""Typed /model and /effort route through the toolbar handlers, validated."""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


def _window(driver):
    calls: list[tuple[str, str]] = []
    toasts: list[str] = []
    window = types.SimpleNamespace(
        _driver=driver,
        _chat_toolbar=object(),
        _toast=lambda message, **_kw: toasts.append(message),
        _on_toolbar_model_changed=lambda _tb, alias: calls.append(("model", alias)),
        _on_toolbar_effort_changed=lambda _tb, key: calls.append(("effort", key)),
    )
    window._known_claude_models = types.MethodType(MainWindow._known_claude_models, window)
    window._dispatch_claude_setting_command = types.MethodType(
        MainWindow._dispatch_claude_setting_command, window
    )
    return window, calls, toasts


def test_model_and_effort_route_and_validate(monkeypatch):
    driver = ClaudeCliDriver(cwd="/tmp", permission_mode="default")
    driver._cli_models = [{"value": "claude-sonnet-5", "displayName": "Sonnet 5"}]
    window, calls, toasts = _window(driver)
    monkeypatch.setattr(
        "helios.backend.model_catalog.anthropic_entries",
        lambda **_kw: [types.SimpleNamespace(id="fable"), types.SimpleNamespace(id="sonnet")],
    )

    assert window._dispatch_claude_setting_command("/model Sonnet") is True
    assert calls == [("model", "sonnet")]
    assert window._dispatch_claude_setting_command("/model claude-sonnet-5") is True
    assert calls[-1] == ("model", "claude-sonnet-5")
    assert window._dispatch_claude_setting_command("/model bogus-model") is True
    assert calls[-1] == ("model", "claude-sonnet-5"), "an unknown alias is not applied"
    assert toasts and "Unknown Claude model" in toasts[-1]

    assert window._dispatch_claude_setting_command("/effort max") is True
    assert calls[-1] == ("effort", "max")
    assert window._dispatch_claude_setting_command("/effort silly") is True
    assert calls[-1] == ("effort", "max")
    assert "Usage: /effort" in toasts[-1]

    assert window._dispatch_claude_setting_command("/compact keep it") is False, "not a setting"
    assert window._dispatch_claude_setting_command("/model") is True and "Usage: /model" in toasts[-1]


def test_a_non_claude_driver_falls_through():
    window, calls, toasts = _window(object())
    assert window._dispatch_claude_setting_command("/model sonnet") is False
    assert calls == [] and toasts == []
