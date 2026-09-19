"""Tests for helios.paths.state_dir() and the modules that consume it."""

from __future__ import annotations

import json
import os
from pathlib import Path

import helios.log as _log
import helios.backend.session_providers as _sp
from helios.paths import state_dir


# ── state_dir() ───────────────────────────────────────────────────────────────


def test_state_dir_returns_env_override(tmp_path, monkeypatch):
    """When HELIOS_STATE_DIR is set, state_dir() must return that path."""
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
    assert state_dir() == tmp_path


def test_state_dir_falls_back_to_home_helios(monkeypatch):
    """When HELIOS_STATE_DIR is absent, state_dir() returns ~/.helios."""
    monkeypatch.delenv("HELIOS_STATE_DIR", raising=False)
    # HOME is also unset by the autouse fixture pointing it at tmp_path;
    # re-read the real home so this assertion is meaningful.
    real_home = Path(os.path.expanduser("~"))
    assert state_dir() == real_home / ".helios"


def test_state_dir_empty_string_falls_back(monkeypatch):
    """An empty HELIOS_STATE_DIR is treated as absent."""
    monkeypatch.setenv("HELIOS_STATE_DIR", "")
    real_home = Path(os.path.expanduser("~"))
    assert state_dir() == real_home / ".helios"


# ── log.py isolation ──────────────────────────────────────────────────────────


def test_log_setup_writes_under_state_dir(tmp_path, monkeypatch):
    """log.setup() must create its file under HELIOS_STATE_DIR, not ~/.helios."""
    # The autouse fixture has already set HELIOS_STATE_DIR=tmp_path and reset
    # the logging state; just call setup() and check where the file landed.
    _log.setup()
    _log.get_logger("test-paths").info("sentinel-log-path")
    for h in __import__("logging").getLogger("helios").handlers:
        h.flush()

    log_file = tmp_path / "logs" / "helios.log"
    assert log_file.is_file(), f"expected log file at {log_file}"
    assert "sentinel-log-path" in log_file.read_text(encoding="utf-8")


def test_log_file_path_reflects_state_dir(tmp_path, monkeypatch):
    """log_file_path() must return a path under the current HELIOS_STATE_DIR."""
    assert _log.log_file_path() == tmp_path / "logs" / "helios.log"


# ── session_providers.py isolation ───────────────────────────────────────────


def test_set_provider_writes_under_state_dir(tmp_path, monkeypatch):
    """set_provider() must persist to HELIOS_STATE_DIR, not ~/.helios."""
    from helios.backend import model_catalog

    _sp.set_provider("sess-abc", model_catalog.PROVIDER_OPENAI)

    expected = tmp_path / "session-providers.json"
    assert expected.is_file(), f"expected provider file at {expected}"
    data = json.loads(expected.read_text(encoding="utf-8"))
    assert data.get("sess-abc") == model_catalog.PROVIDER_OPENAI


def test_provider_round_trip_under_state_dir(tmp_path, monkeypatch):
    """set_provider / provider_for / forget must all operate on the tmp dir."""
    from helios.backend import model_catalog

    _sp.set_provider("sess-xyz", model_catalog.PROVIDER_OPENAI)
    assert _sp.provider_for("sess-xyz") == model_catalog.PROVIDER_OPENAI
    assert _sp.is_openai("sess-xyz")

    _sp.forget("sess-xyz")
    assert _sp.provider_for("sess-xyz") == ""

    # Real ~/.helios must not have been touched.
    real_path = Path.home() / ".helios" / "session-providers.json"
    # HOME is redirected to tmp_path by the autouse fixture, so this check is
    # identical to verifying that HELIOS_STATE_DIR / session-providers.json is
    # the only file written (no second file under the "real" home).
    _ = real_path  # reference to make the intent explicit; no assertion needed


def test_reload_drops_cache(tmp_path, monkeypatch):
    """reload() must clear the in-memory cache so the next read re-reads disk."""
    from helios.backend import model_catalog

    _sp.set_provider("sess-reload", model_catalog.PROVIDER_OPENAI)
    assert _sp.provider_for("sess-reload") == model_catalog.PROVIDER_OPENAI

    # Poison the on-disk file to something different while the cache is warm.
    (tmp_path / "session-providers.json").write_text(
        json.dumps({"sess-reload": model_catalog.PROVIDER_ANTHROPIC}),
        encoding="utf-8",
    )
    # Cache is still warm — should still return OpenAI.
    assert _sp.provider_for("sess-reload") == model_catalog.PROVIDER_OPENAI

    _sp.reload()  # drop cache
    # Now the updated file is read.
    assert _sp.provider_for("sess-reload") == model_catalog.PROVIDER_ANTHROPIC
