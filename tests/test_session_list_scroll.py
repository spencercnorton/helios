"""Sidebar scroll offset survives a preserve_selection rebuild.

Every completed turn calls `reload(preserve_selection=True, rescan_pool=False)`,
which rebuilds the whole listbox, and the sidebar teleported to the top.

NOT because the vadjustment is reconfigured to a zero upper -- that was the
plan's theory and it is wrong. Measured on GTK 4.22: `upper` is 1140.0 before
and after the rebuild and `changed` never fires on it. The real mechanism is in
the source comment at session_list.py:504: emptying the listbox destroys the
focused row, GTK moves focus to the top, and the viewport's scroll-to-focus
animates the sidebar up there. That distinction matters -- a restore driven off
the `changed` signal, which the wrong theory implies, would be a guaranteed
no-op.

These assert the *decision* (what gets captured, what gets restored), not
pixels: an unrealized ListBox has no meaningful allocation, so pinning a
restored offset here would be a test that lies.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:  # pragma: no cover - host without the GTK4/Adw typelibs
    pytest.skip("Gtk 4.0 / Adw 1 unavailable", allow_module_level=True)

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from helios.widgets.session_list import SessionList  # noqa: E402

Adw.init()


@pytest.fixture()
def sidebar():
    """A SessionList with no sessions.

    __init__ arms a 2s status timer and connects a TitleGenerator; shutdown()
    is what stops both, and leaving them live on a finalizing widget is the
    exact shape of the intermittent PyGObject teardown crash the gtk_tests lane
    repeats a slice to catch.
    """
    widget = SessionList()
    widget._local_sessions = []
    widget._pool_sessions = []
    try:
        yield widget
    finally:
        widget.shutdown()


def _scrolled(sidebar, value: float) -> Gtk.Adjustment:
    """Force a scrolled-down adjustment; set_value alone clamps to 0."""
    adj = sidebar._scroller.get_vadjustment()
    adj.configure(value, 0.0, 1000.0, 10.0, 100.0, 200.0)
    assert adj.get_value() == value
    return adj


def test_preserve_selection_render_captures_the_offset(sidebar) -> None:
    _scrolled(sidebar, 120.0)

    sidebar._render(preserve_selection=True)

    assert sidebar._pending_scroll == 120.0


def test_a_second_render_in_the_same_frame_keeps_the_first_reading(sidebar) -> None:
    """reload() and set_live_session_ids() both land at a turn boundary.

    The second rebuild would otherwise capture the already-collapsed value and
    make the restore a no-op.
    """
    _scrolled(sidebar, 120.0)
    sidebar._render(preserve_selection=True)

    _scrolled(sidebar, 500.0)
    sidebar._render(preserve_selection=True)

    assert sidebar._pending_scroll == 120.0


def test_a_fresh_list_is_allowed_to_sit_at_the_top(sidebar) -> None:
    """preserve_selection=False selects row 0 — the top is the right place."""
    _scrolled(sidebar, 120.0)

    sidebar._render(preserve_selection=False)

    assert sidebar._pending_scroll is None


def _arm(sidebar, value: float) -> Gtk.Adjustment:
    adj = sidebar._scroller.get_vadjustment()
    sidebar._pending_scroll = value
    sidebar._scroll_frames = sidebar._SCROLL_RESTORE_FRAMES
    return adj


def test_restore_puts_the_offset_back(sidebar) -> None:
    adj = _arm(sidebar, 120.0)
    adj.configure(0.0, 0.0, 1000.0, 10.0, 100.0, 200.0)

    sidebar._restore_scroll(sidebar, None)

    assert adj.get_value() == 120.0


def test_restore_holds_for_several_frames_then_rearms(sidebar) -> None:
    """One frame is not enough — a single assignment loses to the scroll animation.

    The callback must keep asking to run until the budget is spent, and only
    then clear _pending_scroll so a later render can capture again.
    """
    _arm(sidebar, 120.0)
    assert sidebar._SCROLL_RESTORE_FRAMES > 1, "one frame loses to the scroll animation"

    verdicts = [
        sidebar._restore_scroll(sidebar, None)
        for _ in range(sidebar._SCROLL_RESTORE_FRAMES)
    ]

    assert verdicts[:-1] == [GLib.SOURCE_CONTINUE] * (len(verdicts) - 1)
    assert verdicts[-1] == GLib.SOURCE_REMOVE
    assert sidebar._pending_scroll is None, "a later render must be able to capture"


def test_restore_clamps_to_a_shorter_list(sidebar) -> None:
    """A rebuild that produced fewer rows lands at its own bottom, not nowhere.

    Gtk.Adjustment.set_value does the clamping; this pins the contract so a
    future hand-rolled clamp (or a scroll_to that does not clamp) is caught.
    """
    adj = _arm(sidebar, 900.0)
    adj.configure(0.0, 0.0, 300.0, 10.0, 100.0, 200.0)

    sidebar._restore_scroll(sidebar, None)

    assert adj.get_value() == 100.0


def test_restore_is_inert_after_shutdown(sidebar) -> None:
    """shutdown() must leave nothing that touches a finalizing widget tree."""
    _arm(sidebar, 120.0)
    sidebar.shutdown()

    assert sidebar._restore_scroll(sidebar, None) == GLib.SOURCE_REMOVE
    assert sidebar._pending_scroll is None


def test_a_capturing_render_actually_schedules_the_restore(sidebar) -> None:
    """The one line that connects the two halves.

    Every other test here drives `_render` (capture) or `_restore_scroll`
    (restore) as isolated functions, and several arm `_pending_scroll`
    themselves. So deleting `self.add_tick_callback(self._restore_scroll)` --
    the entire wiring -- left all seven of them green: `_pending_scroll` gets
    set, nothing ever reads it, the sidebar still jumps to the top on every
    completed turn, and CI is green. Measured, not hypothesised.

    A spy is required rather than asserting on `_scroll_frames`: that is
    assigned inside the same `if offset > 0:` block, so it survives the
    deletion too and would report a fix that does nothing.
    """
    calls: list = []
    sidebar.add_tick_callback = lambda cb, *a: calls.append(cb) or 1

    _scrolled(sidebar, 120.0)
    sidebar._render(preserve_selection=True)
    assert len(calls) == 1, (
        "a preserve_selection render at a non-zero offset must schedule the "
        f"restore tick; got {len(calls)} call(s)"
    )
    assert calls[0] == sidebar._restore_scroll

    # At the top there is nothing to restore, and a preserve_selection=False
    # render deliberately wants row 0 -- neither may schedule a tick.
    calls.clear()
    sidebar._pending_scroll = None
    _scrolled(sidebar, 0.0)
    sidebar._render(preserve_selection=True)
    assert calls == [], "offset 0 must not schedule a restore"

    sidebar._pending_scroll = None
    _scrolled(sidebar, 120.0)
    sidebar._render(preserve_selection=False)
    assert calls == [], "preserve_selection=False must not schedule a restore"


def test_a_non_preserving_render_cancels_an_in_flight_restore(sidebar) -> None:
    """"Land at the top" has to win over a restore that is already running.

    A preserving render arms three frames of restore. If a
    preserve_selection=False render lands inside that window — it selects row 0
    on purpose — the remaining frames would drag the sidebar back to the old
    offset and silently overrule the newer intent.

    The sibling test above only covers a NON-preserving render with nothing in
    flight, which is the easy half and passes either way.
    """
    _scrolled(sidebar, 120.0)
    sidebar._render(preserve_selection=True)
    assert sidebar._pending_scroll == 120.0, "precondition: a restore is armed"

    sidebar._render(preserve_selection=False)

    assert sidebar._pending_scroll is None, (
        "a non-preserving render must cancel the in-flight restore, or those "
        "frames pull the sidebar back off the top"
    )
    assert sidebar._scroll_frames == 0

    # The already-registered tick must retire itself rather than reapply.
    assert sidebar._restore_scroll(sidebar, None) == GLib.SOURCE_REMOVE


def test_a_cancelled_restore_cannot_leave_two_ticks_sharing_the_budget(sidebar) -> None:
    """Cancelling must UNREGISTER, not just clear the offset.

    Clearing `_pending_scroll` alone leaves the callback registered. A later
    preserving render sees `None`, captures a fresh offset and registers a
    SECOND tick — and both then decrement the shared `_scroll_frames`, so the
    3-frame budget is spent in about 2. That is below the 2-frame minimum
    measured on GTK 4.22, i.e. a restore that lands at the wrong offset.
    """
    registered: list = []
    removed: list = []
    sidebar.add_tick_callback = lambda cb, *a: (registered.append(cb), len(registered))[1]
    sidebar.remove_tick_callback = lambda tid: removed.append(tid)

    _scrolled(sidebar, 120.0)
    sidebar._render(preserve_selection=True)
    assert len(registered) == 1 and sidebar._scroll_tick is not None

    sidebar._render(preserve_selection=False)          # cancel
    assert removed == [1], f"the in-flight tick must be unregistered, got {removed}"
    assert sidebar._scroll_tick is None

    _scrolled(sidebar, 200.0)
    sidebar._render(preserve_selection=True)           # re-arm
    assert len(registered) == 2, "a fresh restore must register again after a cancel"
    assert sidebar._scroll_frames == sidebar._SCROLL_RESTORE_FRAMES, (
        "the new restore must get a full budget, not one shared with a stale tick"
    )
