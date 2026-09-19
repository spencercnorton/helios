"""The control-channel model switch.

Verified against claude 2.1.241 on 2026-08-25: `set_model` is a real control
subtype — it ACKs `success` without a turn, and the CLI echoes a
`<local-command-stdout>Set model to …</local-command-stdout>` user record,
which the dispatch loop must drop (a "You" bubble the user never typed).
A definitive error leaves the process and the staged model untouched; only an
ambiguous outcome fences the process.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import ClaudeCliDriver


def _live_driver(**kwargs):
    class FakeStdin:
        def close(self, _cancellable):
            return True

    driver = ClaudeCliDriver(cwd="/tmp", permission_mode="default", **kwargs)
    writes: list[dict] = []
    driver._proc = object()
    driver._stdin = FakeStdin()
    driver._write_stdin_line = writes.append
    return driver, writes


def _control_response(driver, frame, *, success=True, error="rejected"):
    response = {
        "subtype": "success" if success else "error",
        "request_id": frame["request_id"],
    }
    if success:
        response["response"] = {}
    else:
        response["error"] = error
    driver._dispatch_record({"type": "control_response", "response": response})


def test_set_model_ack_updates_model():
    driver, writes = _live_driver(model="claude-old")
    outcomes: list[tuple[bool, str]] = []
    accepted = driver.set_model(
        "claude-new", lambda ok, detail="": outcomes.append((ok, detail))
    )
    assert accepted
    frame = writes[0]
    assert frame["request"] == {"subtype": "set_model", "model": "claude-new"}
    _control_response(driver, frame, success=True)
    assert outcomes == [(True, "")]
    assert driver.model == "claude-new"
    assert not driver.execution_restart_required


def test_set_model_error_keeps_model_and_process():
    driver, writes = _live_driver(model="claude-old")
    outcomes: list[bool] = []
    driver.set_model("claude-new", lambda ok, detail="": outcomes.append(ok))
    _control_response(driver, writes[0], success=False, error="unknown model")
    assert outcomes == [False]
    assert driver.model == "claude-old"
    # A definitive rejection is not ambiguity — the process stays usable.
    assert not driver.execution_restart_required


@pytest.mark.parametrize("success", [True, False])
def test_model_change_updates_effort_capability_identity_only_after_ack(success):
    driver, writes = _live_driver(model="haiku")
    driver._cli_models = [
        {"value": "haiku", "supportsEffort": True, "supportedEffortLevels": ["low", "medium"]},
        {"value": "fable", "resolvedModel": "claude-fable-5-1", "supportsEffort": True,
         "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"]},
    ]
    assert driver.set_model("fable")
    assert driver.supported_effort_levels() == ["low", "medium"]
    _control_response(driver, writes[0], success=success)
    if success:
        assert driver.supported_effort_levels() == ["low", "medium", "high", "xhigh", "max"]
        assert driver._requested_model == "fable"
    else:
        assert driver.supported_effort_levels() == ["low", "medium"]
        assert driver._requested_model == "haiku"


def test_context_qualified_live_switch_uses_native_resolved_identity_without_a_turn():
    driver, writes = _live_driver(model="haiku")
    driver._cli_models = [{
        "value": "claude-fable-5-1[1m]",
        "resolvedModel": "claude-fable-5-1",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    }]
    seen = []
    driver.connect("capabilities-updated", lambda *_args: seen.append(True))
    assert driver.set_model("fable[1m]")
    _control_response(driver, writes[0])
    assert writes[1]["request"] == {"subtype": "get_context_usage"}
    assert driver.supported_effort_levels() == []
    driver._dispatch_record({"type": "control_response", "response": {
        "subtype": "success", "request_id": writes[1]["request_id"], "response": {
            "model": "claude-fable-5-1", "maxTokens": 1000000,
            "categories": [{"name": "System prompt", "tokens": 100}],
        },
    }})
    assert driver._requested_model == "fable[1m]"
    assert driver.model == "claude-fable-5-1"
    assert driver.supported_effort_levels() == ["low", "medium", "high", "xhigh", "max"]
    assert seen == [True]
    assert all(frame["type"] == "control_request" for frame in writes)


def test_old_context_reply_cannot_restore_model_capabilities_after_another_switch():
    driver, writes = _live_driver(model="haiku")
    assert driver.set_model("fable[1m]")
    _control_response(driver, writes[0])
    old_context = writes[1]
    assert driver.set_model("sonnet")
    _control_response(driver, writes[2])
    driver._dispatch_record({"type": "control_response", "response": {
        "subtype": "success", "request_id": old_context["request_id"], "response": {
            "model": "claude-fable-5-1", "maxTokens": 1000000,
            "categories": [{"name": "System prompt", "tokens": 100}],
        },
    }})
    assert driver.model == driver._requested_model == "sonnet"
    assert driver._ctx_usage == {}


def test_set_model_write_failure_is_ambiguous_and_fences():
    driver, _writes = _live_driver(model="claude-old")
    driver._write_stdin_line = lambda _frame: False
    outcomes: list[bool] = []
    driver.set_model("claude-new", lambda ok, detail="": outcomes.append(ok))
    assert outcomes == [False]
    assert driver.model == "claude-old"
    assert driver.execution_restart_required


def test_set_model_busy_refused_without_write():
    driver, writes = _live_driver(model="claude-old")
    driver._busy = True
    details: list[str] = []
    accepted = driver.set_model(
        "claude-new", lambda ok, detail="": details.append(detail)
    )
    assert not accepted
    assert writes == []
    assert details == ["the conversation is busy"]


def test_set_model_same_value_is_a_synchronous_success():
    driver, writes = _live_driver(model="claude-same")
    outcomes: list[bool] = []
    assert driver.set_model("claude-same", lambda ok, detail="": outcomes.append(ok))
    assert outcomes == [True]
    assert writes == []


def test_set_model_second_change_refused_while_pending():
    driver, writes = _live_driver(model="claude-old")
    driver.set_model("claude-a")
    details: list[str] = []
    accepted = driver.set_model(
        "claude-b", lambda ok, detail="": details.append(detail)
    )
    assert not accepted
    assert details == ["another model change is pending"]
    assert len(writes) == 1


def test_set_model_rejects_blank():
    driver, writes = _live_driver()
    details: list[str] = []
    assert not driver.set_model("  ", lambda ok, detail="": details.append(detail))
    assert details == ["unknown model"]
    assert writes == []


def test_local_command_stdout_record_never_reaches_the_transcript():
    driver, _writes = _live_driver()
    appended: list[object] = []
    driver.connect("turn-appended", lambda _d, turn: appended.append(turn))
    record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                "<local-command-stdout>Set model to claude-new"
                "</local-command-stdout>"
            ),
        },
        "session_id": "s1",
    }
    # Round-trip through JSON to mirror the wire exactly.
    driver._dispatch_record(json.loads(json.dumps(record)))
    assert appended == []
