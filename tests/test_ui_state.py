"""Tests for the persistent UI/layout state store."""
from __future__ import annotations

import json
from pathlib import Path

from helios.backend import ui_state
from helios.backend.ui_state import UiStateStore


def test_set_get_roundtrip(tmp_path):
    p = tmp_path / "ui.json"
    s = UiStateStore(p)
    s.set("panel_projects", False)
    s.set("window_width", 1600)
    # Reload from disk -> values persisted.
    s2 = UiStateStore(p)
    assert s2.get("panel_projects") is False
    assert s2.get("window_width") == 1600


def test_default_for_missing_key(tmp_path):
    s = UiStateStore(tmp_path / "ui.json")
    assert s.get("nope", 42) == 42
    assert s.get("nope") is None


def test_update_writes_once(tmp_path):
    p = tmp_path / "ui.json"
    s = UiStateStore(p)
    s.update(a=1, b=2, c=3)
    assert json.loads(p.read_text()) == {"a": 1, "b": 2, "c": 3}


def test_unchanged_value_does_not_rewrite(tmp_path):
    p = tmp_path / "ui.json"
    s = UiStateStore(p)
    s.set("k", "v")
    mtime1 = p.stat().st_mtime_ns
    s.set("k", "v")  # same value -> no write
    assert p.stat().st_mtime_ns == mtime1


def test_file_permissions_are_owner_only(tmp_path):
    p = tmp_path / "ui.json"
    UiStateStore(p).set("k", "v")
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_corrupt_file_resets_gracefully(tmp_path):
    p = tmp_path / "ui.json"
    p.write_text("{ not valid json", encoding="utf-8")
    s = UiStateStore(p)  # must not raise
    assert s.get("anything", "default") == "default"


def test_save_oserror_does_not_propagate(tmp_path, monkeypatch):
    """A failing write (disk full / read-only ~/.helios, e.g. the iCloud-overlay
    wedge) must not let an OSError escape set() into a GTK signal handler."""
    p = tmp_path / "ui.json"
    s = UiStateStore(p)

    def boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    s.set("k", "v")  # must swallow the OSError, not propagate
    assert not p.is_file()  # nothing written; existing file (none) untouched


def _isolated_store(tmp_path, monkeypatch):
    """Point the module singleton at a temp file so default_cwd() reads it."""
    monkeypatch.setattr(ui_state, "_global", UiStateStore(tmp_path / "ui.json"))
    return ui_state.store()


def test_default_cwd_falls_back_to_home_when_unset(tmp_path, monkeypatch):
    _isolated_store(tmp_path, monkeypatch)
    assert ui_state.default_cwd() == str(Path.home())


def test_default_cwd_returns_configured_directory(tmp_path, monkeypatch):
    s = _isolated_store(tmp_path, monkeypatch)
    workspace = tmp_path / "srv" / "helios"
    workspace.mkdir(parents=True)
    s.set(ui_state.DEFAULT_CWD_KEY, str(workspace))
    assert ui_state.default_cwd() == str(workspace)


def test_default_cwd_falls_back_when_directory_is_gone(tmp_path, monkeypatch):
    """An unmounted or deleted workspace must not stop Helios from opening.

    Falling back to $HOME re-arms the read-only clamp, which makes the degraded
    state visible instead of granting write access somewhere unintended.
    """
    s = _isolated_store(tmp_path, monkeypatch)
    s.set(ui_state.DEFAULT_CWD_KEY, str(tmp_path / "never-created"))
    assert ui_state.default_cwd() == str(Path.home())

    # A file where a directory is expected is the same kind of misconfiguration.
    not_a_dir = tmp_path / "regular-file"
    not_a_dir.write_text("", encoding="utf-8")
    s.set(ui_state.DEFAULT_CWD_KEY, str(not_a_dir))
    assert ui_state.default_cwd() == str(Path.home())


def test_default_cwd_ignores_blank_and_whitespace(tmp_path, monkeypatch):
    s = _isolated_store(tmp_path, monkeypatch)
    for blank in ("", "   ", None):
        s.set(ui_state.DEFAULT_CWD_KEY, blank)
        assert ui_state.default_cwd() == str(Path.home())
