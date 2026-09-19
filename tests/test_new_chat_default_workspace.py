"""The configured default workspace owns New chat, not the last selection.

selecting a session re-points `_next_chat`, and `_start_new_chat` used
to prefer that staged target. So after clicking any session, Ctrl+N / the
header button / the provider menu items all opened a chat in *that* session's
folder and silently ignored Settings -> Defaults.
"""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios import main_window as main_window_module  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


class _Project:
    def __init__(self, cwd: str, *, read_only: bool = False) -> None:
        self.cwd = cwd
        self.read_only = read_only


class _Composer:
    def grab_input_focus(self) -> None:
        pass


def _window(staged: _Project | None):
    shown: list[_Project] = []
    window = types.SimpleNamespace(
        _next_chat=(
            types.SimpleNamespace(project=staged) if staged is not None else None
        ),
        _composer=_Composer(),
        _show_fresh_chat_ui=shown.append,
        _toast=lambda _msg: None,
    )
    return window, shown


def _patch_projects(monkeypatch, *, configured: str, newest: str | None) -> None:
    monkeypatch.setattr(
        main_window_module, "configured_default_cwd", lambda: configured
    )
    monkeypatch.setattr(main_window_module, "default_cwd", lambda: configured or "/home")
    monkeypatch.setattr(main_window_module, "ensure_local_project", _Project)
    monkeypatch.setattr(
        main_window_module,
        "_default_chat_project",
        lambda: _Project(newest) if newest else None,
    )


def test_configured_default_beats_the_selected_session(monkeypatch) -> None:
    """The whole point: a selection must not leak into New chat."""

    _patch_projects(monkeypatch, configured="/home/alice/helios", newest="/newest")
    window, shown = _window(_Project("/some/other/project"))

    MainWindow._start_new_chat(window)

    assert [p.cwd for p in shown] == ["/home/alice/helios"]


def test_without_a_configured_default_the_staged_target_still_wins(
    monkeypatch,
) -> None:
    """Unset must NOT resolve to $HOME here — that was the v0.58/v0.59 regression."""

    _patch_projects(monkeypatch, configured="", newest="/newest")
    window, shown = _window(_Project("/some/other/project"))

    MainWindow._start_new_chat(window)

    assert [p.cwd for p in shown] == ["/some/other/project"]


def test_unset_default_with_a_read_only_target_falls_back_to_newest(
    monkeypatch,
) -> None:
    _patch_projects(monkeypatch, configured="", newest="/newest")
    window, shown = _window(_Project("/remote/pool", read_only=True))

    MainWindow._start_new_chat(window)

    assert [p.cwd for p in shown] == ["/newest"]


def test_an_unusable_default_toasts_instead_of_opening_a_chat(monkeypatch) -> None:
    _patch_projects(monkeypatch, configured="/gone", newest="/newest")
    window, shown = _window(_Project("/some/other/project"))
    toasts: list[str] = []
    window._toast = toasts.append

    def _boom(_cwd):
        raise OSError("stale mount")

    monkeypatch.setattr(main_window_module, "ensure_local_project", _boom)

    MainWindow._start_new_chat(window)

    assert shown == []
    assert toasts and "stale mount" in toasts[0]
