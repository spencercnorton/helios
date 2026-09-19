"""Shutdown-teardown tests for the leaf widgets that own GLib timers/threads.

These need real GTK + a display, so they're skipped on the GTK-free CI image
(no gi) and on any headless box (no display). They guard against the class of
GTK shutdown crash where a periodic timer or a daemon thread's idle_add fires
against a finalizing widget tree.
"""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from gi.repository import GLib  # noqa: E402

from helios.widgets.activity_indicator import ActivityIndicator  # noqa: E402
from helios.widgets.chat_toolbar import ChatToolbar  # noqa: E402
from helios.widgets.context_pane import ContextPane  # noqa: E402
from helios.widgets.goal_strip import GoalStrip  # noqa: E402
from helios.widgets.message_bubble import MessageBubble  # noqa: E402
from helios.widgets.mission_pane import MissionPane  # noqa: E402
from helios.widgets.plan_pane import PlanPane  # noqa: E402
from helios.widgets.search_dialog import SearchDialog  # noqa: E402
from helios.widgets.session_list import SessionList  # noqa: E402
from helios.widgets.shared_context_pane import SharedContextPane  # noqa: E402
from helios.widgets.transcript_view import TranscriptView  # noqa: E402


def _source_exists(source_id: int) -> bool:
    return GLib.MainContext.default().find_source_by_id(source_id) is not None


def _find_descendant(widget, cls):
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, cls):
            return child
        found = _find_descendant(child, cls)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def _count_children(widget) -> int:
    count = 0
    child = widget.get_first_child()
    while child is not None:
        count += 1
        child = child.get_next_sibling()
    return count


def test_activity_indicator_shutdown_stops_tick():
    ai = ActivityIndicator()
    ai._ensure_tick()
    assert ai._tick_id != 0
    ai.shutdown()
    assert ai._tick_id == 0
    # Idempotent.
    ai.shutdown()
    assert ai._tick_id == 0


def test_activity_indicator_set_activity_noop_after_shutdown():
    """[blocker #1] A driver stopped by window close still emits a final
    activity event; after shutdown set_activity must not re-arm the 1s tick or
    reveal the strip against the finalizing widget."""
    from helios.widgets.activity_indicator import STATE_BASH, STATE_THINKING

    ai = ActivityIndicator()
    ai.shutdown()
    assert ai._destroyed is True
    assert ai._tick_id == 0

    ai.set_activity(STATE_THINKING)
    ai.set_activity(STATE_BASH, "rm -rf /tmp/whatever")
    ai.set_token_estimate(123)

    assert ai._tick_id == 0
    assert ai.get_reveal_child() is False


def test_activity_indicator_clear_and_estimate_noop_after_shutdown():
    ai = ActivityIndicator()
    ai.shutdown()
    # clear() and set_token_estimate() must be inert after shutdown.
    ai.clear()
    ai.set_token_estimate(999)
    assert ai._token_estimate == 0
    assert ai.get_reveal_child() is False


def test_activity_indicator_tick_drops_without_inspecting_gtk_after_shutdown(monkeypatch):
    """A tick already dispatched into the main loop when shutdown() ran must
    return False WITHOUT inspecting/mutating GTK (no get_reveal_child)."""
    ai = ActivityIndicator()
    ai._ensure_tick()
    ai.shutdown()

    def _boom(*_a, **_k):
        raise AssertionError("_on_tick inspected GTK after shutdown")

    monkeypatch.setattr(ai, "get_reveal_child", _boom)
    assert ai._on_tick() is False
    assert ai._tick_id == 0


def test_session_list_live_id_projection_noop_after_shutdown():
    """register_started()/forget() emit live-id changes that reach the list via
    on_live_ids_changed; after shutdown the projection must not re-render."""
    sl = SessionList()
    sl.shutdown()
    before = set(sl._live_session_ids)
    sl.set_live_session_ids({"a-newly-live-id"})
    assert sl._live_session_ids == before  # projection refused


def test_native_goal_persists_but_strip_not_revealed_after_shutdown(monkeypatch, tmp_path):
    """[audit #6] Canonical goal persistence completes before UI gating; a
    native status flip arriving as the window closes still reconciles to disk,
    but the (shut-down) GoalStrip is not revealed."""
    import types as _types

    from helios.backend import session_goals as sg
    from helios.backend.session_goals import GoalState
    from helios.main_window import MainWindow

    monkeypatch.setattr(sg, "_PATH", tmp_path / "session-goals.json")
    sg.reload()
    sg.set_goal("w1", GoalState(objective="ship the release", status=sg.GOAL_ACTIVE))

    gs = GoalStrip()
    gs.shutdown()

    f = _types.SimpleNamespace(
        _destroyed=True,
        _drv_is_current=lambda _drv: True,
        _goal_strip=gs,
        _work_coordinator=None,
        _driver_provider=lambda _drv: "openai",
        _visible_goal=lambda: sg.get_goal("w1"),
    )
    f._refresh_goal_strip = _types.MethodType(MainWindow._refresh_goal_strip, f)

    drv = _types.SimpleNamespace(_helios_work_id="w1", session_id="s")
    payload = {"goal": {"objective": "ship the release", "status": "paused"}}
    _types.MethodType(MainWindow._on_native_goal_updated, f)(drv, payload)

    assert sg.get_goal("w1").status == sg.GOAL_PAUSED   # persistence completed
    assert gs.get_reveal_child() is False                # strip not revealed
    assert gs._native_goal is None                       # overlay state untouched


def test_plan_pane_mutators_noop_after_shutdown():
    """[blocker #1] Late provider events (show_streaming / append_turn /
    show_native_plan / show_native_diff / show_native_agents /
    show_agent_activity, and the
    session-swap mutators) must not touch a shut-down PlanPane."""
    pane = PlanPane()
    pane.shutdown()
    assert pane._destroyed is True

    before_diff = pane._native_diff
    # None of these may raise or mutate the finalizing pane.
    pane.show_streaming(object())
    pane.append_turn(object())
    pane.show_native_plan({"plan": [{"step": "late", "status": "inProgress"}]})
    pane.show_native_diff({"diff": "+late\n"})
    pane.show_native_agents({"child": {"name": "R", "status": "running"}})
    from helios.backend.agent_activity import AgentActivityModel

    pane.show_agent_activity(AgentActivityModel().snapshot())
    pane.begin_native_turn()
    pane.set_session(object())
    pane.set_live_pending("Claude")
    pane.clear()

    assert pane._native_diff == before_diff
    assert pane._native_plan_summary is None
    assert pane._agents_section.get_visible() is False
    assert pane._diff_window is None


def test_goal_strip_mutators_noop_after_shutdown():
    """[blocker #1] A native goal-status event arriving as the window closes
    persists to disk and *then* updates this strip (the ledger write is not
    gated). After shutdown that widget update must not touch the finalizing
    tree — every mutator funnels through set_goal(), so one guard covers them."""
    gs = GoalStrip()
    gs.shutdown()
    assert gs._destroyed is True

    # None of these may raise, reveal the strip, or even write internal overlay
    # state (set_native_goal mutates _native_goal *before* the set_goal guard).
    gs.set_goal(object())
    gs.set_native_goal({"goal": {"objective": "x", "status": "paused"}})
    gs.clear_native_goal()
    gs.clear_goal()

    assert gs.get_reveal_child() is False
    assert gs._native_goal is None      # overlay state untouched
    # Idempotent.
    gs.shutdown()


def test_session_list_shutdown_removes_status_timer():
    sl = SessionList()
    assert sl._status_timer_id != 0
    assert sl._destroyed is False
    sl.shutdown()
    assert sl._status_timer_id == 0
    assert sl._destroyed is True


def test_session_list_idle_callbacks_noop_after_shutdown():
    """A daemon thread that resolves a title/status right as the window closes
    will call these via idle_add — after shutdown they must be inert."""
    sl = SessionList()
    gen = sl._reload_gen
    sl.shutdown()
    # All of these would otherwise touch the (finalizing) widget tree.
    assert sl._apply_status_updates(gen, {"x": object()}, {"x": object()}) is False
    assert sl._apply_pool_sessions([], gen) is False
    assert sl._apply_resolved_title("any-id", gen) is False
    # The repeating status timer callback drops itself (returns False).
    assert sl._tick_status() is False


def test_session_list_late_generated_title_does_not_refresh_detached_row():
    calls: list[str] = []
    session = types.SimpleNamespace(title="old", first_message_loaded=False)
    row = types.SimpleNamespace(
        session=session,
        refresh_title=lambda: calls.append("refresh"),
    )
    sl = SessionList()
    sl._rows_by_id = {"sid": row}
    sl.shutdown()

    sl._on_title_generated(sl._title_gen, "sid", "late")

    assert calls == []
    assert session.title == "old"


def test_transcript_shutdown_removes_all_owned_sources_and_callbacks():
    tv = TranscriptView()
    tv.show_streaming_assistant(types.SimpleNamespace(blocks=[]))
    stream_source = tv._streaming_flush_id
    idle_source = tv._queue_idle(lambda: False)
    assert stream_source and _source_exists(stream_source)
    assert idle_source and _source_exists(idle_source)

    tv.shutdown()

    assert tv._streaming_flush_id == 0
    assert tv._idle_source_ids == set()
    assert not _source_exists(stream_source)
    assert not _source_exists(idle_source)

    class ExplodingAdjustment:
        def __getattr__(self, _name):
            raise AssertionError("transcript callback inspected GTK after shutdown")

    class ExplodingIterator:
        def __next__(self):
            raise AssertionError("render callback advanced after shutdown")

    exploding = ExplodingAdjustment()
    token = tv._render_token
    assert tv._flush_streaming() is False
    assert tv._scroll_to_end_now(exploding) is False
    assert tv._landing_step(exploding, token) is False
    assert tv._render_batch(ExplodingIterator(), token) is False
    tv._on_vadj_changed(exploding)
    tv._on_vadj_value_changed(exploding)
    assert tv._queue_idle(lambda: (_ for _ in ()).throw(AssertionError())) == 0


@pytest.mark.parametrize(
    "removal",
    ["stream-replacement", "show-live-session", "set-session"],
)
def test_transcript_normal_removal_shuts_down_bubble_activity(removal):
    """Normal navigation/replacement must dispose a bubble just like close."""
    from helios.backend.transcript import ToolUse, Turn

    turn = Turn(role="assistant")
    turn.tool_uses.extend(
        ToolUse(name=f"tool-{i}", input={"i": i}) for i in range(100)
    )
    transcript = TranscriptView()
    bubble = MessageBubble(turn)
    transcript._append_content(bubble)
    expander = _find_descendant(bubble, Gtk.Expander)
    assert expander is not None
    expander.set_expanded(True)
    source_ids = set(expander._activity_source_ids)
    assert source_ids and all(_source_exists(sid) for sid in source_ids)

    if removal == "stream-replacement":
        transcript._streaming_bubble = bubble
        transcript._clear_streaming_bubble()
    elif removal == "show-live-session":
        transcript.show_live_session("/tmp/project", "fable")
    else:
        transcript.set_session(None)

    assert bubble._destroyed is True
    assert bubble.get_parent() is None
    assert expander._activity_source_ids == set()
    assert all(not _source_exists(sid) for sid in source_ids)
    transcript.shutdown()


def test_chat_toolbar_shutdown_cancels_hover_and_meter_sources(monkeypatch):
    from gi.repository import Adw

    toolbar = ChatToolbar()
    toolbar._context_meter.set_usage(50, 100)
    toolbar._on_hover_leave()
    hover_source = toolbar._popdown_timer_id
    assert hover_source and _source_exists(hover_source)

    # Recorded, not raised: the animation target is invoked from C and
    # PyGObject swallows an exception at that boundary.
    draws: list[float] = []
    monkeypatch.setattr(
        toolbar._context_meter, "queue_draw", lambda *_a, **_k: draws.append(0.0)
    )

    # Record the pause. Neither assertion below has teeth on its own: headless,
    # set_usage() already drives the animation to FINISHED inside play(), so the
    # state check is true before shutdown() is ever called; and queue_draw is
    # patched after the only call that would draw, so `draws == []` holds with
    # no pause() at all. Deleting the pause() left the whole suite green — on a
    # mapped window that is up to SLOW_MS of _set_fraction -> queue_draw on a
    # widget the app has declared destroyed. Patched BEFORE shutdown: the
    # meter's shutdown early-returns on _destroyed, so a second call records
    # nothing.
    paused: list[int] = []
    monkeypatch.setattr(
        toolbar._context_meter._anim, "pause", lambda *_a: paused.append(1)
    )

    toolbar.shutdown()

    assert paused, "shutdown must pause the arc animation"
    assert toolbar._popdown_timer_id == 0
    assert not _source_exists(hover_source)
    assert toolbar._context_meter._anim.get_state() != Adw.AnimationState.PLAYING
    # pause(), not reset(): reset() writes value_from back through the target,
    # repainting the arc — and snapping it backwards — during teardown.
    assert draws == []

    def boom(*_args, **_kwargs):
        raise AssertionError("toolbar callback touched GTK after shutdown")

    monkeypatch.setattr(toolbar._context_popover, "popdown", boom)
    assert toolbar._maybe_popdown() is False
    toolbar._context_meter.set_usage(90, 100)  # post-shutdown
    assert draws == []


def test_context_pane_shutdown_cancels_memory_monitor_and_debounce(tmp_path):
    """ContextPane.shutdown() must remove any pending memory-dir debounce
    timer and cancel the file monitor so neither fires against the
    finalizing tree — same contract as MissionPane's monitors below.

    (The old async preview-idle path this test used to guard is gone: every
    existing file — CLAUDE.md, MEMORY.md, individual memory files — now
    opens in MemoryEditor synchronously, so there is no idle source left to
    race against shutdown.)"""
    from helios.backend.projects import Project

    (tmp_path / "memory").mkdir()
    project = Project(dirname="-proj", cwd=str(tmp_path), path=tmp_path)

    pane = ContextPane()
    pane.set_project(project)
    assert pane._memory_monitor is not None
    # Simulate a debounce scheduled by a monitor callback.
    pane._memory_debounce_id = GLib.timeout_add(100_000, lambda: False)

    pane.shutdown()

    assert pane._destroyed is True
    assert pane._memory_debounce_id == 0
    assert pane._memory_monitor is None
    # Idempotent.
    pane.shutdown()


def test_shared_context_worker_does_not_schedule_after_shutdown(monkeypatch):
    from helios.widgets import shared_context_pane as shared_module

    targets = []
    scheduled = []

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            targets.append(target)

        def start(self):
            pass

    monkeypatch.setattr(shared_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(shared_module.scratchpad, "list_entries", lambda: [])
    monkeypatch.setattr(
        shared_module.GLib,
        "idle_add",
        lambda *args: scheduled.append(args) or 1,
    )

    pane = SharedContextPane()
    pane.refresh()
    assert len(targets) == 1
    pane.shutdown()
    targets[0]()

    assert scheduled == []
    assert pane._apply_entries([], "", pane._gen) is False
    assert pane._apply_detail("key", object(), "", pane._gen) is False


def test_search_shutdown_removes_debounce_source():
    owner = Gtk.Window()
    owner._destroyed = False
    dialog = SearchDialog(owner)
    dialog._entry.set_text("needle")
    dialog._on_search_changed(dialog._entry)
    source_id = dialog._debounce_id
    assert source_id and _source_exists(source_id)

    dialog.shutdown()

    assert dialog._debounce_id == 0
    assert not _source_exists(source_id)


def test_search_worker_fails_closed_when_owner_closes(monkeypatch):
    from helios.widgets import search_dialog as search_module

    owner = Gtk.Window()
    owner._destroyed = False
    dialog = SearchDialog(owner)
    targets = []
    scheduled = []

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            targets.append(target)

        def start(self):
            pass

    monkeypatch.setattr(search_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(search_module, "search_sessions", lambda *_a, **_k: [])
    dialog._entry.set_text("needle")
    dialog._on_search_changed(dialog._entry)
    GLib.source_remove(dialog._debounce_id)
    dialog._debounce_id = 0
    assert dialog._run_search() is False
    assert len(targets) == 1

    owner._destroyed = True
    monkeypatch.setattr(
        search_module.GLib,
        "idle_add",
        lambda *args: scheduled.append(args) or 1,
    )
    targets[0]()

    assert scheduled == []
    assert dialog._apply_results(dialog._gen, "needle", []) is False


@pytest.mark.parametrize("err", ["", "scratchpad down"])
def test_handoff_completion_skips_callbacks_after_parent_close(monkeypatch, err):
    from helios.widgets import handoff_dialog

    parent = types.SimpleNamespace(_destroyed=True)

    def boom(*_args, **_kwargs):
        raise AssertionError("handoff completion touched UI after close")

    monkeypatch.setattr(handoff_dialog, "present_handoff_dialog", boom)
    assert (
        handoff_dialog._finish_handoff(
            parent,
            object(),
            boom,
            boom,
            "key",
            "summary",
            1,
            True,
            "openai",
            err,
        )
        is False
    )


def test_message_bubble_shutdown_cancels_batched_activity_idle(monkeypatch):
    from helios.backend.transcript import ToolUse, Turn
    from helios.widgets import message_bubble as bubble_module

    turn = Turn(role="assistant")
    turn.tool_uses.extend(
        ToolUse(name=f"tool-{i}", input={"i": i}) for i in range(100)
    )
    bubble = MessageBubble(turn)
    expander = _find_descendant(bubble, Gtk.Expander)
    assert expander is not None
    expander.set_expanded(True)
    body = expander.get_child()
    assert _count_children(body) == bubble_module._ACTIVITY_FIRST_BATCH
    source_ids = set(expander._activity_source_ids)
    assert source_ids and all(_source_exists(sid) for sid in source_ids)

    bubble.shutdown()

    assert bubble._destroyed is True
    assert expander._activity_build_closed is True
    assert expander._activity_source_ids == set()
    assert all(not _source_exists(sid) for sid in source_ids)
    assert _count_children(body) == bubble_module._ACTIVITY_FIRST_BATCH

    def boom(*_args, **_kwargs):
        raise AssertionError("activity callback inspected GTK after shutdown")

    monkeypatch.setattr(expander, "get_expanded", boom)
    monkeypatch.setattr(expander, "get_child", boom)
    bubble_module._on_activity_group_expanded(expander, None, turn)
    bubble_module._cancel_on_collapse(expander, None)


def test_plan_pane_shutdown_closes_loader_and_noops_delivery():
    """PlanPane owns a background session loader whose idle_add would
    otherwise fire against a finalizing widget on window close."""
    from helios.backend.plan_summary import empty_summary

    pane = PlanPane()
    assert pane._destroyed is False
    pane.shutdown()
    assert pane._destroyed is True
    # A late session-state delivery (idle already queued) must no-op.
    assert (
        pane._apply_session_state(
            empty_summary(), [], [], pane._token, pane._session_id
        )
        is False
    )
    # Idempotent.
    pane.shutdown()


def test_mission_pane_shutdown_cancels_timer_and_monitors(monkeypatch, tmp_path):
    """MissionPane.shutdown() must remove any pending debounce timer and
    cancel both file monitors so neither fires against the finalizing tree."""
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path / ".tandem"))
    (tmp_path / ".tandem" / "missions").mkdir(parents=True)

    pane = MissionPane()
    assert pane._missions_monitor is not None
    # Simulate a debounce scheduled by a monitor callback.
    pane._debounce_id = GLib.timeout_add(100_000, lambda: False)

    pane.shutdown()
    assert pane._destroyed is True
    assert pane._debounce_id == 0
    assert pane._missions_monitor is None
    assert pane._mission_monitor is None
    # Idempotent.
    pane.shutdown()
