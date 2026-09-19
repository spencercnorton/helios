"""Session-load feedback: spinner, no restart on re-click, bounded reveal.

The reported symptom was "does it need a double click, or is it just taking
forever?" — which the old code manufactured: the list was held at opacity 0 with
no indicator for the whole load, and clicking again *restarted* the render.
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

from gi.repository import Adw  # noqa: E402

from helios.widgets.transcript_view import TranscriptView  # noqa: E402

Adw.init()


def _session(session_id: str, tmp_path=None):
    """Enough Session surface for set_session's banner + transcript read."""
    path = None
    if tmp_path is not None:
        path = tmp_path / f"{session_id}.jsonl"
        path.write_text("")
    return types.SimpleNamespace(
        session_id=session_id,
        path=path,
        project=types.SimpleNamespace(cwd="/tmp/proj", read_only=False),
        ensure_title=lambda: session_id,
    )


# --- no restart on re-click ----------------------------------------------


def test_reselecting_the_loading_session_does_not_restart_the_render() -> None:
    view = TranscriptView()
    view._session = _session("abc")
    view._bulk_loading = True
    token = view._render_token

    view.set_session(_session("abc"))

    assert view._render_token == token, "a re-click must not abort the in-flight render"


def test_a_different_session_while_loading_still_switches(tmp_path) -> None:
    """The no-op must be scoped to the same id, not 'ignore clicks while busy'."""

    view = TranscriptView()
    view._session = _session("abc")
    view._bulk_loading = True
    token = view._render_token

    view.set_session(_session("xyz", tmp_path))

    assert view._render_token != token


def test_reselecting_after_the_load_finished_still_re_renders(tmp_path) -> None:
    view = TranscriptView()
    view._session = _session("abc")
    view._bulk_loading = False
    token = view._render_token

    view.set_session(_session("abc", tmp_path))

    assert view._render_token != token


# --- spinner -------------------------------------------------------------


def test_the_spinner_is_shown_while_hidden_and_stopped_after() -> None:
    view = TranscriptView()

    view._set_loading(True)
    assert view._spinner.get_visible() is True
    assert "helios-loading" in view._list.get_css_classes()

    view._set_loading(False)
    # Stopped as well as hidden: an invisible spinning spinner keeps asking
    # GTK for frames.
    assert view._spinner.get_visible() is False
    assert "helios-loading" not in view._list.get_css_classes()


# --- jump-to-bottom --------------------------------------------------------


def test_set_pinned_false_shows_the_jump_button() -> None:
    view = TranscriptView()

    view._set_pinned(False)
    assert view._jump_btn.get_visible() is True

    view._set_pinned(True)
    assert view._jump_btn.get_visible() is False


def test_jump_button_stays_hidden_during_bulk_load_even_when_unpinned() -> None:
    view = TranscriptView()
    view._bulk_loading = True

    view._set_pinned(False)

    assert view._jump_btn.get_visible() is False


# --- bounded reveal ------------------------------------------------------


def test_a_never_converging_upper_still_reveals_on_the_deadline() -> None:
    """An actively streaming session keeps `upper` moving, so convergence alone
    could hold the pane blank for every one of the 12 attempts."""

    view = TranscriptView()
    view._render_token = 7
    view._landing_remaining = view._LANDING_MAX_ATTEMPTS
    view._landing_last_upper = -1.0
    view._landing_deadline = 0.0  # already expired

    moving = types.SimpleNamespace(
        _n=0.0,
        get_upper=lambda self=None: 0.0,
    )

    view._landing_step(moving, 7)

    assert view._spinner.get_visible() is False, "deadline must reveal the transcript"
    assert "helios-loading" not in view._list.get_css_classes()


def test_the_deadline_is_armed_when_the_landing_starts() -> None:
    view = TranscriptView()
    view._landing_deadline = 0.0

    view._start_landing_scroll()

    # Either armed, or revealed immediately because there is no adjustment.
    assert view._landing_deadline > 0.0 or view._spinner.get_visible() is False


def test_attempts_remain_a_second_independent_bound() -> None:
    """Deadline is additive, not a replacement — keep the attempt ceiling."""

    view = TranscriptView()
    view._render_token = 3
    view._landing_remaining = 0
    view._landing_deadline = float("inf")

    view._landing_step(types.SimpleNamespace(get_upper=lambda: 1.0), 3)

    assert view._spinner.get_visible() is False


def test_finishing_a_bulk_load_drains_what_arrived_during_it(tmp_path):
    """A record written mid-bulk-load must not have its wakeup discarded.

    `append_new_from_disk` returns 0 while `_bulk_loading` is set, and its only
    caller is a one-shot GLib timeout that re-arms on the NEXT FileMonitor
    change. Without a drain when the load finishes, a write during the load is
    lost until some later unrelated write — and if it was the turn's last
    record, until the user reselects the session.

    This is the seam, not the whole path: it pins that leaving bulk-load mode
    drains, which is the part that was missing.
    """
    view = TranscriptView()
    session = _session("bulk", tmp_path)
    view._session = session
    view._bulk_loading = True

    calls: list[str] = []
    view.append_new_from_disk = lambda: calls.append("drained") or 0
    view._start_landing_scroll = lambda: calls.append("scroll")

    view._finish_bulk_load()

    assert view._bulk_loading is False
    # Order is load-bearing twice over: the drain has to happen at all, and it
    # has to happen after the flag is cleared or append_new_from_disk returns 0
    # and the drain is a no-op that looks like a fix.
    assert calls == ["drained", "scroll"], (
        f"expected drain-then-scroll after leaving bulk-load mode, got {calls}"
    )


def test_a_restart_during_the_drain_does_not_scroll_the_obsolete_load(tmp_path):
    """The drain can re-enter set_session.

    `append_new_from_disk` calls `set_session` when it sees `tail.restarted`,
    and that starts a REPLACEMENT bulk load, setting `_bulk_loading` again.
    Control then returns here. Landing-scrolling at that point scrolls against a
    list that is still batching and clears feedback the new load depends on —
    the replacement owns its own landing scroll.
    """
    view = TranscriptView()
    view._session = _session("restarted", tmp_path)
    view._bulk_loading = True

    calls: list[str] = []

    def _drain_that_restarts():
        calls.append("drained")
        view._bulk_loading = True  # what set_session does on the restart path
        return 0

    view.append_new_from_disk = _drain_that_restarts
    view._start_landing_scroll = lambda: calls.append("scroll")

    view._finish_bulk_load()

    assert calls == ["drained"], (
        f"the obsolete load must not landing-scroll over a replacement, got {calls}"
    )


# --- provider errors / hook notices ---------------------------------------


def test_append_error_and_append_notice_do_not_clear_a_live_streaming_bubble() -> None:
    """An error or a hook notice arriving mid-stream must not erase the
    partial response it is explaining — neither calls append_turn()."""
    from helios.widgets.message_bubble import StreamingBubble

    view = TranscriptView()
    bubble = StreamingBubble()
    view._streaming_bubble = bubble

    view.append_error("provider failed")
    assert view._streaming_bubble is bubble

    view.append_notice("Hook PreToolUse · Bash blocked", "reason", "warning")
    assert view._streaming_bubble is bubble

