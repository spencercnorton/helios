from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios import main_window as main_window_module  # noqa: E402
from helios.backend.process.driver_manager import DriverManager  # noqa: E402
from helios.backend.composer_state import DraftBook  # noqa: E402
from helios.backend.projects import Project, Session  # noqa: E402
from helios.backend import session_providers  # noqa: E402
from helios.backend.session_router import ChatTarget  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


class _Driver:
    def __init__(self, provider: str, session_id: str, work_id: str) -> None:
        self.provider = provider
        self.session_id = session_id
        self.is_accepting_input = True
        self.is_busy = False
        self._helios_work_id = work_id
        self._helios_participant_provider = provider
        self._helios_participant_generation = 1


def test_provider_toggle_rebinds_live_work_siblings_when_reveal_misses(tmp_path):
    work_id = "work-1"
    project = Project(
        dirname="-repo",
        cwd="/repo",
        path=tmp_path,
    )
    for session_id in ("claude-native", "gpt-native"):
        (tmp_path / f"{session_id}.jsonl").write_text("", encoding="utf-8")
    session_providers.set_provider("claude-native", "anthropic")
    session_providers.set_provider("gpt-native", "openai")

    claude = _Driver("anthropic", "claude-native", work_id)
    gpt = _Driver("openai", "gpt-native", work_id)
    manager = DriverManager(max_live=4, idle_seconds=60)
    manager.handlers = {claude: [1], gpt: [2]}
    manager.live = {claude.session_id: claude, gpt.session_id: gpt}
    manager.bind_current(claude)

    participants = {
        "anthropic": types.SimpleNamespace(generation=1),
        "openai": types.SimpleNamespace(generation=1),
    }
    native_ids = {
        "anthropic": claude.session_id,
        "openai": gpt.session_id,
    }
    selected_provider = ["anthropic"]
    selected_sessions: list[str] = []
    reveal_attempts: list[str] = []
    window = types.SimpleNamespace(
        _driver=claude,
        _driver_manager=manager,
        _next_chat=ChatTarget(project, claude.session_id, work_id),
        _main_stack=types.SimpleNamespace(
            get_visible_child_name=lambda: "transcript"
        ),
        _work_coordinator=types.SimpleNamespace(
            resume_id=lambda candidate_work, provider: (
                native_ids[provider] if candidate_work == work_id else ""
            ),
            participant=lambda candidate_work, provider: (
                participants[provider] if candidate_work == work_id else None
            ),
        ),
        _sessions=types.SimpleNamespace(
            reveal_session=lambda session_id: (
                reveal_attempts.append(session_id) or False
            )
        ),
        _selected_provider=lambda: selected_provider[0],
        _current_work_id=lambda: work_id,
        _work_is_executing=lambda _work_id: False,
        _ensure_target_work=lambda **_kwargs: work_id,
        _driver_matches_selected_provider=lambda driver: (
            driver.provider == selected_provider[0]
        ),
        _session_for_native_binding=lambda candidate_project, session_id: (
            MainWindow._session_for_native_binding(candidate_project, session_id)
        ),
        _show_fresh_chat_ui=lambda *_args, **_kwargs: pytest.fail(
            "a live Work sibling must not be rendered as a fresh chat"
        ),
    )

    def select_session(_list, session) -> None:
        driver = manager.driver_for_session(session.session_id)
        assert driver is not None
        assert driver.provider == selected_provider[0]
        manager.bind_current(driver)
        window._driver = driver
        window._next_chat = ChatTarget(session.project, session.session_id, work_id)
        selected_sessions.append(session.session_id)

    window._on_session_selected = select_session

    selected_provider[0] = "openai"
    MainWindow._stage_fresh_chat_for_provider_switch(window)
    assert manager.current is gpt
    assert window._next_chat.resume_id == "gpt-native"

    selected_provider[0] = "anthropic"
    MainWindow._stage_fresh_chat_for_provider_switch(window)
    assert manager.current is claude
    assert window._next_chat.resume_id == "claude-native"

    selected_provider[0] = "openai"
    MainWindow._stage_fresh_chat_for_provider_switch(window)
    assert manager.current is gpt
    assert window._next_chat.work_id == work_id
    assert selected_sessions == ["gpt-native", "claude-native", "gpt-native"]
    assert reveal_attempts == selected_sessions
    assert manager.live == {
        "claude-native": claude,
        "gpt-native": gpt,
    }


def _busy_switch_window(tmp_path, *, sibling: str):
    """An OpenRouter chat mid-turn in Work-1; the user toggles to Claude."""
    project = Project(dirname="-repo", cwd="/repo", path=tmp_path)
    running = _Driver("openrouter", "or-native", "work-1")
    if sibling:
        (tmp_path / f"{sibling}.jsonl").write_text("", encoding="utf-8")
        session_providers.set_provider(sibling, "anthropic")
    fresh: list[tuple] = []
    revealed: list[str] = []
    window = types.SimpleNamespace(
        _driver=running,
        _next_chat=ChatTarget(project, running.session_id, "work-1"),
        _main_stack=types.SimpleNamespace(
            get_visible_child_name=lambda: "transcript"
        ),
        _work_coordinator=types.SimpleNamespace(
            resume_id=lambda _work_id, provider: (
                sibling if provider == "anthropic" else running.session_id
            ),
            participant=lambda _work_id, _provider: types.SimpleNamespace(generation=1),
        ),
        _selected_provider=lambda: "anthropic",
        _current_work_id=lambda: "work-1",
        _work_is_executing=lambda work_id: work_id == "work-1",
        _ensure_target_work=lambda **_kwargs: pytest.fail(
            "a busy Work must not gain a second participant"
        ),
        _conversation_perms=types.SimpleNamespace(
            providers_for_session=lambda _session_id: frozenset(),
        ),
        _sessions=types.SimpleNamespace(
            reveal_session=lambda session_id: revealed.append(session_id) or True
        ),
        _show_fresh_chat_ui=lambda *args, **kwargs: fresh.append((args, kwargs)),
    )
    return window, project, fresh, revealed


def test_provider_toggle_on_a_busy_work_opens_a_fresh_chat_outside_it(tmp_path):
    """2026-09-16: toggling Claude while Kimi ran made Claude a second
    participant of the running Work, and every send then failed with
    'This Work is already executing'."""
    window, project, fresh, revealed = _busy_switch_window(tmp_path, sibling="")

    assert MainWindow._stage_fresh_chat_for_provider_switch(window) is False

    assert fresh == [((project,), {})]  # no work_id: a new Work on send
    assert revealed == []


def test_provider_toggle_on_a_busy_work_still_reveals_an_existing_sibling(tmp_path):
    window, _project, fresh, revealed = _busy_switch_window(
        tmp_path, sibling="claude-native"
    )
    window._ensure_target_work = lambda **_kwargs: "work-1"

    assert MainWindow._stage_fresh_chat_for_provider_switch(window) is True

    assert revealed == ["claude-native"]
    assert fresh == []


def test_apply_model_choice_names_the_provider_actually_left(monkeypatch):
    """The toast hardcoded GPT as the peer, so OpenRouter → Claude said
    'GPT remains available'."""
    toasts: list[str] = []
    ui_state: dict[str, object] = {}
    switch_result = [True]
    window = types.SimpleNamespace(
        _model="moonshotai/kimi-k3",
        _ui_state=types.SimpleNamespace(set=lambda key, value: ui_state.__setitem__(key, value)),
        _provider_models={},
        _chat_toolbar=types.SimpleNamespace(
            set_model=lambda _alias: None,
            set_context_model=lambda _alias: None,
        ),
        _sync_provider_toggle=lambda: None,
        _sync_assistant_labels=lambda: None,
        _sync_effort_sensitivity=lambda: None,
        _refresh_openrouter_credits=lambda: None,
        _stage_fresh_chat_for_provider_switch=lambda: switch_result[0],
        _current_work_id=lambda: "work-1",
        _toast=lambda text: toasts.append(text),
        _driver=None,
    )

    MainWindow._apply_model_choice(window, "opus")
    assert toasts == ["Claude is now leading this Work — OpenRouter remains available."]

    window._model = "opus"
    switch_result[0] = False
    MainWindow._apply_model_choice(window, "moonshotai/kimi-k3")
    assert toasts[-1] == (
        "New OpenRouter chat — the running Claude chat continues in the background."
    )


def test_failed_driver_activation_restores_composer_text(monkeypatch):
    scheduled: list[tuple[object, str]] = []
    text = [""]
    focus_count = [0]
    composer = types.SimpleNamespace(
        current_text=lambda: text[0],
        set_text=lambda value: text.__setitem__(0, value),
        grab_input_focus=lambda: focus_count.__setitem__(0, focus_count[0] + 1),
    )
    window = types.SimpleNamespace(
        _destroyed=False,
        _driver=None,
        _composer=composer,
        _execution_change_pending_for=lambda _driver: False,
        _restore_in_flight=False,
        _ensure_driver=lambda: False,
        _restore_blocked_execution_send=lambda value: (
            MainWindow._restore_blocked_execution_send(window, value)
        ),
    )
    monkeypatch.setattr(
        main_window_module.GLib,
        "idle_add",
        lambda callback, value: scheduled.append((callback, value)) or 1,
    )

    MainWindow._on_composer_send(window, composer, "keep this draft")

    assert len(scheduled) == 1
    callback, value = scheduled[0]
    # Composer clears after its synchronous send signal returns.
    assert callback(value) is False
    assert text == ["keep this draft"]
    assert focus_count == [1]


def test_unresolved_work_binding_is_rejected_before_work_mutation(tmp_path):
    project = Project(dirname="-repo", cwd="/repo", path=tmp_path)
    mutations: list[str] = []
    window = types.SimpleNamespace(
        _next_chat=ChatTarget(project, work_id="work-1"),
        _work_coordinator=types.SimpleNamespace(
            resume_id=lambda _work_id, _provider: "opaque-native-id",
        ),
        _conversation_perms=types.SimpleNamespace(
            providers_for_session=lambda _session_id: frozenset(),
        ),
        _selected_provider=lambda: "anthropic",
        _ensure_target_work=lambda **_kwargs: mutations.append("ensure") or "work-1",
    )

    assert MainWindow._stage_selected_work_participant(window) is False
    assert mutations == []
    assert window._next_chat == ChatTarget(project, work_id="work-1")


def test_read_only_pool_copy_never_reuses_matching_local_live_driver(tmp_path):
    session_id = "shared-native-id"
    local_driver = _Driver("anthropic", session_id, "work-local")
    local_driver._cwd = "/repo"
    manager = DriverManager(max_live=4, idle_seconds=60)
    manager.handlers = {local_driver: [1]}
    manager.live = {session_id: local_driver}
    manager.bind_current(local_driver)
    project = Project(
        dirname="-repo",
        cwd="/repo",
        path=tmp_path,
        origin="workstation",
        read_only=True,
    )
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text("", encoding="utf-8")
    session = Session(project, session_id, path, 0.0, 0)
    session_providers.set_provider(session_id, "anthropic")
    binds: list[object] = []
    read_only: list[tuple[bool, str]] = []
    window = types.SimpleNamespace(
        _driver=local_driver,
        _driver_manager=manager,
        _conversation_perms=types.SimpleNamespace(
            providers_for_session=lambda _session_id: frozenset(),
        ),
        _shared=types.SimpleNamespace(set_handoff_target=lambda _session: None),
        _staged_permission_mode="",
        _staged_effort_key="",
        _model="opus",
        _driver_provider=lambda driver: driver.provider,
        _driver_matches_selected_provider=lambda driver: driver.provider == "anthropic",
        _bind_visible_driver=lambda driver: binds.append(driver),
        _sync_assistant_labels=lambda: None,
        _stop_following=lambda: None,
        _start_following=lambda _session: None,
        _sync_execution_control=lambda: None,
        _context=types.SimpleNamespace(set_project=lambda _project: None),
        _chat_toolbar=types.SimpleNamespace(
            set_context_usage=lambda *_args: None,
            set_context_model=lambda _model: None,
            set_visible=lambda _visible: None,
        ),
        _load_context_fill_async=lambda _session: None,
        _transcript=types.SimpleNamespace(set_session=lambda _session: None),
        _refresh_goal_strip=lambda: None,
        _plan=types.SimpleNamespace(set_session=lambda _session: None),
        _drafts=DraftBook(),
        _draft_key="",
        _composer=types.SimpleNamespace(
            set_visible=lambda _visible: None,
            set_read_only=lambda value, note: read_only.append((value, note)),
            current_text=lambda: "",
            set_text=lambda _text: None,
        ),
        _main_stack=types.SimpleNamespace(
            set_visible_child_name=lambda _name: None,
        ),
        _selected_provider=lambda: "anthropic",
        _work_coordinator=None,
        _pump_questions=lambda: None,
    )

    MainWindow._on_session_selected(window, None, session)

    assert binds == [None]
    assert read_only and read_only[-1][0] is True
    assert "workstation" in read_only[-1][1]
    assert window._next_chat.project.read_only is True


def test_degraded_gpt_view_fails_closed_without_a_bounded_work(
    tmp_path,
    monkeypatch,
):
    project = Project(dirname="-repo", cwd="/repo", path=tmp_path)
    gpt_id = "gpt-viewed"
    session_providers.set_provider(gpt_id, "openai")
    target = ChatTarget(project=project, resume_id=gpt_id)
    claude_live = _Driver("anthropic", "claude-live", "")
    claude_live._cwd = "/repo"
    claude_live._helios_identity_confirmed = True
    calls: list[str] = []

    class _Manager:
        def driver_for_work_participant(self, *_args, **_kwargs):
            raise AssertionError("fail-closed path must not inspect a live sibling")

        def driver_for_session(self, _session_id):
            return None

    window = types.SimpleNamespace(
        _next_chat=target,
        _driver=None,
        _driver_manager=_Manager(),
        _conversation_perms=types.SimpleNamespace(
            providers_for_session=lambda _session_id: frozenset(),
        ),
        _work_coordinator=None,
        _selected_provider=lambda: "anthropic",
        _guard_openai_driver_activation=lambda: True,
        _can_activate_target_binding=lambda _target: True,
        _fence_mismatched_target_resume=lambda: False,
        _ensure_target_work=lambda: "",
        _sessions=types.SimpleNamespace(reveal_session=lambda _session_id: True),
    )
    # Bind the real fence to the complete harness after construction.
    window._fence_mismatched_target_resume = types.MethodType(
        MainWindow._fence_mismatched_target_resume,
        window,
    )
    monkeypatch.setattr(
        MainWindow,
        "_show_fresh_execution_binding",
        lambda _candidate, _viewed: calls.append("fresh"),
    )

    window._toast = lambda text: calls.append(f"toast:{text}")

    assert MainWindow._ensure_driver(window) is False
    assert calls == [
        "toast:Helios could not establish a bounded Work, so it did not "
        "send your message.",
    ]
    assert window._next_chat.resume_id == gpt_id
