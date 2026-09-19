"""Real GTK widgets with deterministic worker delivery and window hooks."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, GLib, Gtk

from helios.backend.git_changes import ChangedFile, ChangesSnapshot
from helios.widgets import changes_pane

Adw.init()


class HeldRunner:
    def __init__(self, **kwargs):
        self.requests = []
        self.closed = False

    def submit(self, request):
        self.requests.append(request)

    def shutdown(self, **kwargs):
        self.closed = True


@pytest.fixture
def pane(monkeypatch):
    monkeypatch.setattr(changes_pane, "LatestTaskRunner", HeldRunner)
    widget = changes_pane.ChangesPane()
    yield widget
    widget.close()


def project(cwd, *, remote=False):
    return SimpleNamespace(cwd=cwd, read_only=remote)


def text(pane):
    return pane._buffer.get_text(pane._buffer.get_start_iter(), pane._buffer.get_end_iter(), True)


def test_selected_project_and_file_fence_all_late_results(pane):
    pane.set_project(project("/old"))
    old_request = pane._status_runner.requests[-1]
    pane.set_project(project("/new"))
    new_request = pane._status_runner.requests[-1]
    pane._apply_status(old_request, ChangesSnapshot("/old", (ChangedFile("wrong", " M"),)))
    assert pane._files.get_row_at_index(0) is None
    snapshot = ChangesSnapshot("/new", (ChangedFile("first", " M"), ChangedFile("second", "??")))
    pane._apply_status(new_request, snapshot)
    first_request = pane._diff_runner.requests[-1]
    pane._files.select_row(pane._files.get_row_at_index(1))
    second_request = pane._diff_runner.requests[-1]
    pane._apply_diff(first_request, "STALE FIRST FILE")
    assert "STALE" not in text(pane)
    pane._apply_diff(second_request, "selected second file")
    assert text(pane) == "selected second file"
    pane.refresh()
    pane._apply_diff(second_request, "STALE BEFORE REFRESH")
    assert "STALE" not in text(pane)
    pane._apply_status(pane._status_runner.requests[-1], snapshot)
    assert pane._selected_path == "second"
    latest = pane._diff_runner.requests[-1]
    pane.close()
    pane._apply_diff(latest, "STALE AFTER CLOSE")
    assert "STALE" not in text(pane)
    assert pane._status_runner.closed and pane._diff_runner.closed


def test_refresh_button_empty_error_and_remote_states(pane):
    assert not pane._refresh.get_sensitive()
    pane.set_project(project("/repo"))
    request = pane._status_runner.requests[-1]
    pane._apply_status(request, ChangesSnapshot("/repo", ()))
    assert pane._status.get_label() == "Working tree is clean."
    assert not pane._diff_scroll.get_visible()
    pane._refresh.emit("clicked")
    assert pane._status_runner.requests[-1] != request
    previous = pane._status_runner.requests[-1]
    pane.set_project(project("/repo"))
    assert pane._status_runner.requests[-1] != previous
    pane._apply_status(pane._status_runner.requests[-1], RuntimeError("fatal: not a git repository"))
    assert pane._status.get_label() == "This folder is not a Git repository."
    pane.set_project(project("/remote", remote=True))
    assert pane.cwd == "" and not pane._refresh.get_sensitive()
    assert not pane._view.get_editable()


def test_untracked_previews_use_file_language_and_tracked_files_keep_diff(pane):
    pane.set_project(project("/repo"))
    snapshot = ChangesSnapshot("/repo", (
        ChangedFile("acceptance.md", "??"),
        ChangedFile("new_script-\udcff.py", "??"),
        ChangedFile("unknown.helios_test_unknown", "??"),
        ChangedFile("tracked.md", " M"),
    ))
    pane._apply_status(pane._status_runner.requests[-1], snapshot)
    assert pane._buffer.get_language().get_id() == "markdown"
    markdown_request = pane._diff_runner.requests[-1]
    pane._apply_diff(markdown_request, "Untracked file\n\n- Acceptance criterion")
    assert "preview" in pane._view.get_tooltip_text()

    pane._files.select_row(pane._files.get_row_at_index(1))
    assert pane._buffer.get_language().get_id() in {"python", "python3"}
    pane._apply_diff(markdown_request, "STALE MARKDOWN")
    assert "STALE" not in text(pane)
    assert pane._buffer.get_language().get_id() in {"python", "python3"}

    pane._files.select_row(pane._files.get_row_at_index(2))
    assert pane._buffer.get_language() is None  # unknown type stays plain text
    pane._files.select_row(pane._files.get_row_at_index(3))
    assert pane._buffer.get_language().get_id() == "diff"
    assert "diff" in pane._view.get_tooltip_text()


def test_git_work_runs_off_gtk_and_returns_through_main_context(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    caller = threading.get_ident()
    thread_ids = []

    def work(cwd):
        thread_ids.append(threading.get_ident())
        entered.set()
        release.wait(3)
        return ChangesSnapshot(cwd, ())

    monkeypatch.setattr(changes_pane, "read_changes", work)
    widget = changes_pane.ChangesPane()
    try:
        widget.set_project(project("/repo"))
        assert entered.wait(1)
        assert thread_ids == [thread_ids[0]] and thread_ids[0] != caller
        assert widget._status.get_label() == "Refreshing changes…"
        release.set()
        deadline = time.monotonic() + 2
        context = GLib.MainContext.default()
        while widget._status.get_label() == "Refreshing changes…" and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)
        assert widget._status.get_label() == "Working tree is clean."
    finally:
        release.set()
        widget.close()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openrouter"])
def test_window_completion_refreshes_matching_workspace_for_every_provider(provider):
    from helios.main_window import MainWindow

    refreshed = []
    window = SimpleNamespace(
        _destroyed=False,
        _changes=SimpleNamespace(cwd="/repo", refresh=lambda: refreshed.append(True)),
        _drv_is_current=lambda drv: False,
        _push_background_activity=lambda *args: None,
        _notify_session_finished=lambda *args: None,
    )
    driver = SimpleNamespace(provider=provider, _cwd="/other", queued_messages=lambda: [])
    MainWindow._on_turn_result(window, driver, {})
    assert refreshed == []
    driver._cwd = "/repo"
    MainWindow._on_turn_result(window, driver, {})
    assert refreshed == [True]
    window._destroyed = True
    MainWindow._on_turn_result(window, driver, {})
    assert refreshed == [True]


def test_compact_layout_restores_sessions_without_saving_automatic_toggle():
    from helios.main_window import MainWindow

    saved = []
    window = SimpleNamespace(
        _sessions_btn=Gtk.ToggleButton(active=True),
        _sessions_column=Gtk.Box(),
        _sessions=Gtk.Box(),
        _compact_new_chat=Gtk.Button(),
        _outer=Adw.OverlaySplitView(),
        _ui_state=SimpleNamespace(set=lambda *args: saved.append(args)),
        _wide_sessions_visible=True,
    )
    window._sessions_btn.connect(
        "toggled", lambda btn: MainWindow._on_panel_toggle_column(window, btn, "panel_sessions")
    )
    MainWindow._set_compact_layout(window, True)
    assert not window._sessions_column.get_visible()
    assert not window._sessions.get_visible()
    assert window._compact_new_chat.get_visible()
    assert window._outer.get_collapsed()
    assert saved == []
    MainWindow._set_compact_layout(window, False)
    assert window._sessions_column.get_visible()
    assert window._sessions.get_visible()
    assert not window._compact_new_chat.get_visible()
    assert saved == []


def test_overlay_dismissal_updates_the_toggle():
    from helios.main_window import MainWindow

    toggle = Gtk.ToggleButton(active=True)
    window = SimpleNamespace(_context_btn=toggle, _layout_auto_change=False)
    split = Adw.OverlaySplitView()
    split.set_show_sidebar(False)
    MainWindow._on_right_visibility_changed(window, split, None)
    assert not toggle.get_active()
