"""Unit tests for Gemini CLI driver and Google environment introspection."""

from __future__ import annotations

import json
import io
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib

from helios.backend import google_env
from helios.backend.process import gemini_driver as gd
from helios.backend.process.driver_manager import DriverManager
from helios.backend.process.gemini_driver import GeminiCliDriver, GeminiDriverSpawnError
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence,
    ExecutionDispatchEvidence,
    ExecutionTerminalEvidence,
    MessageDelivery,
    PreparedPrompt,
    RequiredPromptContextError,
)
from helios.backend.process.streaming import StreamingAssistant


MODEL = "gemini-3.8-flash-medium"
NATIVE_ID = "c3b66b04-872b-4fbe-a3a4-058a026ef20a"


def _drain_main_context():
    while GLib.MainContext.default().iteration(False):
        pass


@pytest.fixture(autouse=True)
def drain_driver_callbacks():
    _drain_main_context()
    yield
    _drain_main_context()


class _FakeStdin:
    """Record complete attempted frames, including ambiguous failed writes."""

    def __init__(self):
        self.writes = []
        self.fail_after_write = False
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        if self.fail_after_write:
            raise BrokenPipeError("pipe closed after accepting bytes")
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True

    def frames(self):
        return [json.loads(data.decode("utf-8")) for data in self.writes]


class _FakeProcess:
    def __init__(self):
        self.pid = 123456
        self.stdin = _FakeStdin()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else 0


class _InertThread:
    """Startup is tested without racing reader threads against manual frames."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


def _init_frame(cwd, conversation_id=NATIVE_ID):
    # This is the official agy envelope, not Claude's type/system protocol.
    return {
        "event": "init",
        "conversation_id": conversation_id,
        "init": {
            "cwd": str(cwd),
            "model": MODEL,
            "permission_mode": "request-review",
            "tools": ["view_file", "run_command", "write_to_file"],
        },
    }


def _step(step_type, step_index, **fields):
    return {
        "event": "step_update",
        "step_update": {
            "conversation_id": NATIVE_ID,
            "step_index": step_index,
            "state": "DONE",
            "step_type": step_type,
            **fields,
        },
    }


def _result(status="SUCCESS", response="answer", num_turns=1, **fields):
    return {
        "event": "result",
        "result": {
            "conversation_id": NATIVE_ID,
            "status": status,
            "response": response,
            "duration_seconds": 2.0,
            "num_turns": num_turns,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "thinking_tokens": 5,
                "cache_read_tokens": 20,
                "total_tokens": 110,
            },
            **fields,
        },
    }


@pytest.fixture
def driver_factory(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        gd,
        "find_google_binary",
        lambda: google_env.GoogleBinary(Path("/test-bin/agy"), source="test"),
    )
    monkeypatch.setattr(gd.threading, "Thread", _InertThread)
    monkeypatch.setattr(gd.os, "killpg", lambda *_args: None)
    processes = []

    def popen(command, **kwargs):
        proc = _FakeProcess()
        proc.command = command
        proc.spawn_kwargs = kwargs
        processes.append(proc)
        return proc

    monkeypatch.setattr(gd.subprocess, "Popen", popen)

    def make(*, resume=None, ready=True, model=MODEL, permission_mode="dontAsk"):
        driver = GeminiCliDriver(
            cwd=str(project),
            model=model,
            permission_mode=permission_mode,
            resume_session_id=resume,
        )
        driver._test_errors = []
        driver._test_lifecycle = []
        driver.connect("error", lambda _driver, error: driver._test_errors.append(error))
        sequence = iter(range(1, 100))

        def admit(_driver):
            attempt = f"attempt-{next(sequence)}"
            driver._test_lifecycle.append(("admit", attempt))
            return attempt, ""

        def dispatch(_driver, attempt, evidence):
            driver._test_lifecycle.append(("dispatch", attempt, evidence))

        def accept(_driver, attempt, evidence):
            driver._test_lifecycle.append(("accept", attempt, evidence))

        def contribution(_driver, turn):
            driver._test_lifecycle.append(("contribution", turn))
            return turn

        def finish(_driver, attempt, evidence):
            driver._test_lifecycle.append(("terminal", attempt, evidence))

        driver.set_execution_attempt_controller(
            admit,
            lambda *_args: pytest.fail("Google must use the typed terminal adapter"),
            record_dispatch=dispatch,
            record_acceptance=accept,
            record_stop=lambda *_args: None,
            record_contribution=contribution,
            finish_with_evidence=finish,
        )
        driver.start()
        _drain_main_context()
        driver._test_process = processes[-1]
        if ready:
            driver._dispatch_message(_init_frame(project, resume or NATIVE_ID))
            _drain_main_context()
            # MainWindow verifies the native identity before turn admission.
            driver._helios_identity_confirmed = True
        return driver

    return make


def test_find_google_binary_env_var(monkeypatch, tmp_path):
    custom_bin = tmp_path / "custom_agy"
    custom_bin.write_text("#!/bin/sh\necho ok\n")
    custom_bin.chmod(0o755)

    monkeypatch.setenv("HELIOS_GOOGLE_BINARY", str(custom_bin))
    assert google_env.find_google_binary().path == custom_bin


def test_find_google_binary_not_found(monkeypatch):
    monkeypatch.delenv("HELIOS_GOOGLE_BINARY", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(google_env.GoogleBinaryNotFound):
        google_env.find_google_binary()


def test_list_mcp_servers(tmp_path, monkeypatch):
    config_dir = tmp_path / ".gemini" / "config"
    config_dir.mkdir(parents=True)
    mcp_file = config_dir / "mcp_config.json"
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test-server": {
                        "command": "node",
                        "args": ["server.js"],
                    }
                }
            }
        )
    )

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    servers = google_env.list_mcp_servers()
    assert len(servers) == 1
    assert servers[0]["name"] == "test-server"
    assert servers[0]["command"] == "node"


def test_gemini_driver_initial_state(tmp_path):
    driver = GeminiCliDriver(cwd=str(tmp_path), model=MODEL)
    assert driver.display_name == "gemini"
    assert driver.provider == "google"
    assert driver.is_running is False
    assert driver.is_busy is False
    assert driver.is_accepting_input is False


def test_start_uses_persistent_agy_flags_and_sandbox(driver_factory):
    driver = driver_factory(ready=False)
    command = driver._test_process.command
    assert command[0] == "/test-bin/agy"
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert command[command.index("--model") + 1] == MODEL
    assert "--sandbox" in command
    assert "--print" not in command
    assert "--resume" not in command
    assert "--dangerously-skip-permissions" not in command
    assert not driver.is_accepting_input


def test_send_before_verified_native_init_cannot_write(driver_factory):
    driver = driver_factory(ready=False)

    outcome = driver.send_user_text("must remain local")

    assert outcome.rejected
    assert driver._test_process.stdin.writes == []
    assert driver._test_lifecycle == []


def test_missing_selected_model_fails_before_spawn(driver_factory):
    with pytest.raises((ValueError, GeminiDriverSpawnError)):
        driver_factory(model="")


def test_start_does_not_invent_a_native_session_id(driver_factory):
    driver = driver_factory(ready=False)
    started = []
    driver.connect("session-started", lambda _driver, *args: started.append(args))
    assert driver.session_id == ""
    assert started == []

    driver._dispatch_message(_init_frame(driver._cwd))
    _drain_main_context()

    assert driver.session_id == NATIVE_ID
    assert started == [(NATIVE_ID, driver._cwd, MODEL)]
    assert driver.is_accepting_input


def test_resume_binds_only_the_requested_native_conversation(driver_factory):
    driver = driver_factory(resume=NATIVE_ID, ready=False)
    command = driver._test_process.command
    assert command[command.index("--conversation") + 1] == NATIVE_ID
    started = []
    driver.connect("session-started", lambda _driver, *args: started.append(args))

    driver._dispatch_message(_init_frame(driver._cwd, "different-native-id"))
    _drain_main_context()

    assert started == []
    assert not driver.is_accepting_input
    assert driver._test_errors
    assert driver.send_user_text("must remain local").rejected
    assert driver._test_process.stdin.writes == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "gemini-other-model"),
        ("cwd", "/different/project"),
        ("permission_mode", "always-proceed"),
    ],
)
def test_native_init_must_match_selected_execution_policy(driver_factory, field, value):
    driver = driver_factory(ready=False)
    frame = _init_frame(driver._cwd)
    frame["init"][field] = value

    driver._dispatch_message(frame)
    _drain_main_context()

    assert driver.session_id == ""
    assert not driver.is_accepting_input
    assert driver._test_errors
    assert driver._test_process.stdin.writes == []


def test_second_init_cannot_rebind_a_running_work(driver_factory):
    driver = driver_factory()
    driver.send_user_text("hello")
    started = []
    driver.connect("session-started", lambda _driver, *args: started.append(args))

    driver._dispatch_message(_init_frame(driver._cwd, "different-native-id"))
    _drain_main_context()

    assert driver.session_id == NATIVE_ID
    assert started == []
    assert not driver.is_accepting_input
    assert driver.execution_attempt_id == "attempt-1"
    assert not any(row[0] == "terminal" for row in driver._test_lifecycle)


def test_native_signals_used_by_main_window_connect(driver_factory):
    driver = driver_factory()
    driver.connect("turn-status-updated", lambda *_args: None)
    driver.connect("delivery-confirmed", lambda *_args: None)


def test_execution_guard_denial_cannot_write_to_google(driver_factory):
    driver = driver_factory()
    driver.set_execution_guard(lambda _driver: "This Work is paused.")

    outcome = driver.send_user_text("must remain local")

    assert isinstance(outcome, MessageDelivery)
    assert outcome.rejected
    assert driver._test_process.stdin.writes == []
    assert driver._test_lifecycle == []
    assert driver._test_errors == ["This Work is paused."]


def test_required_context_failure_cannot_write_to_google(driver_factory):
    driver = driver_factory()

    def unavailable_context(_driver, _text):
        raise RequiredPromptContextError()

    driver.set_prompt_context_provider(unavailable_context)

    outcome = driver.send_user_text("must remain local")

    assert outcome.rejected
    assert driver._test_process.stdin.writes == []
    assert driver._test_lifecycle == []
    assert driver._test_errors == [RequiredPromptContextError.user_message]


def test_prepared_context_is_the_exact_dispatched_frame(driver_factory):
    driver = driver_factory()
    acknowledged = []
    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt(
            f"Required Work context\n{text}", lambda: acknowledged.append(True)
        )
    )
    confirmed = []
    driver.connect("delivery-confirmed", lambda _driver: confirmed.append(True))

    outcome = driver.send_user_text("hello")

    assert isinstance(outcome, MessageDelivery)
    assert outcome.accepted or outcome.pending
    assert driver._test_process.stdin.frames() == [{
        "event": "user",
        "message": {"content": "Required Work context\nhello"},
    }]
    dispatch = next(row[2] for row in driver._test_lifecycle if row[0] == "dispatch")
    assert isinstance(dispatch, ExecutionDispatchEvidence)
    assert dispatch.wire_prompt_text == "Required Work context\nhello"
    assert dispatch.native_binding_id == NATIVE_ID
    assert not acknowledged and not confirmed

    driver._dispatch_message(_step("user_input", 0))
    _drain_main_context()

    assert acknowledged == [True]
    assert confirmed == [True]
    acceptance = next(row[2] for row in driver._test_lifecycle if row[0] == "accept")
    assert isinstance(acceptance, ExecutionAcceptanceEvidence)
    assert acceptance.native_binding_id == NATIVE_ID
    assert driver.execution_attempt_id == "attempt-1"

    driver._dispatch_message(_step("user_input", 0))
    _drain_main_context()
    assert acknowledged == [True]
    assert confirmed == [True]


def test_dispatch_persistence_failure_never_writes(driver_factory):
    driver = driver_factory()

    def broken_dispatch(*_args):
        raise RuntimeError("ledger unavailable")

    driver.set_execution_attempt_controller(
        driver._execution_attempt_admitter,
        driver._execution_attempt_finisher,
        record_dispatch=broken_dispatch,
    )

    outcome = driver.send_user_text("must remain local")

    assert outcome.rejected
    assert driver._test_process.stdin.writes == []
    assert driver.execution_attempt_id == ""
    terminal = [row[2] for row in driver._test_lifecycle if row[0] == "terminal"]
    assert len(terminal) == 1
    assert terminal[0].evidence_type == "local_abort"


def test_ambiguous_write_holds_lane_and_quarantines_queued_head(driver_factory):
    driver = driver_factory()
    driver._test_process.stdin.fail_after_write = True
    sent = []
    driver.connect("queued-user-sent", lambda _driver, qid, text: sent.append((qid, text)))
    qid = driver.queue_user_text("must not replay")

    driver._flush_user_queue()
    driver._flush_user_queue()
    _drain_main_context()

    assert len(driver._test_process.stdin.writes) == 1
    assert driver.execution_attempt_id == "attempt-1"
    assert not driver.is_accepting_input
    assert not sent
    assert driver._uncertain_queue_delivery == (qid, "must not replay")
    assert driver.take_queued() == []
    assert not any(row[0] == "terminal" for row in driver._test_lifecycle)


def test_late_native_acceptance_promotes_one_ambiguous_queued_head(driver_factory):
    driver = driver_factory()
    driver._test_process.stdin.fail_after_write = True
    sent = []
    driver.connect("queued-user-sent", lambda _driver, qid, text: sent.append((qid, text)))
    qid = driver.queue_user_text("accepted despite pipe failure")
    driver._flush_user_queue()
    assert driver._uncertain_queue_delivery == (qid, "accepted despite pipe failure")

    driver._dispatch_message(_step("user_input", 0))
    driver._dispatch_message(_step("user_input", 0))
    _drain_main_context()

    assert sent == [(qid, "accepted despite pipe failure")]
    assert driver.queued_messages() == []
    assert driver._uncertain_queue_delivery is None
    assert driver.execution_attempt_id == "attempt-1"
    assert len(driver._test_process.stdin.writes) == 1


def test_step_updates_render_the_same_shape_as_other_drivers(driver_factory):
    driver = driver_factory()
    streaming = []
    turns = []
    results = []
    driver.connect("assistant-streaming", lambda _driver, value: streaming.append(value))
    driver.connect("turn-appended", lambda _driver, turn: turns.append(turn))
    driver.connect("result", lambda _driver, result: results.append(result))
    driver.send_user_text("hello")
    driver._dispatch_message(_step("user_input", 0))
    driver._dispatch_message(_step("agent_response", 2, state="ACTIVE", text_delta="hello "))
    driver._dispatch_message(_step("agent_response", 2, text_delta="world\n"))
    _drain_main_context()

    assert streaming
    assert isinstance(streaming[-1], StreamingAssistant)
    assert "".join(block.text for block in streaming[-1].blocks if block.type == "text") == "hello world\n"

    driver._dispatch_message(_result(response="hello world\n"))
    _drain_main_context()

    assistant = [turn for turn in turns if turn.role == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].text == "hello world\n"
    assert len(results) == 1
    assert results[0]["subtype"] == "success"
    assert results[0]["usage"]["input_tokens"] == 100
    assert driver.execution_attempt_id == ""
    contribution = next(i for i, row in enumerate(driver._test_lifecycle) if row[0] == "contribution")
    terminal = next(i for i, row in enumerate(driver._test_lifecycle) if row[0] == "terminal")
    assert contribution < terminal
    evidence = driver._test_lifecycle[terminal][2]
    assert isinstance(evidence, ExecutionTerminalEvidence)
    assert evidence.evidence_type == "provider_terminal"
    assert evidence.status == "completed"
    assert evidence.native_id == NATIVE_ID


@pytest.mark.parametrize("status", ["ERROR", "CANCELED", "INTERRUPTED", "INVALID"])
def test_native_failure_is_not_reported_as_success(driver_factory, status):
    driver = driver_factory()
    results = []
    driver.connect("result", lambda _driver, result: results.append(result))
    driver.send_user_text("hello")
    driver._dispatch_message(_step("user_input", 0))
    driver._dispatch_message(_result(status=status, response=""))
    _drain_main_context()

    assert len(results) == 1
    assert results[0]["subtype"] != "success"
    terminal = next(row[2] for row in driver._test_lifecycle if row[0] == "terminal")
    assert terminal.status != "completed"


def test_contribution_persistence_failure_keeps_lane_reserved(driver_factory):
    driver = driver_factory()
    driver.set_execution_attempt_controller(
        driver._execution_attempt_admitter,
        driver._execution_attempt_finisher,
        record_contribution=lambda *_args: None,
    )
    driver.send_user_text("hello")
    driver._dispatch_message(_step("user_input", 0))
    driver._dispatch_message(_result())
    _drain_main_context()

    assert driver.execution_attempt_id == "attempt-1"
    assert driver.is_busy
    assert not driver.is_accepting_input
    assert not any(row[0] == "terminal" for row in driver._test_lifecycle)
    assert driver.send_user_text("must remain local").rejected
    assert len(driver._test_process.stdin.writes) == 1


@pytest.mark.parametrize("failure", ["acceptance", "user_transcript", "assistant_transcript"])
def test_accounting_failure_stops_further_input_without_releasing_lane(driver_factory, monkeypatch, failure):
    driver = driver_factory()

    def unavailable_storage(*_args, **_kwargs):
        raise RuntimeError("storage unavailable")

    if failure == "acceptance":
        driver.set_execution_attempt_controller(
            driver._execution_attempt_admitter,
            driver._execution_attempt_finisher,
            record_acceptance=unavailable_storage,
        )
    elif failure == "user_transcript":
        monkeypatch.setattr(driver._transcript, "note_user_text", unavailable_storage)
    else:
        monkeypatch.setattr(driver._transcript, "append_assistant", unavailable_storage)
    driver.send_user_text("first")
    driver._dispatch_message(_step("user_input", 0))
    driver._dispatch_message(_result())
    _drain_main_context()

    assert driver.execution_attempt_id == "attempt-1"
    assert not driver.is_accepting_input
    assert driver.send_user_text("must remain local").rejected
    assert len(driver._test_process.stdin.writes) == 1
    assert not any(row[0] == "terminal" for row in driver._test_lifecycle)


def test_session_identity_storage_failure_cannot_be_reenabled_by_another_init(driver_factory, monkeypatch):
    driver = driver_factory(ready=False)

    def unavailable_storage(*_args):
        raise RuntimeError("identity storage unavailable")

    monkeypatch.setattr(driver._transcript, "bind_thread", unavailable_storage)
    driver._dispatch_message(_init_frame(driver._cwd))
    driver._dispatch_message(_init_frame(driver._cwd))
    _drain_main_context()

    assert driver.session_id == ""
    assert not driver.is_accepting_input
    assert driver.send_user_text("must remain local").rejected
    assert driver._test_process.stdin.writes == []


def test_duplicate_result_cannot_finish_the_next_queued_turn(driver_factory):
    driver = driver_factory()
    results = []
    driver.connect("result", lambda _driver, result: results.append(result))
    driver.send_user_text("first")
    driver._dispatch_message(_step("user_input", 0))
    driver.queue_user_text("second")
    first_result = _result(response="first answer", num_turns=1)

    driver._dispatch_message(first_result)
    _drain_main_context()
    assert driver.execution_attempt_id == "attempt-2"
    assert len(driver._test_process.stdin.writes) == 2

    driver._dispatch_message(first_result)
    _drain_main_context()

    assert driver.execution_attempt_id == "attempt-2"
    assert len(results) == 1
    assert len([row for row in driver._test_lifecycle if row[0] == "terminal"]) == 1
    assert len([row for row in driver._test_lifecycle if row[0] == "contribution"]) == 1


def test_prior_user_input_step_cannot_acknowledge_the_next_turn(driver_factory):
    driver = driver_factory()
    acknowledged = []
    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt(text, lambda: acknowledged.append(text))
    )
    driver.send_user_text("first")
    driver._dispatch_message(_step("user_input", 0))
    driver.queue_user_text("second")
    driver._dispatch_message(_result())
    assert driver.execution_attempt_id == "attempt-2"
    assert acknowledged == ["first"]

    driver._dispatch_message(_step("user_input", 0))
    _drain_main_context()

    assert acknowledged == ["first"]
    assert len([row for row in driver._test_lifecycle if row[0] == "accept"]) == 1
    driver._dispatch_message(_step("user_input", 3))
    _drain_main_context()
    assert acknowledged == ["first", "second"]


def test_duplicate_result_after_terminal_storage_failure_does_not_duplicate_contribution(driver_factory):
    driver = driver_factory()

    def unavailable_terminal_store(*_args):
        raise RuntimeError("terminal storage unavailable")

    driver.set_execution_attempt_controller(
        driver._execution_attempt_admitter,
        driver._execution_attempt_finisher,
        finish_with_evidence=unavailable_terminal_store,
    )
    driver.send_user_text("first")
    driver._dispatch_message(_step("user_input", 0))
    terminal = _result()
    driver._dispatch_message(terminal)
    assert driver.execution_attempt_id == "attempt-1"

    driver._dispatch_message(terminal)
    _drain_main_context()

    assert driver.execution_attempt_id == "attempt-1"
    assert len([row for row in driver._test_lifecycle if row[0] == "contribution"]) == 1
    assert len(driver._test_process.stdin.writes) == 1


def test_nonterminal_result_cannot_release_an_execution_lane(driver_factory):
    driver = driver_factory()
    driver.send_user_text("hello")

    driver._dispatch_message(_result(status="WAITING", response=""))
    _drain_main_context()

    assert driver.execution_attempt_id == "attempt-1"
    assert not driver.is_accepting_input
    assert not any(row[0] == "terminal" for row in driver._test_lifecycle)


def test_second_turn_footer_uses_turn_usage_not_session_totals(driver_factory):
    driver = driver_factory()
    results = []
    driver.connect("result", lambda _driver, result: results.append(result))
    driver.send_user_text("first")
    driver._dispatch_message(_step("user_input", 0))
    driver._dispatch_message(_result())
    driver.send_user_text("second")
    driver._dispatch_message(_step("user_input", 3))
    driver._dispatch_message(_result(num_turns=2, usage={
        "input_tokens": 130,
        "output_tokens": 17,
        "thinking_tokens": 8,
        "cache_read_tokens": 60,
        "total_tokens": 147,
    }))
    _drain_main_context()

    assert results[-1]["usage"]["input_tokens"] == 130
    assert results[-1]["turn_usage"]["input_tokens"] == 30
    assert results[-1]["turn_usage"]["output_tokens"] == 7
    assert results[-1]["turn_usage"]["cache_read_tokens"] == 40
    # agy input_tokens already reports the native input count; adding a
    # Claude-style cache alias would count cached input a second time.
    assert "cache_read_input_tokens" not in results[-1]["turn_usage"]


def test_first_resumed_result_sets_an_unknown_usage_baseline(driver_factory):
    driver = driver_factory(resume=NATIVE_ID)
    results = []
    driver.connect("result", lambda _driver, result: results.append(result))
    driver.send_user_text("first since resume")
    driver._dispatch_message(_step("user_input", 30))
    driver._dispatch_message(_result(num_turns=8))
    assert results[-1]["turn_usage"] == {}

    driver.send_user_text("next")
    driver._dispatch_message(_step("user_input", 33))
    driver._dispatch_message(_result(num_turns=9, usage={
        "input_tokens": 140,
        "output_tokens": 18,
        "total_tokens": 158,
    }))
    _drain_main_context()

    assert results[-1]["turn_usage"]["input_tokens"] == 40
    assert results[-1]["turn_usage"]["output_tokens"] == 8


def test_reader_applies_native_frames_on_the_glib_context(driver_factory):
    driver = driver_factory(ready=False)
    driver._test_process.stdout = io.BytesIO(
        (json.dumps(_init_frame(driver._cwd)) + "\n").encode("utf-8")
    )

    driver._read_stdout()
    assert driver.session_id == ""
    _drain_main_context()

    assert driver.session_id == NATIVE_ID


def test_stderr_auth_failure_is_drained_and_visible(driver_factory):
    driver = driver_factory(ready=False)
    driver._test_process.stderr = io.BytesIO(b"authentication required\n")

    driver._read_stderr()
    driver._on_exit(1)
    _drain_main_context()

    assert any("authentication required" in error for error in driver._test_errors)


def test_exit_without_native_terminal_does_not_release_or_replay(driver_factory):
    driver = driver_factory()
    driver.send_user_text("hello")
    driver.queue_user_text("must remain queued")

    driver._on_exit(1)
    _drain_main_context()

    assert driver.execution_attempt_id == "attempt-1"
    assert not driver.is_accepting_input
    assert len(driver._test_process.stdin.writes) == 1
    assert driver.queued_messages()[0][1] == "must remain queued"
    assert not any(row[0] == "terminal" for row in driver._test_lifecycle)


@pytest.mark.parametrize("interrupt", [True, False])
def test_driver_manager_stop_reaches_the_google_process_group(driver_factory, monkeypatch, interrupt):
    driver = driver_factory()
    killed = []
    monkeypatch.setattr(gd.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    driver.send_user_text("hello")
    manager = DriverManager(max_live=2, idle_seconds=100)

    manager.stop_driver(driver, interrupt=interrupt)

    assert killed == [(driver._test_process.pid, gd.signal.SIGTERM)]
    assert not driver.is_accepting_input
    assert driver.execution_attempt_id == "attempt-1"
