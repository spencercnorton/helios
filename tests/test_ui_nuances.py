"""Focused regressions for small but user-visible GTK presentation defects."""

from __future__ import annotations

import importlib.resources
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402


def _gtk_or_skip() -> None:
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("GTK display unavailable")


def test_effort_icon_is_portable_and_used_by_the_execution_chip() -> None:
    _gtk_or_skip()
    from helios.widgets.chat_toolbar import ChatToolbar, EFFORT_ICON_NAME

    adwaita = Gtk.IconTheme.new()
    adwaita.set_theme_name("Adwaita")
    assert adwaita.has_icon(EFFORT_ICON_NAME)

    toolbar = ChatToolbar()
    assert toolbar._effort_icon.get_icon_name() == EFFORT_ICON_NAME
    toolbar.shutdown()


def test_composer_uses_packaged_launch_icon_independent_of_host_theme() -> None:
    _gtk_or_skip()
    from helios.app import register_app_icons
    from helios.resources.icons import LAUNCH_ICON_NAME
    from helios.widgets.composer import Composer

    display = Gdk.Display.get_default()
    assert register_app_icons(display) is True
    assert Gtk.IconTheme.get_for_display(display).has_icon(LAUNCH_ICON_NAME)

    asset = importlib.resources.files("helios.resources.icons").joinpath(
        f"{LAUNCH_ICON_NAME}.svg"
    )
    svg = asset.read_text(encoding="utf-8")
    assert 'viewBox="0 0 16 16"' in svg
    assert svg.count('fill="#2e3436"') == 2

    composer = Composer()
    assert composer._send_btn.get_icon_name() == LAUNCH_ICON_NAME
    assert composer._send_btn.get_tooltip_text() == "Send message (Ctrl+Enter)"


def test_composer_command_button_emits_and_respects_read_only() -> None:
    _gtk_or_skip()
    from helios.widgets.composer import Composer

    composer = Composer()
    requested = []
    composer.connect("commands-requested", lambda *_: requested.append(True))

    composer._commands_btn.emit("clicked")
    assert requested == [True]
    assert composer._commands_btn.get_sensitive()

    composer.set_read_only(True)
    assert not composer._commands_btn.get_sensitive()
    composer.shutdown()


def test_compaction_marker_does_not_supersede_a_live_stream() -> None:
    _gtk_or_skip()
    from helios.backend.transcript import Turn
    from helios.widgets.transcript_view import TranscriptView

    transcript = TranscriptView()
    live_stream = object()
    transcript._streaming_bubble = live_stream
    boundary = Turn(role="system", is_meta=True)
    boundary.add("text", "Context compacted here.")

    transcript.append_meta_turn(boundary)

    assert transcript._streaming_bubble is live_stream
    assert transcript._rendered_turns == 1
    transcript._streaming_bubble = None
    transcript.shutdown()


def test_send_accessibility_names_send_and_queue_actions() -> None:
    from helios.widgets.composer import _set_send_accessibility

    updates = []

    class FakeButton:
        def update_property(self, properties, values):
            updates.append((properties, values))

    button = FakeButton()
    _set_send_accessibility(button, queued=False)
    _set_send_accessibility(button, queued=True)

    assert updates[0][0] == [
        Gtk.AccessibleProperty.LABEL,
        Gtk.AccessibleProperty.DESCRIPTION,
    ]
    assert updates[0][1][0] == "Send message"
    assert updates[1][1][0] == "Queue message"
    assert all("Enter" in values[1] for _properties, values in updates)


def test_acceptance_flourish_is_one_shot_and_not_a_click_effect(monkeypatch) -> None:
    _gtk_or_skip()
    from helios.app import register_app_icons
    from helios.widgets import composer as composer_module

    register_app_icons(Gdk.Display.get_default())
    composer = composer_module.Composer()
    sent = []
    composer.connect("send", lambda _composer, text: sent.append(text))
    composer.set_text("Launch")
    composer._on_send_clicked()

    assert sent == ["Launch"]
    assert not composer._send_btn.has_css_class("helios-send-accepted")

    scheduled = {}

    def schedule_once(duration, callback):
        scheduled.update(duration=duration, callback=callback)
        return 41

    monkeypatch.setattr(composer_module.GLib, "timeout_add", schedule_once)
    composer.set_busy(True)
    button_box = composer._stop_btn.get_parent()
    before_measure = button_box.measure(Gtk.Orientation.HORIZONTAL, -1)
    assert composer._send_slot.get_visible()
    assert composer._stop_btn.get_next_sibling() is composer._send_slot
    assert not composer._send_btn.get_visible()
    composer.acknowledge_accepted()

    assert scheduled["duration"] == composer_module.SEND_ACCEPTANCE_ACK_MS == 160
    assert button_box.measure(Gtk.Orientation.HORIZONTAL, -1) == before_measure
    assert composer._send_btn.get_visible()
    assert composer._send_btn.has_css_class("helios-send-accepted")
    assert scheduled["callback"]() == composer_module.GLib.SOURCE_REMOVE
    assert not composer._send_btn.get_visible()
    assert not composer._send_btn.has_css_class("helios-send-accepted")


def test_composer_shutdown_owns_pending_ack_source_idempotently(monkeypatch) -> None:
    _gtk_or_skip()
    from helios.app import register_app_icons
    from helios.widgets import composer as composer_module

    register_app_icons(Gdk.Display.get_default())
    scheduled = {}
    removed = []

    def schedule_once(_duration, callback):
        scheduled["callback"] = callback
        return 73

    monkeypatch.setattr(composer_module.GLib, "timeout_add", schedule_once)
    monkeypatch.setattr(composer_module.GLib, "source_remove", removed.append)
    composer = composer_module.Composer()
    composer.acknowledge_accepted()

    assert composer._send_acceptance_ack_id == 73
    composer.shutdown()
    composer.shutdown()

    assert removed == [73]
    assert composer._send_acceptance_ack_id == 0
    assert not composer._send_btn.has_css_class("helios-send-accepted")
    # Simulate an already-queued callback racing the removal. Shutdown makes
    # it inert and it cannot re-enter widget-state updates.
    assert scheduled["callback"]() == composer_module.GLib.SOURCE_REMOVE
    assert not composer._send_btn.has_css_class("helios-send-accepted")


def test_send_acceptance_css_has_static_reduced_motion_feedback() -> None:
    css = importlib.resources.files("helios.resources.style").joinpath(
        "helios.css"
    ).read_text(encoding="utf-8")

    assert "animation: helios-send-accepted 160ms" in css
    selector = (
        ".helios-window.reduced-motion "
        ".helios-send-button.helios-send-accepted"
    )
    reduced_rule = css.split(selector, 1)[1].split("}", 1)[0]
    assert "animation: none" in reduced_rule
    assert "box-shadow:" in reduced_rule


def test_replacement_status_icons_resolve_in_baseline_adwaita() -> None:
    adwaita = Gtk.IconTheme.new()
    adwaita.set_theme_name("Adwaita")

    for icon_name in ("object-select-symbolic", "emblem-system-symbolic"):
        assert adwaita.has_icon(icon_name), icon_name


def test_nonportable_static_icon_names_do_not_regress() -> None:
    repo = Path(__file__).resolve().parents[1]
    widget_sources = (repo / "src" / "helios" / "widgets").glob("*.py")
    offenders = {
        path.name: banned
        for path in widget_sources
        for banned in ("emblem-default-symbolic", "emblem-ok-symbolic", "system-symbolic")
        if f'"{banned}"' in path.read_text(encoding="utf-8")
    }

    assert offenders == {}


def test_context_tooltip_reports_authoritative_usage_before_tween() -> None:
    _gtk_or_skip()
    from helios.widgets.chat_toolbar import _ContextMeter

    meter = _ContextMeter()
    meter.set_usage(50, 100)

    # The visual arc may still be animating (and headless it has already
    # jumped to the end); neither may leak into the copy.
    assert meter.get_tooltip_text() == "Context: 50 / 100 tokens (50%)"
    meter.shutdown()


def test_context_empty_state_is_parented_and_swaps_to_files(tmp_path: Path) -> None:
    _gtk_or_skip()
    from helios.backend.projects import Project
    from helios.widgets.context_pane import ContextPane

    pane = ContextPane()
    assert pane._empty.get_parent() is pane._content_stack
    assert pane._content_stack.get_visible_child_name() == "empty"

    pane.set_project(Project(dirname="-repo", cwd=str(tmp_path), path=tmp_path))
    assert pane._content_stack.get_visible_child_name() == "files"
    pane.shutdown()


def test_global_context_row_uses_a_portable_system_icon(tmp_path: Path) -> None:
    _gtk_or_skip()
    from helios.backend.memory import ContextFile
    from helios.widgets.context_pane import _ContextRow

    row = _ContextRow(
        ContextFile("Global", tmp_path / "CLAUDE.md", "global-claude-md", True)
    )
    box = row.get_child()
    icon = box.get_first_child()
    assert isinstance(icon, Gtk.Image)
    assert icon.get_icon_name() == "emblem-system-symbolic"


def test_session_row_tooltip_tracks_full_title_and_cwd(tmp_path: Path) -> None:
    _gtk_or_skip()
    from helios.backend.projects import Project, Session
    from helios.backend.session_state import STATE_IDLE, SessionStatus
    from helios.widgets.session_list import _SessionRow

    project = Project(dirname="-repo", cwd="/very/long/project/path", path=tmp_path)
    session = Session(project, "session-id", tmp_path / "session.jsonl", 0, 0)
    session.title = "A very long conversation title that will be ellipsized"
    row = _SessionRow(
        session,
        SessionStatus(STATE_IDLE, 0),
        lambda _session: None,
        show_host_chip=False,
    )
    assert row.get_tooltip_text() == f"{session.title}\n{project.cwd}"

    session.title = "Renamed conversation"
    row.refresh_title()
    assert row.get_tooltip_text() == f"Renamed conversation\n{project.cwd}"


def test_read_only_rows_get_no_secondary_click_gesture(tmp_path: Path) -> None:
    """The context menu is the only route to "Stop session" and "Delete", and a
    pooled row must reach neither — it is another machine's archive over CIFS.

    That is enforced by never installing the gesture, 30 lines away from the
    menu itself, which is easy to miss when reading the menu builder alone. The
    condition inside the builder repeats the read-only term for the same reason,
    but this test pins the primary guard: no controller, no popover, no items.
    """
    _gtk_or_skip()
    from helios.backend.projects import Project, Session
    from helios.backend.session_state import STATE_WORKING, SessionStatus
    from helios.widgets.session_list import _SessionRow

    def _secondary_click_gestures(row) -> list:
        found = []
        controllers = row.observe_controllers()
        for i in range(controllers.get_n_items()):
            ctrl = controllers.get_item(i)
            if isinstance(ctrl, Gtk.GestureClick) and ctrl.get_button() == 3:
                found.append(ctrl)
        return found

    def _row(*, read_only: bool):
        project = Project(
            dirname="-repo", cwd="/repo", path=tmp_path, read_only=read_only
        )
        session = Session(project, "sid", tmp_path / "s.jsonl", 0, 0)
        return _SessionRow(
            session,
            SessionStatus(STATE_WORKING, 0),
            lambda _s: None,
            on_stop=lambda _s: None,
            show_host_chip=False,
        )

    # Working state + a live on_stop callback: everything the menu condition
    # wants. Read-only is the only thing standing between a pool row and a
    # destructive action, so assert it alone decides.
    assert _secondary_click_gestures(_row(read_only=False)), (
        "a local row must keep its right-click menu"
    )
    assert not _secondary_click_gestures(_row(read_only=True)), (
        "a read-only pool row must have no right-click gesture at all"
    )


def test_choice_accessibility_names_the_control_itself() -> None:
    from helios.widgets.question_dialog import _set_choice_accessibility

    captured = {}

    class FakeControl:
        def update_property(self, properties, values):
            captured["properties"] = properties
            captured["values"] = values

    _set_choice_accessibility(FakeControl(), "Run now", "Starts immediately.")

    assert captured["properties"] == [
        Gtk.AccessibleProperty.LABEL,
        Gtk.AccessibleProperty.DESCRIPTION,
    ]
    assert captured["values"] == ["Run now", "Starts immediately."]
