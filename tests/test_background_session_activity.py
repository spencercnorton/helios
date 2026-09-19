"""Background sessions stop being dark.

`_on_assistant_streaming` and `_on_native_activity_updated` both began with
`if self._destroyed or not self._drv_is_current(drv): return`, so every signal a
NON-visible driver emitted was discarded. A session running in another chat was
therefore an 8px sidebar dot and a Gio.Notification when it finished — nothing
between "it exists" and "it's done", and no way to tell a session reading a file
from one that had wedged without switching to it and losing your place.

The row now carries the same one-line summary the activity strip uses. It is
deliberately the *background* view: the visible session already has the strip,
so its row is cleared the moment it is promoted.
"""

from __future__ import annotations

import json
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:  # pragma: no cover - host without the GTK4/Adw typelibs
    pytest.skip("Gtk 4.0 / Adw 1 unavailable", allow_module_level=True)

from gi.repository import Adw  # noqa: E402

from helios.backend.projects import Project, Session  # noqa: E402
from helios.widgets import session_list as SL  # noqa: E402
from helios.widgets.activity_indicator import (  # noqa: E402
    STATE_BASH,
    STATE_IDLE,
    STATE_READING,
    STATE_RETRYING,
    STATE_THINKING,
    activity_summary,
)

Adw.init()


def _session(tmp_path, session_id: str):
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"sessionId": session_id, "type": "assistant"}) + "\n",
        encoding="utf-8",
    )
    project = Project(
        dirname="-home-alice-proj",
        cwd="/home/alice/proj",
        path=tmp_path,
        origin="local",
        read_only=False,
    )
    return Session(
        project=project,
        session_id=session_id,
        path=path,
        mtime=time.time(),
        size=path.stat().st_size,
    )


def _row(tmp_path, session_id="bg"):
    return SL._SessionRow(
        _session(tmp_path, session_id),
        SL.SessionStatus(state=SL.STATE_WORKING, last_response_at=0.0),
        on_delete=lambda _s: None,
    )


# ── the wording is shared with the strip ───────────────────────────────────


def test_summary_uses_the_strip_s_own_vocabulary() -> None:
    assert activity_summary(STATE_READING, "main_window.py") == "Reading main_window.py"
    assert activity_summary(STATE_BASH, "pytest -q") == "Running pytest -q"
    assert (
        activity_summary(STATE_RETRYING, "attempt 2 of 5 · HTTP 529")
        == "Retrying attempt 2 of 5 · HTTP 529"
    )


def test_thinking_is_the_fixed_word_not_a_cycling_verb() -> None:
    """The strip rotates Thinking/Reasoning/Considering because you watch it.
    A sidebar row is glanced at; wording that changes on its own is noise."""

    assert activity_summary(STATE_THINKING) == "Thinking"
    assert activity_summary(STATE_THINKING, "") == "Thinking"


def test_idle_and_empty_render_nothing_rather_than_a_state_name() -> None:
    assert activity_summary(STATE_IDLE, "anything") == ""
    assert activity_summary("", "anything") == ""


def test_a_long_detail_is_shortened_for_the_narrow_row() -> None:
    summary = activity_summary(STATE_BASH, "x" * 200)
    assert len(summary) < 60
    assert summary.startswith("Running ")


# ── the row shows it, and takes it back ────────────────────────────────────


def test_a_fresh_row_shows_no_activity_line(tmp_path) -> None:
    row = _row(tmp_path)
    assert row._activity.get_text() == ""
    assert not row._activity.get_visible()


def test_setting_and_clearing_activity_shows_and_hides_the_line(tmp_path) -> None:
    row = _row(tmp_path)
    row.set_live_activity("Reading main_window.py")
    assert row._activity.get_visible()
    assert row._activity.get_text() == "Reading main_window.py"

    row.set_live_activity("")
    assert not row._activity.get_visible()
    assert row._activity.get_text() == ""


# ── the list routes it without re-rendering ────────────────────────────────


def test_the_list_writes_through_to_the_row(tmp_path) -> None:
    sidebar = SL.SessionList()
    try:
        row = _row(tmp_path, "sid-1")
        sidebar._rows_by_id["sid-1"] = row
        renders = []
        sidebar._render = lambda **kw: renders.append(kw)

        sidebar.set_live_activity("sid-1", "Editing helios.css")
        assert row._activity.get_text() == "Editing helios.css"
        # A full rebuild here would fire on every stream delta of every running
        # session and make the sidebar unusable.
        assert renders == []
    finally:
        sidebar.shutdown()


def test_activity_for_an_unrendered_session_is_kept_for_its_row(tmp_path) -> None:
    """A background session can be filtered out of the current view; its text
    must not be lost, or the row would appear blank when the filter changes."""

    sidebar = SL.SessionList()
    try:
        sidebar._render = lambda **kw: None
        sidebar.set_live_activity("not-rendered", "Running make test")
        assert sidebar._live_activity["not-rendered"] == "Running make test"

        sidebar.set_live_activity("not-rendered", "")
        assert "not-rendered" not in sidebar._live_activity
    finally:
        sidebar.shutdown()


def test_an_empty_session_id_is_ignored(tmp_path) -> None:
    """A driver that has not had its system/init yet has no row to write to."""

    sidebar = SL.SessionList()
    try:
        sidebar._render = lambda **kw: None
        sidebar.set_live_activity("", "Thinking")
        assert sidebar._live_activity == {}
    finally:
        sidebar.shutdown()


# ── the window routes a NON-visible driver's signals there ─────────────────


import functools  # noqa: E402
import types  # noqa: E402

from helios.backend.process.streaming import StreamingAssistant  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


class _Sessions:
    def __init__(self) -> None:
        self.pushed: list[tuple[str, str]] = []

    def set_live_activity(self, session_id, text):
        self.pushed.append((session_id, text))


def _fake_window(visible=None):
    sessions = _Sessions()
    window = types.SimpleNamespace(
        _destroyed=False,
        _drv_is_current=lambda drv: drv is visible,
        _sessions=sessions,
        _driver_manager=types.SimpleNamespace(current=visible),
        _activity=types.SimpleNamespace(set_activity=lambda *a: None),
    )
    # The helpers under test are real MainWindow methods; bind them so these
    # exercise the shipped routing rather than a stand-in.
    for name in (
        "_push_background_activity",
        "_remember_activity",
        "_set_row_activity",
    ):
        method = getattr(MainWindow, name)
        setattr(window, name, functools.partial(method, window))
    return window, sessions


def _bg_driver(session_id="bg-1"):
    return types.SimpleNamespace(session_id=session_id)


def test_a_background_driver_s_stream_lands_on_its_row() -> None:
    window, sessions = _fake_window(visible=object())
    drv = _bg_driver()
    streaming = StreamingAssistant(model="claude-opus-5")
    streaming.apply_stream_event(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Read"},
        }
    )
    streaming.apply_stream_event(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"file_path": "/repo/main_window.py"}',
            },
        }
    )

    MainWindow._on_assistant_streaming(window, drv, streaming)

    assert sessions.pushed == [("bg-1", "Reading /repo/main_window.py")]


def test_a_background_driver_s_native_activity_lands_on_its_row() -> None:
    """The Codex/Claude `activity-updated` lane, not the stream lane."""

    window, sessions = _fake_window(visible=object())
    drv = _bg_driver("bg-2")

    MainWindow._on_native_activity_updated(
        window, drv, {"category": "command", "command": "pytest -q"}
    )

    assert sessions.pushed == [("bg-2", "Running pytest -q")]


def test_a_finished_background_turn_clears_the_row() -> None:
    """Otherwise a row freezes on whatever tool it happened to end on."""

    window, sessions = _fake_window(visible=object())
    drv = _bg_driver("bg-3")
    drv.queued_messages = lambda: []
    notified: list[object] = []
    window._notify_session_finished = notified.append

    MainWindow._on_turn_result(window, drv, {"is_error": False})

    assert sessions.pushed == [("bg-3", "")]
    assert notified == [drv]


def test_a_driver_with_no_session_id_yet_writes_to_no_row() -> None:
    window, sessions = _fake_window(visible=object())
    MainWindow._on_native_activity_updated(
        window, _bg_driver(""), {"category": "command", "command": "ls"}
    )
    assert sessions.pushed == []


def test_promotion_to_visible_clears_the_row(tmp_path) -> None:
    """The visible session's live state belongs to the activity strip; leaving
    the row's copy behind would strand it on whatever it last did in the
    background."""

    window, sessions = _fake_window()
    MainWindow._driver.fset(window, _bg_driver("promoted"))

    assert sessions.pushed == [("promoted", "")]
    assert window._driver_manager.current.session_id == "promoted"


def test_switching_away_hands_the_demoted_row_its_caption() -> None:
    """The demotion half. A `make test` that runs for two minutes emits
    nothing between the switch and its completion, so a row that waited for
    the next event would stay blank for the whole command."""

    working = _bg_driver("working")
    window, sessions = _fake_window(visible=working)

    MainWindow._on_native_activity_updated(
        window, working, {"category": "command", "command": "make test"}
    )
    assert sessions.pushed == [], "the visible session speaks through the strip"

    MainWindow._driver.fset(window, _bg_driver("other"))

    assert sessions.pushed == [("other", ""), ("working", "Running make test")]


def test_a_demoted_driver_that_was_idle_leaves_its_row_empty() -> None:
    window, sessions = _fake_window(visible=_bg_driver("quiet"))
    MainWindow._driver.fset(window, _bg_driver("other"))
    assert sessions.pushed == [("other", ""), ("quiet", "")]


def test_dropping_the_driver_on_teardown_touches_no_row() -> None:
    window, sessions = _fake_window(visible=object())
    MainWindow._driver.fset(window, None)
    assert sessions.pushed == []
