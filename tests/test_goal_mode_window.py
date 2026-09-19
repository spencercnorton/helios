from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios import main_window as main_window_module  # noqa: E402
from helios.backend import session_goals as sg  # noqa: E402
from helios.backend.transcript import ToolUse, Turn  # noqa: E402
from helios.main_window import ChatTarget, MainWindow  # noqa: E402
from helios.backend.work_store import ExecutionAdmissionError  # noqa: E402
from helios.backend.work_store import WorkStore  # noqa: E402
from helios.backend.work_coordinator import WorkCoordinator, tag_driver  # noqa: E402
from helios.backend.process.message_queue import (  # noqa: E402
    RequiredPromptContextError,
)


class FakeGoalStrip:
    def __init__(self) -> None:
        self.goals: list[sg.GoalState | None] = []
        self.native_goals: list[dict | None] = []

    def set_goal(self, goal) -> None:
        self.goals.append(goal)

    def set_native_goal(self, payload) -> None:
        self.native_goals.append(payload)

    def clear_native_goal(self) -> None:
        self.native_goals.append(None)


class FakeDrv:
    provider = "openai"

    def __init__(self, sid: str = "", work_id: str = "") -> None:
        self.session_id = sid
        self._helios_work_id = work_id
        self._helios_participant_provider = self.provider
        self.init_tools = []
        self.init_mcp_servers = []


class FakeDriverManager:
    def __init__(self, starting=None) -> None:
        self.starting = starting
        self.live = {}

    def register_started(self, driver, session_id: str) -> None:
        driver.session_id = session_id
        self.live[session_id] = driver
        if self.starting is driver:
            self.starting = None


def test_driver_execution_guard_rejects_rebound_participant_generation():
    current = types.SimpleNamespace(
        participant_id="participant-1",
        provider="openai",
        generation=1,
        native_id="thread-1",
    )
    coordinator = types.SimpleNamespace(
        execution_block_reason=lambda _work_id: "",
        participant=lambda _work_id, _provider: current,
    )
    driver = FakeDrv("thread-1", "work-1")
    driver._helios_participant_id = "participant-1"
    driver._helios_participant_generation = 1
    window = types.SimpleNamespace(
        _budget_blocked_work_ids=set(),
        _work_coordinator=coordinator,
        _driver_provider=lambda candidate: candidate.provider,
    )

    assert MainWindow._execution_guard_for_driver(window, driver) == ""

    current.generation = 2
    assert "binding changed" in MainWindow._execution_guard_for_driver(
        window,
        driver,
    )

    def driver_provider(self, driver, default: str = "anthropic") -> str:
        return getattr(driver, "provider", default)


def test_window_execution_controller_routes_exact_participant_identity():
    calls = []

    class Coordinator:
        def start_execution_attempt(self, **kwargs):
            calls.append(("start", kwargs))
            return types.SimpleNamespace(attempt_id="attempt-1")

        def finish_execution_attempt(self, attempt_id, **kwargs):
            calls.append(("finish", attempt_id, kwargs))

    driver = FakeDrv("thread-1", "work-1")
    driver._helios_participant_id = "participant-1"
    driver._helios_participant_generation = 4
    driver._effort = "high"
    driver.model = "gpt-5.6"
    driver.display_name = "codex-app-server"
    window = types.SimpleNamespace(_work_coordinator=Coordinator())

    assert MainWindow._start_execution_attempt_for_driver(window, driver) == (
        "attempt-1",
        "",
    )
    MainWindow._finish_execution_attempt_for_driver(
        window,
        driver,
        "attempt-1",
        "completed",
        "success",
    )

    assert calls == [
        (
            "start",
            {
                "work_id": "work-1",
                "participant_id": "participant-1",
                "provider": "openai",
                "participant_generation": 4,
                "metadata": {
                    "model": "gpt-5.6",
                    "effort": "high",
                    "transport": "codex-app-server",
                },
                # the workspace lease. This fake driver exposes neither, so
                # both are empty — and an empty mode is treated as a WRITER,
                # because fail-closed is the safe direction for a lease.
                "workspace_root": "",
                "permission_mode": "",
            },
        ),
        (
            "finish",
            "attempt-1",
            {"status": "completed", "terminal_reason": "success"},
        ),
    ]


def test_window_execution_admission_surfaces_busy_owner_reason():
    denial = (
        "Another non-Claude Work is already executing (provider=openai, "
        "Work=work-owner, attempt=attempt-owner). Helios currently allows "
        "one running GPT, OpenRouter, or other non-Claude attempt at a time; "
        "unrelated Claude Works can run concurrently. Wait for it to finish "
        "or stop it first."
    )

    class Coordinator:
        def start_execution_attempt(self, **_kwargs):
            raise ExecutionAdmissionError(denial)

    driver = FakeDrv("thread-1", "work-1")
    driver._helios_participant_id = "participant-1"
    driver._helios_participant_generation = 1
    window = types.SimpleNamespace(_work_coordinator=Coordinator())

    attempt_id, reason = MainWindow._start_execution_attempt_for_driver(
        window,
        driver,
    )

    assert attempt_id == ""
    assert reason == denial


@pytest.mark.parametrize(
    ("provider", "denial"),
    (
        (
            "anthropic",
            "This Work is already executing (provider=anthropic, "
            "Work=work-owner, attempt=attempt-owner). Wait for it to finish "
            "or stop it first.",
        ),
        (
            "openrouter",
            "Another non-Claude Work is already executing (provider=openai, "
            "Work=work-owner, attempt=attempt-owner). Helios currently allows "
            "one running GPT, OpenRouter, or other non-Claude attempt at a "
            "time; unrelated Claude Works can run concurrently. Wait for it "
            "to finish or stop it first.",
        ),
    ),
)
def test_established_non_native_admission_denial_restores_uncommitted_prompt(
    provider,
    denial,
):
    """A durable-slot denial must not leave a Claude/OpenRouter UI lie."""

    class Driver:
        def __init__(self) -> None:
            self.provider = provider
            self.session_id = "native-session"
            self.is_busy = False
            self.is_accepting_input = True
            self._helios_work_id = "work-1"
            self._helios_participant_provider = provider
            self._helios_participant_id = "participant-1"
            self._helios_participant_generation = 1

        def send_user_text(self, text):
            sent.append(text)
            # Drivers emit admission errors synchronously and remain idle.
            window._on_driver_error(
                self,
                denial,
            )

    driver = Driver()
    sent: list[str] = []
    visible: list[str] = []
    recorded: list[tuple[object, str]] = []
    toasts: list[str] = []
    pending_dispatch: list[object] = []
    composer_text = ["Build it"]
    participant = types.SimpleNamespace(
        participant_id="participant-1",
        generation=1,
        native_id="native-session",
    )
    window = types.SimpleNamespace(
        _destroyed=False,
        _driver=driver,
        _restore_in_flight=False,
        _native_pending_user={},
        _identity_pending_user={},
        _budget_blocked_work_ids=set(),
        _work_coordinator=types.SimpleNamespace(
            execution_block_reason=lambda _work_id: "",
            participant=lambda _work_id, _provider: participant,
            record_user_message=lambda candidate, text: recorded.append(
                (candidate, text)
            ),
        ),
        _execution_change_pending_for=lambda _candidate: False,
        _ensure_driver=lambda: True,
        _stop_following=lambda: None,
        _driver_provider=lambda _candidate: provider,
        _drv_is_current=lambda candidate: candidate is driver,
        _append_visible_user_turn=visible.append,
        _return_native_pending_user=lambda _candidate: False,
        _composer=types.SimpleNamespace(
            current_text=lambda: composer_text[0],
            set_text=lambda text: composer_text.__setitem__(0, text),
            set_busy=lambda _busy: None,
        ),
            _chat_toolbar=types.SimpleNamespace(set_busy=lambda _busy: None),
            _clear_busy_ui=lambda: None,
            _activity=types.SimpleNamespace(
            set_activity=lambda _state: None,
            clear=lambda: None,
        ),
        _toast=toasts.append,
        _transcript=types.SimpleNamespace(append_error=lambda _message: None),
        _capture_checkpoint=lambda _drv, _text, dispatch: pending_dispatch.append(
            dispatch
        ),
    )
    window._return_identity_pending_user = types.MethodType(
        MainWindow._return_identity_pending_user,
        window,
    )
    window._accept_identity_pending_user = types.MethodType(
        MainWindow._accept_identity_pending_user,
        window,
    )
    window._on_driver_error = types.MethodType(MainWindow._on_driver_error, window)

    MainWindow._on_composer_send(window, None, "Build it")

    # Gtk clears the composer after the send signal; checkpoint release occurs
    # later on the main loop.
    composer_text[0] = ""
    assert visible == []
    assert recorded == []
    assert len(pending_dispatch) == 1

    pending_dispatch[0]()

    assert sent == ["Build it"]
    assert visible == []
    assert recorded == []
    assert composer_text == ["Build it"]
    assert window._identity_pending_user == {}
    assert toasts[-1] == denial


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(sg, "_PATH", tmp_path / ".helios" / "session-goals.json")
    sg.reload()


def _fake_project(cwd: str = "/repo", read_only: bool = False):
    return types.SimpleNamespace(cwd=cwd, read_only=read_only)


def _bind_goal_methods(f) -> None:
    for name in (
        "_current_work_id",
        "_goal_work_id",
        "_goal_session_id",
        "_visible_goal",
        "_refresh_goal_strip",
        "_goal_context_for_driver",
        "_goal_with_current_metadata",
        "_goal_with_driver_metadata",
        "_save_current_goal",
        "_set_goal_status",
        "_clear_goal",
        "_bind_pending_goal_to_session",
    ):
        setattr(f, name, types.MethodType(getattr(MainWindow, name), f))


def test_pending_goal_binds_on_session_started(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr("helios.main_window.GLib.timeout_add", lambda *_a, **_k: 0)
    drv = FakeDrv(work_id="work-1")
    f = types.SimpleNamespace()
    f._destroyed = False
    f._driver = drv
    f._driver_manager = FakeDriverManager(starting=drv)
    f._next_chat = None
    f._pending_goal = sg.GoalState("Finish the app")
    f._work_coordinator = None
    f._goal_strip = FakeGoalStrip()
    f._sync_live_ids = lambda: None
    f._drv_is_current = lambda d: d is drv
    f._driver_provider = lambda d: d.provider
    f._chat_toolbar = types.SimpleNamespace(set_context_model=lambda _model: None)
    f._refresh_sidebar_for_live = lambda *_a: False
    f._sync_effort_sensitivity = lambda: None
    f._sync_execution_control = lambda: None
    f._staged_permission_mode = ""
    f._staged_effort_key = ""
    _bind_goal_methods(f)
    f._on_session_started = types.MethodType(MainWindow._on_session_started, f)

    f._on_session_started(drv, "sid-1", "/repo", "gpt")

    stored = sg.get_goal("work-1")
    assert stored is not None
    assert stored.objective == "Finish the app"
    assert stored.cwd == "/repo"
    assert stored.provider == "openai"
    assert f._pending_goal is None
    assert f._goal_strip.goals[-1].objective == "Finish the app"


def test_resumed_native_thread_without_work_goal_is_explicitly_cleared(
    monkeypatch,
    tmp_path,
):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr("helios.main_window.GLib.timeout_add", lambda *_a, **_k: 0)
    drv = FakeDrv("old-thread", "work-1")
    drv.supports_native_goals = True
    drv._helios_goal_session_id_hint = "old-thread"
    cleared = []
    drv.clear_native_goal = lambda: cleared.append(True)
    drv.sync_goal = lambda *_args: pytest.fail("missing Work goal was set")
    f = types.SimpleNamespace(
        _destroyed=False,
        _driver=drv,
        _driver_manager=FakeDriverManager(starting=drv),
        _next_chat=None,
        _pending_goal=None,
        _work_coordinator=None,
        _goal_strip=FakeGoalStrip(),
        _sync_live_ids=lambda: None,
        _drv_is_current=lambda candidate: candidate is drv,
        _driver_provider=lambda candidate: candidate.provider,
        _chat_toolbar=types.SimpleNamespace(set_context_model=lambda _model: None),
        _refresh_sidebar_for_live=lambda *_args: False,
        _sync_effort_sensitivity=lambda: None,
        _sync_execution_control=lambda: None,
        _staged_permission_mode="",
        _staged_effort_key="",
    )
    _bind_goal_methods(f)
    f._on_session_started = types.MethodType(MainWindow._on_session_started, f)

    f._on_session_started(drv, "old-thread", "/repo", "gpt")

    assert cleared == [True]


def test_visible_goal_follows_selected_session(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    sg.set_goal("work-a", sg.GoalState("Goal A"))
    sg.set_goal("work-b", sg.GoalState("Goal B"))
    f = types.SimpleNamespace()
    f._driver = None
    f._pending_goal = None
    f._next_chat = ChatTarget(
        project=_fake_project(), resume_id="sid-a", work_id="work-a"
    )
    _bind_goal_methods(f)
    assert f._visible_goal().objective == "Goal A"

    f._next_chat = ChatTarget(
        project=_fake_project(), resume_id="sid-b", work_id="work-b"
    )
    assert f._visible_goal().objective == "Goal B"


def test_pause_resume_complete_and_clear(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    sg.set_goal("work-1", sg.GoalState("Keep going"))
    f = types.SimpleNamespace()
    f._driver = None
    f._next_chat = ChatTarget(
        project=_fake_project(), resume_id="sid", work_id="work-1"
    )
    f._pending_goal = None
    f._goal_strip = FakeGoalStrip()
    f._current_project_cwd = lambda: "/repo"
    f._selected_provider = lambda: "anthropic"
    f._work_coordinator = None
    _bind_goal_methods(f)

    f._set_goal_status(sg.GOAL_PAUSED)
    assert sg.get_goal("work-1").status == sg.GOAL_PAUSED
    f._set_goal_status(sg.GOAL_ACTIVE)
    assert sg.get_goal("work-1").status == sg.GOAL_ACTIVE
    f._set_goal_status(sg.GOAL_COMPLETE)
    assert sg.get_goal("work-1").status == sg.GOAL_COMPLETE
    f._clear_goal()
    assert sg.get_goal("work-1") is None


def test_prompt_context_uses_stored_and_pending_goals(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    sg.set_goal("work-1", sg.GoalState("Stored goal"))
    stored_drv = FakeDrv("sid", "work-1")
    f = types.SimpleNamespace()
    f._driver = stored_drv
    f._driver_manager = FakeDriverManager()
    f._pending_goal = None
    f._work_coordinator = None
    _bind_goal_methods(f)

    wrapped = f._goal_context_for_driver(stored_drv, "hello")
    assert "Objective: Stored goal" in wrapped
    assert sg.strip_goal_envelope(wrapped) == "hello"

    sg.set_goal("work-1", sg.GoalState("Paused goal", status=sg.GOAL_PAUSED))
    assert f._goal_context_for_driver(stored_drv, "hello") == "hello"

    pending_drv = FakeDrv()
    f._driver = pending_drv
    f._driver_manager.starting = pending_drv
    f._pending_goal = sg.GoalState("Pending goal")
    wrapped = f._goal_context_for_driver(pending_drv, "next")
    assert "Objective: Pending goal" in wrapped
    assert sg.strip_goal_envelope(wrapped) == "next"


def test_tandem_context_failure_blocks_before_provider_boundary(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    coordinator = WorkCoordinator(WorkStore(tmp_path / "work-tandem.db"))
    work = coordinator.ensure_work(
        cwd="/repo",
        objective="Ship safely",
        definition_of_done="Peer context is preserved",
        mode="tandem",
    )
    participant = coordinator.bind_participant(work.work_id, "openai")
    drv = FakeDrv("thread-1", work.work_id)
    tag_driver(drv, participant)
    monkeypatch.setattr(
        coordinator,
        "prepare_prompt",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("sensitive detail")),
    )
    toasts = []
    f = types.SimpleNamespace(
        _driver=drv,
        _driver_manager=FakeDriverManager(),
        _pending_goal=None,
        _work_coordinator=coordinator,
        _driver_provider=lambda candidate: candidate.provider,
        _toast=lambda text, **_kwargs: toasts.append(text),
    )
    _bind_goal_methods(f)

    with pytest.raises(RequiredPromptContextError) as caught:
        f._goal_context_for_driver(drv, "must stay local")

    assert caught.value.user_message == str(caught.value)
    assert "sensitive detail" not in str(caught.value)
    assert toasts == []
    coordinator.store.close()


def test_single_context_failure_requires_audit_and_visible_degradation(
    monkeypatch, tmp_path
):
    _redirect(monkeypatch, tmp_path)
    coordinator = WorkCoordinator(WorkStore(tmp_path / "work-single.db"))
    work = coordinator.ensure_work(cwd="/repo", mode="single")
    participant = coordinator.bind_participant(work.work_id, "openai")
    drv = FakeDrv("thread-1", work.work_id)
    tag_driver(drv, participant)
    monkeypatch.setattr(
        coordinator,
        "prepare_prompt",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("database locked")),
    )
    toasts = []
    f = types.SimpleNamespace(
        _driver=drv,
        _driver_manager=FakeDriverManager(),
        _pending_goal=None,
        _work_coordinator=coordinator,
        _driver_provider=lambda candidate: candidate.provider,
        _toast=lambda text, **_kwargs: toasts.append(text),
    )
    _bind_goal_methods(f)

    assert f._goal_context_for_driver(drv, "continue") == "continue"
    events = coordinator.store.list_events(work.work_id)
    assert events[-1].event_type == "context.degraded"
    assert events[-1].control_plane is True
    assert toasts and "Goal context only" in toasts[0]
    coordinator.store.close()


def test_synced_native_goal_still_receives_done_when_and_acceptance_checklist(
    monkeypatch, tmp_path
):
    _redirect(monkeypatch, tmp_path)
    sg.set_goal(
        "work-1",
        sg.GoalState(
            "Native objective",
            definition_of_done="All focused checks pass",
            items=[sg.GoalPlanItem("Verify persistence", sg.PLAN_PENDING)],
        ),
    )
    drv = FakeDrv("thread-1", "work-1")
    drv.supports_native_goals = True
    drv.native_goal_synced = True
    f = types.SimpleNamespace(
        _driver=drv,
        _driver_manager=FakeDriverManager(),
        _pending_goal=None,
        _work_coordinator=None,
    )
    _bind_goal_methods(f)

    wrapped = f._goal_context_for_driver(drv, "continue")

    assert "Objective: Native objective" not in wrapped
    assert "Done when: All focused checks pass" in wrapped
    assert "- [pending] Verify persistence" in wrapped
    assert sg.strip_goal_envelope(wrapped) == "continue"


def test_model_todo_plan_does_not_rewrite_user_acceptance_checklist(
    monkeypatch, tmp_path
):
    _redirect(monkeypatch, tmp_path)
    accepted = sg.GoalState(
        "Ship it",
        items=[sg.GoalPlanItem("User acceptance item", sg.PLAN_PENDING)],
    )
    sg.set_goal("work-1", accepted)
    driver = FakeDrv("thread-1", "work-1")
    turn = Turn(role="assistant")
    turn.tool_uses.append(
        ToolUse(
            name="TodoWrite",
            input={
                "todos": [
                    {"content": "Model execution task", "status": "completed"}
                ]
            },
        )
    )
    window = types.SimpleNamespace(
        _destroyed=True,
        _work_coordinator=None,
        _drv_is_current=lambda _candidate: True,
    )

    MainWindow._on_turn_appended(window, driver, turn)

    assert sg.get_goal("work-1").items == accepted.items


def test_legacy_goal_fallback_without_work_coordinator(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    drv = FakeDrv("legacy-sid")
    f = types.SimpleNamespace()
    f._driver = drv
    f._driver_manager = FakeDriverManager(starting=drv)
    f._next_chat = ChatTarget(
        project=_fake_project(),
        resume_id="legacy-sid",
    )
    f._pending_goal = sg.GoalState("Legacy objective")
    f._goal_strip = FakeGoalStrip()
    f._work_coordinator = None
    f._drv_is_current = lambda d: d is drv
    f._driver_provider = lambda d: d.provider
    _bind_goal_methods(f)

    f._bind_pending_goal_to_session(drv, "legacy-sid", "/repo")

    assert sg.get_goal("legacy-sid").objective == "Legacy objective"
    assert f._visible_goal().objective == "Legacy objective"
    assert "Objective: Legacy objective" in f._goal_context_for_driver(drv, "go")
    f._clear_goal()
    assert sg.get_goal("legacy-sid") is None


def test_native_goal_status_reconciles_without_deleting_work_on_clear(
    monkeypatch, tmp_path
):
    _redirect(monkeypatch, tmp_path)
    sg.set_goal("work-1", sg.GoalState("Finish the app"))
    drv = FakeDrv("thread-1", "work-1")
    drv.sync_goal = lambda *_args: pytest.fail("native reconciliation re-synced")
    recorded: list[sg.GoalState] = []
    coordinator = types.SimpleNamespace(
        record_goal=lambda **kwargs: recorded.append(kwargs["goal"])
    )
    f = types.SimpleNamespace()
    f._driver = drv
    f._next_chat = ChatTarget(
        project=_fake_project(), resume_id="thread-1", work_id="work-1"
    )
    f._pending_goal = None
    f._work_coordinator = coordinator
    f._goal_strip = FakeGoalStrip()
    f._drv_is_current = lambda candidate: candidate is drv
    f._driver_provider = lambda candidate: candidate.provider
    _bind_goal_methods(f)
    f._on_native_goal_updated = types.MethodType(
        MainWindow._on_native_goal_updated, f
    )

    payload = {
        "threadId": "thread-1",
        "goal": {
            "objective": "Finish the app",
            "status": "budgetLimited",
            "tokensUsed": 40000,
            "tokenBudget": 40000,
            "timeUsedSeconds": 600,
        },
    }
    f._on_native_goal_updated(drv, payload)

    assert sg.get_goal("work-1").status == sg.GOAL_BLOCKED
    assert recorded[-1].status == sg.GOAL_BLOCKED
    assert f._goal_strip.native_goals[-1] == payload

    f._on_native_goal_updated(drv, {"threadId": "thread-1", "cleared": True})
    assert sg.get_goal("work-1").objective == "Finish the app"
    assert f._goal_strip.native_goals[-1] is None


def test_native_user_bubble_and_work_event_wait_for_prompt_acceptance():
    drv = FakeDrv("thread-1", "work-1")
    turns = []
    plan_turns = []
    recorded = []
    f = types.SimpleNamespace(
        _destroyed=False,
        _native_pending_user={id(drv): (drv, "Build it")},
        _transcript=types.SimpleNamespace(append_turn=turns.append),
        _plan=types.SimpleNamespace(append_turn=plan_turns.append),
        _work_coordinator=types.SimpleNamespace(
            record_user_message=lambda driver, text: recorded.append((driver, text))
        ),
        _drv_is_current=lambda candidate: candidate is drv,
    )
    f._append_visible_user_turn = types.MethodType(
        MainWindow._append_visible_user_turn,
        f,
    )
    f._on_native_prompt_accepted = types.MethodType(
        MainWindow._on_native_prompt_accepted,
        f,
    )

    assert turns == []
    assert recorded == []
    f._on_native_prompt_accepted(drv, "Build it")

    assert [turn.text for turn in turns] == ["Build it"]
    assert [turn.text for turn in plan_turns] == ["Build it"]
    assert recorded == [(drv, "Build it")]
    assert f._native_pending_user == {}


def test_queued_user_promotion_stages_the_plan_boundary_once():
    drv = FakeDrv("thread-1", "work-1")
    transcript_turns = []
    plan_turns = []
    removed = []
    busy = []
    activity = []
    f = types.SimpleNamespace(
        _destroyed=False,
        _work_coordinator=None,
        _drv_is_current=lambda candidate: candidate is drv,
        _transcript=types.SimpleNamespace(
            remove_queued=removed.append,
            append_turn=transcript_turns.append,
        ),
        _plan=types.SimpleNamespace(append_turn=plan_turns.append),
        _composer=types.SimpleNamespace(set_busy=busy.append),
        _chat_toolbar=types.SimpleNamespace(set_busy=busy.append),
        _activity=types.SimpleNamespace(set_activity=activity.append),
    )
    f._on_queued_user_sent = types.MethodType(MainWindow._on_queued_user_sent, f)

    f._on_queued_user_sent(drv, 7, "Queued request")

    assert removed == [7]
    assert [turn.text for turn in transcript_turns] == ["Queued request"]
    assert plan_turns == transcript_turns
    assert busy == [True, True]
    assert len(activity) == 1


def test_rejected_background_native_prompt_is_restored_when_rebound():
    drv = FakeDrv("thread-1", "work-1")
    composer_text = [""]
    toasts = []
    current = [False]
    f = types.SimpleNamespace(
        _destroyed=False,
        _native_pending_user={id(drv): (drv, "Preserve this prompt")},
        _native_unsent_drafts={},
        _drv_is_current=lambda candidate: current[0] and candidate is drv,
        _composer=types.SimpleNamespace(
            current_text=lambda: composer_text[0],
            set_text=lambda text: composer_text.__setitem__(0, text),
        ),
        _toast=toasts.append,
        _native_draft_key=lambda candidate: MainWindow._native_draft_key(candidate),
    )
    f._return_native_pending_user = types.MethodType(
        MainWindow._return_native_pending_user,
        f,
    )
    f._restore_native_unsent_drafts = types.MethodType(
        MainWindow._restore_native_unsent_drafts,
        f,
    )

    f._return_native_pending_user(drv)
    assert composer_text == [""]
    assert f._native_unsent_drafts == {
        "openai:work-1": ["Preserve this prompt"]
    }

    claude = FakeDrv("claude-session", "work-1")
    claude.provider = "anthropic"
    current[0] = True
    f._restore_native_unsent_drafts(claude)
    assert composer_text == [""]
    assert "openai:work-1" in f._native_unsent_drafts

    f._restore_native_unsent_drafts(drv)
    assert composer_text == ["Preserve this prompt"]
    assert f._native_unsent_drafts == {}
    assert "Restored 1 unaccepted GPT prompt" in toasts[-1]


def test_budget_exhaustion_stops_entire_work_family_and_preserves_queues():
    def queued_driver(provider: str, texts: list[str]):
        class Drv:
            pass

        queue = list(enumerate(texts, start=1))
        drv = Drv()
        drv.provider = provider
        drv._helios_work_id = "work-1"
        drv._helios_work_id_hint = "work-1"
        drv.queued_messages = lambda: list(queue)

        def take_queued():
            saved = [text for _qid, text in queue]
            queue.clear()
            return saved

        drv.take_queued = take_queued
        return drv, queue

    visible, visible_queue = queued_driver("openai", ["visible queued"])
    background, background_queue = queued_driver(
        "anthropic", ["background queued"]
    )
    stopped: list[tuple[object, bool]] = []
    persisted: list[dict] = []
    composer_text = [""]
    removed: list[int] = []
    busy_cleared: list[bool] = []
    f = types.SimpleNamespace(
        _destroyed=False,
        _budget_blocked_work_ids=set(),
        _native_pending_user={},
        _identity_pending_user={},
        _native_unsent_drafts={},
        _work_coordinator=types.SimpleNamespace(
            mark_budget_exhausted=lambda **kwargs: persisted.append(kwargs)
        ),
        _driver_manager=types.SimpleNamespace(
            drivers_for_work=lambda _work_id: {visible, background},
            stop_driver=lambda drv, interrupt=True: stopped.append((drv, interrupt)),
        ),
        _driver_provider=lambda drv: drv.provider,
        _drv_is_current=lambda drv: drv is visible,
        _transcript=types.SimpleNamespace(remove_queued=removed.append),
        _composer=types.SimpleNamespace(
            current_text=lambda: composer_text[0],
            set_text=lambda text: composer_text.__setitem__(0, text),
        ),
        _clear_busy_ui=lambda: busy_cleared.append(True),
        _toast=lambda *_args, **_kwargs: None,
    )

    MainWindow._on_budget_exhausted(
        f,
        visible,
        {"kind": "tokens", "limit": 200_000, "provider": "openai"},
    )

    assert f._budget_blocked_work_ids == {"work-1"}
    assert persisted[0]["work_id"] == "work-1"
    assert {id(drv) for drv, interrupt in stopped if interrupt} == {
        id(visible),
        id(background),
    }
    assert visible_queue == []
    assert background_queue == []
    assert composer_text == ["visible queued"]
    assert f._native_unsent_drafts == {
        "anthropic:work-1": ["background queued"]
    }
    assert removed == [1]
    assert busy_cleared == [True]


def test_emergency_stop_all_uses_codex_safety_abort(monkeypatch):
    class CodexDriver:
        provider = "openai"
        session_id = "thread-1"

        @staticmethod
        def queued_messages():
            return []

    codex = CodexDriver()
    stopped: list[tuple[object, bool]] = []
    aborts: list[int] = []
    toasts: list[str] = []
    window = types.SimpleNamespace(
        _destroyed=False,
        _native_pending_user={},
        _identity_pending_user={},
        _native_unsent_drafts={},
        _driver_manager=types.SimpleNamespace(
            drivers_for_shutdown=lambda: {codex},
            stop_driver=lambda drv, interrupt=True: stopped.append((drv, interrupt)),
        ),
        _drv_is_current=lambda drv: drv is codex,
        _clear_busy_ui=lambda: None,
        _toast=toasts.append,
    )
    monkeypatch.setattr(main_window_module, "CodexAppServerDriver", CodexDriver)
    monkeypatch.setattr(
        main_window_module,
        "get_shared_hub",
        lambda: types.SimpleNamespace(
            abort_transport=lambda *, returncode: aborts.append(returncode)
        ),
    )

    MainWindow._on_emergency_stop_all(window)

    assert stopped == [(codex, True)]
    assert aborts == [-1]
    assert toasts == [
        "Emergency stop sent to 1 live session. Unsent drafts were preserved."
    ]
