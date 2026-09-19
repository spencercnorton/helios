from __future__ import annotations

from concurrent.futures import Future
import threading
from types import SimpleNamespace
import time

import pytest

pytest.importorskip("gi")

from gi.repository import GLib  # noqa: E402

from helios.backend.agent_activity import (  # noqa: E402
    AgentActivityModel,
    AgentActivityScope,
    AgentObservedStatus,
)
from helios.backend.process import codex_app_events as events  # noqa: E402
from helios.backend.process import codex_app_driver  # noqa: E402
from helios.backend.process.codex_app_driver import (  # noqa: E402
    CODEX_BUDGET_INTERRUPT_DEADLINE_MS,
    CODEX_STANDARD_TOKEN_BUDGET,
    NATIVE_DELIVERY_ACCEPTED,
    NATIVE_DELIVERY_IDLE,
    NATIVE_DELIVERY_REJECTED,
    NATIVE_DELIVERY_UNKNOWN,
    NATIVE_DELIVERY_WIRE,
    CodexAppServerDriver,
)
from helios.backend.process.codex_app_server import (  # noqa: E402
    CodexAppServerExited,
    CodexAppServerRpcError,
    CodexAppServerTimeout,
)
from helios.backend.project_perms import PROTECTED_HOME_CWD  # noqa: E402
from helios.backend.process.message_queue import (  # noqa: E402
    PreparedPrompt,
    RequiredPromptContextError,
)


class FakeMirror:
    def __init__(self):
        self.bound = []
        self.users = []
        self.assistant = []
        self.compactions = []

    def bind_thread(self, thread_id):
        self.bound.append(thread_id)

    def note_user_text(self, text):
        self.users.append(text)

    def append_assistant(self, turn, *, model="", result=None, turn_id=""):
        self.assistant.append((turn, model, result, turn_id))

    def append_compaction(self, **details):
        self.compactions.append(details)


class FakeHub:
    def __init__(
        self,
        *,
        acquire_error=None,
        bind_error=None,
        thread_id="thread-native",
        collaboration_modes=("default", "plan"),
    ):
        self.acquire_error = acquire_error
        self.bind_error = bind_error
        self.thread_id = thread_id
        self.collaboration_modes = collaboration_modes
        self.acquired = []
        self.bound = []
        self.calls = []
        self.requests = []
        self.released = []
        self.aborts = []
        self.request_results = {}

    def acquire(self, client, binary=None):
        if self.acquire_error:
            raise self.acquire_error
        self.acquired.append((client, binary))
        return self

    def bind_thread(self, client, thread_id):
        if self.bind_error:
            raise self.bind_error
        self.bound.append((client, thread_id))

    def call(self, method, params=None, *, timeout=None):
        self.calls.append((method, params, timeout))
        if method == "collaborationMode/list":
            return {"data": [{"name": mode.title(), "mode": mode} for mode in self.collaboration_modes]}
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": self.thread_id}}
        return {}

    def request(self, method, params=None, *, callback=None, timeout=None):
        self.requests.append((method, params, timeout))
        result = self.request_results.get(method)
        if isinstance(result, Future):
            future = result
        else:
            future = Future()
        if isinstance(result, BaseException):
            future.set_exception(result)
        elif result is not None and not isinstance(result, Future):
            future.set_result(result)
        elif not isinstance(result, Future) and method == "turn/start":
            future.set_result({"turn": {"id": "turn-native"}})
        elif not isinstance(result, Future):
            future.set_result({})
        if callback is not None:
            future.add_done_callback(callback)
        return future

    def release(self, client):
        self.released.append(client)

    def abort_transport(self, *, returncode=-1):
        self.aborts.append(returncode)


class FakeRequest:
    def __init__(self, request_id, method, params):
        self.id = request_id
        self.method = method
        self.params = params
        self.responses = []
        self.errors = []
        self.abandoned = 0
        self._claimed = False

    def respond(self, result=None):
        if self._claimed:
            return False
        self._claimed = True
        self.responses.append(result)
        return True

    def respond_error(self, code, message, data=None):
        if self._claimed:
            return False
        self._claimed = True
        self.errors.append((code, message, data))
        return True

    def abandon(self):
        if self._claimed:
            return False
        self._claimed = True
        self.abandoned += 1
        return True


def _drain_main_context():
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def _start(driver, timeout=2.0):
    driver.start()
    deadline = time.monotonic() + timeout
    while driver.native_starting and time.monotonic() < deadline:
        _drain_main_context()
        time.sleep(0.005)
    _drain_main_context()
    assert not driver.native_starting


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _capture_glib_timeouts(monkeypatch):
    timers = []
    removed = []

    def timeout_add(interval, callback, *args):
        source_id = len(timers) + 1
        timers.append((source_id, interval, callback, args))
        return source_id

    monkeypatch.setattr(codex_app_driver.GLib, "timeout_add", timeout_add)
    monkeypatch.setattr(
        codex_app_driver.GLib,
        "source_remove",
        lambda source_id: removed.append(source_id) or True,
    )
    return timers, removed


@pytest.fixture
def driver_factory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "helios.backend.codex_env.find_codex_binary",
        lambda: SimpleNamespace(path="/fake/codex"),
    )
    monkeypatch.setattr(
        "helios.backend.codex_env.fetch_auth_status",
        lambda: SimpleNamespace(logged_in=True),
    )
    monkeypatch.setattr(
        "helios.backend.codex_context.inject_shared_context",
        lambda _cwd, text: text,
    )

    # Pinned rather than resolved so this file keeps testing the per-token
    # billing semantics it was written for; the billing-gated default has its
    # own tests below.
    def make(
        hub,
        *,
        mode="auto",
        workflow="default",
        resume="",
        cwd=None,
        token_budget=CODEX_STANDARD_TOKEN_BUDGET,
    ):
        driver = CodexAppServerDriver(
            cwd=str(cwd or tmp_path),
            model="gpt-5.6",
            permission_mode=mode,
            workflow_mode=workflow,
            resume_session_id=resume,
            effort="high",
            hub=hub,
            token_budget=token_budget,
        )
        driver._mirror = FakeMirror()
        driver._test_execution_events = []
        attempt_seq = iter(range(1, 10_000))

        def admit(_driver):
            attempt_id = f"attempt-{next(attempt_seq)}"
            driver._test_execution_events.append(("admit", attempt_id))
            return attempt_id, ""

        def finish(_driver, attempt_id, status, reason):
            driver._test_execution_events.append(
                ("finish", attempt_id, status, reason)
            )

        driver.set_execution_attempt_controller(
            admit,
            finish,
            record_dispatch=lambda *_args: None,
            record_acceptance=lambda *_args: None,
            record_stop=lambda *_args: None,
            record_contribution=lambda *_args: True,
        )
        return driver

    return make


def test_start_binds_fresh_native_thread_with_permission_profile(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    started = []
    driver.connect(
        "session-started", lambda _d, sid, cwd, model: started.append((sid, cwd, model))
    )

    _start(driver)

    method, params, _timeout = next(
        row for row in hub.calls if row[0] == "thread/start"
    )
    assert method == "thread/start"
    assert params["approvalPolicy"] == "on-request"
    assert params["approvalsReviewer"] == "user"
    assert params["sandbox"] == "workspace-write"
    assert hub.bound[0][1] == "thread-native"
    assert driver.session_id == "thread-native"
    assert driver.model == "gpt-5.6"
    assert driver.native_transport
    assert started[0][0] == "thread-native"
    driver.stop(interrupt=False)


def test_resume_uses_native_thread_resume(driver_factory):
    hub = FakeHub(thread_id="existing")
    driver = driver_factory(hub, resume="existing")
    _start(driver)

    resume_call = next(row for row in hub.calls if row[0] == "thread/resume")
    assert resume_call[1]["threadId"] == "existing"
    driver.stop(interrupt=False)


def test_native_plan_is_capability_verified_and_forces_read_only(driver_factory):
    hub = FakeHub(collaboration_modes=("default", "plan"))
    driver = driver_factory(hub, mode="auto", workflow="plan")

    _start(driver)
    thread_params = next(row[1] for row in hub.calls if row[0] == "thread/start")
    assert thread_params["sandbox"] == "read-only"
    assert thread_params["config"]["tools"]["update_plan"]["enabled"] is True
    assert thread_params["config"]["suppress_unstable_features_warning"] is True

    driver.send_user_text("inspect and propose a plan")
    turn_params = next(row[1] for row in hub.requests if row[0] == "turn/start")
    assert turn_params["collaborationMode"]["mode"] == "plan"
    assert turn_params["collaborationMode"]["settings"]["developer_instructions"] is None
    assert turn_params["additionalContext"]["helios-policy"]["kind"] == "application"
    assert turn_params["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": False,
    }
    driver.stop(interrupt=False)


def test_unadvertised_native_plan_fails_closed_before_thread_open(driver_factory):
    hub = FakeHub(collaboration_modes=("default",))
    driver = driver_factory(hub, workflow="plan")
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))

    _start(driver)

    assert not driver.native_transport
    assert not any(method.startswith("thread/") for method, _params, _timeout in hub.calls)
    assert hub.released == [driver]
    assert errors and "does not advertise" in errors[0]


def test_live_workflow_change_applies_to_next_turn_on_same_thread(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    original_thread = driver.session_id
    callbacks = []

    assert driver.set_workflow_mode(
        "plan",
        lambda success, detail: callbacks.append((success, detail)),
    )
    driver.send_user_text("plan it")
    turn_params = next(row[1] for row in hub.requests if row[0] == "turn/start")

    assert driver.session_id == original_thread
    assert driver.workflow_mode == "plan"
    assert turn_params["collaborationMode"]["mode"] == "plan"
    assert callbacks == [(True, "")]
    driver.stop(interrupt=False)


def test_unknown_live_workflow_is_rejected_without_changing_state(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    callbacks = []

    assert not driver.set_workflow_mode(
        "parallel",
        lambda success, detail: callbacks.append((success, detail)),
    )
    assert driver.workflow_mode == "default"
    assert callbacks == [(False, "unknown workflow mode")]
    driver.stop(interrupt=False)


def test_stop_during_pre_acquire_start_is_terminal(driver_factory):
    class BlockingHub(FakeHub):
        def __init__(self):
            super().__init__()
            self.acquire_entered = threading.Event()
            self.acquire_release = threading.Event()

        def acquire(self, client, binary=None):
            self.acquire_entered.set()
            assert self.acquire_release.wait(timeout=2)
            return super().acquire(client, binary=binary)

    hub = BlockingHub()
    driver = driver_factory(hub)
    exited = []
    driver.connect("exited", lambda _driver, code: exited.append(code))

    driver.start()
    assert hub.acquire_entered.wait(timeout=2)
    driver.stop(interrupt=True)
    hub.abort_transport(returncode=-1)
    hub.acquire_release.set()
    assert _wait_until(lambda: bool(hub.released))
    _drain_main_context()

    assert exited == [0]
    assert driver._closed is True
    assert driver.native_starting is False
    assert driver.native_transport is False
    assert not any(method.startswith("thread/") for method, _params, _timeout in hub.calls)
    assert hub.bound == []


def test_buffered_startup_prompt_rechecks_execution_guard(driver_factory):
    class BlockingHub(FakeHub):
        def __init__(self):
            super().__init__()
            self.acquire_entered = threading.Event()
            self.acquire_release = threading.Event()

        def acquire(self, client, binary=None):
            self.acquire_entered.set()
            assert self.acquire_release.wait(timeout=2)
            return super().acquire(client, binary=binary)

    hub = BlockingHub()
    driver = driver_factory(hub)
    block = {"reason": ""}
    errors = []
    driver.set_execution_guard(lambda _driver: block["reason"])
    driver.connect("error", lambda _driver, message: errors.append(message))

    driver.start()
    assert hub.acquire_entered.wait(timeout=2)
    driver.send_user_text("do not dispatch after breaker")
    assert driver._startup_pending_text
    block["reason"] = "This Work reached its execution budget."
    hub.acquire_release.set()
    assert _wait_until(lambda: bool(hub.calls))
    deadline = time.monotonic() + 2
    while driver.native_starting and time.monotonic() < deadline:
        _drain_main_context()
        time.sleep(0.005)
    _drain_main_context()

    assert not [row for row in hub.requests if row[0] == "turn/start"]
    assert errors == ["This Work reached its execution budget."]
    assert driver.is_busy is False
    assert driver.execution_attempt_id == ""
    assert driver._test_execution_events[1][:3] == (
        "finish",
        "attempt-1",
        "aborted",
    )
    driver.stop(interrupt=False)


def test_bind_collision_releases_acquired_hub_client(driver_factory):
    hub = FakeHub(bind_error=RuntimeError("already bound"))
    driver = driver_factory(hub, resume="existing")

    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    assert hub.released == [driver]
    assert not driver.native_transport
    assert "already active" in errors[0]


def test_send_marks_context_and_mirror_only_after_turn_acceptance(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    acknowledgements = []
    from helios.backend.process.message_queue import PreparedPrompt

    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt(
            f"work:{text}", lambda: acknowledgements.append(True)
        )
    )
    accepted = []
    driver.connect("prompt-accepted", lambda _driver, text: accepted.append(text))
    driver.start()
    driver.send_user_text("hello")
    assert driver._test_execution_events == [("admit", "attempt-1")]
    _start_wait_deadline = time.monotonic() + 2.0
    while driver.native_starting and time.monotonic() < _start_wait_deadline:
        _drain_main_context()
        time.sleep(0.005)
    _drain_main_context()

    turn_request = next(row for row in hub.requests if row[0] == "turn/start")
    assert turn_request[1]["input"] == [{"type": "text", "text": "work:hello"}]
    assert turn_request[1]["clientUserMessageId"] == "attempt-1"
    assert turn_request[1]["effort"] == "high"
    assert turn_request[1]["approvalPolicy"] == "on-request"
    assert turn_request[1]["approvalsReviewer"] == "user"
    # Fixture mode is Auto, which carries egress.
    assert turn_request[1]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(driver._cwd)],
        "networkAccess": True,
    }
    assert acknowledgements == [True]
    assert driver._mirror.users == ["hello"]
    assert accepted == ["hello"]
    assert driver.is_busy
    driver.stop(interrupt=False)


def test_required_work_context_failure_never_reaches_app_server(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    driver.set_prompt_context_provider(
        lambda _driver, _text: (_ for _ in ()).throw(
            RequiredPromptContextError()
        )
    )
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    outcome = driver.send_user_text("must remain local")

    assert outcome.rejected
    assert not any(method == "turn/start" for method, _params, _timeout in hub.requests)
    assert errors == [RequiredPromptContextError.user_message]
    assert driver.execution_attempt_id == ""
    driver.stop(interrupt=False)


def test_native_execution_changes_apply_to_same_thread_next_turn(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    original_thread = driver.session_id
    callbacks = []

    assert driver.set_permission_mode(
        "plan",
        lambda success, detail: callbacks.append(("permission", success, detail)),
    )
    assert driver.set_effort(
        "low", lambda success, detail: callbacks.append(("effort", success, detail))
    )
    assert driver.permission_mode == "plan"
    assert driver.effort_key == "low"
    assert driver.session_id == original_thread

    driver.send_user_text("inspect only")
    turn_request = next(row for row in hub.requests if row[0] == "turn/start")

    assert turn_request[1]["threadId"] == original_thread
    assert turn_request[1]["effort"] == "low"
    assert turn_request[1]["approvalPolicy"] == "never"
    assert turn_request[1]["approvalsReviewer"] == "user"
    assert turn_request[1]["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": False,
    }
    assert callbacks == [
        ("permission", True, ""),
        ("effort", True, ""),
    ]
    driver.stop(interrupt=False)


def test_native_execution_changes_reject_invalid_but_accept_mid_turn(driver_factory):
    """A running turn must not lock the toolbar. Both fields are read when the
    NEXT turn/start is built, so accepting the change while busy is exactly
    "apply at the earliest opportunity" and cannot disturb the live turn."""
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    callbacks = []

    assert not driver.set_permission_mode(
        "future", lambda success, detail: callbacks.append((success, detail))
    )
    driver.send_user_text("working")
    assert driver.is_busy
    turn_starts = len([row for row in hub.requests if row[0] == "turn/start"])

    assert driver.set_permission_mode(
        "plan", lambda success, detail: callbacks.append((success, detail))
    )
    assert driver.set_effort(
        "low", lambda success, detail: callbacks.append((success, detail))
    )

    assert [success for success, _detail in callbacks] == [False, True, True]
    assert driver.permission_mode == "plan"
    assert driver.effort_key == "low"
    # Staged only: the live turn was never re-sent or interrupted.
    assert len([row for row in hub.requests if row[0] == "turn/start"]) == turn_starts
    assert not any(row[0] == "turn/interrupt" for row in hub.requests)
    driver.stop(interrupt=False)


@pytest.mark.parametrize("mode", ["auto", "bypassPermissions"])
def test_native_execution_change_cannot_widen_home_past_plan(driver_factory, mode):
    driver = driver_factory(
        FakeHub(),
        mode="plan",
        cwd=PROTECTED_HOME_CWD,
    )
    callbacks = []
    _start(driver)

    assert not driver.set_permission_mode(
        mode,
        lambda success, detail: callbacks.append((success, detail)),
    )
    assert driver.permission_mode == "plan"
    assert callbacks == [(False, "HOME is locked to read-only permissions")]
    driver.stop(interrupt=False)


def test_start_does_not_block_caller_while_native_thread_opens(driver_factory):
    hub = FakeHub()
    release = threading.Event()
    original_call = hub.call

    def blocking_call(method, params=None, *, timeout=None):
        release.wait(1.0)
        return original_call(method, params, timeout=timeout)

    hub.call = blocking_call
    driver = driver_factory(hub)

    started_at = time.monotonic()
    driver.start()
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.1
    assert driver.native_starting
    release.set()
    deadline = time.monotonic() + 2.0
    while driver.native_starting and time.monotonic() < deadline:
        _drain_main_context()
        time.sleep(0.005)
    assert driver.native_transport
    driver.stop(interrupt=False)


def test_send_before_native_start_never_spawns_exec_fallback(driver_factory):
    driver = driver_factory(FakeHub(), mode="plan")
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))

    driver.send_user_text("must not leave through exec")

    assert not driver.is_busy
    assert not driver.native_transport
    assert driver._proc is None
    assert errors == [
        "Codex App Server is not connected. Exec fallback is disabled "
        "because it cannot enforce this Work's containment contract."
    ]


def test_stop_during_async_start_cancels_locally_buffered_first_prompt(
    driver_factory,
):
    hub = FakeHub()
    release = threading.Event()
    original_call = hub.call

    def blocking_call(method, params=None, *, timeout=None):
        release.wait(1.0)
        return original_call(method, params, timeout=timeout)

    hub.call = blocking_call
    driver = driver_factory(hub)
    driver.start()
    driver.send_user_text("not accepted yet")
    assert driver.is_busy

    driver.stop()
    assert not driver.is_busy
    assert driver._startup_pending_text is None
    release.set()
    deadline = time.monotonic() + 2.0
    while driver.native_starting and time.monotonic() < deadline:
        _drain_main_context()
        time.sleep(0.005)
    _drain_main_context()

    assert not any(method == "turn/start" for method, _params, _timeout in hub.requests)
    driver.stop(interrupt=False)


def test_identity_rejection_aborts_post_signal_prompt_dispatch(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    driver._startup_pending_text = "must remain unsent"

    def reject(candidate, _session_id, _cwd, _model):
        candidate._helios_identity_rejected = True
        candidate.stop(interrupt=False)

    driver.connect("session-started", reject)

    assert driver._finish_native_start(hub, "unsafe-thread") is False

    assert driver._closed is True
    assert hub.released == [driver]
    assert not any(method == "turn/start" for method, _params, _timeout in hub.requests)


def test_turn_start_timeout_closes_without_acknowledging_or_replaying(
    driver_factory,
):
    hub = FakeHub()
    hub.request_results["turn/start"] = CodexAppServerTimeout(
        "timed out waiting for turn/start"
    )
    driver = driver_factory(hub)
    acknowledgements = []
    accepted = []
    errors = []
    from helios.backend.process.message_queue import PreparedPrompt

    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt(
            text,
            lambda: acknowledgements.append(text),
        )
    )
    driver.connect("prompt-accepted", lambda _driver, text: accepted.append(text))
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    driver.send_user_text("do not replay")
    _drain_main_context()

    assert driver._closed
    assert driver.execution_attempt_id == "attempt-1"
    assert driver._test_execution_events == [("admit", "attempt-1")]
    assert acknowledgements == []
    assert accepted == []
    assert driver._mirror.users == []
    assert hub.released == [driver]
    assert "acceptance is unknown" in errors[-1]


@pytest.mark.parametrize("source", ["direct", "queued"])
def test_turn_start_timeout_pins_unknown_for_direct_and_queued_delivery(
    driver_factory,
    source,
):
    hub = FakeHub()
    hub.request_results["turn/start"] = CodexAppServerTimeout("turn/start timeout")
    driver = driver_factory(hub)
    _start(driver)

    qid = None
    if source == "direct":
        driver.send_user_text("FIRST")
    else:
        qid = driver.queue_user_text("FIRST")
        driver._flush_user_queue()

    # FakeHub resolves inline, so the transport-thread classifier has already
    # run even though the GTK completion callback has not.
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_UNKNOWN
    if qid is not None:
        assert driver._pending_queue_delivery == (qid, "FIRST")
        assert driver.queued_messages() == [(qid, "FIRST")]

    _drain_main_context()

    assert driver._closed
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_UNKNOWN
    if qid is not None:
        assert driver._pending_queue_delivery == (qid, "FIRST")
        assert driver.queued_messages() == [(qid, "FIRST")]


@pytest.mark.parametrize("source", ["direct", "queued"])
@pytest.mark.parametrize("outcome", ["accepted", "rejected"])
@pytest.mark.parametrize("exit_first", [False, True])
def test_turn_start_outcome_cannot_be_downgraded_by_app_exit(
    driver_factory,
    source,
    outcome,
    exit_first,
):
    pending: Future = Future()
    hub = FakeHub()
    hub.request_results["turn/start"] = pending
    driver = driver_factory(hub)
    _start(driver)

    if source == "direct":
        driver.send_user_text("FIRST")
    else:
        driver.queue_user_text("FIRST")
        driver._flush_user_queue()
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_WIRE

    if exit_first:
        driver.on_app_exit(9)
        assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_UNKNOWN
    if outcome == "accepted":
        pending.set_result({"turn": {"id": "turn-authoritative"}})
        expected = NATIVE_DELIVERY_ACCEPTED
    else:
        pending.set_exception(CodexAppServerRpcError("definite rejection"))
        expected = NATIVE_DELIVERY_REJECTED
    if not exit_first:
        driver.on_app_exit(9)

    # Both lock acquisition orders are covered. Exit may propose UNKNOWN only
    # from WIRE; an authoritative response always wins and remains pinned.
    assert driver._native_delivery_state_snapshot() == expected
    _drain_main_context()
    assert driver._native_delivery_state_snapshot() == expected
    if outcome == "rejected":
        assert driver.execution_attempt_id == ""
        assert driver._test_execution_events[-1][:3] == (
            "finish",
            "attempt-1",
            "failed",
        )
    else:
        assert driver.execution_attempt_id == "attempt-1"


def test_rpc_rejection_after_exit_callback_terminalizes_held_attempt(
    driver_factory,
):
    pending: Future = Future()
    hub = FakeHub()
    hub.request_results["turn/start"] = pending
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("FIRST")

    # Force GTK to consume process exit while turn/start is still unresolved.
    driver.on_app_exit(9)
    _drain_main_context()
    assert driver._closed
    assert driver.execution_attempt_id == "attempt-1"

    # The authoritative response arrives after the live binding is closed.
    # Its scheduled callback must refine the held stop into terminal rejection.
    pending.set_exception(CodexAppServerRpcError("definite late rejection"))
    _drain_main_context()

    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_REJECTED
    assert driver.execution_attempt_id == ""
    assert driver._test_execution_events[-1][:3] == (
        "finish",
        "attempt-1",
        "failed",
    )


@pytest.mark.parametrize("source", ["direct", "queued"])
@pytest.mark.parametrize("future_first", [False, True])
def test_transport_exit_keeps_acceptance_unknown_without_replay(
    driver_factory,
    source,
    future_first,
):
    pending: Future = Future()
    hub = FakeHub()
    hub.request_results["turn/start"] = pending
    driver = driver_factory(hub)
    _start(driver)

    if source == "direct":
        driver.send_user_text("FIRST")
    else:
        qid = driver.queue_user_text("FIRST")
        driver._flush_user_queue()

    if future_first:
        pending.set_exception(CodexAppServerExited(17))
        driver.on_app_exit(17)
    else:
        driver.on_app_exit(17)
        pending.set_exception(CodexAppServerExited(17))

    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_UNKNOWN
    _drain_main_context()
    assert driver._closed
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_UNKNOWN
    if source == "queued":
        assert driver.queued_messages() == [(qid, "FIRST")]
        assert driver._pending_queue_delivery == (qid, "FIRST")


def test_completed_turn_resets_delivery_before_next_prewire_rejection(
    driver_factory,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    driver.send_user_text("FIRST")
    _drain_main_context()
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_ACCEPTED

    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_RESULT,
            payload={"subtype": "success", "usage": {}},
        )
    )
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_IDLE
    assert not driver.is_busy

    driver.set_execution_guard(lambda _driver: "This Work is stopped.")
    driver.send_user_text("NEXT")

    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_REJECTED
    assert errors == ["This Work is stopped."]
    assert [method for method, _params, _timeout in hub.requests].count(
        "turn/start"
    ) == 1
    driver.stop(interrupt=False)


def test_two_sequential_turns_each_get_fresh_delivery_lifecycle(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    accepted = []
    driver.connect("prompt-accepted", lambda _driver, text: accepted.append(text))
    _start(driver)

    for text in ("FIRST", "SECOND"):
        delayed: Future = Future()
        hub.request_results["turn/start"] = delayed
        driver.send_user_text(text)
        assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_WIRE

        delayed.set_result({"turn": {"id": f"turn-{text.lower()}"}})
        assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_ACCEPTED
        _drain_main_context()
        assert accepted[-1] == text

        driver._dispatch_app_action(
            SimpleNamespace(
                kind=events.ACT_RESULT,
                payload={"subtype": "success", "usage": {}},
            )
        )
        assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_IDLE

    assert accepted == ["FIRST", "SECOND"]
    assert [method for method, _params, _timeout in hub.requests].count(
        "turn/start"
    ) == 2
    driver.stop(interrupt=False)


def test_turn_start_malformed_success_retains_fifo_slot(driver_factory):
    hub = FakeHub()
    hub.request_results["turn/start"] = {"turn": {}}
    driver = driver_factory(hub)
    _start(driver)

    outcome = driver.send_user_text("malformed response must not release")
    _drain_main_context()

    assert outcome.pending
    assert driver._closed is True
    assert driver.execution_attempt_id == "attempt-1"
    assert driver._test_execution_events == [("admit", "attempt-1")]


def test_transport_exit_without_terminal_event_retains_fifo_slot(driver_factory):
    hub = FakeHub()
    hub.request_results["turn/start"] = Future()
    driver = driver_factory(hub)
    _start(driver)
    assert driver.send_user_text("transport may have accepted").pending

    driver._consume_app_exit(9)

    assert driver.execution_attempt_id == "attempt-1"
    assert driver._test_execution_events == [("admit", "attempt-1")]


def test_turn_params_build_failure_happens_before_admission(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    monkeypatch.setattr(
        codex_app_driver,
        "build_turn_start_params",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("invalid params")),
    )

    outcome = driver.send_user_text("must remain local")

    assert outcome.rejected
    assert driver.execution_attempt_id == ""
    assert driver._test_execution_events == []
    assert not any(method == "turn/start" for method, _params, _timeout in hub.requests)


def test_dispatch_and_acceptance_carry_durable_native_identity(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    evidence = []
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-native", ""),
        lambda *_args: None,
        record_dispatch=lambda _driver, attempt, item: evidence.append(
            ("dispatch", attempt, item)
        ),
        record_acceptance=lambda _driver, attempt, item: evidence.append(
            ("accept", attempt, item)
        ),
        record_stop=lambda *_args: None,
        record_contribution=lambda *_args: True,
    )
    _start(driver)

    assert driver.send_user_text("one native request").pending

    dispatch = evidence[0][2]
    acceptance = evidence[1][2]
    assert dispatch.provider_request_key == "attempt-native"
    assert dispatch.native_binding_id == "thread-native"
    assert acceptance.provider_request_key == "attempt-native"
    assert acceptance.native_binding_id == "thread-native"
    assert acceptance.accepted_turn_id == "turn-native"
    request = next(row for row in hub.requests if row[0] == "turn/start")
    assert request[1]["clientUserMessageId"] == "attempt-native"


def test_acceptance_persistence_failure_does_not_drop_wire_authorization(
    driver_factory,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-acceptance-fails", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: None,
        record_acceptance=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("store unavailable")
        ),
        record_stop=lambda *_args: None,
        record_contribution=lambda *_args: True,
    )
    _start(driver)

    assert driver.send_user_text("accepted on wire").pending

    assert driver._router_authorized_turn_id == "turn-native"
    assert driver.execution_attempt_id == "attempt-acceptance-fails"


def test_codex_contribution_and_usage_persist_before_terminal_release(
    driver_factory,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    order = []
    terminal = []
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-ordered", ""),
        lambda *_args: None,
        record_dispatch=lambda _driver, _attempt, _item: order.append("dispatch"),
        record_acceptance=lambda _driver, _attempt, _item: order.append(
            "acceptance"
        ),
        record_stop=lambda *_args: None,
        record_contribution=lambda _driver, turn: (
            order.append(("contribution", turn.text)) or True
        ),
        finish_with_evidence=lambda _driver, _attempt, evidence: (
            order.append("terminal"),
            terminal.append(evidence),
        ),
    )
    _start(driver)
    driver._helios_identity_confirmed = True
    assert driver.send_user_text("ordered").pending
    _drain_main_context()
    driver._consume_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-native", "status": "inProgress"},
        },
    )
    driver._consume_app_notification(
        events.METHOD_THREAD_TOKEN_USAGE,
        {
            "threadId": driver.session_id,
            "turnId": "turn-native",
            "tokenUsage": {
                "total": {"totalTokens": 22},
                "last": {
                    "inputTokens": 11,
                    "cachedInputTokens": 4,
                    "outputTokens": 7,
                },
                "modelContextWindow": 200_000,
            },
        },
    )
    driver._consume_app_notification(
        events.METHOD_ITEM_COMPLETED,
        {
            "threadId": driver.session_id,
            "turnId": "turn-native",
            "item": {
                "id": "answer",
                "type": "agentMessage",
                "text": "done",
                "phase": "final_answer",
            },
        },
    )
    driver._consume_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-native", "status": "completed", "items": []},
        },
    )

    assert order == [
        "dispatch",
        "acceptance",
        ("contribution", "done"),
        "terminal",
    ]
    assert terminal[0].reason_code == "codex.provider_terminal"
    assert terminal[0].request_id == "attempt-ordered"
    assert terminal[0].turn_id == "turn-native"
    assert terminal[0].native_id == "thread-native"
    assert terminal[0].usage == {
        "input_tokens": 7,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 0,
        "output_tokens": 7,
    }
    assert terminal[0].queue_disposition == "released"
    assert driver.execution_attempt_id == ""


@pytest.mark.parametrize("source", ["direct", "queued"])
def test_turn_start_sync_transport_failure_retains_ambiguous_slot(
    driver_factory,
    source,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    order = []

    def admit(_driver):
        order.append(("admit",))
        return "attempt-boundary", ""

    def finish(_driver, attempt_id, status, _reason):
        order.append(("finish", attempt_id, status))

    original_request = hub.request

    def request(method, params=None, *, callback=None, timeout=None):
        if method == "turn/start":
            order.append(("request", method))
            raise RuntimeError("transport rejected write")
        return original_request(
            method,
            params,
            callback=callback,
            timeout=timeout,
        )

    hub.request = request
    driver.set_execution_attempt_controller(admit, finish)
    driver.connect("error", lambda _driver, _message: order.append(("error",)))

    qid = None
    if source == "direct":
        driver.send_user_text("bounded turn")
    else:
        qid = driver.queue_user_text("bounded turn")
        driver._flush_user_queue()

    assert order == [("admit",), ("request", "turn/start"), ("error",)]
    assert driver.execution_attempt_id == "attempt-boundary"
    if qid is not None:
        assert driver._pending_queue_delivery == (qid, "bounded turn")
        assert driver.queued_messages() == [(qid, "bounded turn")]


def test_failed_turn_start_keeps_router_authorization_suppressed(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    hub.request_results["turn/start"] = CodexAppServerRpcError(
        "turn rejected",
        code=-32000,
    )
    driver = driver_factory(hub)
    _start(driver)
    calls = []
    monkeypatch.setattr(
        "helios.backend.process.codex_app_driver.RouterClient.call",
        lambda *_args, **_kwargs: calls.append(True),
    )

    driver.send_user_text("failed turn")
    _drain_main_context()
    assert driver.execution_attempt_id == ""
    assert driver._test_execution_events[1][:3] == (
        "finish",
        "attempt-1",
        "failed",
    )
    driver.on_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "late-failed-turn", "status": "inProgress"},
        },
    )
    request = FakeRequest(
        "router-after-failed-start",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "late-failed-turn",
            "callId": "call-after-failed-start",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver._run_dynamic_tool_request(request)

    assert calls == []
    assert request.responses == []
    assert request.errors == [
        (-32602, "Invalid or unbound Helios dynamic tool request", None)
    ]
    driver.stop(interrupt=False)


def test_prompt_waits_for_native_goal_sync_before_turn_start(driver_factory):
    hub = FakeHub()
    goal_future = Future()
    original_request = hub.request

    def request(method, params=None, *, callback=None, timeout=None):
        if method == "thread/goal/set":
            hub.requests.append((method, params, timeout))
            if callback is not None:
                goal_future.add_done_callback(callback)
            return goal_future
        return original_request(method, params, callback=callback, timeout=timeout)

    hub.request = request
    driver = driver_factory(hub)
    _start(driver)

    assert driver.sync_goal(SimpleNamespace(objective="Ship it", status="active"))
    goal_params = next(
        params
        for method, params, _timeout in hub.requests
        if method == "thread/goal/set"
    )
    assert goal_params["tokenBudget"] == CODEX_STANDARD_TOKEN_BUDGET
    driver.send_user_text("continue")
    assert not any(method == "turn/start" for method, _params, _timeout in hub.requests)
    assert driver.is_busy

    goal_future.set_result({"goal": {"objective": "Ship it"}})
    _drain_main_context()

    assert any(method == "turn/start" for method, _params, _timeout in hub.requests)
    assert driver.native_goal_synced
    driver.stop(interrupt=False)


def test_stop_cancels_prompt_that_has_not_passed_native_goal_sync(driver_factory):
    hub = FakeHub()
    goal_future = Future()
    original_request = hub.request

    def request(method, params=None, *, callback=None, timeout=None):
        if method == "thread/goal/set":
            hub.requests.append((method, params, timeout))
            return goal_future
        return original_request(method, params, callback=callback, timeout=timeout)

    hub.request = request
    driver = driver_factory(hub)
    _start(driver)
    driver.sync_goal(SimpleNamespace(objective="Ship it", status="active"))
    driver.send_user_text("cancel before send")

    driver.stop()
    goal_future.set_result({})
    _drain_main_context()

    assert not driver.is_busy
    assert not any(method == "turn/start" for method, _params, _timeout in hub.requests)
    driver.stop(interrupt=False)


def test_failed_native_goal_mutation_does_not_release_buffered_prompt(
    driver_factory,
):
    hub = FakeHub()
    goal_future = Future()
    original_request = hub.request

    def request(method, params=None, *, callback=None, timeout=None):
        if method == "thread/goal/clear":
            hub.requests.append((method, params, timeout))
            return goal_future
        return original_request(method, params, callback=callback, timeout=timeout)

    hub.request = request
    driver = driver_factory(hub)
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)
    assert driver.clear_native_goal()
    driver.send_user_text("must not run under stale goal")

    goal_future.set_exception(RuntimeError("goal store unavailable"))
    _drain_main_context()

    assert not driver.is_busy
    assert not driver.native_goal_synced
    assert not any(method == "turn/start" for method, _params, _timeout in hub.requests)
    assert "Could not clear Codex native goal" in errors[-1]
    driver.send_user_text("still must not run")
    assert not any(method == "turn/start" for method, _params, _timeout in hub.requests)
    assert "reopen this GPT session" in errors[-1]
    driver.stop(interrupt=False)


def test_queued_prompt_retries_after_goal_reconciliation_recovers(driver_factory):
    hub = FakeHub()
    goal_futures = [Future(), Future()]
    original_request = hub.request

    def request(method, params=None, *, callback=None, timeout=None):
        if method == "thread/goal/clear":
            hub.requests.append((method, params, timeout))
            return goal_futures.pop(0)
        return original_request(method, params, callback=callback, timeout=timeout)

    hub.request = request
    driver = driver_factory(hub)
    _start(driver)
    first_goal_future = goal_futures[0]
    driver.clear_native_goal()
    qid = driver.queue_user_text("queued behind goal")
    driver._flush_user_queue()
    assert driver.is_busy

    first_goal_future.set_exception(RuntimeError("temporary failure"))
    _drain_main_context()
    assert driver.queued_messages() == [(qid, "queued behind goal")]
    assert driver._pending_queue_delivery == (qid, "queued behind goal")

    retry = goal_futures[0]
    driver.clear_native_goal()
    retry.set_result({})
    _drain_main_context()

    assert any(method == "turn/start" for method, _params, _timeout in hub.requests)
    assert driver.queued_messages() == []
    driver.stop(interrupt=False)


def test_oversized_goal_clears_native_state_for_full_work_envelope(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    objective = "x" * 5000

    assert driver.sync_goal(SimpleNamespace(objective=objective, status="active"))
    _drain_main_context()

    assert any(
        method == "thread/goal/clear" for method, _params, _timeout in hub.requests
    )
    assert not any(
        method == "thread/goal/set" for method, _params, _timeout in hub.requests
    )
    assert not driver.native_goal_synced
    driver.stop(interrupt=False)


def test_queued_message_stays_pending_until_native_turn_acceptance(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    sent = []
    driver.connect(
        "queued-user-sent",
        lambda _driver, qid, text: sent.append((qid, text)),
    )
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()
    qid = driver.queue_user_text("second")
    delayed = Future()
    hub.request_results["turn/start"] = delayed

    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_RESULT,
            payload={"subtype": "success", "usage": {}},
        )
    )

    assert driver.queued_messages() == [(qid, "second")]
    assert sent == []
    assert [event[:3] for event in driver._test_execution_events] == [
        ("admit", "attempt-1"),
        ("finish", "attempt-1", "completed"),
        ("admit", "attempt-2"),
    ]
    delayed.set_result({"turn": {"id": "turn-second"}})
    _drain_main_context()
    assert driver.queued_messages() == []
    assert sent == [(qid, "second")]
    driver.stop(interrupt=False)


@pytest.mark.parametrize("late_outcome", ["accepted", "rejected"])
def test_late_first_turn_start_callback_cannot_mutate_queued_second(
    driver_factory,
    late_outcome,
):
    from helios.backend.process.message_queue import PreparedPrompt

    first_start = Future()
    second_start = Future()
    hub = FakeHub()
    hub.request_results["turn/start"] = first_start
    driver = driver_factory(hub)
    sent = []
    accepted = []
    committed = []
    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt(
            text,
            lambda: committed.append(text),
        )
    )
    driver.connect(
        "queued-user-sent",
        lambda _driver, qid, text: sent.append((qid, text)),
    )
    driver.connect(
        "prompt-accepted",
        lambda _driver, text: accepted.append(text),
    )
    _start(driver)

    driver.send_user_text("FIRST")
    driver._consume_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-first", "status": "inProgress"},
        },
    )
    assert committed == ["FIRST"]
    assert accepted == ["FIRST"]
    assert driver._mirror.users == ["FIRST"]
    qid = driver.queue_user_text("SECOND")
    hub.request_results["turn/start"] = second_start

    # Provider terminal notification wins before FIRST's JSON-RPC response.
    # It releases attempt-1 and auto-dispatches queued SECOND as attempt-2.
    driver._consume_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-first", "status": "completed", "items": []},
        },
    )
    assert driver.execution_attempt_id == "attempt-2"
    assert driver._pending_queue_delivery == (qid, "SECOND")
    assert driver.queued_messages() == [(qid, "SECOND")]
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_WIRE

    if late_outcome == "accepted":
        first_start.set_result({"turn": {"id": "turn-first"}})
    else:
        first_start.set_exception(CodexAppServerRpcError("late FIRST rejection"))
    _drain_main_context()

    # FIRST's late callback can neither accept/reject attempt-2 nor consume
    # SECOND's queue ownership.
    assert driver.execution_attempt_id == "attempt-2"
    assert driver._pending_queue_delivery == (qid, "SECOND")
    assert driver.queued_messages() == [(qid, "SECOND")]
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_WIRE
    assert driver._app_turn_id == ""
    assert sent == []
    assert accepted == ["FIRST"]
    assert committed == ["FIRST"]
    assert driver._mirror.users == ["FIRST"]

    # A duplicate FIRST terminal notification is stale for the same reason.
    driver._consume_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-first", "status": "completed", "items": []},
        },
    )
    assert driver.execution_attempt_id == "attempt-2"
    assert driver._pending_queue_delivery == (qid, "SECOND")

    second_start.set_result({"turn": {"id": "turn-second"}})
    _drain_main_context()
    assert driver.execution_attempt_id == "attempt-2"
    assert driver._app_turn_id == "turn-second"
    assert driver.queued_messages() == []
    assert sent == [(qid, "SECOND")]
    assert accepted == ["FIRST", "SECOND"]
    assert committed == ["FIRST", "SECOND"]
    assert driver._mirror.users == ["FIRST", "SECOND"]
    driver.stop(interrupt=False)


def test_queued_first_is_promoted_once_by_turn_started_before_late_future(
    driver_factory,
):
    from helios.backend.process.message_queue import PreparedPrompt

    first_start = Future()
    hub = FakeHub()
    hub.request_results["turn/start"] = first_start
    driver = driver_factory(hub)
    sent = []
    accepted = []
    committed = []
    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt(
            text,
            lambda: committed.append(text),
        )
    )
    driver.connect(
        "queued-user-sent",
        lambda _driver, qid, text: sent.append((qid, text)),
    )
    driver.connect(
        "prompt-accepted",
        lambda _driver, text: accepted.append(text),
    )
    _start(driver)

    qid = driver.queue_user_text("FIRST")
    driver._flush_user_queue()
    driver._consume_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-first", "status": "inProgress"},
        },
    )

    assert committed == ["FIRST"]
    assert accepted == ["FIRST"]
    assert sent == [(qid, "FIRST")]
    assert driver._mirror.users == ["FIRST"]
    assert driver.queued_messages() == []
    assert driver._pending_queue_delivery is None

    driver._consume_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-first", "status": "completed", "items": []},
        },
    )
    assert driver.execution_attempt_id == ""

    first_start.set_result({"turn": {"id": "turn-first"}})
    _drain_main_context()

    assert committed == ["FIRST"]
    assert accepted == ["FIRST"]
    assert sent == [(qid, "FIRST")]
    assert driver._mirror.users == ["FIRST"]
    driver.stop(interrupt=False)


@pytest.mark.parametrize("source", ["direct", "queued"])
@pytest.mark.parametrize(
    "late_error",
    [
        CodexAppServerRpcError("late rejection"),
        CodexAppServerTimeout("late timeout"),
    ],
    ids=["rpc-rejection", "timeout"],
)
def test_turn_started_acceptance_dominates_late_future_error(
    driver_factory,
    source,
    late_error,
):
    first_start = Future()
    hub = FakeHub()
    hub.request_results["turn/start"] = first_start
    driver = driver_factory(hub)
    accepted = []
    sent = []
    driver.connect(
        "prompt-accepted",
        lambda _driver, text: accepted.append(text),
    )
    driver.connect(
        "queued-user-sent",
        lambda _driver, qid, text: sent.append((qid, text)),
    )
    _start(driver)

    qid = None
    if source == "direct":
        driver.send_user_text("FIRST")
    else:
        qid = driver.queue_user_text("FIRST")
        driver._flush_user_queue()
    driver._consume_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-first", "status": "inProgress"},
        },
    )

    assert accepted == ["FIRST"]
    assert sent == ([(qid, "FIRST")] if qid is not None else [])
    assert driver.execution_attempt_id == "attempt-1"
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_ACCEPTED

    first_start.set_exception(late_error)
    _drain_main_context()

    assert accepted == ["FIRST"]
    assert sent == ([(qid, "FIRST")] if qid is not None else [])
    assert driver.execution_attempt_id == "attempt-1"
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_ACCEPTED
    assert driver._closed is False
    assert not any(event[0] == "finish" for event in driver._test_execution_events)

    driver._consume_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-first", "status": "completed", "items": []},
        },
    )
    assert driver.execution_attempt_id == ""
    driver.stop(interrupt=False)


def test_late_interrupt_ack_cannot_write_stop_evidence_to_queued_second(
    driver_factory,
):
    interrupt_response = Future()
    second_start = Future()
    hub = FakeHub()
    driver = driver_factory(hub)
    stop_evidence = []
    driver._execution_attempt_stop_recorder = (
        lambda _driver, attempt_id, evidence: stop_evidence.append(
            (attempt_id, evidence)
        )
    )
    _start(driver)
    driver.send_user_text("FIRST")
    _drain_main_context()
    driver._consume_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-native", "status": "inProgress"},
        },
    )
    hub.request_results["turn/interrupt"] = interrupt_response
    driver.stop(interrupt=True)

    qid = driver.queue_user_text("SECOND")
    hub.request_results["turn/start"] = second_start
    driver._consume_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-native", "status": "interrupted", "items": []},
        },
    )
    assert driver.execution_attempt_id == "attempt-2"
    assert driver._pending_queue_delivery == (qid, "SECOND")

    interrupt_response.set_result({})
    _drain_main_context()

    assert stop_evidence == []
    assert driver.execution_attempt_id == "attempt-2"
    assert driver._pending_queue_delivery == (qid, "SECOND")
    second_start.set_result({"turn": {"id": "turn-second"}})
    _drain_main_context()
    driver.stop(interrupt=False)


def test_native_queue_flush_honors_work_execution_guard(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()
    qid = driver.queue_user_text("must stay queued")
    driver.set_execution_guard(lambda _driver: "This Work is stopped.")

    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_RESULT,
            payload={"subtype": "success", "usage": {}},
        )
    )

    assert driver.queued_messages() == [(qid, "must stay queued")]
    assert errors == ["This Work is stopped."]
    assert [method for method, _params, _timeout in hub.requests].count(
        "turn/start"
    ) == 1
    driver.stop(interrupt=False)


@pytest.mark.parametrize("mode", ["auto", "bypassPermissions"])
def test_family_token_budget_aggregates_root_and_child_counters(driver_factory, mode):
    hub = FakeHub()
    driver = driver_factory(hub, mode=mode)
    errors = []
    budgets = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    driver.connect(
        "budget-exhausted",
        lambda _driver, payload: budgets.append(dict(payload)),
    )
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()
    qid = driver.queue_user_text("do not auto-send")

    def report(thread_id, lifetime):
        driver._consume_app_notification(
            events.METHOD_THREAD_TOKEN_USAGE,
            {
                "threadId": thread_id,
                "turnId": "turn-native",
                "tokenUsage": {
                    "total": {"totalTokens": lifetime},
                    "last": {"totalTokens": 1_000},
                    "modelContextWindow": 200_000,
                },
            },
        )

    report(driver.session_id, 120_000)
    report(driver.session_id, 130_000)
    assert driver._lifetime_tokens_used == 130_000
    assert not driver._token_budget_exhausted

    # The root event accumulator rejects child IDs.  The driver must still
    # account the child counter delivered through its hub family binding.
    report("child-one", 70_000)

    assert driver._token_budget_exhausted is True
    assert driver._lifetime_tokens_used == CODEX_STANDARD_TOKEN_BUDGET
    assert driver._lifetime_tokens_by_thread == {
        driver.session_id: 130_000,
        "child-one": 70_000,
    }
    assert budgets == [
        {
            "kind": "tokens",
            "limit": CODEX_STANDARD_TOKEN_BUDGET,
            "provider": "openai",
        }
    ]
    assert any(method == "turn/interrupt" for method, _params, _timeout in hub.requests)
    assert "safety limit" in errors[-1]

    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_RESULT,
            payload={"subtype": "aborted", "usage": {}},
        )
    )
    assert driver.queued_messages() == [(qid, "do not auto-send")]

    before = len(hub.requests)
    driver.send_user_text("more work")
    assert len(hub.requests) == before
    assert "Start a new bounded Work" in errors[-1]
    driver.stop(interrupt=False)


def test_subscription_billing_does_not_terminate_on_token_total(driver_factory):
    """A token cap is not a spend control on a subscription.

    The counterpart of the Claude dollar-breaker fix. Usage is still tracked
    and reported — it just does not kill the Work at an arbitrary total.
    """

    hub = FakeHub()
    driver = driver_factory(hub, token_budget=None)
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()

    def report(thread_id, lifetime):
        driver._consume_app_notification(
            events.METHOD_THREAD_TOKEN_USAGE,
            {
                "threadId": thread_id,
                "turnId": "turn-native",
                "tokenUsage": {
                    "total": {"totalTokens": lifetime},
                    "last": {"totalTokens": 1_000},
                    "modelContextWindow": 200_000,
                },
            },
        )

    # Far past the per-token cap, including across a child thread.
    report(driver.session_id, 900_000)
    report("child-one", 900_000)

    assert driver._lifetime_tokens_used == 1_800_000  # still counted
    assert driver._token_budget_exhausted is False
    assert not any(
        method == "turn/interrupt" for method, _params, _timeout in hub.requests
    )
    assert not errors

    # And a further prompt is still accepted rather than rejected.
    before = len(hub.requests)
    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_RESULT,
            payload={"subtype": "success", "usage": {}},
        )
    )
    driver.send_user_text("keep going")
    assert len(hub.requests) > before
    driver.stop(interrupt=False)


def test_subscription_billing_omits_native_token_budget(driver_factory):
    """App Server enforces `tokenBudget` itself, so it must not be sent."""

    hub = FakeHub()
    driver = driver_factory(hub, token_budget=None)
    _start(driver)
    assert driver.sync_goal(SimpleNamespace(objective="Ship it", status="active"))
    goal_params = next(
        params
        for method, params, _timeout in hub.requests
        if method == "thread/goal/set"
    )
    assert "tokenBudget" not in goal_params
    assert goal_params["objective"] == "Ship it"
    driver.stop(interrupt=False)


def test_provider_reported_budget_still_latches_on_subscription(driver_factory):
    """A real `sessionBudgetExceeded` is the provider's, not Helios's cap."""

    hub = FakeHub()
    driver = driver_factory(hub, token_budget=None)
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()

    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_TURN_STATUS,
            payload={"error": {"codexErrorInfo": "sessionBudgetExceeded"}},
        )
    )

    assert driver._token_budget_exhausted is True
    # No Helios limit to name, so the message must not invent one.
    assert "safety limit" not in errors[-1]
    assert "session budget" in errors[-1]
    driver.stop(interrupt=False)


def test_default_token_budget_follows_billing_mode(monkeypatch):
    import helios.backend.codex_env as codex_env
    from helios.backend.process.codex_app_driver import default_token_budget

    monkeypatch.setattr(codex_env, "_billing_cache", {})
    monkeypatch.setattr(codex_env, "auth_mode", lambda: "chatgpt")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    assert default_token_budget() is None

    monkeypatch.setattr(codex_env, "_billing_cache", {})
    monkeypatch.setattr(codex_env, "auth_mode", lambda: "apikey")
    assert default_token_budget() == CODEX_STANDARD_TOKEN_BUDGET

    # An explicit key is real per-token billing even alongside a ChatGPT login.
    monkeypatch.setattr(codex_env, "_billing_cache", {})
    monkeypatch.setattr(codex_env, "auth_mode", lambda: "chatgpt")
    monkeypatch.setenv("CODEX_API_KEY", "sk-test")
    assert default_token_budget() == CODEX_STANDARD_TOKEN_BUDGET

    # Unreadable auth fails CLOSED: keep the cap.
    monkeypatch.setattr(codex_env, "_billing_cache", {})
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(codex_env, "auth_mode", lambda: "")
    assert default_token_budget() == CODEX_STANDARD_TOKEN_BUDGET


def test_native_budget_error_latches_before_result_queue_flush(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    errors = []
    budgets = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    driver.connect(
        "budget-exhausted",
        lambda _driver, payload: budgets.append(dict(payload)),
    )
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()
    qid = driver.queue_user_text("do not auto-send")
    error = {
        "message": "Session token budget exceeded",
        "codexErrorInfo": "sessionBudgetExceeded",
    }

    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_TURN_STATUS,
            payload={"status": "failed", "turnId": "turn-native", "error": error},
        )
    )
    driver._dispatch_app_action(
        SimpleNamespace(kind=events.ACT_ERROR, payload=error["message"])
    )
    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_RESULT,
            payload={"subtype": "error", "usage": {}, "error": error},
        )
    )

    assert driver._token_budget_exhausted
    assert driver._lifetime_tokens_used == CODEX_STANDARD_TOKEN_BUDGET
    assert driver.queued_messages() == [(qid, "do not auto-send")]
    assert len(budgets) == 1
    assert errors == [
        "Codex reached this session family's 200,000-token safety limit. "
        "The active turn was interrupted and queued messages were kept."
    ]
    assert [method for method, _params, _timeout in hub.requests].count(
        "turn/start"
    ) == 1
    driver.stop(interrupt=False)


def test_budget_interrupt_request_failure_aborts_shared_transport(
    driver_factory,
    monkeypatch,
):
    timers, removed = _capture_glib_timeouts(monkeypatch)
    hub = FakeHub()
    hub.request_results["turn/interrupt"] = RuntimeError("interrupt channel broke")
    driver = driver_factory(hub)
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()

    driver._mark_token_budget_exhausted(interrupt_active=True)
    _drain_main_context()

    assert _wait_until(lambda: hub.aborts == [-1])
    assert len(timers) == 1
    assert timers[0][1] == CODEX_BUDGET_INTERRUPT_DEADLINE_MS
    assert removed == [timers[0][0]]
    assert any("Could not interrupt Codex turn" in message for message in errors)
    assert any(
        "shutting down the shared Codex App Server" in message for message in errors
    )
    driver.stop(interrupt=False)


def test_budget_interrupt_deadline_aborts_while_turn_remains_busy(
    driver_factory,
    monkeypatch,
):
    timers, _removed = _capture_glib_timeouts(monkeypatch)
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()

    driver._mark_token_budget_exhausted(interrupt_active=True)

    assert len(timers) == 1
    _source_id, interval, callback, args = timers[0]
    assert interval == CODEX_BUDGET_INTERRUPT_DEADLINE_MS
    assert callback(*args) is False
    assert _wait_until(lambda: hub.aborts == [-1])
    assert driver.is_busy
    driver.stop(interrupt=False)


def test_completed_budget_interrupt_cancels_watchdog_without_escalation(
    driver_factory,
    monkeypatch,
):
    timers, removed = _capture_glib_timeouts(monkeypatch)
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()

    driver._mark_token_budget_exhausted(interrupt_active=True)
    assert len(timers) == 1
    source_id, _interval, callback, args = timers[0]
    driver._dispatch_app_action(
        SimpleNamespace(
            kind=events.ACT_RESULT,
            payload={"subtype": "aborted", "usage": {}},
        )
    )

    assert not driver.is_busy
    assert removed == [source_id]
    # A timeout already queued by GLib is harmless after completion because
    # cancelling the watchdog advances its generation fence.
    assert callback(*args) is False
    assert hub.aborts == []
    driver.stop(interrupt=False)


def test_ordinary_stop_interrupt_failure_never_aborts_shared_transport(
    driver_factory,
):
    hub = FakeHub()
    hub.request_results["turn/interrupt"] = RuntimeError("ordinary stop failed")
    driver = driver_factory(hub)
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)
    driver.send_user_text("first")
    _drain_main_context()

    driver.stop(interrupt=True)
    _drain_main_context()

    assert hub.aborts == []
    assert driver._budget_interrupt_source_id == 0
    assert any("ordinary stop failed" in message for message in errors)
    driver.stop(interrupt=False)


def test_native_agent_snapshot_is_scoped_to_one_turn(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    snapshots = []
    driver.connect(
        "agents-updated",
        lambda _driver, snapshot: snapshots.append(dict(snapshot)),
    )
    _start(driver)

    driver._begin_native_turn("turn-one")
    driver._capture_child_thread(
        {
            "turnId": "turn-one",
            "thread": {
                "id": "child-one",
                "parentThreadId": driver.session_id,
                "agentNickname": "Reviewer",
            }
        }
    )
    assert "child-one" in snapshots[-1]

    driver._begin_native_turn("turn-two")
    assert snapshots[-1] == {}
    assert driver.observed_agent_root_turn_id == "turn-two"
    assert driver.observed_agent_snapshot() == {}
    driver.stop(interrupt=False)


def test_late_agent_activity_cannot_mutate_new_root_turn(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    snapshots = []
    driver.connect("agents-updated", lambda _driver, snapshot: snapshots.append(snapshot))
    _start(driver)

    driver._begin_native_turn("turn-first")
    assert driver._apply_subagent_activity(
        {
            "turnId": "turn-first",
            "receiverThreadIds": ["child-first"],
            "agentsStates": {"child-first": {"status": "running"}},
        }
    ) is True
    driver._begin_native_turn("turn-second")
    before = len(snapshots)

    assert driver._apply_subagent_activity(
        {
            "turnId": "turn-first",
            "receiverThreadIds": ["child-first"],
            "agentsStates": {"child-first": {"status": "completed"}},
        }
    ) is False
    assert len(snapshots) == before
    assert driver.observed_agent_snapshot() == {}
    driver.stop(interrupt=False)


def test_child_thread_projection_requires_exact_root_turn(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    snapshots = []
    driver.connect("agents-updated", lambda _driver, snapshot: snapshots.append(snapshot))
    _start(driver)
    driver._begin_native_turn("turn-current")
    before = len(snapshots)
    child = {
        "id": "child-thread",
        "parentThreadId": driver.session_id,
        "agentNickname": "Reviewer",
    }

    driver._capture_child_thread({"thread": child})
    driver._capture_child_thread({"turnId": "turn-old", "thread": child})

    assert len(snapshots) == before
    assert driver.observed_agent_snapshot() == {}

    driver._capture_child_thread({"turnId": "turn-current", "thread": child})
    assert driver.observed_agent_snapshot()["child-thread"]["name"] == "Reviewer"
    driver.stop(interrupt=False)


def test_terminal_agent_cannot_resurrect_in_retained_rebind_snapshot(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver._begin_native_turn("turn-current")
    assert driver._apply_subagent_activity(
        {
            "turnId": "turn-current",
            "receiverThreadIds": ["child-thread"],
            "agentsStates": {
                "child-thread": {
                    "status": "completed",
                    "name": "Reviewer",
                    "message": "Review complete",
                }
            },
        }
    ) is True

    # Both native sources can arrive late. Neither may rewrite the driver's
    # retained raw state, because a visible-conversation rebind rebuilds the
    # neutral model from this snapshot rather than its prior live projection.
    assert driver._apply_subagent_activity(
        {
            "turnId": "turn-current",
            "agentThreadId": "child-thread",
            "kind": "running",
            "agentPath": "late activity",
        }
    ) is True
    assert driver._apply_subagent_activity(
        {
            "turnId": "turn-current",
            "receiverThreadIds": ["child-thread"],
            "agentsStates": {
                "child-thread": {
                    "status": "running",
                    "name": "Reviewer state late",
                    "role": "validator",
                    "message": "stale message",
                    "path": "stale path",
                    "agentPath": "stale agent path",
                }
            },
        }
    ) is True
    driver._capture_child_thread(
        {
            "turnId": "turn-current",
            "thread": {
                "id": "child-thread",
                "parentThreadId": driver.session_id,
                "agentNickname": "Reviewer late",
                "agentRole": "reviewer",
            },
        }
    )

    retained = driver.observed_agent_snapshot()
    assert retained["child-thread"]["status"] == "completed"
    assert retained["child-thread"]["message"] == "Review complete"
    assert "path" not in retained["child-thread"]
    assert "agentPath" not in retained["child-thread"]
    assert retained["child-thread"]["name"] == "Reviewer late"
    assert retained["child-thread"]["role"] == "reviewer"
    model = AgentActivityModel()
    scope = AgentActivityScope(
        "openai",
        "work-one",
        "turn-current",
        driver.session_id,
    )
    model.begin_scope(scope)
    rebound = model.observe(scope, retained)
    assert rebound.activities[0].status is AgentObservedStatus.COMPLETED
    assert rebound.activities[0].detail == "Review complete"
    assert rebound.activities[0].name == "Reviewer late"
    driver.stop(interrupt=False)


@pytest.mark.parametrize(
    ("current_status", "expected"),
    [
        ("running", AgentObservedStatus.RUNNING),
        ("needsInput", AgentObservedStatus.NEEDS_INPUT),
    ],
)
def test_retained_rebind_snapshot_rejects_late_starting_regression(
    driver_factory,
    current_status,
    expected,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver._begin_native_turn("turn-current")
    assert driver._apply_subagent_activity(
        {
            "turnId": "turn-current",
            "receiverThreadIds": ["child-thread"],
            "agentsStates": {
                "child-thread": {
                    "status": current_status,
                    "message": "current detail",
                }
            },
        }
    ) is True
    assert driver._apply_subagent_activity(
        {
            "turnId": "turn-current",
            "receiverThreadIds": ["child-thread"],
            "agentsStates": {
                "child-thread": {
                    "status": "pendingInit",
                    "message": "later identity detail",
                }
            },
        }
    ) is True

    retained = driver.observed_agent_snapshot()
    assert retained["child-thread"]["status"] == current_status
    model = AgentActivityModel()
    scope = AgentActivityScope(
        "openai",
        "work-one",
        "turn-current",
        driver.session_id,
    )
    model.begin_scope(scope)
    rebound = model.observe(scope, retained)
    assert rebound.activities[0].status is expected
    assert rebound.activities[0].detail == "later identity detail"
    driver.stop(interrupt=False)


def test_child_thread_goal_does_not_mark_parent_goal_synced(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    updates = []
    driver.connect(
        "native-goal-updated",
        lambda _driver, payload: updates.append(payload),
    )
    _start(driver)

    driver._consume_app_notification(
        "thread/goal/updated",
        {
            "threadId": "child-thread",
            "goal": {"objective": "Child work", "status": "active"},
        },
    )

    assert updates == []
    assert not driver.native_goal_synced
    driver.stop(interrupt=False)


def test_native_completion_emits_one_result_and_clears_busy(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    results = []
    driver.connect("result", lambda _driver, result: results.append(result))
    _start(driver)
    driver.send_user_text("hello")
    _drain_main_context()

    driver._consume_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-native", "status": "inProgress"},
        },
    )
    driver._consume_app_notification(
        events.METHOD_ITEM_COMPLETED,
        {
            "threadId": driver.session_id,
            "turnId": "turn-native",
            "completedAtMs": 2,
            "item": {
                "id": "answer",
                "type": "agentMessage",
                "text": "Done",
                "phase": "final_answer",
            },
        },
    )
    driver._consume_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-native", "status": "completed", "items": []},
        },
    )

    assert len(results) == 1
    assert results[0]["subtype"] == "success"
    assert not driver.is_busy
    assert len(driver._mirror.assistant) == 1
    driver.stop(interrupt=False)


def test_stop_before_turn_start_response_interrupts_new_not_stale_turn(
    driver_factory,
):
    hub = FakeHub()
    accepted = Future()
    hub.request_results["turn/start"] = accepted
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("prompt")

    driver.stop()
    assert driver._interrupt_when_turn_known is True
    assert not any(method == "turn/interrupt" for method, _p, _t in hub.requests)

    accepted.set_result({"turn": {"id": "new-turn"}})
    _drain_main_context()

    interrupt = next(row for row in hub.requests if row[0] == "turn/interrupt")
    assert interrupt[1] == {"threadId": driver.session_id, "turnId": "new-turn"}
    driver.stop(interrupt=False)


def test_stop_suppresses_delayed_router_turn_authorization(
    driver_factory,
    monkeypatch,
):
    pending_start: Future = Future()
    hub = FakeHub()
    hub.request_results["turn/start"] = pending_start
    driver = driver_factory(hub)
    _start(driver)
    calls = []

    def fake_call(_client, method, params, *, binding):
        calls.append((method, params, binding))
        return {"contract_version": 1, "status": "completed"}

    monkeypatch.setattr(
        "helios.backend.process.codex_app_driver.RouterClient.call",
        fake_call,
    )

    driver.send_user_text("start a turn")
    driver.stop()
    pending_start.set_result({"turn": {"id": "turn-after-stop"}})
    _drain_main_context()
    driver.on_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-after-stop", "status": "inProgress"},
        },
    )
    request = FakeRequest(
        "router-after-stop",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "turn-after-stop",
            "callId": "call-after-stop",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver.on_app_request(request)

    assert _wait_until(lambda: bool(request.errors))
    assert calls == []
    assert request.responses == []
    assert request.errors == [
        (-32000, "Helios delegation is disabled for standard Work", None)
    ]
    driver.stop(interrupt=False)


def test_approval_and_user_input_responses_are_schema_exact(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    questions = []
    driver.connect(
        "question-asked", lambda _d, payload, token: questions.append((payload, token))
    )
    _start(driver)

    approval = FakeRequest(
        7,
        "item/commandExecution/requestApproval",
        {"threadId": driver.session_id, "command": "make test", "cwd": "/repo"},
    )
    driver._consume_app_request(approval)
    payload, token = questions.pop()
    assert payload["allowOther"] is False
    driver.answer_question(token, "Approve for session")
    assert approval.responses == [{"decision": "acceptForSession"}]

    request_input = FakeRequest(
        "ask-1",
        "item/tool/requestUserInput",
        {
            "threadId": driver.session_id,
            "questions": [{"id": "language", "header": "Language", "question": "Pick"}],
        },
    )
    driver._consume_app_request(request_input)
    _payload, token = questions.pop()
    answer = {"language": {"answers": ["Python"]}}
    driver.answer_question(token, answer)
    assert request_input.responses == [{"answers": answer}]

    permissions = FakeRequest(
        "permissions-1",
        "item/permissions/requestApproval",
        {
            "threadId": driver.session_id,
            "cwd": "/repo",
            "permissions": {"network": {"enabled": True}},
        },
    )
    driver._consume_app_request(permissions)
    _payload, token = questions.pop()
    driver.answer_question(token, "Approve once")
    assert permissions.responses == [
        {"permissions": {"network": {"enabled": True}}, "scope": "turn"}
    ]

    elicitation = FakeRequest(
        "mcp-1",
        "mcpServer/elicitation/request",
        {"threadId": driver.session_id, "mode": "form", "message": "Input"},
    )
    driver._consume_app_request(elicitation)
    assert elicitation.responses == [{"action": "decline"}]
    driver.stop(interrupt=False)


def test_schema_known_denied_request_fails_closed_without_drift(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    reports = []
    driver.connect("capability-drift", lambda _d, payload: reports.append(payload))
    _start(driver)

    request = FakeRequest("attest-1", "attestation/generate", {})
    driver._consume_app_request(request)

    assert request.responses == []
    assert request.errors == [
        (
            -32000,
            "Helios denied attestation/generate: client attestation is not implemented",
            None,
        )
    ]
    assert reports == []
    driver.stop(interrupt=False)


def test_schema_supported_current_time_request_is_answered_without_prompt(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    questions = []
    driver.connect("question-asked", lambda *_args: questions.append(True))
    _start(driver)
    monkeypatch.setattr(codex_app_driver.time, "time", lambda: 1_788_000_123.9)

    request = FakeRequest("time-1", "currentTime/read", {"threadId": driver.session_id})
    driver._consume_app_request(request)

    assert request.responses == [{"currentTimeAt": 1_788_000_123}]
    assert request.errors == []
    assert questions == []
    driver.stop(interrupt=False)


def test_schema_unknown_request_is_reported_as_protocol_drift(
    driver_factory,
    monkeypatch,
):
    monkeypatch.setattr(codex_app_driver, "_UNKNOWN_SERVER_REQUEST_METHODS", set())
    hub = FakeHub()
    driver = driver_factory(hub)
    reports = []
    driver.connect("capability-drift", lambda _d, payload: reports.append(payload))
    _start(driver)

    request = FakeRequest("future-1", "item/tool/futurePrompt", {})
    driver._consume_app_request(request)

    assert request.responses == []
    assert request.errors == [
        (-32601, "Helios does not support item/tool/futurePrompt", None)
    ]
    assert reports == [
        {
            "provider": "codex",
            "version": "",
            "degraded": [
                "a server request this build does not understand "
                "(item/tool/futurePrompt)"
            ],
        }
    ]
    driver.stop(interrupt=False)


def test_dynamic_router_tool_uses_authoritative_thread_binding(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    calls = []

    def fake_call(_client, method, params, *, binding):
        calls.append((method, params, binding))
        return {
            "contract_version": 1,
            "accepted": True,
            "status": "completed",
            "result": {"text": "bounded result"},
        }

    monkeypatch.setattr(
        "helios.backend.process.codex_app_driver.RouterClient.call",
        fake_call,
    )
    driver._app_turn_id = "turn-1"
    driver._router_authorized_turn_id = "turn-1"
    request = FakeRequest(
        "router-1",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "turn-1",
            "callId": "call-1",
            "namespace": "helios",
            "tool": "delegate_task",
            "arguments": {"contract_version": 1},
        },
    )

    driver._run_dynamic_tool_request(request)

    assert calls == [
        (
            "delegate_task",
            {"contract_version": 1},
            {
                "kind": "codex",
                "thread_id": driver.session_id,
                "turn_id": "turn-1",
                "call_id": "call-1",
            },
        )
    ]
    assert request.errors == []
    response = request.responses[0]
    assert response["success"] is True
    assert '"status":"completed"' in response["contentItems"][0]["text"]
    driver.stop(interrupt=False)


def test_dynamic_router_tool_rejects_cross_thread_request(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    request = FakeRequest(
        "router-2",
        "item/tool/call",
        {
            "threadId": "different-thread",
            "turnId": "turn-1",
            "callId": "call-1",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver._run_dynamic_tool_request(request)

    assert request.responses == []
    assert request.errors == [
        (-32602, "Invalid or unbound Helios dynamic tool request", None)
    ]
    driver.stop(interrupt=False)


def test_dynamic_router_tool_rejects_stale_turn_request(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver._app_turn_id = "turn-current"
    driver._router_authorized_turn_id = "turn-current"
    request = FakeRequest(
        "router-stale-turn",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "turn-old",
            "callId": "call-1",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver._run_dynamic_tool_request(request)

    assert request.responses == []
    assert request.errors == [
        (-32602, "Invalid or unbound Helios dynamic tool request", None)
    ]
    driver.stop(interrupt=False)


def test_dynamic_router_tool_accepts_wire_turn_before_gtk_completion(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    future: Future = Future()
    future.set_result({"turn": {"id": "turn-wire"}})

    driver._record_authorized_turn_from_result(future)

    assert driver._app_turn_id == ""
    calls = []

    def fake_call(_client, method, params, *, binding):
        calls.append((method, params, binding))
        return {"contract_version": 1, "status": "completed"}

    monkeypatch.setattr(
        "helios.backend.process.codex_app_driver.RouterClient.call",
        fake_call,
    )
    request = FakeRequest(
        "router-wire-race",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "turn-wire",
            "callId": "call-wire",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver._run_dynamic_tool_request(request)

    assert calls[0][2]["turn_id"] == "turn-wire"
    assert request.responses[0]["success"] is True
    driver.stop(interrupt=False)


def test_dynamic_router_tool_workers_are_bounded_and_slots_are_reused(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver._delegation_enabled = True
    driver._app_turn_id = "turn-capacity"
    driver._router_authorized_turn_id = "turn-capacity"
    other_driver = driver_factory(FakeHub(thread_id="thread-other"))
    _start(other_driver)
    other_driver._delegation_enabled = True
    other_driver._app_turn_id = "turn-other"
    other_driver._router_authorized_turn_id = "turn-other"
    capacity = codex_app_driver._MAX_ROUTER_TOOL_WORKERS
    calls = []
    calls_lock = threading.Lock()
    all_workers_started = threading.Event()
    release_workers = threading.Event()

    def fake_call(_client, method, params, *, binding):
        with calls_lock:
            calls.append((method, params, binding))
            if len(calls) == capacity:
                all_workers_started.set()
        release_workers.wait(timeout=2.0)
        return {"contract_version": 1, "status": "completed"}

    monkeypatch.setattr(
        "helios.backend.process.codex_app_driver.RouterClient.call",
        fake_call,
    )

    def request_for(index, *, target=driver, turn_id="turn-capacity"):
        return FakeRequest(
            f"router-capacity-{index}",
            "item/tool/call",
            {
                "threadId": target.session_id,
                "turnId": turn_id,
                "callId": f"call-{index}",
                "namespace": "helios",
                "tool": "routing_status",
                "arguments": {"contract_version": 1},
            },
        )

    active_requests = [request_for(index) for index in range(capacity)]
    for request in active_requests:
        driver.on_app_request(request)
    assert all_workers_started.wait(timeout=2.0)

    overflow = request_for(
        "overflow",
        target=other_driver,
        turn_id="turn-other",
    )
    other_driver.on_app_request(overflow)
    assert overflow.responses == []
    assert overflow.errors == [
        (-32000, "Helios Router dynamic-tool capacity is in use", None)
    ]

    release_workers.set()
    assert _wait_until(
        lambda: all(request.responses or request.errors for request in active_requests)
    )
    assert all(request.responses and not request.errors for request in active_requests)

    def all_slots_are_available():
        acquired = 0
        for _index in range(capacity):
            if not driver._router_tool_slots.acquire(blocking=False):
                break
            acquired += 1
        for _index in range(acquired):
            driver._router_tool_slots.release()
        return acquired == capacity

    assert _wait_until(all_slots_are_available)

    reused = request_for(
        "reused",
        target=other_driver,
        turn_id="turn-other",
    )
    other_driver.on_app_request(reused)
    assert _wait_until(lambda: bool(reused.responses or reused.errors))
    assert reused.responses and reused.errors == []
    assert len(calls) == capacity + 1
    driver.stop(interrupt=False)
    other_driver.stop(interrupt=False)


def test_dynamic_router_tool_start_failure_releases_global_slot(
    driver_factory,
    monkeypatch,
):
    driver = driver_factory(FakeHub())
    _start(driver)
    driver._delegation_enabled = True
    driver._app_turn_id = "turn-start-failure"
    driver._router_authorized_turn_id = "turn-start-failure"

    def fail_start(_thread):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    request = FakeRequest(
        "router-start-failure",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "turn-start-failure",
            "callId": "call-start-failure",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver.on_app_request(request)

    assert request.responses == []
    assert request.errors == [
        (-32000, "Helios Router dynamic-tool worker is unavailable", None)
    ]
    acquired = [
        driver._router_tool_slots.acquire(blocking=False)
        for _index in range(codex_app_driver._MAX_ROUTER_TOOL_WORKERS)
    ]
    assert all(acquired)
    for _index in acquired:
        driver._router_tool_slots.release()
    driver.stop(interrupt=False)


def test_dynamic_router_tool_discards_result_after_turn_completes(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver._delegation_enabled = True
    driver._app_turn_id = "turn-finishing"
    driver._router_authorized_turn_id = "turn-finishing"
    broker_started = threading.Event()
    release_broker = threading.Event()

    def fake_call(_client, method, params, *, binding):
        broker_started.set()
        release_broker.wait(timeout=2.0)
        return {"contract_version": 1, "status": "completed"}

    monkeypatch.setattr(
        "helios.backend.process.codex_app_driver.RouterClient.call",
        fake_call,
    )
    request = FakeRequest(
        "router-completed-turn",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "turn-finishing",
            "callId": "call-finishing",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver.on_app_request(request)
    assert broker_started.wait(timeout=2.0)
    driver.on_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "turn-finishing", "status": "completed"},
        },
    )
    release_broker.set()

    assert _wait_until(lambda: bool(request.errors))
    assert request.responses == []
    assert request.errors == [
        (-32000, "Helios session or router turn is no longer active", None)
    ]
    assert request.abandoned == 0
    driver.stop(interrupt=False)


def test_dynamic_router_tool_discards_result_after_user_interrupt(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver._delegation_enabled = True
    driver._busy = True
    driver._app_turn_id = "turn-interrupted"
    driver._router_authorized_turn_id = "turn-interrupted"
    broker_started = threading.Event()
    release_broker = threading.Event()

    def fake_call(_client, method, params, *, binding):
        broker_started.set()
        release_broker.wait(timeout=2.0)
        return {"contract_version": 1, "status": "completed"}

    monkeypatch.setattr(
        "helios.backend.process.codex_app_driver.RouterClient.call",
        fake_call,
    )
    request = FakeRequest(
        "router-interrupted-turn",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "turn-interrupted",
            "callId": "call-interrupted",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver.on_app_request(request)
    assert broker_started.wait(timeout=2.0)
    driver.stop()
    release_broker.set()

    assert _wait_until(lambda: bool(request.errors))
    assert request.responses == []
    assert request.errors == [
        (-32000, "Helios session or router turn is no longer active", None)
    ]
    assert request.abandoned == 0
    driver.stop(interrupt=False)


def test_child_turn_completion_does_not_invalidate_parent_router_turn(
    driver_factory,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver._app_turn_id = "turn-parent"
    driver._router_authorized_turn_id = "turn-parent"
    generation = driver._router_tool_generation

    driver.on_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": "child-thread",
            "turn": {"id": "turn-child", "status": "completed"},
        },
    )

    assert driver._router_authorized_turn_id == "turn-parent"
    assert driver._router_tool_generation == generation
    driver.stop(interrupt=False)


def test_dynamic_router_tool_discards_result_after_session_closes(
    driver_factory,
    monkeypatch,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver._delegation_enabled = True
    driver._app_turn_id = "turn-closing"
    driver._router_authorized_turn_id = "turn-closing"
    broker_started = threading.Event()
    release_broker = threading.Event()

    def fake_call(_client, method, params, *, binding):
        broker_started.set()
        release_broker.wait(timeout=2.0)
        return {"contract_version": 1, "status": "completed"}

    monkeypatch.setattr(
        "helios.backend.process.codex_app_driver.RouterClient.call",
        fake_call,
    )
    request = FakeRequest(
        "router-closed-session",
        "item/tool/call",
        {
            "threadId": driver.session_id,
            "turnId": "turn-closing",
            "callId": "call-closing",
            "namespace": "helios",
            "tool": "routing_status",
            "arguments": {"contract_version": 1},
        },
    )

    driver.on_app_request(request)
    assert broker_started.wait(timeout=2.0)
    driver.stop(interrupt=False)
    release_broker.set()

    assert _wait_until(lambda: bool(request.errors))
    assert request.responses == []
    assert request.errors == [
        (-32000, "Helios session or router turn is no longer active", None)
    ]
    assert request.abandoned == 0


def test_router_request_rejection_failure_does_not_block_native_finalization(
    driver_factory,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)

    class RaisingRequest(FakeRequest):
        def __init__(self):
            super().__init__("router-raising", "item/tool/call", {})
            self.rejection_attempts = 0

        def respond_error(self, code, message, data=None):
            self.rejection_attempts += 1
            raise RuntimeError("transport write failed")

    raising = RaisingRequest()
    retired = FakeRequest("router-retired", "item/tool/call", {})
    driver._router_tool_requests[id(raising)] = raising
    driver._router_tool_requests[id(retired)] = retired

    driver.stop(interrupt=False)

    assert raising.rejection_attempts == 1
    assert retired.errors == [
        (-32000, "Helios session or router turn is no longer active", None)
    ]
    assert driver._closed
    assert hub.released == [driver]


@pytest.mark.parametrize("mode", ["plan", "dontAsk", "bypassPermissions"])
def test_noninteractive_modes_decline_unexpected_approval_without_dialog(
    driver_factory,
    mode,
):
    hub = FakeHub()
    driver = driver_factory(hub, mode=mode)
    questions = []
    driver.connect("question-asked", lambda *_args: questions.append(True))
    _start(driver)
    request = FakeRequest(
        "unexpected-approval",
        "item/commandExecution/requestApproval",
        {"threadId": driver.session_id, "command": "touch forbidden"},
    )

    driver._consume_app_request(request)

    assert request.responses == [{"decision": "decline"}]
    assert questions == []
    driver.stop(interrupt=False)


def test_resolved_notification_before_glib_request_delivery_never_prompts(
    driver_factory,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    questions = []
    driver.connect("question-asked", lambda *_args: questions.append(True))
    _start(driver)

    driver._resolve_interaction_by_request_id("raced-request")
    request = FakeRequest(
        "raced-request",
        "item/tool/requestUserInput",
        {"threadId": driver.session_id, "questions": []},
    )
    driver._consume_app_request(request)

    assert questions == []
    assert request.abandoned == 1
    driver.stop(interrupt=False)


def test_reused_server_request_id_gets_a_fresh_ui_token(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    tokens = []
    driver.connect(
        "question-asked",
        lambda _driver, _payload, token: tokens.append(token),
    )
    _start(driver)
    params = {"threadId": driver.session_id, "questions": []}

    first = FakeRequest(7, "item/tool/requestUserInput", params)
    driver._consume_app_request(first)
    driver._resolve_interaction_by_request_id(7)
    second = FakeRequest(7, "item/tool/requestUserInput", params)
    driver._consume_app_request(second)

    assert len(tokens) == 2
    assert tokens[0] != tokens[1]
    driver.stop(interrupt=False)


def test_teardown_declines_delivered_interactions_before_shared_release(
    driver_factory,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    approval = FakeRequest(
        9,
        "item/fileChange/requestApproval",
        {"threadId": driver.session_id, "itemId": "change"},
    )
    driver._consume_app_request(approval)

    driver.stop(interrupt=False)

    assert approval.responses == [{"decision": "decline"}]
    assert hub.released == [driver]


@pytest.mark.parametrize("mode", ["default", "auto", "plan", "dontAsk"])
def test_app_server_unavailable_never_falls_back_without_parity(
    driver_factory,
    mode,
):
    driver = driver_factory(
        FakeHub(acquire_error=RuntimeError("no app server")),
        mode=mode,
    )
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))

    _start(driver)

    assert driver._closed
    assert not driver.native_transport
    assert driver.fallback_reason == ""
    assert "will not fall back" in errors[0]
    assert "lifetime budgets" in errors[0]


def test_bypass_no_longer_falls_back_to_ungated_exec(driver_factory):
    """Explicit Bypass cannot revive the retired, ungated exec fallback."""
    driver = driver_factory(
        FakeHub(acquire_error=RuntimeError("no app server")),
        mode="bypassPermissions",
    )
    errors = []
    driver.connect("error", lambda _d, msg: errors.append(msg))

    _start(driver)

    assert not driver.native_transport
    assert driver.fallback_reason == ""  # never silently fell back
    assert errors and "will not fall back" in errors[0]


@pytest.mark.parametrize(
    "mode",
    ["default", "auto", "plan", "dontAsk", "bypassPermissions"],
)
def test_exec_transport_env_fails_closed_without_containment_parity(
    driver_factory, monkeypatch, mode
):
    monkeypatch.setenv("HELIOS_CODEX_TRANSPORT", "exec")
    hub = FakeHub()
    driver = driver_factory(hub, mode=mode)
    errors = []
    driver.connect("error", lambda _d, msg: errors.append(msg))

    _start(driver)

    assert driver._closed
    assert errors and "developer-instruction" in errors[0]
    assert "lifetime-budget guarantees" in errors[0]
    assert not driver.native_transport
    assert driver.fallback_reason == ""  # never silently fell back to exec
    assert hub.acquired == []  # returned before touching the hub


def test_exec_fallback_live_settings_reject_interactive_permissions(
    driver_factory,
):
    driver = driver_factory(FakeHub(), mode="dontAsk")
    callbacks = []

    assert not driver.set_permission_mode(
        "default", lambda success, detail: callbacks.append((success, detail))
    )
    assert driver.set_permission_mode(
        "plan", lambda success, detail: callbacks.append((success, detail))
    )
    assert driver.set_effort(
        "low", lambda success, detail: callbacks.append((success, detail))
    )

    assert driver.permission_mode == "plan"
    assert driver.effort_key == "low"
    assert [success for success, _detail in callbacks] == [False, True, True]


# --- turn/steer: a mid-turn message reaches the RUNNING turn ---------------


def _busy_native_turn(driver_factory):
    """A driver with an accepted native turn in flight."""
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("count to sixty")
    _drain_main_context()
    driver._app_turn_id = "turn-native"
    driver._busy = True
    return hub, driver


def test_steer_injects_into_the_live_turn_without_starting_another(driver_factory):
    """Measured against codex-cli 0.149: `turn/steer` returns the SAME turnId,
    no second turn/started arrives, and the model obeys the correction inside
    the running turn. Queueing could only ever deliver it afterwards."""
    hub, driver = _busy_native_turn(driver_factory)
    turn_starts = len([row for row in hub.requests if row[0] == "turn/start"])
    accepted = []
    driver.connect("prompt-accepted", lambda _d, text: accepted.append(text))

    assert driver.steer_user_text("actually, just say BANANA") is True
    _drain_main_context()

    steers = [row for row in hub.requests if row[0] == "turn/steer"]
    assert len(steers) == 1
    assert steers[0][1] == {
        "threadId": driver.session_id,
        "expectedTurnId": "turn-native",
        "input": [{"type": "text", "text": "actually, just say BANANA"}],
    }
    # The live turn is untouched: not re-sent, not interrupted.
    assert len([r for r in hub.requests if r[0] == "turn/start"]) == turn_starts
    assert not any(row[0] == "turn/interrupt" for row in hub.requests)
    # The bubble commits on App Server's confirmation, not on dispatch.
    assert accepted == ["actually, just say BANANA"]
    assert driver.queued_messages() == []
    driver.stop(interrupt=False)


def test_declined_steer_falls_back_to_the_queue_and_never_drops_the_text(
    driver_factory,
):
    """The turn usually ends between dispatch and delivery. That must degrade
    to exactly the old behaviour — a queued message — not a lost one. Only an
    AUTHORITATIVE App Server rejection may requeue; ambiguity must not (see
    the unconfirmed-steer test)."""
    hub = FakeHub()
    hub.request_results["turn/steer"] = CodexAppServerRpcError("turn already completed")
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("count to sixty")
    _drain_main_context()
    driver._app_turn_id = "turn-native"
    driver._busy = True
    accepted = []
    driver.connect("prompt-accepted", lambda _d, text: accepted.append(text))

    assert driver.steer_user_text("actually, just say BANANA") is True
    _drain_main_context()

    assert accepted == []
    assert driver.queued_messages() == [(1, "actually, just say BANANA")]
    driver.stop(interrupt=False)


def test_steer_declines_when_there_is_no_live_turn_so_the_window_queues(
    driver_factory,
):
    """`False` is the signal MainWindow uses to fall back to queueing."""
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)

    # Idle: nothing to steer into.
    assert driver.steer_user_text("hello") is False
    # Busy but no turn id yet — the turn has not been accepted.
    driver._busy = True
    driver._app_turn_id = ""
    assert driver.steer_user_text("hello") is False
    assert not any(row[0] == "turn/steer" for row in hub.requests)
    driver.stop(interrupt=False)


def test_second_steer_while_one_is_outstanding_refuses_so_the_window_queues(
    driver_factory,
):
    """One steer may await acknowledgement at a time.

    Without the latch, two quick submissions dispatch concurrent turn/steer
    RPCs; if the first fails back to the queue while the second lands, the
    user's messages reorder. Refusing the second sends it down the ordered
    queue path — and once the first resolves, steering is available again.
    """
    hub, driver = _busy_native_turn(driver_factory)
    accepted = []
    driver.connect("prompt-accepted", lambda _d, text: accepted.append(text))

    assert driver.steer_user_text("first correction") is True
    # In flight: the successor must refuse, not race.
    assert driver.steer_user_text("second correction") is False
    assert len([row for row in hub.requests if row[0] == "turn/steer"]) == 1
    _drain_main_context()

    assert accepted == ["first correction"]
    # Resolved: the latch is clear and steering works again.
    assert driver.steer_user_text("third correction") is True
    _drain_main_context()
    assert len([row for row in hub.requests if row[0] == "turn/steer"]) == 2
    assert accepted == ["first correction", "third correction"]
    driver.stop(interrupt=False)


def test_failed_steer_clears_the_latch_before_requeueing(driver_factory):
    """A declined steer must not wedge steering for the rest of the turn."""
    hub = FakeHub()
    hub.request_results["turn/steer"] = CodexAppServerRpcError("turn already completed")
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("count to sixty")
    _drain_main_context()
    driver._app_turn_id = "turn-native"
    driver._busy = True

    assert driver.steer_user_text("first correction") is True
    assert driver.steer_user_text("second correction") is False
    _drain_main_context()

    # The failure requeued the text and released the latch.
    assert driver.queued_messages() == [(1, "first correction")]
    assert driver._steer_inflight is False
    driver.stop(interrupt=False)


def test_declined_steer_keeps_its_place_ahead_of_messages_queued_in_flight(
    driver_factory,
):
    """The reorder Jeeves caught: steer A in flight, submission B refused and
    queued by the window, then A authoritatively declined. An append would
    deliver B before A; the head insert restores submission order. The guard
    guarantees the queue was empty when A dispatched, so head == A's original
    position."""
    hub = FakeHub()
    hub.request_results["turn/steer"] = CodexAppServerRpcError("turn already completed")
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("count to sixty")
    _drain_main_context()
    driver._app_turn_id = "turn-native"
    driver._busy = True

    assert driver.steer_user_text("first correction") is True
    # The window's fallback for the refused second submission: the queue.
    assert driver.steer_user_text("second submission") is False
    driver.queue_user_text("second submission")
    _drain_main_context()

    assert [text for _qid, text in driver.queued_messages()] == [
        "first correction",
        "second submission",
    ]
    driver.stop(interrupt=False)


def test_steer_refuses_while_the_ordinary_queue_holds_messages(driver_factory):
    """Queued messages own the order: steering around them would put a later
    submission into the turn ahead of an earlier one."""
    hub, driver = _busy_native_turn(driver_factory)
    driver.queue_user_text("second submission")

    assert driver.steer_user_text("third submission") is False
    assert not any(row[0] == "turn/steer" for row in hub.requests)
    assert driver.queued_messages() == [(1, "second submission")]
    driver.stop(interrupt=False)


def test_unconfirmed_steer_is_not_resent_and_says_so(driver_factory):
    """A timeout proves nothing — App Server may have applied the steer while
    the response was lost, and a steer has no idempotency key. The only
    duplicate-safe move is to not resend and tell the user exactly that."""
    hub = FakeHub()
    hub.request_results["turn/steer"] = CodexAppServerTimeout(
        "no response within 30.0s"
    )
    driver = driver_factory(hub)
    _start(driver)
    driver.send_user_text("count to sixty")
    _drain_main_context()
    driver._app_turn_id = "turn-native"
    driver._busy = True
    accepted: list[str] = []
    errors: list[str] = []
    driver.connect("prompt-accepted", lambda _d, text: accepted.append(text))
    driver.connect("error", lambda _d, message: errors.append(message))

    assert driver.steer_user_text("first correction") is True
    _drain_main_context()

    assert accepted == []
    # Not requeued — a resend could duplicate the contribution.
    assert driver.queued_messages() == []
    assert driver._steer_inflight is False
    assert len(errors) == 1
    assert "first correction" in errors[0]
    assert "not re-sent" in errors[0]
    driver.stop(interrupt=False)


# ── provider-native Review and Fork ─────────────────────────────────────


def test_native_review_narrows_settings_then_uses_authoritative_response_turn(
    driver_factory,
):
    hub = FakeHub()
    settings: Future = Future()
    review: Future = Future()
    hub.request_results["thread/settings/update"] = settings
    hub.request_results["review/start"] = review
    driver = driver_factory(hub)
    acknowledgements: list[str] = []
    accepted: list[str] = []
    results: list[dict] = []
    statuses: list[dict] = []
    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt(
            f"work:{text}",
            lambda: acknowledgements.append(text),
        )
    )
    driver.connect("prompt-accepted", lambda _driver, text: accepted.append(text))
    driver.connect("result", lambda _driver, payload: results.append(payload))
    driver.connect(
        "turn-status-updated",
        lambda _driver, payload: statuses.append(payload),
    )
    _start(driver)

    delivery = driver.request_review("focus on concurrency")

    assert delivery.pending
    assert driver.is_busy
    assert driver.execution_attempt_id == ""
    review_control_requests = [
        row
        for row in hub.requests
        if row[0] in {"thread/settings/update", "review/start"}
    ]
    assert [method for method, *_ in review_control_requests] == [
        "thread/settings/update"
    ]
    settings_params = review_control_requests[0][1]
    assert settings_params["approvalPolicy"] == "never"
    assert settings_params["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": False,
    }

    settings.set_result({})
    _drain_main_context()

    review_request = next(row for row in hub.requests if row[0] == "review/start")
    assert review_request[1]["delivery"] == "inline"
    assert review_request[1]["target"] == {
        "type": "custom",
        "instructions": "work:focus on concurrency",
    }
    assert driver.execution_attempt_id == "attempt-1"

    # Live App Server emits this internal reviewer identity before returning
    # review/start's actual source-turn identity. It must not accept the user
    # command, authorize tools, or become the Stop target.
    driver.on_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": driver.session_id,
            "turn": {"id": "internal-reviewer-turn", "status": "inProgress"},
        },
    )
    _drain_main_context()
    assert driver._app_turn_id == ""
    assert driver._router_authorized_turn_id == ""
    assert accepted == []
    assert acknowledgements == []
    assert statuses == []

    review.set_result(
        {
            "reviewThreadId": "thread-native",
            "turn": {"id": "review-source-turn", "status": "inProgress"},
        }
    )
    _drain_main_context()

    assert driver._app_turn_id == "review-source-turn"
    assert driver._router_authorized_turn_id == ""
    assert accepted == ["/review focus on concurrency"]
    assert acknowledgements == ["focus on concurrency"]
    assert driver._mirror.users == ["/review focus on concurrency"]
    assert statuses == [
        {
            "threadId": "thread-native",
            "turnId": "review-source-turn",
            "status": "inProgress",
            "durationMs": None,
            "error": None,
        }
    ]

    driver._consume_app_notification(
        events.METHOD_ITEM_COMPLETED,
        {
            "threadId": driver.session_id,
            "turnId": "review-source-turn",
            "item": {
                "id": "review-answer",
                "type": "agentMessage",
                "text": "One finding",
                "phase": "final_answer",
            },
        },
    )
    driver._consume_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": driver.session_id,
            "turn": {
                "id": "review-source-turn",
                "status": "completed",
                "items": [],
            },
        },
    )

    assert results and results[0]["subtype"] == "success"
    assert driver.execution_attempt_id == ""
    assert driver._mirror.assistant[0][3] == "review-source-turn"
    driver.stop(interrupt=False)


def test_review_settings_failure_never_admits_or_dispatches_model_work(
    driver_factory,
):
    hub = FakeHub()
    hub.request_results["thread/settings/update"] = CodexAppServerRpcError(
        "settings refused"
    )
    driver = driver_factory(hub)
    errors: list[str] = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    assert driver.request_review().pending
    _drain_main_context()

    assert not any(method == "review/start" for method, *_ in hub.requests)
    assert driver.execution_attempt_id == ""
    assert driver._test_execution_events == []
    assert not driver.is_busy
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_REJECTED
    assert errors and "read-only settings could not be confirmed" in errors[-1]
    driver.stop(interrupt=False)


def test_stop_during_review_settings_prevents_model_dispatch(driver_factory):
    hub = FakeHub()
    settings: Future = Future()
    hub.request_results["thread/settings/update"] = settings
    driver = driver_factory(hub)
    errors: list[str] = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    assert driver.request_review().pending
    driver.stop()
    settings.set_result({})
    _drain_main_context()

    assert not any(method == "review/start" for method, *_ in hub.requests)
    assert driver.execution_attempt_id == ""
    assert not driver.is_busy
    assert errors and "stopped before model execution" in errors[-1]
    driver.stop(interrupt=False)


def test_review_start_timeout_is_held_and_never_replayed(driver_factory):
    hub = FakeHub()
    hub.request_results["thread/settings/update"] = {}
    hub.request_results["review/start"] = CodexAppServerTimeout("review timeout")
    driver = driver_factory(hub)
    errors: list[str] = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    assert driver.request_review().pending
    _drain_main_context()

    assert driver._closed
    assert driver.execution_attempt_id == "attempt-1"
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_UNKNOWN
    assert len([row for row in hub.requests if row[0] == "review/start"]) == 1
    assert errors and "Review acceptance is unknown" in errors[-1]


def test_review_on_an_unexpected_thread_is_held_as_ambiguous(driver_factory):
    hub = FakeHub()
    hub.request_results["thread/settings/update"] = {}
    hub.request_results["review/start"] = {
        "reviewThreadId": "different-thread",
        "turn": {"id": "review-turn", "status": "inProgress"},
    }
    driver = driver_factory(hub)
    accepted: list[str] = []
    errors: list[str] = []
    driver.connect("prompt-accepted", lambda _driver, text: accepted.append(text))
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    assert driver.request_review().pending
    _drain_main_context()

    assert driver._closed
    assert driver.execution_attempt_id == "attempt-1"
    assert driver._native_delivery_state_snapshot() == NATIVE_DELIVERY_UNKNOWN
    assert accepted == []
    assert errors and "Review acceptance is unknown" in errors[-1]


def test_native_fork_is_persisted_fresh_and_owns_no_execution_attempt(
    driver_factory,
):
    hub = FakeHub()
    hub.request_results["thread/fork"] = {
        "thread": {
            "id": "fork-thread",
            "forkedFromId": "thread-native",
            "turns": [{"id": "turn-1"}],
        }
    }
    driver = driver_factory(hub)
    forked: list[dict] = []
    driver.connect("thread-forked", lambda _driver, payload: forked.append(payload))
    _start(driver)

    delivery = driver.request_fork()
    assert delivery.pending
    _drain_main_context()

    request = next(row for row in hub.requests if row[0] == "thread/fork")
    assert request[1]["threadId"] == "thread-native"
    assert request[1]["ephemeral"] is False
    assert "deferGoalContinuation" not in request[1]
    assert request[1]["config"]["agents"]["enabled"] is False
    assert forked == [
        {
            "source_thread_id": "thread-native",
            "thread_id": "fork-thread",
            "forked_from_id": "thread-native",
            "turn_count": 1,
            "stop_requested": False,
        }
    ]
    assert driver._test_execution_events == []
    assert not driver.is_busy
    driver.stop(interrupt=False)


def test_stopped_fork_releases_a_later_queued_prompt_after_reconciliation(
    driver_factory,
):
    hub = FakeHub()
    fork: Future = Future()
    hub.request_results["thread/fork"] = fork
    driver = driver_factory(hub)
    forked: list[dict] = []
    driver.connect("thread-forked", lambda _driver, payload: forked.append(payload))
    _start(driver)

    assert driver.request_fork().pending
    driver.stop()
    driver.queue_user_text("continue on the source")
    fork.set_result(
        {
            "thread": {
                "id": "fork-thread",
                "forkedFromId": "thread-native",
                "ephemeral": False,
                "turns": [],
            }
        }
    )
    _drain_main_context()

    assert forked[0]["stop_requested"] is True
    turn_requests = [row for row in hub.requests if row[0] == "turn/start"]
    assert len(turn_requests) == 1
    assert turn_requests[0][1]["input"][0]["text"] == "continue on the source"
    assert driver._app_turn_id == "turn-native"
    driver.stop(interrupt=False)


def test_fork_timeout_is_not_replayed_or_reported_as_created(driver_factory):
    hub = FakeHub()
    hub.request_results["thread/fork"] = CodexAppServerTimeout("fork timeout")
    driver = driver_factory(hub)
    forked: list[dict] = []
    errors: list[str] = []
    driver.connect("thread-forked", lambda _driver, payload: forked.append(payload))
    driver.connect("error", lambda _driver, message: errors.append(message))
    _start(driver)

    assert driver.request_fork().pending
    _drain_main_context()

    assert forked == []
    assert len([row for row in hub.requests if row[0] == "thread/fork"]) == 1
    assert not driver.is_busy
    assert errors and "outcome is unknown" in errors[-1]
    driver.stop(interrupt=False)


# ── provider-native context compaction ───────────────────────────────────


def test_manual_compaction_is_an_isolated_native_maintenance_turn(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    context_boundaries: list[dict] = []
    results: list[dict] = []
    statuses: list[dict] = []
    plans: list[dict] = []
    contributions: list[object] = []
    driver.connect(
        "context-compacted",
        lambda _driver, payload: context_boundaries.append(payload),
    )
    driver.connect("result", lambda _driver, payload: results.append(payload))
    driver.connect(
        "turn-status-updated",
        lambda _driver, payload: statuses.append(payload),
    )
    driver.connect(
        "turn-appended",
        lambda _driver, turn: contributions.append(turn),
    )
    driver.connect("plan-updated", lambda _driver, payload: plans.append(payload))
    prior_accumulator_turn = driver._app_acc.turn_id

    assert driver.request_compaction() is True
    assert driver.is_busy
    assert hub.requests[-1] == (
        "thread/compact/start",
        {"threadId": "thread-native"},
        20.0,
    )

    driver.on_app_notification(
        events.METHOD_TURN_STARTED,
        {
            "threadId": "thread-native",
            "turn": {"id": "turn-compact", "status": "inProgress"},
        },
    )
    driver.on_app_notification(
        events.METHOD_ITEM_STARTED,
        {
            "threadId": "thread-native",
            "turnId": "turn-compact",
            "item": {"id": "item-compact", "type": "contextCompaction"},
        },
    )
    driver.on_app_notification(
        events.METHOD_ITEM_COMPLETED,
        {
            "threadId": "thread-native",
            "turnId": "turn-compact",
            "item": {"id": "item-compact", "type": "contextCompaction"},
        },
    )
    # Any future turn-scoped metadata on the maintenance turn is fail-closed:
    # it must not be mistaken for the model-owned execution plan.
    driver.on_app_notification(
        events.METHOD_TURN_PLAN_UPDATED,
        {
            "threadId": "thread-native",
            "turnId": "turn-compact",
            "plan": [{"step": "not model work", "status": "completed"}],
        },
    )
    driver.on_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": "thread-native",
            "turn": {
                "id": "turn-compact",
                "status": "completed",
                "items": [],
            },
        },
    )
    _drain_main_context()

    assert not driver.is_busy
    assert driver._router_authorized_turn_id == ""
    assert driver._app_acc.turn_id == prior_accumulator_turn
    assert driver._test_execution_events == []
    assert results == []
    assert statuses == []
    assert plans == []
    assert contributions == []
    assert driver._mirror.compactions == [
        {
            "trigger": "manual",
            "pre_tokens": 0,
            "post_tokens": 0,
            "turn_id": "turn-compact",
            "item_id": "item-compact",
        }
    ]
    assert context_boundaries == [
        {
            "trigger": "manual",
            "pre_tokens": 0,
            "post_tokens": 0,
            "turn_id": "turn-compact",
            "item_id": "item-compact",
            "source": "codex-app-server",
        }
    ]
    driver.stop(interrupt=False)


def test_manual_compaction_rpc_rejection_returns_driver_to_idle(driver_factory):
    hub = FakeHub()
    hub.request_results["thread/compact/start"] = CodexAppServerRpcError(
        "compaction unavailable"
    )
    driver = driver_factory(hub)
    _start(driver)
    errors: list[str] = []
    driver.connect("error", lambda _driver, message: errors.append(message))

    assert driver.request_compaction() is True
    _drain_main_context()

    assert not driver.is_busy
    assert not driver._manual_compaction_pending
    assert driver.is_accepting_input
    assert driver._mirror.compactions == []
    assert errors and "declined context compaction" in errors[-1]
    driver.stop(interrupt=False)


def test_manual_compaction_terminal_payload_repairs_missed_item_events(
    driver_factory,
):
    driver = driver_factory(FakeHub())
    _start(driver)
    assert driver.request_compaction() is True

    driver.on_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": "thread-native",
            "turn": {
                "id": "turn-repaired-compact",
                "status": "completed",
                "items": [
                    {"id": "item-repaired-compact", "type": "contextCompaction"}
                ],
            },
        },
    )
    _drain_main_context()

    assert not driver.is_busy
    assert driver._mirror.compactions[0]["turn_id"] == "turn-repaired-compact"
    assert driver._mirror.compactions[0]["item_id"] == "item-repaired-compact"
    assert driver._test_execution_events == []
    driver.stop(interrupt=False)


def test_failed_compaction_releases_a_queued_user_turn_after_terminal_evidence(
    driver_factory,
):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    assert driver.request_compaction() is True
    driver.queue_user_text("continue after maintenance")

    driver.on_app_notification(
        events.METHOD_TURN_COMPLETED,
        {
            "threadId": "thread-native",
            "turn": {
                "id": "turn-failed-compact",
                "status": "failed",
                "items": [],
                "error": {"message": "nothing to compact"},
            },
        },
    )
    _drain_main_context()

    assert any(method == "turn/start" for method, *_ in hub.requests)
    assert driver.is_busy
    assert driver.queued_messages() == []
    assert driver._mirror.users == ["continue after maintenance"]
    driver.stop(interrupt=False)


def test_manual_compaction_refuses_a_busy_user_turn(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    errors: list[str] = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    driver._busy = True

    assert driver.request_compaction() is False

    assert not any(method == "thread/compact/start" for method, *_ in hub.requests)
    assert errors and "current provider operation" in errors[-1]
    driver._busy = False
    driver.stop(interrupt=False)


def test_automatic_compaction_action_persists_once(driver_factory):
    driver = driver_factory(FakeHub())
    _start(driver)
    seen: list[dict] = []
    driver.connect(
        "context-compacted",
        lambda _driver, payload: seen.append(payload),
    )
    driver._last_context_used = 1234

    driver._dispatch_app_action(
        events.Action(
            events.ACT_CONTEXT_COMPACTED,
            {
                "threadId": "thread-native",
                "turnId": "turn-user",
                "itemId": "item-auto",
                "lifecycle": "started",
            },
        )
    )
    driver._last_context_used = 456
    completed = events.Action(
        events.ACT_CONTEXT_COMPACTED,
        {
            "threadId": "thread-native",
            "turnId": "turn-user",
            "itemId": "item-auto",
            "lifecycle": "completed",
        },
    )
    driver._dispatch_app_action(completed)
    driver._dispatch_app_action(completed)

    assert driver._mirror.compactions == [
        {
            "trigger": "auto",
            "pre_tokens": 1234,
            "post_tokens": 456,
            "turn_id": "turn-user",
            "item_id": "item-auto",
        }
    ]
    assert len(seen) == 1
    assert seen[0]["trigger"] == "auto"
    driver.stop(interrupt=False)


@pytest.mark.parametrize("resume", ["", "thread-native"])
def test_bypass_uses_native_full_access_on_start_resume_and_turn(driver_factory, resume):
    hub = FakeHub()
    driver = driver_factory(hub, mode="bypassPermissions", resume=resume)
    _start(driver)
    method = "thread/resume" if resume else "thread/start"
    params = next(row[1] for row in hub.calls if row[0] == method)
    assert driver.permission_mode == "bypassPermissions"
    assert params["sandbox"] == "danger-full-access"
    assert params["approvalPolicy"] == "never"
    assert params["config"]["agents"] == {"enabled": False}
    driver.send_user_text("continue")
    _drain_main_context()
    turn = next(row[1] for row in hub.requests if row[0] == "turn/start")
    assert turn["sandboxPolicy"] == {"type": "dangerFullAccess"}
    assert turn["approvalPolicy"] == "never"
    assert driver.is_busy
    driver.stop(interrupt=True)
    _drain_main_context()
    assert any(row[0] == "turn/interrupt" for row in hub.requests)


def test_live_permission_change_enters_and_leaves_bypass_without_rebinding(driver_factory):
    hub = FakeHub()
    driver = driver_factory(hub)
    _start(driver)
    assert driver.set_permission_mode("bypassPermissions")
    driver.send_user_text("full access")
    _drain_main_context()
    driver._dispatch_app_action(SimpleNamespace(
        kind=events.ACT_RESULT, payload={"subtype": "success", "usage": {}},
    ))
    assert driver.set_permission_mode("default")
    driver.send_user_text("project access")
    _drain_main_context()
    turns = [row[1] for row in hub.requests if row[0] == "turn/start"]
    assert [p["sandboxPolicy"]["type"] for p in turns] == ["dangerFullAccess", "workspaceWrite"]
    assert [p["approvalPolicy"] for p in turns] == ["never", "on-request"]
    assert len([row for row in hub.calls if row[0] in {"thread/start", "thread/resume"}]) == 1
    driver.stop(interrupt=False)
