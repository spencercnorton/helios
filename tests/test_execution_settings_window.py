"""MainWindow routing for current-conversation execution settings.

These tests deliberately exercise the unbound MainWindow methods with small
fakes.  The contract under test is conversation-local state: a blank chat is
staged until it receives a provider id, a resumable chat is keyed by
``(provider, native_session_id)``, and live provider acknowledgements are
tracked independently per driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from helios import main_window as main_window_module
from helios.backend.project_perms import reload_legacy_permissions
from helios.main_window import MainWindow


class _WorkspacePermStore:
    """Legacy workspace fallback probe; current-chat writes must not land here."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, cwd: str) -> str:
        return self.values.get(cwd, "")

    def set(self, cwd: str, mode: str) -> None:
        self.values[cwd] = mode


class _ConversationStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict[str, str]] = {}
        self.permission_calls: list[tuple[str, str, str, str | None]] = []
        self.effort_calls: list[tuple[str, str, str, str | None]] = []
        self.workflow_calls: list[tuple[str, str, str, str | None]] = []
        self.permission_save_ok = True
        self.effort_save_ok = True

    def get(self, provider: str, session_id: str, default: str = "") -> str:
        record = self.values.get((provider, session_id))
        return record["permission_mode"] if record is not None else default

    def get_effort(
        self,
        provider: str,
        session_id: str,
        default: str = "",
    ) -> str:
        record = self.values.get((provider, session_id))
        if record is None:
            return default
        return record.get("effort_key", "") or default

    def get_settings(self, provider: str, session_id: str):
        record = self.values.get((provider, session_id))
        return SimpleNamespace(**record) if record is not None else None

    def get_workflow(
        self,
        provider: str,
        session_id: str,
        default: str = "default",
    ) -> str:
        record = self.values.get((provider, session_id))
        return record.get("workflow_mode", "default") if record else default

    def providers_for_session(self, session_id: str) -> frozenset[str]:
        return frozenset(
            provider
            for provider, native_id in self.values
            if native_id == session_id
        )

    def set(
        self,
        provider: str,
        session_id: str,
        mode: str,
        *,
        effort_key: str | None = None,
        workflow_mode: str | None = None,
    ) -> bool:
        self.permission_calls.append((provider, session_id, mode, effort_key))
        if not self.permission_save_ok:
            return False
        current = self.values.get((provider, session_id), {})
        selected_effort = (
            current.get("effort_key", "") if effort_key is None else effort_key
        )
        record = {
            "permission_mode": mode,
            "effort_key": selected_effort,
        }
        selected_workflow = (
            current.get("workflow_mode", "default")
            if workflow_mode is None
            else workflow_mode
        )
        if selected_workflow != "default":
            record["workflow_mode"] = selected_workflow
        self.values[(provider, session_id)] = record
        return True

    def set_effort(
        self,
        provider: str,
        session_id: str,
        key: str,
        *,
        permission_mode: str | None = None,
    ) -> bool:
        self.effort_calls.append((provider, session_id, key, permission_mode))
        if not self.effort_save_ok:
            return False
        current = self.values.get((provider, session_id))
        mode = (
            current["permission_mode"]
            if current is not None
            else str(permission_mode or "")
        )
        if not mode:
            return False
        self.values[(provider, session_id)] = {
            "permission_mode": mode,
            "effort_key": key,
            **(
                {"workflow_mode": current["workflow_mode"]}
                if current is not None and "workflow_mode" in current
                else {}
            ),
        }
        return True

    def set_workflow(
        self,
        provider: str,
        session_id: str,
        mode: str,
        *,
        permission_mode: str | None = None,
    ) -> bool:
        self.workflow_calls.append((provider, session_id, mode, permission_mode))
        current = self.values.get((provider, session_id))
        selected_permission = (
            current["permission_mode"]
            if current is not None
            else str(permission_mode or "")
        )
        if not selected_permission:
            return False
        record = dict(current or {})
        record["permission_mode"] = selected_permission
        record.setdefault("effort_key", "")
        if mode == "default":
            record.pop("workflow_mode", None)
        else:
            record["workflow_mode"] = mode
        self.values[(provider, session_id)] = record
        return True


class _UiState:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value
        self.calls.append((key, value))


class _ToolbarProbe:
    def __init__(self) -> None:
        self.permission = ""
        self.workflow = ""
        self.workflow_options: tuple[str, ...] = ()
        self.effort = ""
        self.scope = ""
        self.scope_detail = ""
        self.sensitive: bool | None = None
        self.pending: bool | None = None
        self.pending_history: list[bool] = []
        self.effort_options: list[tuple[str, str]] = []
        self.effort_option_selections: list[str] = []
        self.effort_sensitive_history: list[bool] = []

    def set_permission_mode(self, mode: str) -> None:
        self.permission = mode

    def set_workflow_options(self, modes) -> str:
        self.workflow_options = tuple(modes)
        return self.workflow if self.workflow in self.workflow_options else "default"

    def set_workflow_mode(self, mode: str) -> None:
        self.workflow = mode

    def set_effort(self, effort: str) -> None:
        self.effort = effort

    def set_execution_scope(self, scope: str, detail: str = "") -> None:
        self.scope = scope
        self.scope_detail = detail

    def set_execution_sensitive(self, sensitive: bool) -> None:
        self.sensitive = sensitive

    def set_execution_pending(self, pending: bool) -> None:
        self.pending = pending
        self.pending_history.append(pending)

    def set_effort_options(
        self,
        efforts,
        *,
        default_effort: str = "",
        selected_effort: str = "",
    ) -> str:
        self.effort_options = list(efforts)
        self.effort_option_selections.append(selected_effort)
        keys = [key for key, _description in efforts]
        return selected_effort if selected_effort in keys else default_effort

    def set_anthropic_effort_options(self, selected_effort: str = "") -> str:
        self.effort_option_selections.append(selected_effort)
        return selected_effort or "high"

    def set_effort_sensitive(self, sensitive: bool) -> None:
        self.effort_sensitive_history.append(sensitive)


class _ComposerProbe:
    def __init__(self) -> None:
        self.text = ""
        self.focus_count = 0

    def current_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text

    def grab_input_focus(self) -> None:
        self.focus_count += 1


class _AsyncDriver:
    def __init__(
        self,
        *,
        provider: str = "anthropic",
        session_id: str = "session-1",
        permission_mode: str = "default",
        effort_key: str = "high",
        workflow_mode: str = "default",
    ) -> None:
        self.provider = provider
        self.session_id = session_id
        self.permission_mode = permission_mode
        self.effort_key = effort_key
        self.workflow_mode = workflow_mode
        self.supported_workflow_modes = ("default", "plan")
        self.is_accepting_input = True
        self.is_running = True
        self.is_busy = False
        self.supports_native_goals = False
        self.permission_request = None
        self.effort_request = None
        self.workflow_request = None
        self.permission_error: Exception | None = None
        self.effort_error: Exception | None = None
        self.permission_accepts = True
        self.effort_accepts = True
        self.workflow_accepts = True
        self.execution_restart_required = False

    def set_permission_mode(self, mode, callback):
        if self.permission_error is not None:
            raise self.permission_error
        self.permission_request = (mode, callback)
        return self.permission_accepts

    def set_effort(self, effort, callback):
        if self.effort_error is not None:
            raise self.effort_error
        self.effort_request = (effort, callback)
        return self.effort_accepts

    def set_workflow_mode(self, mode, callback):
        self.workflow_request = (mode, callback)
        return self.workflow_accepts


@dataclass
class _Harness:
    window: SimpleNamespace
    conversations: _ConversationStore
    workspace: _WorkspacePermStore
    toolbar: _ToolbarProbe
    ui_state: _UiState
    toasts: list[str]
    syncs: list[bool]


def _target(
    session_id: str = "",
    *,
    cwd: str = "/repo",
    read_only: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        resume_id=session_id,
        resume_provider="",
        work_id="",
        project=SimpleNamespace(cwd=cwd, read_only=read_only),
    )


def _window(
    driver=None,
    *,
    session_id: str = "",
    read_only: bool = False,
    selected_provider: str = "anthropic",
    target_provider: str | None = None,
    register_target: bool = True,
) -> _Harness:
    conversations = _ConversationStore()
    workspace = _WorkspacePermStore()
    toolbar = _ToolbarProbe()
    ui_state = _UiState()
    toasts: list[str] = []
    syncs: list[bool] = []
    if session_id and register_target:
        main_window_module.session_providers.set_provider(
            session_id,
            target_provider or selected_provider,
        )
    window = SimpleNamespace(
        _destroyed=False,
        # _on_composer_send consults the rewind mutex before anything else.
        _restore_in_flight=False,
        _driver=driver,
        _next_chat=_target(session_id, read_only=read_only),
        _perms=workspace,
        _permission_mode="default",
        _conversation_perms=conversations,
        _permission_changes={},
        _effort_changes={},
        _workflow_changes={},
        _staged_permission_mode="",
        _staged_effort_key="",
        _staged_workflow_mode="",
        _openai_workflow_modes=("default", "plan"),
        _provider_efforts={"anthropic": "high", "openai": "high"},
        _effort_key="high",
        _model=("gpt-5.6" if selected_provider == "openai" else "opus"),
        _catalog_entries_by_id=(
            {
                "gpt-5.6": SimpleNamespace(
                    reasoning_efforts=(("high", ""), ("xhigh", "")),
                    default_effort="high",
                )
            }
            if selected_provider == "openai"
            else {}
        ),
        _ui_state=ui_state,
        _chat_toolbar=toolbar,
        _selected_provider=lambda: selected_provider,
        _driver_provider=lambda candidate: candidate.provider,
        _toast=lambda text, **_kwargs: toasts.append(text),
    )
    window._driver_matches_selected_provider = lambda candidate: (
        candidate is window._driver
    )
    window._driver_matches_target_binding = lambda candidate: (
        not window._next_chat.resume_id
        or not candidate.session_id
        or candidate.session_id == window._next_chat.resume_id
    )
    window._current_project_cwd = lambda: window._next_chat.project.cwd
    window._effective_permission_mode = lambda cwd: workspace.get(cwd) or "default"

    def sync() -> None:
        syncs.append(True)
        MainWindow._sync_execution_control(window)

    window._sync_execution_control = sync
    window._execution_change_pending_for = lambda candidate: (
        MainWindow._execution_change_pending_for(
            window,
            candidate,
        )
    )
    window._restore_blocked_execution_send = lambda text: (
        MainWindow._restore_blocked_execution_send(window, text)
    )
    return _Harness(
        window,
        conversations,
        workspace,
        toolbar,
        ui_state,
        toasts,
        syncs,
    )


def test_fresh_permission_is_staged_until_provider_assigns_session_id():
    h = _window()

    MainWindow._on_toolbar_permission_changed(
        h.window,
        None,
        "acceptEdits",
    )

    assert h.window._staged_permission_mode == "acceptEdits"
    assert h.conversations.permission_calls == []
    assert h.workspace.values == {}
    assert h.toolbar.permission == "acceptEdits"
    # A successful change no longer toasts: the toolbar label above is the
    # confirmation, and one banner per change queued over the composer.
    assert h.toasts == []


def test_fresh_native_plan_is_staged_without_rewriting_permissions():
    h = _window(selected_provider="openai")

    MainWindow._on_toolbar_workflow_changed(h.window, None, "plan")

    assert h.window._staged_workflow_mode == "plan"
    assert h.window._staged_permission_mode == ""
    assert h.conversations.workflow_calls == []
    assert h.toolbar.workflow == "plan"


def test_staged_plan_is_never_silently_downgraded_before_live_capability_check():
    h = _window(selected_provider="openai")
    h.window._staged_workflow_mode = "plan"
    h.window._openai_workflow_modes = ("default",)

    assert MainWindow._workflow_for_spawn(
        h.window,
        h.window._next_chat,
        "openai",
    ) == "plan"


def test_unbound_resume_workflow_persists_by_provider_and_session():
    h = _window(session_id="thread-plan", selected_provider="openai")

    MainWindow._on_toolbar_workflow_changed(h.window, None, "plan")

    assert h.conversations.workflow_calls == [
        ("openai", "thread-plan", "plan", "default")
    ]
    assert h.conversations.get_workflow("openai", "thread-plan") == "plan"
    assert h.window._staged_workflow_mode == ""


def test_live_workflow_commits_only_after_provider_ack():
    driver = _AsyncDriver(provider="openai", session_id="thread-plan")
    h = _window(driver, session_id="thread-plan", selected_provider="openai")

    MainWindow._on_toolbar_workflow_changed(h.window, None, "plan")

    assert driver in h.window._workflow_changes
    mode, callback = driver.workflow_request
    assert mode == "plan"
    assert h.conversations.workflow_calls == []
    driver.workflow_mode = mode
    callback(True, "")

    assert h.window._workflow_changes == {}
    assert h.conversations.get_workflow("openai", "thread-plan") == "plan"
    assert h.toolbar.workflow == "plan"


def _ui_state(confirmed: bool) -> SimpleNamespace:
    return SimpleNamespace(
        get=lambda key, default=None: (
            confirmed if key == "permission_mode_confirmed" else default
        )
    )


def test_legacy_workspace_policy_is_disclosed_without_widening(tmp_path):
    path = tmp_path / "project-perms.json"
    path.write_text('{"/repo": "plan"}\n', encoding="utf-8")
    reload_legacy_permissions()
    # No confirmed global default → the legacy clamp still applies (un-migrated).
    window = SimpleNamespace(_permission_mode="auto", _ui_state=_ui_state(False))

    assert MainWindow._permission_fallback(window, "/repo") == ("plan", "legacy")
    assert MainWindow._effective_permission_mode(window, "/repo") == "plan"

    path.write_text('{"/repo": "bypassPermissions"}\n', encoding="utf-8")
    reload_legacy_permissions()

    assert MainWindow._permission_fallback(window, "/repo") == (
        "default",
        "legacy-bypass-retired",
    )


def test_confirmed_bypass_is_authoritative_but_unconfirmed_is_not(tmp_path):
    """Bypass is honored only as a current, explicit choice.

    Confirmed in Settings, it is authoritative and the retired workspace file
    must not narrow it. Unconfirmed, the legacy record still clamps it — an
    upgrade must never silently widen an un-migrated workspace to full access.
    """
    path = tmp_path / "project-perms.json"
    path.write_text('{"/repo": "dontAsk"}\n', encoding="utf-8")
    reload_legacy_permissions()

    confirmed = SimpleNamespace(
        _permission_mode="bypassPermissions", _ui_state=_ui_state(True)
    )
    assert MainWindow._permission_fallback(confirmed, "/repo") == (
        "bypassPermissions",
        "default",
    )
    assert (
        MainWindow._effective_permission_mode(confirmed, "/repo")
        == "bypassPermissions"
    )

    # Unconfirmed still respects the legacy clamp — no silent widening.
    unconfirmed = SimpleNamespace(
        _permission_mode="bypassPermissions", _ui_state=_ui_state(False)
    )
    assert MainWindow._permission_fallback(unconfirmed, "/repo") == (
        "dontAsk",
        "legacy",
    )


def test_confirmed_bypass_is_still_plan_only_in_home(tmp_path):
    """HOME outranks even a confirmed global Bypass."""
    from helios.backend import project_perms

    reload_legacy_permissions()
    confirmed = SimpleNamespace(
        _permission_mode="bypassPermissions", _ui_state=_ui_state(True)
    )
    assert MainWindow._permission_fallback(
        confirmed, project_perms.PROTECTED_HOME_CWD
    ) == ("plan", "home-plan")


def test_unbound_resume_permission_persists_by_provider_and_session(
):
    h = _window(session_id="thread-7", selected_provider="openai")
    h.window._staged_permission_mode = "stale-fresh-choice"

    MainWindow._on_toolbar_permission_changed(h.window, None, "plan")

    assert h.conversations.permission_calls == [("openai", "thread-7", "plan", None)]
    assert h.conversations.get("openai", "thread-7") == "plan"
    assert h.window._staged_permission_mode == ""
    assert h.workspace.values == {}
    assert h.toolbar.permission == "plan"
    # A successful change no longer toasts: the toolbar label above is the
    # confirmation, and one banner per change queued over the composer.
    assert h.toasts == []


def test_live_permission_commits_only_after_provider_ack():
    driver = _AsyncDriver(provider="openai", session_id="thread-live")
    h = _window(driver, session_id="thread-live", selected_provider="openai")

    MainWindow._on_toolbar_permission_changed(h.window, None, "auto")

    assert driver in h.window._permission_changes
    assert h.toolbar.pending is True
    assert h.conversations.permission_calls == []
    mode, callback = driver.permission_request
    assert mode == "auto"

    # Provider drivers update their effective property before acknowledging.
    driver.permission_mode = mode
    callback(True, "")

    assert h.window._permission_changes == {}
    assert h.toolbar.pending is False
    assert h.conversations.permission_calls == [("openai", "thread-live", "auto", None)]
    assert h.conversations.get("openai", "thread-live") == "auto"
    assert h.workspace.values == {}
    # A successful change no longer toasts: the toolbar label above is the
    # confirmation, and one banner per change queued over the composer.
    assert h.toasts == []


def test_live_permission_failure_reverts_without_persisting():
    driver = _AsyncDriver()
    h = _window(driver, session_id=driver.session_id)

    MainWindow._on_toolbar_permission_changed(
        h.window,
        None,
        "auto",
    )
    _mode, callback = driver.permission_request
    callback(False, "mode disabled by policy")

    assert h.window._permission_changes == {}
    assert h.conversations.permission_calls == []
    assert h.toolbar.permission == "default"
    assert h.toolbar.pending is False
    assert h.toasts == ["Could not change permissions: mode disabled by policy"]


@pytest.mark.parametrize("setting", ["permission", "effort"])
def test_ambiguous_live_change_tears_down_exact_driver_without_persisting(setting):
    driver = _AsyncDriver()
    h = _window(driver, session_id=driver.session_id)
    torn_down: list[object] = []
    h.window._teardown_driver = lambda candidate: torn_down.append(candidate)

    if setting == "permission":
        MainWindow._on_toolbar_permission_changed(h.window, None, "auto")
        callback = driver.permission_request[1]
    else:
        MainWindow._on_toolbar_effort_changed(h.window, None, "xhigh")
        callback = driver.effort_request[1]
    driver.execution_restart_required = True
    callback(False, "process state could not be verified")

    assert torn_down == [driver]
    assert h.conversations.permission_calls == []
    assert h.conversations.effort_calls == []


def test_live_effort_success_persists_only_after_ack():
    driver = _AsyncDriver(provider="openai", session_id="thread-effort")
    h = _window(driver, session_id="thread-effort", selected_provider="openai")

    MainWindow._on_toolbar_effort_changed(h.window, None, "xhigh")

    assert driver in h.window._effort_changes
    assert h.toolbar.pending is True
    assert h.conversations.effort_calls == []
    key, callback = driver.effort_request
    assert key == "xhigh"

    driver.effort_key = key
    callback(True, "")

    assert h.window._effort_changes == {}
    assert h.toolbar.pending is False
    assert h.window._provider_efforts["openai"] == "high"
    assert h.window._effort_key == "high"
    assert h.ui_state.calls == []
    assert h.conversations.effort_calls == [
        ("openai", "thread-effort", "xhigh", "default")
    ]
    # A successful change no longer toasts: the toolbar label above is the
    # confirmation, and one banner per change queued over the composer.
    assert h.toasts == []


def test_live_effort_failure_leaves_sticky_and_durable_state_unchanged():
    driver = _AsyncDriver(provider="openai", session_id="thread-effort")
    h = _window(driver, session_id="thread-effort", selected_provider="openai")

    MainWindow._on_toolbar_effort_changed(h.window, None, "xhigh")
    _key, callback = driver.effort_request
    callback(False, "unsupported effort")

    assert h.window._effort_changes == {}
    assert h.window._provider_efforts["openai"] == "high"
    assert h.window._effort_key == "high"
    assert h.ui_state.calls == []
    assert h.conversations.effort_calls == []
    assert h.toolbar.effort == "high"
    assert h.toolbar.pending is False
    assert h.toasts == ["Could not change reasoning: unsupported effort"]


def test_unbound_resume_effort_is_saved_and_clears_fresh_staging():
    h = _window(session_id="session-resume")
    h.window._staged_effort_key = "low"

    MainWindow._on_toolbar_effort_changed(h.window, None, "medium")

    assert h.conversations.effort_calls == [
        ("anthropic", "session-resume", "medium", "default")
    ]
    assert h.conversations.get_effort("anthropic", "session-resume") == ("medium")
    assert h.window._staged_effort_key == ""
    assert h.toolbar.effort == "medium"


def test_stopped_gpt_ultra_is_staged_for_one_spawn_without_sticky_save():
    h = _window(
        session_id="thread-stopped",
        selected_provider="openai",
        target_provider="openai",
    )
    h.window._catalog_entries_by_id["gpt-5.6"].reasoning_efforts = (
        ("high", ""),
        ("ultra", ""),
    )

    MainWindow._on_toolbar_effort_changed(h.window, None, "ultra")

    assert h.window._staged_effort_key == "ultra"
    assert h.conversations.effort_calls == []
    effort, source = MainWindow._execution_effort_for_spawn(
        h.window,
        h.window._next_chat,
        "openai",
    )
    assert (effort, source) == ("ultra", "Staged")
    assert h.toolbar.effort == "ultra"
    assert h.toasts == [
        "Reasoning set to Ultra for this conversation (one session only)."
    ]


def test_gpt_model_default_ultra_is_narrowed_without_explicit_staging():
    h = _window(
        session_id="thread-stopped",
        selected_provider="openai",
        target_provider="openai",
    )
    h.window._catalog_entries_by_id["gpt-5.6"].reasoning_efforts = (
        ("high", ""),
        ("ultra", ""),
    )
    h.window._catalog_entries_by_id["gpt-5.6"].default_effort = "ultra"

    effort, source = MainWindow._execution_effort_for_spawn(
        h.window,
        h.window._next_chat,
        "openai",
    )

    assert (effort, source) == ("high", "Helios standard")


def test_stopped_gpt_non_ultra_effort_remains_durable():
    h = _window(
        session_id="thread-stopped",
        selected_provider="openai",
        target_provider="openai",
    )

    MainWindow._on_toolbar_effort_changed(h.window, None, "xhigh")

    assert h.window._staged_effort_key == ""
    assert h.conversations.effort_calls == [
        ("openai", "thread-stopped", "xhigh", "default")
    ]


def test_two_drivers_keep_pending_execution_changes_independent():
    first = _AsyncDriver(session_id="session-one")
    second = _AsyncDriver(session_id="session-two")
    h = _window(first, session_id=first.session_id)

    MainWindow._on_toolbar_permission_changed(h.window, None, "auto")
    assert MainWindow._execution_change_pending_for(h.window, first)
    assert not MainWindow._execution_change_pending_for(h.window, second)

    h.window._driver = second
    h.window._next_chat = _target(second.session_id)
    assert main_window_module.session_providers.set_provider(
        second.session_id,
        "anthropic",
    )
    MainWindow._on_toolbar_effort_changed(h.window, None, "xhigh")

    assert MainWindow._execution_change_pending_for(h.window, first)
    assert MainWindow._execution_change_pending_for(h.window, second)
    assert h.toolbar.pending is True

    first.permission_mode = "auto"
    first.permission_request[1](True, "")
    assert not MainWindow._execution_change_pending_for(h.window, first)
    assert MainWindow._execution_change_pending_for(h.window, second)
    assert h.toolbar.pending is True

    second.effort_key = "xhigh"
    second.effort_request[1](True, "")
    assert not MainWindow._execution_change_pending_for(h.window, second)
    assert h.toolbar.pending is False


@pytest.mark.parametrize("setting", ["permission", "effort"])
def test_setter_exception_clears_that_drivers_pending_state(setting):
    driver = _AsyncDriver()
    h = _window(driver, session_id=driver.session_id)
    if setting == "permission":
        driver.permission_error = RuntimeError("permission transport broke")
        MainWindow._on_toolbar_permission_changed(h.window, None, "plan")
        expected = "Could not change permissions: permission transport broke"
    else:
        driver.effort_error = RuntimeError("effort transport broke")
        MainWindow._on_toolbar_effort_changed(h.window, None, "xhigh")
        expected = "Could not change reasoning: effort transport broke"

    assert not MainWindow._execution_change_pending_for(h.window, driver)
    assert h.window._permission_changes == {}
    assert h.window._effort_changes == {}
    assert h.toolbar.pending is False
    assert h.conversations.permission_calls == []
    assert h.conversations.effort_calls == []
    assert h.toasts == [expected]


def test_read_only_conversation_rejects_permission_and_effort_mutations():
    driver = _AsyncDriver(session_id="remote-session")
    h = _window(
        driver,
        session_id=driver.session_id,
        read_only=True,
    )

    MainWindow._on_toolbar_permission_changed(h.window, None, "auto")
    MainWindow._on_toolbar_effort_changed(h.window, None, "xhigh")

    assert driver.permission_request is None
    assert driver.effort_request is None
    assert h.conversations.permission_calls == []
    assert h.conversations.effort_calls == []
    assert h.ui_state.calls == []
    assert h.window._staged_permission_mode == ""
    assert h.window._staged_effort_key == ""
    assert h.toolbar.scope == "Read-only · Claude"
    assert h.toolbar.sensitive is False
    assert h.toolbar.pending is False
    assert h.toasts == []


@pytest.mark.parametrize(
    ("conflict", "scope"),
    [
        (False, "Unknown provider · view only"),
        (True, "Conflict · view only"),
    ],
)
def test_unresolved_provider_locks_execution_without_staging_or_saving(
    conflict,
    scope,
):
    h = _window(
        session_id="opaque-native-id",
        register_target=conflict,
        target_provider="anthropic",
    )
    if conflict:
        h.conversations.set("openai", "opaque-native-id", "plan")

    MainWindow._sync_execution_control(h.window)
    MainWindow._on_toolbar_permission_changed(h.window, None, "auto")
    MainWindow._on_toolbar_effort_changed(h.window, None, "max")

    assert h.toolbar.scope == scope
    assert h.toolbar.sensitive is False
    assert h.window._staged_permission_mode == ""
    assert h.window._staged_effort_key == ""
    assert h.conversations.permission_calls == (
        [("openai", "opaque-native-id", "plan", None)] if conflict else []
    )
    assert h.conversations.effort_calls == []


def test_unresolved_resume_is_blocked_before_driver_reuse_or_work_mutation():
    h = _window(
        session_id="opaque-native-id",
        register_target=False,
    )
    original = h.window._next_chat
    h.window._next_chat.project.origin = "local"
    h.window._guard_openai_driver_activation = lambda: pytest.fail(
        "model activation must not run for an unresolved native id"
    )
    h.window._stage_selected_work_participant = lambda: pytest.fail(
        "Work routing must not mutate an unresolved target"
    )

    assert MainWindow._ensure_driver(h.window) is False
    assert h.window._next_chat is original
    assert h.toasts == [
        "Provider ownership is unknown or conflicted, so Helios will not "
        "guess how to resume this conversation."
    ]


@pytest.mark.parametrize(
    ("candidate_provider", "expected_reason"),
    [
        ("", "unknown"),
        ("openai", "unknown"),
    ],
)
def test_selected_work_executor_is_locked_when_candidate_is_unverified_or_wrong(
    candidate_provider,
    expected_reason,
):
    h = _window(session_id="viewed-claude")
    h.window._next_chat.work_id = "work-1"
    candidate = "candidate-native"
    if candidate_provider:
        assert main_window_module.session_providers.set_provider(
            candidate,
            candidate_provider,
        )
    h.window._work_coordinator = SimpleNamespace(
        resume_id=lambda _work_id, _provider: candidate,
    )

    assert MainWindow._execution_target_lock_reason(h.window) == expected_reason

    MainWindow._sync_execution_control(h.window)
    assert h.toolbar.scope == "Unknown provider · view only"
    assert h.toolbar.sensitive is False


def test_selected_work_executor_is_locked_when_coordinator_lookup_fails():
    h = _window(session_id="viewed-claude")
    h.window._next_chat.work_id = "work-1"
    h.window._work_coordinator = SimpleNamespace(
        resume_id=lambda *_args: (_ for _ in ()).throw(RuntimeError("broken")),
    )

    assert MainWindow._execution_target_lock_reason(h.window) == "unknown"


def test_saved_value_equal_to_default_is_still_labeled_saved():
    h = _window(session_id="saved-default")
    h.conversations.set("anthropic", "saved-default", "default")

    MainWindow._sync_execution_control(h.window)

    assert h.toolbar.scope == "Mixed · Claude"
    assert h.toolbar.scope_detail == "Permissions: Saved; Reasoning: Global default"


def test_permission_and_reasoning_share_saved_provenance_when_both_persisted():
    h = _window(session_id="saved-both")
    h.conversations.set(
        "anthropic",
        "saved-both",
        "default",
        effort_key="high",
    )

    MainWindow._sync_execution_control(h.window)

    assert h.toolbar.scope == "Saved · Claude"
    assert h.toolbar.scope_detail == "Permissions: Saved; Reasoning: Saved"


def test_unsupported_saved_effort_reports_effective_global_fallback():
    h = _window(session_id="saved-unsupported")
    h.conversations.set(
        "anthropic",
        "saved-unsupported",
        "default",
        effort_key="future-effort",
    )

    MainWindow._sync_execution_control(h.window)

    assert h.toolbar.effort == "high"
    assert h.toolbar.scope == "Mixed · Claude"
    assert h.toolbar.scope_detail == (
        "Permissions: Saved; Reasoning: Global default"
    )


def test_unsupported_staged_effort_reports_effective_global_fallback():
    h = _window()
    h.window._staged_effort_key = "future-effort"

    MainWindow._sync_execution_control(h.window)

    assert h.toolbar.effort == "high"
    assert h.toolbar.scope == "Global default · Claude"
    assert h.toolbar.scope_detail == (
        "Permissions: Global default; Reasoning: Global default"
    )


@pytest.mark.parametrize(
    ("legacy_mode", "scope"),
    [
        ("plan", "Mixed · Claude"),
        ("bypassPermissions", "Mixed · Claude"),
    ],
)
def test_legacy_fallback_is_visibly_disclosed(tmp_path, legacy_mode, scope):
    (tmp_path / "project-perms.json").write_text(
        f'{{"/repo": "{legacy_mode}"}}\n',
        encoding="utf-8",
    )
    reload_legacy_permissions()
    h = _window()
    h.window._permission_fallback = lambda cwd: MainWindow._permission_fallback(
        h.window,
        cwd,
    )

    MainWindow._sync_execution_control(h.window)

    assert h.toolbar.scope == scope
    expected_permission = (
        "Legacy retired"
        if legacy_mode == "bypassPermissions"
        else "Legacy safeguard"
    )
    assert h.toolbar.scope_detail == (
        f"Permissions: {expected_permission}; Reasoning: Global default"
    )


def test_malformed_legacy_fallback_is_visible_and_plan_only(tmp_path):
    (tmp_path / "project-perms.json").write_text("not-json\n", encoding="utf-8")
    reload_legacy_permissions()
    h = _window()
    h.window._permission_fallback = lambda cwd: MainWindow._permission_fallback(
        h.window,
        cwd,
    )

    MainWindow._sync_execution_control(h.window)

    assert h.toolbar.permission == "plan"
    assert h.toolbar.scope == "Mixed · Claude"
    assert h.toolbar.scope_detail == (
        "Permissions: Legacy invalid; Reasoning: Global default"
    )


def test_sync_uses_live_conversation_settings_over_durable_fallbacks():
    driver = _AsyncDriver(
        session_id="session-live",
        permission_mode="bypassPermissions",
        effort_key="xhigh",
    )
    h = _window(driver, session_id=driver.session_id)
    h.workspace.values["/repo"] = "plan"
    h.conversations.set("anthropic", driver.session_id, "dontAsk")
    h.conversations.set_effort(
        "anthropic",
        driver.session_id,
        "low",
    )

    MainWindow._sync_execution_control(h.window)

    assert h.toolbar.permission == "bypassPermissions"
    assert h.toolbar.effort == "xhigh"
    assert h.toolbar.scope == "Live · Claude"
    assert h.toolbar.sensitive is True
    assert h.toolbar.pending is False


@pytest.mark.parametrize(
    ("provider", "shown"),
    [("openai", "bypassPermissions"), ("openrouter", "bypassPermissions"), ("anthropic", "bypassPermissions")],
)
def test_sync_shows_the_mode_the_provider_will_actually_run(provider, shown):
    """The displayed permission matches each provider's native mode."""
    h = _window(session_id="", selected_provider=provider)
    h.window._model = {"openai": "gpt-5.6", "openrouter": "moonshotai/kimi-k3"}.get(provider, "opus")
    h.window._effective_permission_mode = lambda _cwd: "bypassPermissions"

    MainWindow._sync_execution_control(h.window)

    assert h.toolbar.permission == shown


def test_composer_send_is_blocked_and_text_restored_while_change_pending(
    monkeypatch,
):
    driver = _AsyncDriver()
    h = _window(driver, session_id=driver.session_id)
    composer = _ComposerProbe()
    h.window._composer = composer
    h.window._permission_changes[driver] = object()
    h.window._ensure_driver = lambda: pytest.fail(
        "send must not reach driver creation while execution settings are pending"
    )
    scheduled: list[tuple[object, str]] = []

    def idle_add(callback, text):
        scheduled.append((callback, text))
        return 1

    monkeypatch.setattr(main_window_module.GLib, "idle_add", idle_add)

    MainWindow._on_composer_send(h.window, composer, "keep this message")

    assert h.toasts == [
        "Execution settings are still applying — your message was kept."
    ]
    assert len(scheduled) == 1
    callback, text = scheduled[0]
    # Gtk clears the composer after its synchronous send signal returns.
    composer.text = ""
    assert callback(text) is False
    assert composer.text == "keep this message"
    assert composer.focus_count == 1


@pytest.mark.parametrize("conflict", [False, True])
def test_busy_send_rechecks_provider_identity_before_queueing(monkeypatch, conflict):
    driver = _AsyncDriver(session_id="busy-native")
    driver.is_busy = True
    queued: list[str] = []
    driver.queue_user_text = lambda text: queued.append(text) or 1
    h = _window(driver, session_id=driver.session_id)
    if conflict:
        h.conversations.set("openai", driver.session_id, "plan")
    else:
        assert main_window_module.session_providers.forget(driver.session_id)
    composer = _ComposerProbe()
    h.window._composer = composer
    h.window._transcript = SimpleNamespace(
        append_queued=lambda *_args: pytest.fail(
            "an unresolved target must never gain a queued row"
        )
    )
    h.window._ensure_driver = lambda: pytest.fail(
        "the busy fast path must fail closed before activation"
    )
    scheduled: list[tuple[object, str]] = []
    monkeypatch.setattr(
        main_window_module.GLib,
        "idle_add",
        lambda callback, text: scheduled.append((callback, text)) or 1,
    )

    MainWindow._on_composer_send(h.window, composer, "keep this busy draft")

    assert queued == []
    assert len(scheduled) == 1
    callback, text = scheduled[0]
    assert callback(text) is False
    assert composer.text == "keep this busy draft"
    assert h.toasts == [
        "The selected execution target changed or could not be verified — "
        "your message was kept."
    ]


def test_busy_send_rechecks_exact_target_binding_before_queueing(monkeypatch):
    driver = _AsyncDriver(session_id="busy-native")
    driver.is_busy = True
    queued: list[str] = []
    driver.queue_user_text = lambda text: queued.append(text) or 1
    h = _window(driver, session_id=driver.session_id)
    h.window._driver_matches_target_binding = lambda _candidate: False
    composer = _ComposerProbe()
    h.window._composer = composer
    scheduled: list[tuple[object, str]] = []
    monkeypatch.setattr(
        main_window_module.GLib,
        "idle_add",
        lambda callback, text: scheduled.append((callback, text)) or 1,
    )

    MainWindow._on_composer_send(h.window, composer, "binding changed")

    assert queued == []
    assert len(scheduled) == 1
    callback, text = scheduled[0]
    assert callback(text) is False
    assert composer.text == "binding changed"


def test_busy_send_waits_for_startup_identity_before_queueing(monkeypatch):
    driver = _AsyncDriver(session_id="")
    driver._helios_identity_confirmed = False
    driver.is_busy = True
    queued: list[str] = []
    driver.queue_user_text = lambda text: queued.append(text) or 1
    h = _window(driver)
    composer = _ComposerProbe()
    h.window._composer = composer
    scheduled: list[tuple[object, str]] = []
    monkeypatch.setattr(
        main_window_module.GLib,
        "idle_add",
        lambda callback, text: scheduled.append((callback, text)) or 1,
    )

    MainWindow._on_composer_send(h.window, composer, "wait for identity")

    assert queued == []
    assert len(scheduled) == 1
    callback, text = scheduled[0]
    assert callback(text) is False
    assert composer.text == "wait for identity"
    assert h.toasts == [
        "Helios is still verifying the native conversation — your message was kept."
    ]


def test_identity_rejection_delegates_ordered_recovery_to_teardown():
    driver = _AsyncDriver(provider="openai", session_id="")
    driver._helios_identity_confirmed = False
    order: list[str] = []
    window = SimpleNamespace(
        _driver_manager=SimpleNamespace(),
        _driver_provider=lambda candidate: candidate.provider,
        _drv_is_current=lambda candidate: candidate is driver,
        _identity_pending_user={id(driver): (driver, "first")},
        _teardown_driver=lambda candidate: order.append(
            f"teardown:{candidate is driver}"
        ),
        _toast=lambda *_args, **_kwargs: order.append("toast"),
    )

    MainWindow._reject_started_identity(window, driver, "mismatch")

    assert order == ["teardown:True", "toast"]


def test_session_started_persists_provider_native_execution_settings():
    conversations = _ConversationStore()
    registered: list[tuple[object, str]] = []
    driver = _AsyncDriver(
        provider="openai",
        session_id="",
        permission_mode="auto",
        effort_key="xhigh",
    )
    manager = SimpleNamespace(
        starting=object(),
        register_started=lambda candidate, session_id: registered.append(
            (candidate, session_id)
        ),
    )
    window = SimpleNamespace(
        _driver_manager=manager,
        _work_coordinator=None,
        _conversation_perms=conversations,
        _driver_provider=lambda candidate: candidate.provider,
        _drv_is_current=lambda _candidate: False,
        _bind_pending_goal_to_session=lambda *_args: pytest.fail(
            "a background session must not bind the visible pending goal"
        ),
    )

    MainWindow._on_session_started(
        window,
        driver,
        "thread-native",
        "/repo",
        "gpt-5.6",
    )

    assert registered == [(driver, "thread-native")]
    assert driver._helios_identity_confirmed is True
    assert conversations.permission_calls == [
        ("openai", "thread-native", "auto", "xhigh")
    ]
    assert conversations.values[("openai", "thread-native")] == {
        "permission_mode": "auto",
        "effort_key": "xhigh",
    }


@pytest.mark.parametrize("driver_provider", ["", "local-model"])
def test_session_started_rejects_invalid_runtime_provider(driver_provider):
    conversations = _ConversationStore()
    stopped: list[object] = []
    driver = _AsyncDriver(provider=driver_provider, session_id="")
    manager = SimpleNamespace(
        starting=driver,
        stop_driver=lambda candidate: stopped.append(candidate),
    )
    window = SimpleNamespace(
        _driver_manager=manager,
        _work_coordinator=None,
        _conversation_perms=conversations,
        _driver_provider=lambda candidate: candidate.provider,
    )

    MainWindow._on_session_started(
        window,
        driver,
        "opaque-runtime-id",
        "/repo",
        "model",
    )

    assert stopped == [driver]
    assert conversations.permission_calls == []


def test_session_started_rejects_conflicting_existing_provider_identity():
    main_window_module.session_providers.set_provider("collision", "openai")
    conversations = _ConversationStore()
    stopped: list[object] = []
    driver = _AsyncDriver(provider="anthropic", session_id="")
    manager = SimpleNamespace(
        starting=driver,
        stop_driver=lambda candidate: stopped.append(candidate),
    )
    window = SimpleNamespace(
        _driver_manager=manager,
        _work_coordinator=None,
        _conversation_perms=conversations,
        _driver_provider=lambda candidate: candidate.provider,
        _drv_is_current=lambda _candidate: False,
    )

    MainWindow._on_session_started(
        window,
        driver,
        "collision",
        "/repo",
        "claude",
    )

    assert stopped == [driver]
    assert conversations.permission_calls == []


@pytest.mark.parametrize(
    ("reported_id", "expected_id"),
    [
        ("", ""),
        ("different-native-id", "requested-native-id"),
    ],
)
def test_session_started_tears_down_blank_or_mismatched_runtime_identity(
    reported_id,
    expected_id,
):
    conversations = _ConversationStore()
    driver = _AsyncDriver(provider="openai", session_id="")
    driver._helios_expected_resume_id = expected_id
    stopped: list[bool] = []
    driver.stop = lambda *, interrupt=True: stopped.append(interrupt)
    manager = main_window_module.DriverManager(max_live=2, idle_seconds=60)
    manager.add_starting(driver, [])
    toasts: list[str] = []
    window = SimpleNamespace(
        _driver_manager=manager,
        _work_coordinator=None,
        _conversation_perms=conversations,
        _driver_provider=lambda candidate: candidate.provider,
        _drv_is_current=manager.is_current,
        _toast=lambda text, **_kwargs: toasts.append(text),
    )

    MainWindow._on_session_started(
        window,
        driver,
        reported_id,
        "/repo",
        "gpt-5.6",
    )

    assert driver._helios_identity_rejected is True
    assert manager.current is None
    assert manager.starting is None
    assert stopped == [False]
    assert conversations.permission_calls == []
    assert toasts == [
        "Helios rejected an unsafe provider identity and kept your message unsent."
    ]


def test_session_started_rejects_native_identity_when_work_bind_fails():
    conversations = _ConversationStore()
    driver = _AsyncDriver(provider="openai", session_id="")
    driver._helios_work_id = "work-1"
    stopped: list[bool] = []
    driver.stop = lambda *, interrupt=True: stopped.append(interrupt)
    manager = main_window_module.DriverManager(max_live=2, idle_seconds=60)
    manager.add_starting(driver, [])

    def fail_bind(*_args, **_kwargs):
        raise RuntimeError("work database unavailable")

    window = SimpleNamespace(
        _driver_manager=manager,
        _work_coordinator=SimpleNamespace(
            participant=lambda *_args: None,
            bind_participant=fail_bind,
        ),
        _conversation_perms=conversations,
        _driver_provider=lambda candidate: candidate.provider,
        _drv_is_current=manager.is_current,
        _toast=lambda *_args, **_kwargs: None,
    )

    MainWindow._on_session_started(
        window,
        driver,
        "thread-native",
        "/repo",
        "gpt-5.6",
    )

    assert driver._helios_identity_rejected is True
    assert manager.current is None
    assert manager.starting is None
    assert stopped == [False]
    assert conversations.permission_calls == []


def test_session_started_cannot_revive_detached_participant_generation():
    conversations = _ConversationStore()
    driver = _AsyncDriver(provider="openai", session_id="")
    driver._helios_work_id = "work-detached"
    driver._helios_participant_id = "participant-1"
    driver._helios_participant_generation = 1
    stopped: list[bool] = []
    driver.stop = lambda *, interrupt=True: stopped.append(interrupt)
    manager = main_window_module.DriverManager(max_live=2, idle_seconds=60)
    manager.add_starting(driver, [])
    current = SimpleNamespace(
        participant_id="participant-1",
        generation=2,
        native_id="",
    )
    coordinator = SimpleNamespace(
        participant=lambda *_args: current,
        bind_participant=lambda *_args, **_kwargs: pytest.fail(
            "stale runtime must not revive a detached binding"
        ),
    )
    window = SimpleNamespace(
        _driver_manager=manager,
        _work_coordinator=coordinator,
        _conversation_perms=conversations,
        _driver_provider=lambda candidate: candidate.provider,
        _drv_is_current=manager.is_current,
        _toast=lambda *_args, **_kwargs: None,
    )

    MainWindow._on_session_started(
        window,
        driver,
        "thread-retired",
        "/repo",
        "gpt-5.6",
    )

    assert driver._helios_identity_rejected is True
    assert manager.current is None
    assert manager.starting is None
    assert stopped == [False]
    assert conversations.permission_calls == []


def test_session_started_surfaces_current_conversation_persistence_failure(
    monkeypatch,
):
    conversations = _ConversationStore()
    conversations.permission_save_ok = False
    driver = _AsyncDriver(
        provider="openai",
        session_id="",
        permission_mode="auto",
        effort_key="xhigh",
    )
    manager = SimpleNamespace(starting=driver)

    def register_started(candidate, session_id):
        candidate.session_id = session_id
        manager.starting = None

    manager.register_started = register_started
    toasts: list[str] = []
    window = SimpleNamespace(
        _destroyed=False,
        # _on_composer_send consults the rewind mutex before anything else.
        _restore_in_flight=False,
        _driver_manager=manager,
        _work_coordinator=None,
        _conversation_perms=conversations,
        _driver_provider=lambda candidate: candidate.provider,
        _drv_is_current=lambda candidate: candidate is driver,
        _bind_pending_goal_to_session=lambda *_args: None,
        _next_chat=main_window_module.ChatTarget(
            project=SimpleNamespace(cwd="/repo", read_only=False)
        ),
        _staged_permission_mode="auto",
        _staged_effort_key="xhigh",
        _sync_effort_sensitivity=lambda: None,
        _sync_execution_control=lambda: None,
        _chat_toolbar=SimpleNamespace(set_context_model=lambda _model: None),
        _refresh_sidebar_for_live=lambda *_args: False,
        _toast=lambda text, **_kwargs: toasts.append(text),
    )
    monkeypatch.setattr(main_window_module.GLib, "timeout_add", lambda *_args: 1)

    MainWindow._on_session_started(
        window,
        driver,
        "thread-native",
        "/repo",
        "gpt-5.6",
    )

    assert toasts == [
        "Execution settings are active, but could not be saved for restart."
    ]
    assert driver._helios_execution_persistence_failed is False


def test_confirmed_bypass_record_survives_provider_owned_resume():
    """An explicit Bypass choice is restored for its actual provider."""
    h = _window(session_id="session-a")
    h.workspace.values["/repo"] = "default"
    for provider, expected in (
        ("openai", "bypassPermissions"),
        ("openrouter", "bypassPermissions"),
    ):
        session_id = f"session-{provider}"
        main_window_module.session_providers.set_provider(session_id, provider)
        h.conversations.set(
            provider,
            session_id,
            "bypassPermissions",
            effort_key="max",
        )
        mode, _effort = MainWindow._execution_settings_for_spawn(
            h.window,
            _target(session_id),
            provider,
        )
        assert mode == expected


def test_reaped_or_restarted_conversations_spawn_with_their_own_settings():
    h = _window(session_id="session-a")
    h.workspace.values["/repo"] = "default"
    h.conversations.set(
        "anthropic",
        "session-a",
        "plan",
        effort_key="low",
    )
    h.conversations.set(
        "anthropic",
        "session-b",
        "bypassPermissions",
        effort_key="max",
    )

    assert MainWindow._execution_settings_for_spawn(
        h.window,
        _target("session-a"),
        "anthropic",
    ) == ("plan", "low")
    assert MainWindow._execution_settings_for_spawn(
        h.window,
        _target("session-b"),
        "anthropic",
    ) == ("bypassPermissions", "max")

    # A still-unsaved current composer has no provider id yet, so its explicit
    # staging wins until session-started binds that choice durably.
    h.window._staged_permission_mode = "dontAsk"
    h.window._staged_effort_key = "high"
    assert MainWindow._execution_settings_for_spawn(
        h.window,
        _target(""),
        "anthropic",
    ) == ("dontAsk", "high")


def test_spawn_rejects_stale_effort_not_supported_by_selected_model():
    h = _window(session_id="thread", selected_provider="openai")
    h.window._model = "gpt-current"
    h.window._catalog_entries_by_id = {
        "gpt-current": SimpleNamespace(
            reasoning_efforts=(("medium", ""), ("high", "")),
            default_effort="high",
        )
    }
    h.conversations.set(
        "openai",
        "thread",
        "default",
        effort_key="retired-effort",
    )

    assert MainWindow._execution_settings_for_spawn(
        h.window,
        _target("thread"),
        "openai",
    ) == ("default", "high")


def test_catalog_refresh_keeps_live_conversation_effort_authoritative():
    driver = _AsyncDriver(
        provider="openai",
        session_id="thread-live",
        effort_key="xhigh",
    )
    driver.model = "gpt-5.6"
    h = _window(driver=driver, session_id="thread-live", selected_provider="openai")
    h.window._provider_efforts["openai"] = "high"

    MainWindow._sync_effort_sensitivity(h.window)

    assert h.toolbar.effort_option_selections == ["xhigh"]
    assert h.toolbar.effort == "xhigh"
    assert h.window._provider_efforts["openai"] == "high"
    assert h.ui_state.calls == []


def test_catalog_refresh_keeps_stored_conversation_effort_authoritative():
    h = _window(session_id="thread-stored", selected_provider="openai")
    h.window._provider_efforts["openai"] = "high"
    h.conversations.set(
        "openai",
        "thread-stored",
        "auto",
        effort_key="xhigh",
    )

    MainWindow._sync_effort_sensitivity(h.window)

    assert h.toolbar.effort_option_selections == ["xhigh"]
    assert h.toolbar.effort == "xhigh"
    assert h.window._provider_efforts["openai"] == "high"
    assert h.ui_state.calls == []


def test_catalog_refresh_keeps_staged_conversation_effort_authoritative():
    h = _window(selected_provider="openai")
    h.window._provider_efforts["openai"] = "high"
    h.window._staged_effort_key = "xhigh"

    MainWindow._sync_effort_sensitivity(h.window)

    assert h.toolbar.effort_option_selections == ["xhigh"]
    assert h.toolbar.effort == "xhigh"
    assert h.window._provider_efforts["openai"] == "high"
    assert h.ui_state.calls == []


def test_provider_mismatch_stages_settings_for_the_executing_participant(
):
    h = _window(
        session_id="thread-gpt",
        selected_provider="anthropic",
        target_provider="openai",
    )
    h.window._next_chat.work_id = "work-1"
    h.window._work_coordinator = None
    h.workspace.values["/repo"] = "bypassPermissions"
    h.window._effective_permission_mode = lambda cwd: (
        MainWindow._effective_permission_mode(h.window, cwd)
    )
    h.conversations.set(
        "openai",
        "thread-gpt",
        "default",
        effort_key="xhigh",
    )

    MainWindow._sync_execution_control(h.window)
    assert h.toolbar.permission == "default"
    assert h.toolbar.effort == "high"
    assert h.toolbar.scope == "Global · Claude · view GPT"

    MainWindow._on_toolbar_permission_changed(h.window, None, "plan")
    MainWindow._on_toolbar_effort_changed(h.window, None, "max")

    assert h.window._staged_permission_mode == "plan"
    assert h.window._staged_effort_key == "max"
    assert h.toolbar.scope == "Staged · Claude · view GPT"
    assert h.conversations.get("openai", "thread-gpt") == "default"
    assert h.conversations.get_effort("openai", "thread-gpt") == "xhigh"
    assert h.conversations.get("anthropic", "thread-gpt") == ""
    assert MainWindow._execution_settings_for_spawn(
        h.window,
        _target(""),
        "anthropic",
    ) == ("plan", "max")


# --- fresh-chat default cwd (the HOME/read-only collision) ------------------


def _proj(cwd: str, *, read_only: bool = False, mtime: float = 0.0):
    from helios.backend.projects import Project
    from pathlib import Path

    return Project(
        dirname=cwd.replace("/", "-"),
        cwd=cwd,
        path=Path(cwd),
        last_modified=mtime,
        read_only=read_only,
    )


def test_fresh_chat_default_skips_a_deleted_directory(monkeypatch, tmp_path):
    """~/.claude/projects/ outlives the cwd it describes, so a deleted scratch
    dir keeps appearing as the newest project. Seeding a chat there sets a cwd
    that is gone and the spawn fails — the v0.58.0 regression that pointed every
    fresh chat at a deleted ~/Documents/temp."""
    real = tmp_path / "real-project"
    real.mkdir()
    gone = tmp_path / "deleted-temp"  # never created
    stale_file = tmp_path / "a-file"
    stale_file.write_text("not a directory")

    # tmp_path lives under /tmp, which is_throwaway_cwd rejects. Neutralise
    # that filter so this test exercises only the existence check; the
    # throwaway rule has its own test below.
    monkeypatch.setattr(main_window_module, "is_throwaway_cwd", lambda _cwd: False)
    monkeypatch.setattr(
        main_window_module,
        "discover_projects",
        lambda: [
            _proj(str(gone), mtime=900.0),        # newest, but deleted
            _proj(str(stale_file), mtime=800.0),  # exists, but not a dir
            _proj(str(real), mtime=700.0),        # the real answer
        ],
    )

    chosen = main_window_module._default_chat_project()

    assert chosen is not None
    assert chosen.cwd == str(real)


def test_fresh_chat_default_prefers_a_real_project_over_throwaways(monkeypatch):
    """$HOME is read-only, so it must not be the default a fresh chat lands in.

    Regression guard for the v0.55.0 collision: $HOME was the seeded catch-all
    AND newly clamped to Plan, so every fresh chat was silently read-only.
    """
    home = main_window_module.HOME_CWD
    monkeypatch.setattr(main_window_module.os.path, "isdir", lambda _p: True)
    monkeypatch.setattr(
        main_window_module,
        "discover_projects",
        lambda: [
            _proj(home, mtime=500.0),  # newest, but read-only
            _proj("/tmp/scratch", mtime=400.0),  # throwaway
            _proj("/home/alice/Kleos/_tmp_abc_repo", mtime=300.0),  # throwaway
            _proj("/home/alice/remote-thing", read_only=True, mtime=200.0),
            _proj("/home/alice/helios", mtime=100.0),  # the real answer
        ],
    )

    chosen = main_window_module._default_chat_project()

    assert chosen is not None
    assert chosen.cwd == "/home/alice/helios"


def test_fresh_chat_default_takes_a_throwaway_over_plan_only_home(monkeypatch):
    """A writable scratch dir beats a policy-blocked one. Excluding throwaways
    outright sent a throwaway-only profile back to read-only $HOME — the exact
    bug the default change existed to fix."""
    home = main_window_module.HOME_CWD
    monkeypatch.setattr(main_window_module.os.path, "isdir", lambda _p: True)
    monkeypatch.setattr(
        main_window_module,
        "discover_projects",
        lambda: [
            _proj(home, mtime=500.0),
            _proj("/tmp/scratch", mtime=400.0),
        ],
    )

    chosen = main_window_module._default_chat_project()

    assert chosen is not None
    assert chosen.cwd == "/tmp/scratch"


def test_fresh_chat_default_falls_back_to_home_on_a_bare_profile(monkeypatch):
    """A profile with nothing executable still has to be able to chat."""
    monkeypatch.setattr(main_window_module, "discover_projects", lambda: [])

    assert main_window_module._default_chat_project() is None


# --- proactive rate-limit warnings (the real ceiling) -----------------------


def _rate_window():
    """Minimal shell for the GTK-free warning logic."""
    return SimpleNamespace(_destroyed=False, _toast=lambda msg: toasts.append(msg))


toasts: list[str] = []


def test_rate_limit_warns_once_per_band_not_per_event():
    """On a subscription this is the only ceiling that actually stops work, and
    it used to be disclosed only passively in the context popover."""
    toasts.clear()
    w = _rate_window()

    for used in (81, 84, 88):
        MainWindow._warn_on_rate_limit(w, {"rateLimitType": "weekly", "usedPercent": used})
    assert len(toasts) == 1, toasts
    assert "81%" in toasts[0]

    # Crossing into the higher band warns again, once.
    for used in (96, 97):
        MainWindow._warn_on_rate_limit(w, {"rateLimitType": "weekly", "usedPercent": used})
    assert len(toasts) == 2, toasts
    assert "96%" in toasts[1]


def test_rate_limit_below_the_first_band_is_silent():
    toasts.clear()
    w = _rate_window()

    MainWindow._warn_on_rate_limit(w, {"rateLimitType": "weekly", "usedPercent": 40})

    assert toasts == []


def test_blocking_rate_limit_status_always_warns():
    """A blocking status must never be silent, whatever the percentage says."""
    toasts.clear()
    w = _rate_window()

    MainWindow._warn_on_rate_limit(
        w, {"rateLimitType": "five_hour", "status": "blocked"})

    assert len(toasts) == 1
    assert "refusing turns" in toasts[0]


def test_separate_limit_types_warn_independently():
    toasts.clear()
    w = _rate_window()

    MainWindow._warn_on_rate_limit(w, {"rateLimitType": "weekly", "usedPercent": 96})
    MainWindow._warn_on_rate_limit(w, {"rateLimitType": "five_hour", "usedPercent": 96})

    assert len(toasts) == 2


def test_rate_limit_without_a_type_is_ignored():
    toasts.clear()
    w = _rate_window()

    MainWindow._warn_on_rate_limit(w, {"usedPercent": 99})
    MainWindow._warn_on_rate_limit(w, {"rateLimitType": "weekly", "usedPercent": "n/a"})

    assert toasts == []
