"""Unit tests for Gemini CLI driver and Google environment introspection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib

from helios.backend import google_env
from helios.backend.process.gemini_driver import GeminiCliDriver


def test_find_google_binary_env_var(monkeypatch, tmp_path):
    custom_bin = tmp_path / "custom_agy"
    custom_bin.write_text("#!/bin/sh\necho ok\n")
    custom_bin.chmod(0o755)

    monkeypatch.setenv("HELIOS_GOOGLE_BINARY", str(custom_bin))
    assert google_env.find_google_binary().path == custom_bin


def test_find_google_binary_not_found(monkeypatch):
    monkeypatch.delenv("HELIOS_GOOGLE_BINARY", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(google_env.GoogleBinaryNotFound):
        google_env.find_google_binary()


def test_list_mcp_servers(tmp_path, monkeypatch):
    config_dir = tmp_path / ".gemini" / "config"
    config_dir.mkdir(parents=True)
    mcp_file = config_dir / "mcp_config.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test-server": {
                        "command": "node",
                        "args": ["server.js"],
                    }
                }
            }
        )
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    servers = google_env.list_mcp_servers()
    assert len(servers) == 1
    assert servers[0]["name"] == "test-server"
    assert servers[0]["command"] == "node"


def test_gemini_driver_initial_state(tmp_path):
    driver = GeminiCliDriver(cwd=str(tmp_path), model="gemini-2.5-pro")
    assert driver.display_name == "gemini"
    assert driver.provider == "google"
    assert driver.is_running is False
    assert driver.is_busy is False
    assert driver.is_accepting_input is False


def test_gemini_driver_message_dispatch(tmp_path):
    driver = GeminiCliDriver(cwd=str(tmp_path), model="gemini-2.5-pro")

    streaming_received = []
    driver.connect("assistant-streaming", lambda _drv, text: streaming_received.append(text))

    usage_received = []
    driver.connect("usage-updated", lambda _drv, u, w: usage_received.append((u, w)))

    # Dispatch delta
    driver._dispatch_message({"type": "delta", "text": "hello "})
    while GLib.MainContext.default().iteration(False):
        pass
    assert streaming_received == ["hello "]

    # Dispatch usage
    driver._dispatch_message({"type": "usage", "tokens_used": 150, "context_window": 2000000})
    while GLib.MainContext.default().iteration(False):
        pass
    assert usage_received == [(150, 2000000)]
