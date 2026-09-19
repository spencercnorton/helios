"""MainWindow close-safety: the full connected-signal matrix.

`_on_close_request` sets `_destroyed = True`, shuts panes down, then stops the
drivers. Stopping a driver still emits terminal events (Claude deliberately
parses terminal stdout after stop; Codex async discovery can also land late),
and `stop_all` neither clears `driver_manager.current` nor disconnects handlers
— so those events still pass the `_drv_is_current` sender guard. Every
driver->widget path must therefore:

  * complete durable, non-UI work first — ledger writes, session/Work binding,
    pending/queued-message preservation, registry forget/disconnect; then
  * do NO GTK mutation (transcript / composer / toast / toolbar / activity /
    plan / goal-strip / settings-dialog / session-list) once `_destroyed`.

These bind the real MainWindow methods onto a fake `self` (same pattern as
test_question_queue.py / test_goal_mode_window.py) so no full GTK app is needed.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios import main_window as mw  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


class _Recorder:
    """Every attribute is a callable that records it was invoked — so a test
    can assert the widget was *not* touched after close."""

    def __init__(self, calls: list[str], label: str) -> None:
        object.__setattr__(self, "_calls", calls)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str):
        def _record(*_a, **_k):
            self._calls.append(f"{self._label}.{name}")
        return _record


def _window_after_close():
    calls: list[str] = []
    f = types.SimpleNamespace()
    f._destroyed = True
    # The driver is STILL current — only `_destroyed` should stop these events.
    f._drv_is_current = lambda _drv: True
    f._plan = _Recorder(calls, "plan")
    f._activity = _Recorder(calls, "activity")
    f._transcript = _Recorder(calls, "transcript")
    f._chat_toolbar = _Recorder(calls, "toolbar")
    f._composer = _Recorder(calls, "composer")
    f._sessions = _Recorder(calls, "sessions")
    f._settings_dialog = _Recorder(calls, "settings")
    f._goal_strip = _Recorder(calls, "goal")
    f._toast = lambda *a, **k: calls.append("toast")
    return f, calls


# Pure widget-mutating handlers: after close they must touch nothing at all.
_WIDGET_ONLY_HANDLERS = [
    ("_on_assistant_streaming", (object(),)),
    ("_on_turn_result", ({"ok": True},)),
    ("_on_usage_updated", (5, 10)),
    ("_on_rate_limit_updated", ({"used": 1},)),
    ("_on_native_turn_status", ({"status": "inProgress"},)),
    ("_on_native_plan_updated", ({"plan": [{"step": "x", "status": "inProgress"}]},)),
    ("_on_native_diff_updated", ({"diff": "+late\n"},)),
    ("_on_native_agents_updated", ({"child": {"name": "R", "status": "running"}},)),
    ("_on_native_activity_updated", ({"category": "command"},)),
    ("_on_codex_transport_status", ({"transport": "exec"},)),
    ("_on_native_mcp_status_updated", ({"servers": []},)),
]


@pytest.mark.parametrize("handler,extra", _WIDGET_ONLY_HANDLERS)
def test_widget_only_handler_noop_after_close(handler, extra):
    f, calls = _window_after_close()
    drv = types.SimpleNamespace(session_id="s", queued_messages=lambda: [])
    types.MethodType(getattr(MainWindow, handler), f)(drv, *extra)
    assert calls == [], f"{handler} mutated the UI after close: {calls}"


def test_turn_appended_records_ledger_but_skips_widgets_after_close():
    f, calls = _window_after_close()
    recorded: list = []
    f._work_coordinator = types.SimpleNamespace(
        record_turn=lambda drv, turn: recorded.append(("record_turn", turn))
    )

    drv = types.SimpleNamespace(session_id="s")
    turn = object()
    types.MethodType(MainWindow._on_turn_appended, f)(drv, turn)

    assert ("record_turn", turn) in recorded  # ledger ran
    assert calls == []                          # no transcript/plan


def test_queued_user_sent_records_ledger_but_skips_widgets_after_close():
    f, calls = _window_after_close()
    recorded: list = []
    f._work_coordinator = types.SimpleNamespace(
        record_user_message=lambda drv, text: recorded.append(("msg", text))
    )
    drv = types.SimpleNamespace(session_id="s")  # not a CodexAppServerDriver
    types.MethodType(MainWindow._on_queued_user_sent, f)(drv, 7, "hello")

    assert ("msg", "hello") in recorded          # ledger ran
    assert calls == []                            # no transcript/composer/toolbar/activity


def test_native_prompt_accepted_pops_pending_and_records_but_no_bubble_after_close():
    f, calls = _window_after_close()
    recorded: list = []
    drv = types.SimpleNamespace(session_id="s")
    f._native_pending_user = {id(drv): (drv, "gpt text")}
    f._append_visible_user_turn = lambda text: calls.append("bubble")
    f._work_coordinator = types.SimpleNamespace(
        record_user_message=lambda drv, text: recorded.append(("msg", text))
    )

    types.MethodType(MainWindow._on_native_prompt_accepted, f)(drv, "gpt text")

    assert f._native_pending_user == {}          # pending-state cleanup ran
    assert ("msg", "gpt text") in recorded        # ledger ran
    assert "bubble" not in calls                  # no visible bubble


def test_native_prompt_accepted_commits_ledger_before_visible_bubble():
    order: list[str] = []
    drv = types.SimpleNamespace(session_id="s")
    f = types.SimpleNamespace(
        _destroyed=False,
        _native_pending_user={id(drv): (drv, "gpt text")},
        _drv_is_current=lambda candidate: candidate is drv,
        _append_visible_user_turn=lambda text: order.append(f"bubble:{text}"),
        _work_coordinator=types.SimpleNamespace(
            record_user_message=lambda _drv, text: order.append(f"ledger:{text}")
        ),
    )

    types.MethodType(MainWindow._on_native_prompt_accepted, f)(drv, "gpt text")

    assert order == ["ledger:gpt text", "bubble:gpt text"]


def test_return_native_pending_user_routes_to_draft_not_composer_after_close():
    f, calls = _window_after_close()
    drv = types.SimpleNamespace(
        provider="openai", _helios_work_id="w1", session_id="s"
    )
    f._native_pending_user = {id(drv): (drv, "unsent prompt")}
    f._native_draft_key = MainWindow._native_draft_key  # staticmethod

    returned = types.MethodType(MainWindow._return_native_pending_user, f)(drv)

    assert returned is True
    assert f._native_pending_user == {}                    # pending popped (durable)
    # Preserved in the non-UI draft bucket, NOT written to the composer.
    assert f._native_unsent_drafts["openai:w1"] == ["unsent prompt"]
    assert calls == []


def test_driver_error_returns_prompt_but_no_toast_or_composer_after_close():
    f, calls = _window_after_close()
    durable: list = []
    f._return_native_pending_user = lambda drv: durable.append("return_pending") or False
    drv = types.SimpleNamespace(is_busy=False, session_id="s")

    types.MethodType(MainWindow._on_driver_error, f)(drv, "boom")

    assert durable == ["return_pending"]          # durable preservation ran
    assert calls == []                             # no toast/composer/toolbar/activity


def test_driver_error_appends_a_transcript_row_before_the_toast():
    """A-7: the row is the durable record, the toast is the attention-getter
    — both fire, and the row lands first."""
    f, calls = _window_after_close()
    f._destroyed = False
    f._return_native_pending_user = lambda drv: False
    drv = types.SimpleNamespace(is_busy=False, session_id="s")

    types.MethodType(MainWindow._on_driver_error, f)(drv, "boom")

    assert "transcript.append_error" in calls
    assert "toast" in calls
    assert calls.index("transcript.append_error") < calls.index("toast")


def test_driver_exited_completes_cleanup_but_no_widgets_after_close():
    f, calls = _window_after_close()
    durable: list = []
    f._pending_checkpoint_dispatches = {}
    f._native_pending_user = {}
    f._identity_pending_user = {}
    f._native_unsent_drafts = {}
    f._forget_driver = lambda drv: durable.append("forget")
    drv = types.SimpleNamespace(
        provider="anthropic",
        session_id="s",
        take_queued=lambda: (durable.append("take_queued"), ["m1", "m2"])[1],
    )

    types.MethodType(MainWindow._on_driver_exited, f)(drv, 1)

    # Durable cleanup ran in order; registry forget/disconnect happened.
    assert durable == ["take_queued", "forget"]
    assert f._native_unsent_drafts == {"anthropic:s": ["m1", "m2"]}
    assert calls == []                             # no transcript/composer/toast/toolbar/activity


def test_driver_exited_forgets_registry_before_queued_or_busy_ui():
    order: list[str] = []

    class Composer:
        def current_text(self):
            order.append("ui:composer-read")
            return ""

        def set_text(self, _text):
            order.append("ui:composer-write")

        def set_busy(self, _busy):
            order.append("ui:composer-busy")

    drv = types.SimpleNamespace(
        session_id="s",
        take_queued=lambda: (order.append("durable:take-queued"), ["queued"])[1],
    )
    f = types.SimpleNamespace(
        _destroyed=False,
        _pending_checkpoint_dispatches={},
        _native_pending_user={id(drv): (drv, "first")},
        _identity_pending_user={},
        _drv_is_current=lambda candidate: candidate is drv,
        _forget_driver=lambda _drv: order.append("durable:forget"),
        _transcript=types.SimpleNamespace(
            clear_queued=lambda: order.append("ui:clear-queued")
        ),
        _composer=Composer(),
        _chat_toolbar=types.SimpleNamespace(
            set_busy=lambda _busy: order.append("ui:toolbar-busy")
        ),
        _activity=types.SimpleNamespace(clear=lambda: order.append("ui:activity")),
        _toast=lambda *_a, **_k: order.append("ui:toast"),
        _sync_execution_control=lambda: order.append("ui:execution"),
    )

    types.MethodType(MainWindow._on_driver_exited, f)(drv, 0)

    assert order[:2] == [
        "durable:take-queued",
        "durable:forget",
    ]
    assert all(item.startswith("ui:") for item in order[2:])


def test_interaction_resolved_clears_state_but_no_dialog_close_after_close():
    f, calls = _window_after_close()
    drv = types.SimpleNamespace(session_id="s")
    other = types.SimpleNamespace(session_id="o")
    f._question_queue = [(drv, {}, "tuid"), (other, {}, "keep")]
    f._question_active_key = (drv, "tuid")
    f._question_active = True
    f._question_dialog = _Recorder(calls, "dialog")
    f._pump_questions = lambda: None  # guarded separately; stub here
    f._sync_pending_question_indicators = types.MethodType(
        MainWindow._sync_pending_question_indicators, f
    )

    types.MethodType(MainWindow._on_interaction_resolved, f)(drv, "tuid")

    # State cleanup ran (durable): the resolved row is filtered out...
    assert f._question_queue == [(other, {}, "keep")]
    assert f._question_active is False
    # ...but the dialog was NOT closed (GTK) against the finalizing tree,
    # and the session list was NOT touched (indicator sync is a no-op after close).
    assert "dialog.close" not in calls
    assert "sessions.set_pending_questions" not in calls


def test_question_asked_is_dropped_after_close():
    f, calls = _window_after_close()
    f._question_queue = []
    f._answered_question_ids = {}
    f._pump_questions = lambda: calls.append("pump")
    drv = types.SimpleNamespace(session_id="s", is_running=lambda: True)

    types.MethodType(MainWindow._on_question_asked, f)(drv, {"questions": []}, "tuid")

    # Fail closed: nothing enqueued, nothing presented.
    assert f._question_queue == []
    assert f._answered_question_ids == {}
    assert "pump" not in calls


def test_session_started_registers_binds_but_no_toolbar_after_close(monkeypatch):
    monkeypatch.setattr(mw, "stage_resume", lambda nc, sid, provider: nc)
    monkeypatch.setattr(mw, "stage_work", lambda nc, wid: nc)
    monkeypatch.setattr(mw.claude_env, "save_init_snapshot", lambda *a, **k: None)

    f, calls = _window_after_close()
    durable: list = []
    f._work_coordinator = None
    # The real method self-defends on the already-destroyed window; this fake
    # only needs to preserve that no-UI contract at the integration seam.
    f._sync_execution_control = lambda: None
    f._driver_provider = lambda drv: "anthropic"
    f._bind_pending_goal_to_session = lambda *a: durable.append("bind")
    f._next_chat = None
    f._driver_manager = types.SimpleNamespace(
        starting=None,
        register_started=lambda drv, sid: durable.append("register"),
    )
    drv = types.SimpleNamespace(
        _helios_work_id="", supports_native_goals=False,
        init_tools=[], init_mcp_servers=[],
    )

    types.MethodType(MainWindow._on_session_started, f)(drv, "sid-1", "/repo", "opus")

    # Durable registration + binding ran even during close...
    assert durable == ["register", "bind"]
    # ...but the toolbar (and sidebar-refresh schedule) did not.
    assert calls == []


def test_archive_refuses_to_start_after_close():
    f = types.SimpleNamespace(
        _destroyed=True,
        _driver_manager=types.SimpleNamespace(
            live_ids=lambda: (_ for _ in ()).throw(AssertionError("touched manager"))
        ),
    )

    result = types.MethodType(MainWindow._archive_old_sessions_async, f)(repeat=True)

    assert result is False


def test_archive_finishes_durable_work_but_skips_ui_if_close_wins(monkeypatch):
    order: list[str] = []
    f = types.SimpleNamespace(
        _destroyed=False,
        _driver_manager=types.SimpleNamespace(live_ids=lambda: {"live"}),
        _sessions=types.SimpleNamespace(
            reload=lambda **_k: order.append("ui:reload")
        ),
        _toast=lambda *_a, **_k: order.append("ui:toast"),
    )

    def archive(*, live_ids):
        assert live_ids == {"live"}
        order.append("durable:archive")
        f._destroyed = True
        return types.SimpleNamespace(archived=["old"], errors=[])

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(mw.session_archiver, "archive_old_sessions", archive)
    monkeypatch.setattr(mw.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(mw.GLib, "idle_add", lambda callback, *args: callback(*args))

    types.MethodType(MainWindow._archive_old_sessions_async, f)(repeat=False)

    assert order == ["durable:archive"]


def test_close_shuts_every_leaf_before_stopping_drivers(monkeypatch):
    order: list[str] = []
    removed_sources: list[int] = []
    monkeypatch.setattr(mw.GLib, "source_remove", removed_sources.append)

    class Leaf:
        def __init__(self, name: str):
            self._name = name

        def shutdown(self, **_kwargs):
            order.append(f"shutdown:{self._name}")

    f = types.SimpleNamespace(
        _destroyed=False,
        _reap_timer_id=0,
        _archive_timer_id=0,
        _startup_archive_timer_id=77,
        _orphan_sweep_timer_id=0,
        _catalog_tick_id=0,
        _follow_debounce_id=0,
        _stop_following=lambda: order.append("shutdown:follow"),
        _ctx_fill_runner=Leaf("context-fill"),
        _sessions=Leaf("sessions"),
        _transcript=Leaf("transcript"),
        _chat_toolbar=Leaf("toolbar"),
        _composer=Leaf("composer"),
        _context=Leaf("context"),
        _shared=Leaf("shared"),
        _activity=Leaf("activity"),
        _agent_dock=Leaf("agent-dock"),
        _plan=Leaf("plan"),
        _missions=Leaf("missions"),
        _goal_strip=Leaf("goal"),
        _save_window_layout=lambda: order.append("save-layout"),
        _driver_manager=types.SimpleNamespace(
            stop_all=lambda **_k: order.append("stop-drivers")
        ),
    )

    result = types.MethodType(MainWindow._on_close_request, f)()

    assert result is False
    stop_index = order.index("stop-drivers")
    for name in (
        "transcript",
        "toolbar",
        "composer",
        "context",
        "shared",
        "agent-dock",
    ):
        assert order.index(f"shutdown:{name}") < stop_index
    assert removed_sources == [77]
    assert f._startup_archive_timer_id == 0
