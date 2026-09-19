from __future__ import annotations

import pytest

pytest.importorskip("gi")

from gi.repository import GLib

from helios.backend.process import cli_driver as cli_driver_module
from helios.backend.process.cli_driver import ClaudeCliDriver
from helios.backend.process.message_queue import PreparedPrompt
from helios.backend.work_coordinator import WorkCoordinator
from helios.backend.work_store import WorkStore


def _driver(mode: str, cwd: str = "/tmp"):
    driver = ClaudeCliDriver(cwd=cwd, permission_mode=mode)
    writes = []
    driver._write_stdin_line = writes.append
    return driver, writes


def _live_driver(mode: str = "default", cwd: str = "/tmp", **kwargs):
    class FakeStdin:
        def __init__(self):
            self.writes = []

        def close(self, _cancellable):
            return True

        def write_all(self, payload, _cancellable):
            self.writes.append(payload)
            return True

    driver = ClaudeCliDriver(cwd=cwd, permission_mode=mode, **kwargs)
    writes = []
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


def test_direct_claude_send_rechecks_execution_guard():
    driver, _writes = _live_driver()
    errors = []
    driver.connect("error", lambda _driver, message: errors.append(message))
    driver.set_execution_guard(lambda _driver: "This Work binding is stale.")

    driver.send_user_text("must not reach stdin")

    assert errors == ["This Work binding is stale."]
    assert driver.is_busy is False


def test_claude_reserves_before_stdin_and_finishes_before_result_signal():
    driver, _writes = _live_driver()
    order = []

    def admit(_driver):
        order.append("admit")
        return "attempt-claude", ""

    def finish(_driver, attempt_id, status, _reason):
        order.append(("finish", attempt_id, status))

    original_write_all = driver._stdin.write_all

    def write_all(payload, cancellable):
        order.append("stdin")
        return original_write_all(payload, cancellable)

    driver._stdin.write_all = write_all
    driver.set_execution_attempt_controller(
        admit,
        finish,
        record_dispatch=lambda *_args: None,
        record_stop=lambda *_args: None,
        record_contribution=lambda *_args: True,
    )
    driver.connect("result", lambda _driver, _payload: order.append("result"))

    driver.send_user_text("bounded turn")
    driver._dispatch_record({"type": "result", "subtype": "success"})

    assert order == [
        "admit",
        "stdin",
        ("finish", "attempt-claude", "completed"),
        "result",
    ]


def test_independent_claude_work_controllers_write_and_finish_independently(
    tmp_path,
):
    store = WorkStore(tmp_path / "work.db")
    coordinator = WorkCoordinator(store)

    def admit(candidate):
        attempt = coordinator.start_execution_attempt(
            work_id=candidate._helios_work_id,
            participant_id=candidate._helios_participant_id,
            provider=candidate._helios_participant_provider,
            participant_generation=candidate._helios_participant_generation,
        )
        return attempt.attempt_id, ""

    def record_dispatch(_candidate, attempt_id, evidence):
        coordinator.record_execution_dispatch(
            attempt_id,
            wire_prompt_text=evidence.wire_prompt_text,
            provider_request_key=evidence.provider_request_key,
        )

    def finish_with_evidence(_candidate, attempt_id, evidence):
        coordinator.finish_execution_attempt(
            attempt_id,
            status=evidence.status,
            terminal_reason=evidence.reason_code,
            terminal_receipt={
                "evidence_type": evidence.evidence_type,
                "provider_status": evidence.provider_status,
                "request_id": evidence.request_id,
            },
            usage=evidence.usage,
            cost_micro_usd=evidence.cost_micro_usd,
            queue_disposition=evidence.queue_disposition,
        )

    def controlled_driver(suffix):
        work = store.create_work(
            objective=f"Claude {suffix}",
            cwd=f"/repo/{suffix}",
        )
        participant = store.bind_participant(work.work_id, "anthropic")
        driver, _writes = _live_driver()
        driver._helios_work_id = work.work_id
        driver._helios_participant_id = participant.participant_id
        driver._helios_participant_generation = participant.generation
        driver._helios_participant_provider = "anthropic"
        driver.set_execution_attempt_controller(
            admit,
            lambda _candidate, attempt_id, status, reason: (
                coordinator.finish_execution_attempt(
                    attempt_id,
                    status=status,
                    terminal_reason=reason,
                )
            ),
            record_dispatch=record_dispatch,
            record_stop=lambda *_args: None,
            finish_with_evidence=finish_with_evidence,
            record_contribution=lambda *_args: True,
        )
        return driver, work

    try:
        first, first_work = controlled_driver("first")
        second, second_work = controlled_driver("second")

        assert first.send_user_text("first provider write").accepted
        assert second.send_user_text("second provider write").accepted
        assert len(first._stdin.writes) == 1
        assert len(second._stdin.writes) == 1

        first_attempt = first.execution_attempt_id
        second_attempt = second.execution_attempt_id
        active = store.list_active_execution_attempts()
        assert {attempt.work_id for attempt in active} == {
            first_work.work_id,
            second_work.work_id,
        }

        first._dispatch_record({"type": "result", "subtype": "success"})

        assert first.execution_attempt_id == ""
        assert second.execution_attempt_id == second_attempt
        remaining = store.list_active_execution_attempts()
        assert [attempt.attempt_id for attempt in remaining] == [second_attempt]
        assert store.get_execution_attempt(first_attempt).status == "completed"
        assert store.get_execution_attempt(second_attempt).status == "running"
    finally:
        store.close()


def test_claude_partial_write_failure_retires_input_and_retains_slot():
    driver, _writes = _live_driver()
    lifecycle = []
    committed = []
    confirmed = []

    def fail_write(_payload, _cancellable):
        raise OSError("broken pipe after partial write")

    driver._stdin.write_all = fail_write
    driver.connect("delivery-confirmed", lambda _driver: confirmed.append(True))
    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt(
            text,
            lambda: committed.append(text),
        )
    )
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-uncertain", ""),
        lambda *_args: lifecycle.append("legacy-finish"),
        record_dispatch=lambda _driver, _attempt, evidence: lifecycle.append(
            ("dispatch", evidence.wire_prompt_text)
        ),
        record_stop=lambda _driver, _attempt, evidence: lifecycle.append(
            ("stop", evidence.queue_disposition, evidence.acknowledgement)
        ),
        finish_with_evidence=lambda *_args: lifecycle.append("terminal"),
        record_contribution=lambda *_args: True,
    )

    outcome = driver.send_user_text("maybe delivered")

    assert outcome.uncertain
    assert driver.execution_attempt_id == "attempt-uncertain"
    assert driver.is_busy is True
    assert driver.is_accepting_input is False
    assert committed == []
    assert lifecycle == [
        ("dispatch", "maybe delivered"),
        ("stop", "held", {}),
    ]

    child = "toolu-child"
    for record in (
        {
            "type": "stream_event",
            "parent_tool_use_id": child,
            "event": {"type": "message_start", "message": {"model": "haiku"}},
        },
        {"type": "assistant", "parent_tool_use_id": child, "message": {}},
        {
            "type": "control_request",
            "parent_tool_use_id": child,
            "request": {"subtype": "not_a_permission"},
        },
        {"type": "result", "parent_tool_use_id": child, "subtype": "success"},
        {
            "type": "stream_event",
            "event": {"type": "message_start", "message": {"model": "opus"}},
        },
        {
            "type": "user",
            "isReplay": True,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "different prompt"}],
            },
        },
    ):
        driver._dispatch_record(record)
    assert committed == []
    assert confirmed == []
    assert driver.execution_attempt_id == "attempt-uncertain"

    driver._dispatch_record(
        {
            "type": "user",
            "isReplay": True,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "maybe delivered"}],
            },
        }
    )
    assert committed == ["maybe delivered"]
    assert confirmed == [True]


def test_claude_queued_partial_write_quarantines_head_and_returns_successors():
    driver, _writes = _live_driver()
    driver._stdin.write_all = lambda *_args: (_ for _ in ()).throw(
        OSError("partial write")
    )
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-queued-uncertain", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: None,
        record_stop=lambda *_args: None,
        record_contribution=lambda *_args: True,
    )
    first = driver.queue_user_text("maybe delivered")
    driver.queue_user_text("later")

    driver._flush_user_queue()

    assert driver._uncertain_queue_delivery == (first, "maybe delivered")
    assert driver.take_queued() == ["later"]
    assert driver.queued_messages() == [(first, "maybe delivered")]
    assert driver._uncertain_queue_delivery == (first, "maybe delivered")

    sent = []
    driver.connect(
        "queued-user-sent",
        lambda _driver, qid, text: sent.append((qid, text)),
    )
    driver._dispatch_record(
        {
            "type": "user",
            "isReplay": True,
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "maybe delivered"}],
            },
        }
    )
    assert sent == [(first, "maybe delivered")]
    assert driver.queued_messages() == []
    assert driver._uncertain_queue_delivery is None


def test_claude_early_exit_without_result_retains_dispatched_slot():
    driver, _writes = _live_driver()
    lifecycle = []
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-early-exit", ""),
        lambda *_args: lifecycle.append("legacy-finish"),
        record_dispatch=lambda *_args: lifecycle.append("dispatch"),
        record_stop=lambda _driver, _attempt, evidence: lifecycle.append(
            ("stop", evidence.queue_disposition)
        ),
        finish_with_evidence=lambda *_args: lifecycle.append("terminal"),
        record_contribution=lambda *_args: True,
    )
    assert driver.send_user_text("accepted by pipe").accepted

    class ExitedProcess:
        @staticmethod
        def get_if_exited():
            return True

        @staticmethod
        def get_exit_status():
            return 17

        @staticmethod
        def get_if_signaled():
            return False

    driver._on_exit(ExitedProcess(), None, None)

    assert driver.execution_attempt_id == "attempt-early-exit"
    assert lifecycle == ["dispatch", ("stop", "held")]


def test_claude_exit_after_stop_releases_the_dispatched_slot():
    """Stop then exit is a confirmed cancellation, not an ambiguous death.

    Holding the row here bricked the Work: per-Work admission refused every
    later message with "already executing" and only a Helios restart cleared it.
    """

    driver, _writes = _live_driver()
    terminal = []
    stops = []
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-stopped", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: None,
        record_stop=lambda _driver, _attempt, evidence: stops.append(evidence),
        finish_with_evidence=lambda _driver, _attempt, evidence: terminal.append(
            evidence
        ),
        record_contribution=lambda *_args: True,
    )
    assert driver.send_user_text("work I will stop").accepted

    class StoppableProcess:
        @staticmethod
        def get_identifier():
            return None

        @staticmethod
        def force_exit():
            return None

    driver._proc = StoppableProcess()
    driver.stop(interrupt=True)
    assert driver._stop_requested is True

    class ExitedProcess:
        @staticmethod
        def get_if_exited():
            return False

        @staticmethod
        def get_if_signaled():
            return True

        @staticmethod
        def get_term_sig():
            return 2

    driver._on_exit(ExitedProcess(), None, None)

    assert stops == [], "a stop we asked for must not park the row as held"
    assert len(terminal) == 1
    evidence = terminal[0]
    assert evidence.evidence_type == "cancellation_ack"
    assert evidence.status == "aborted"
    assert evidence.queue_disposition == "restored"
    assert evidence.stop_acknowledgement["acknowledged"] is True
    assert evidence.stop_acknowledgement["cancellation_confirmed"] is True
    assert evidence.stop_acknowledgement["request_id"] == "attempt-stopped"


def test_claude_uses_only_confirmed_native_session_for_correlation():
    driver, _writes = _live_driver(resume_session_id="claude-native")
    dispatches = []
    terminal = []
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-native", ""),
        lambda *_args: None,
        record_dispatch=lambda _driver, _attempt, evidence: dispatches.append(
            evidence
        ),
        record_stop=lambda *_args: None,
        finish_with_evidence=lambda _driver, _attempt, evidence: terminal.append(
            evidence
        ),
        record_contribution=lambda *_args: True,
    )
    driver.connect(
        "session-started",
        lambda live, *_args: setattr(live, "_helios_identity_confirmed", True),
    )

    assert driver.send_user_text("correlate me").accepted
    assert dispatches[0].native_binding_id == ""

    driver._dispatch_record(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "claude-native",
            "model": "opus",
        }
    )
    driver._dispatch_record({"type": "result", "subtype": "success"})

    assert len(dispatches) == 2
    assert dispatches[1].native_binding_id == "claude-native"
    assert terminal[0].request_id == "attempt-native"
    assert terminal[0].turn_id == ""
    assert terminal[0].native_id == "claude-native"


def test_claude_contribution_and_usage_persist_before_terminal_release():
    driver, _writes = _live_driver()
    lifecycle = []
    terminal = []
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-terminal", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: lifecycle.append("dispatch"),
        record_stop=lambda *_args: None,
        record_contribution=lambda _driver, turn: (
            lifecycle.append(("contribution", turn.text)) or True
        ),
        finish_with_evidence=lambda _driver, _attempt, evidence: (
            lifecycle.append("terminal"),
            terminal.append(evidence),
        ),
    )
    assert driver.send_user_text("run once").accepted
    for event in (
        {"type": "message_start", "message": {"model": "opus"}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "done"},
        },
        {"type": "message_stop"},
    ):
        driver._dispatch_record({"type": "stream_event", "event": event})
    driver._dispatch_record(
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": 0.012345,
            "usage": {
                "input_tokens": 11,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 3,
                "output_tokens": 5,
                "prompt": "must not persist",
            },
            "error": "provider prose must not persist",
        }
    )

    assert lifecycle == ["dispatch", ("contribution", "done"), "terminal"]
    assert terminal[0].reason_code == "claude.provider_terminal"
    assert terminal[0].provider_status == "success"
    assert terminal[0].usage == {
        "input_tokens": 11,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 3,
        "output_tokens": 5,
    }
    assert terminal[0].cost_micro_usd == 12345
    assert "provider prose" not in repr(terminal[0])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.012345, 12_345),
        (True, None),
        (-0.01, None),
        (float("nan"), None),
        (float("inf"), None),
        (float.fromhex("0x1.fffffffffffffp+1023"), None),
        (10**400, None),
        (9_000_000_000_000.0, 9_000_000_000_000_000_000),
        (10_000_000_000_000.0, None),
    ],
)
def test_claude_terminal_cost_is_bounded_to_sqlite_integer(raw, expected):
    assert cli_driver_module._terminal_cost_micro_usd(
        {"total_cost_usd": raw}
    ) == expected


def test_huge_finite_claude_cost_cannot_block_terminal_release():
    driver, _writes = _live_driver()
    terminal = []
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-huge-cost", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: None,
        record_stop=lambda *_args: None,
        finish_with_evidence=lambda _driver, _attempt, evidence: terminal.append(
            evidence
        ),
        record_contribution=lambda *_args: True,
    )

    assert driver.send_user_text("finish even with a malformed cost").accepted
    driver._dispatch_record(
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": float.fromhex("0x1.fffffffffffffp+1023"),
        }
    )

    assert len(terminal) == 1
    assert terminal[0].status == "completed"
    assert terminal[0].cost_micro_usd is None
    assert driver.execution_attempt_id == ""


def test_claude_stop_targets_captured_process_group(monkeypatch):
    driver, _writes = _live_driver()
    driver._process_group_id = 4242
    driver._own_group = True
    signals = []
    timers = []

    class LiveProcess:
        @staticmethod
        def get_identifier():
            return "99"

        @staticmethod
        def force_exit():
            return None

    driver._proc = LiveProcess()
    monkeypatch.setattr(
        cli_driver_module.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    monkeypatch.setattr(
        cli_driver_module.GLib,
        "timeout_add",
        lambda delay, callback: timers.append((delay, callback)) or len(timers),
    )

    driver.stop(interrupt=False)

    assert signals == [(4242, cli_driver_module.signal.SIGTERM)]
    assert [delay for delay, _callback in timers] == [800, 1600]


def test_process_group_capture_uses_kernel_pgid_not_leader_assumption(monkeypatch):
    monkeypatch.setattr(cli_driver_module.os, "getpgrp", lambda: 10)
    monkeypatch.setattr(cli_driver_module.os, "getpgid", lambda _pid: 777)
    monkeypatch.setattr(cli_driver_module.os, "getsid", lambda _pid: 777)

    assert cli_driver_module._capture_process_group(
        123,
        expected_new_session=True,
    ) == 777


def _request(tool_name: str = "Bash"):
    return {
        "request_id": "request-1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": tool_name,
            "tool_use_id": "tool-1",
            "input": {"command": "make test"},
        },
    }


def test_interactive_claude_mode_surfaces_and_answers_tool_approval():
    driver, writes = _driver("default")
    questions = []
    driver.connect(
        "question-asked",
        lambda _driver, payload, token: questions.append((payload, token)),
    )

    driver._handle_control_request(_request())
    payload, token = questions[0]
    # Other is the deny-with-a-reason channel (2026-09-02 audit, standard S5).
    assert payload["allowOther"] is True
    assert payload["requireExplicitChoice"] is True
    assert "Bash" in payload["questions"][0]["question"]
    assert "make test" in payload["questions"][0]["question"]
    driver.answer_question(token, "Allow once")

    assert writes[0]["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "make test"},
    }


def test_safe_fallback_mode_prompts_rather_than_auto_allowing():
    """The unconfigured-workspace fallback must surface a Claude approval,
    never silently allow (bypass) or silently deny."""
    from helios.backend.project_perms import SAFE_FALLBACK_MODE

    driver, writes = _driver(SAFE_FALLBACK_MODE)
    questions = []
    driver.connect(
        "question-asked",
        lambda _driver, payload, token: questions.append((payload, token)),
    )

    driver._handle_control_request(_request())

    assert questions, "fallback mode must ask, not auto-decide"
    assert writes == []  # no immediate allow/deny written before the user answers


def test_claude_bypass_auto_allows_without_prompting():
    """Bypass is the one mode that must not queue an approval dialog: it
    answers the control request itself with behavior=allow."""
    driver, writes = _driver("bypassPermissions")
    questions = []
    driver.connect("question-asked", lambda *_args: questions.append(True))

    driver._handle_control_request(_request())

    assert driver.permission_mode == "bypassPermissions"
    assert questions == []
    assert len(writes) == 1
    assert writes[0]["response"]["response"]["behavior"] == "allow"


def test_claude_bypass_still_defers_askuserquestion_to_the_user():
    """Auto-allow covers tool approvals, not questions addressed to the human."""
    driver, _writes = _driver("bypassPermissions")
    questions = []
    driver.connect("question-asked", lambda *_args: questions.append(True))

    driver._handle_control_request(_request(tool_name="AskUserQuestion"))

    assert questions == [True]


def test_claude_bypass_is_clamped_to_plan_in_home():
    """HOME outranks Bypass below the UI, exactly as it does for every mode."""
    from helios.backend import project_perms

    driver, _writes = _driver(
        "bypassPermissions", cwd=project_perms.PROTECTED_HOME_CWD
    )

    assert driver.permission_mode == "plan"


def test_claude_never_ask_denies_without_dialog():
    driver, writes = _driver("dontAsk")
    driver._handle_control_request(_request())
    assert writes[0]["response"]["response"]["behavior"] == "deny"


def test_claude_approval_discloses_action_but_scrubs_credentials():
    driver, _writes = _driver("default")
    questions = []
    driver.connect(
        "question-asked",
        lambda _driver, payload, _token: questions.append(payload),
    )
    credential = "secrettokenvalue" + "12345"
    authorization = "Bear" + "er"
    request = _request()
    request["request"]["input"] = {
        "command": (
            f"curl -H 'Authorization: {authorization} {credential}' "
            "https://example.test"
        ),
        "cwd": "/repo",
    }

    driver._handle_control_request(request)

    question = questions[0]["questions"][0]["question"]
    assert "curl" in question
    assert "https://example.test" in question
    assert "/repo" in question
    assert credential not in question
    assert "REDACTED BY HELIOS" in question


def test_unknown_claude_tool_discloses_bounded_redacted_input():
    driver, _writes = _driver("default")
    questions = []
    driver.connect(
        "question-asked",
        lambda _driver, payload, _token: questions.append(payload),
    )
    request = _request()
    request["request"]["tool_name"] = "mcp__custom__operate"
    request["request"]["input"] = {
        "operation": "publish release",
        "token": "examplecredential" + "123456789",
    }

    driver._handle_control_request(request)

    question = questions[0]["questions"][0]["question"]
    assert "publish release" in question
    assert "examplecredential123456789" not in question
    assert "REDACTED BY HELIOS" in question


def test_claude_initializes_outbound_control_protocol_exactly_once():
    driver, writes = _live_driver()

    driver._initialize_control_protocol()
    driver._initialize_control_protocol()

    assert writes == [
        {
            "type": "control_request",
            "request_id": "helios-1",
            "request": {
                "subtype": "initialize",
                "hooks": None,
                "perTaskStopAffordance": True,
            },
        }
    ]
    _control_response(driver, writes[0])


def test_live_claude_permission_change_commits_only_after_ack():
    driver, writes = _live_driver()
    callbacks = []

    assert driver.set_permission_mode(
        "plan", lambda success, detail: callbacks.append((success, detail))
    )
    assert writes == [
        {
            "type": "control_request",
            "request_id": "helios-1",
            "request": {
                "subtype": "set_permission_mode",
                "mode": "plan",
            },
        }
    ]
    assert driver.permission_mode == "default"
    assert callbacks == []
    source_id = driver._pending_control[writes[0]["request_id"]][1]
    assert GLib.MainContext.default().find_source_by_id(source_id) is not None

    _control_response(driver, writes[0])

    assert driver.permission_mode == "plan"
    assert callbacks == [(True, "")]
    assert GLib.MainContext.default().find_source_by_id(source_id) is None


def test_live_claude_accepts_bypass_over_the_wire():
    driver, writes = _live_driver()
    callbacks = []

    assert driver.set_permission_mode(
        "bypassPermissions",
        lambda success, detail: callbacks.append((success, detail)),
    )

    # The driver requests the change; it does not commit locally until the CLI
    # acknowledges, so a failed request never looks active in the UI.
    assert len(writes) == 1
    assert driver.permission_mode == "default"


def test_live_claude_rejects_bypass_in_home_without_wire_request():
    from helios.backend import project_perms

    driver, writes = _live_driver(cwd=project_perms.PROTECTED_HOME_CWD)
    callbacks = []

    assert not driver.set_permission_mode(
        "bypassPermissions",
        lambda success, detail: callbacks.append((success, detail)),
    )

    assert writes == []
    assert callbacks == [(False, "HOME is locked to read-only permissions")]


def test_budget_exhaustion_is_terminal_and_does_not_flush_queue():
    driver, _writes = _live_driver()
    ended = []
    flushed = []
    errors = []
    breakers = []
    driver.end_input = lambda: ended.append(True)
    driver._flush_user_queue = lambda: flushed.append(True)
    driver.connect("error", lambda _driver, message: errors.append(message))
    driver.connect(
        "budget-exhausted",
        lambda _driver, details: breakers.append(details),
    )

    driver._dispatch_record(
        {"type": "result", "subtype": "error_max_budget_usd"}
    )

    assert ended == [True]
    assert flushed == []
    assert breakers == [{"kind": "usd", "limit": 10.0, "provider": "anthropic"}]
    assert "budget limit" in errors[0]


def test_live_claude_permission_error_keeps_previous_mode_and_scrubs_detail():
    driver, writes = _live_driver()
    callbacks = []

    assert driver.set_permission_mode(
        "dontAsk", lambda success, detail: callbacks.append((success, detail))
    )
    _control_response(
        driver,
        writes[0],
        success=False,
        error="token=" + "examplecredential123456789 was rejected",
    )

    assert driver.permission_mode == "default"
    assert callbacks == [(False, "token=[REDACTED BY HELIOS] was rejected")]


def test_live_claude_control_timeout_fails_callback_without_committing():
    driver, writes = _live_driver()
    callbacks = []
    assert driver.set_permission_mode(
        "dontAsk", lambda success, detail: callbacks.append((success, detail))
    )
    request_id = writes[0]["request_id"]
    assert request_id in driver._pending_control
    source_id = driver._pending_control[request_id][1]

    assert driver._expire_control_request(request_id) is False

    assert request_id not in driver._pending_control
    assert GLib.MainContext.default().find_source_by_id(source_id) is None
    assert driver.permission_mode == "default"
    assert driver._stdin is None
    assert driver.execution_restart_required is True
    assert callbacks == [
        (
            False,
            "Claude did not acknowledge the setting change in time. The live "
            "process was closed; the next response will resume with the saved "
            "settings.",
        )
    ]


def test_live_claude_permission_change_rejects_invalid_and_closed():
    driver, writes = _live_driver()
    callbacks = []

    assert not driver.set_permission_mode("future", lambda *args: callbacks.append(args))
    driver._closed = True
    assert not driver.set_permission_mode("dontAsk", lambda *args: callbacks.append(args))

    assert writes == []
    assert [success for success, _detail in callbacks] == [False, False]


def test_live_claude_permission_change_is_accepted_mid_turn():
    """The control channel is independent of the turn stream — measured
    against claude 2.1.241, a mid-turn set_permission_mode answers success
    before that turn's result. Refusing it was Helios's own restriction."""
    driver, writes = _live_driver()
    driver._busy = True

    assert driver.set_permission_mode("dontAsk")

    assert len(writes) == 1
    assert writes[0]["request"] == {
        "subtype": "set_permission_mode",
        "mode": "dontAsk",
    }


@pytest.mark.parametrize(
    ("key", "settings", "stored_effort"),
    [
        ("low", {"effortLevel": "low", "ultracode": False}, "low"),
        # max is a plain --effort level; ultracode is cleared on every change
        # because it is sticky in the CLI's own settings.
        ("max", {"effortLevel": "max", "ultracode": False}, "max"),
    ],
)
def test_live_claude_effort_change_commits_after_ack(key, settings, stored_effort):
    driver, writes = _live_driver(effort="high")
    callbacks = []

    assert driver.set_effort(
        key, lambda success, detail: callbacks.append((success, detail))
    )
    assert len(writes) == 1
    assert writes[0]["request"] == {
        "subtype": "apply_flag_settings",
        "settings": settings,
    }
    assert driver.effort_key == "high"

    _control_response(driver, writes[0])
    assert len(writes) == 2
    assert writes[1]["request"] == {
        "subtype": "set_max_thinking_tokens",
        "max_thinking_tokens": None,
    }
    assert driver.effort_key == "high"
    assert callbacks == []
    _control_response(driver, writes[1])

    assert driver.effort_key == stored_effort
    assert callbacks == [(True, "")]


def test_live_claude_effort_off_uses_two_ack_commit_barrier():
    driver, writes = _live_driver(effort="high")
    callbacks = []

    assert driver.set_effort(
        "off", lambda success, detail: callbacks.append((success, detail))
    )
    assert [frame["request"] for frame in writes] == [
        {
            "subtype": "apply_flag_settings",
            "settings": {"ultracode": False},
        }
    ]

    _control_response(driver, writes[0])
    assert writes[1]["request"] == {
        "subtype": "set_max_thinking_tokens",
        "max_thinking_tokens": 0,
    }
    assert driver.effort_key == "high"
    assert callbacks == []
    _control_response(driver, writes[1])

    assert driver.effort_key == "off"
    assert callbacks == [(True, "")]


def test_live_claude_effort_error_and_teardown_never_commit():
    driver, writes = _live_driver(effort="high")
    callbacks = []
    assert driver.set_effort(
        "off", lambda success, detail: callbacks.append((success, detail))
    )

    _control_response(driver, writes[0], success=False, error="unsupported")

    assert driver.effort_key == "high"
    assert callbacks == [(False, "unsupported")]

    callbacks.clear()
    assert driver.set_effort(
        "low", lambda success, detail: callbacks.append((success, detail))
    )
    source_ids = [source_id for _callback, source_id in driver._pending_control.values()]
    driver.end_input()

    assert driver.effort_key == "high"
    assert callbacks == [
        (False, "the Claude process stopped before acknowledging the change")
    ]
    assert all(
        GLib.MainContext.default().find_source_by_id(source_id) is None
        for source_id in source_ids
    )


def test_live_claude_non_off_clear_override_error_never_commits():
    driver, writes = _live_driver(effort="high")
    callbacks = []
    assert driver.set_effort(
        "low", lambda success, detail: callbacks.append((success, detail))
    )

    _control_response(driver, writes[0])
    _control_response(driver, writes[1], success=False, error="cannot clear override")

    assert driver.effort_key == "high"
    assert driver._stdin is None
    assert driver.execution_restart_required is True
    assert callbacks == [
        (
            False,
            "cannot clear override. The live process was closed; the next "
            "response will resume with the saved settings.",
        )
    ]


def test_live_claude_effort_timeout_fences_ambiguous_process_state():
    driver, writes = _live_driver(effort="high")
    callbacks = []

    assert driver.set_effort(
        "low", lambda success, detail: callbacks.append((success, detail))
    )
    assert driver._expire_control_request(writes[0]["request_id"]) is False

    assert driver.effort_key == "high"
    assert driver._stdin is None
    assert driver.execution_restart_required is True
    assert callbacks == [
        (
            False,
            "Claude did not acknowledge the setting change in time. The live "
            "process was closed; the next response will resume with the saved "
            "settings.",
        )
    ]


def test_claude_execution_properties_report_startup_overrides():
    assert ClaudeCliDriver(cwd="/tmp", effort="max").effort_key == "max"
    assert ClaudeCliDriver(cwd="/tmp", max_thinking_tokens=0).effort_key == "off"


def test_claude_model_property_tracks_live_init_model():
    driver = ClaudeCliDriver(cwd="/tmp", model="opus")
    assert driver.model == "opus"

    driver._dispatch_record(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "session-1",
            "model": "claude-opus-4-8",
        }
    )

    assert driver.model == "claude-opus-4-8"


# ── slash commands must reach the CLI as the leading token ───────────────


def _admit(driver):
    """Minimal execution-admission controller so a send can reach stdin."""
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-slash", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: True,
        record_stop=lambda *_args: True,
        finish_with_evidence=lambda *_args: None,
        record_contribution=lambda *_args: True,
    )


def _init(driver, **extra):
    """Feed the driver the `system/init` line it would get from the CLI."""
    import json

    driver._handle_stdout_line(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "s1",
                "model": "opus",
                "tools": [],
                "mcp_servers": [],
                **extra,
            }
        )
    )


def test_a_user_typed_slash_command_is_not_wrapped_in_the_goal_envelope():
    """The bug: `/compact` from the toolbar worked, `/compact` typed did not.

    A command is only a command when it LEADS the message, and the prompt
    context provider prepends the Goal envelope — so on any Work with a goal
    (which is every tandem Work) a typed command reached the model as prose and
    silently did nothing.
    """
    driver, _writes = _live_driver()
    _admit(driver)
    _init(driver, slash_commands=["compact", "model"], terminal_slash_commands=["doctor"])
    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt("GOAL ENVELOPE\n\n" + text)
    )

    driver.send_user_text("/compact")
    sent = driver._stdin.writes[-1].decode()
    assert '"/compact"' in sent, sent
    assert "GOAL ENVELOPE" not in sent, sent


def test_ordinary_prose_still_gets_the_envelope():
    driver, _writes = _live_driver()
    _admit(driver)
    _init(driver, slash_commands=["compact"])
    driver.set_prompt_context_provider(
        lambda _driver, text: PreparedPrompt("GOAL ENVELOPE\n\n" + text)
    )

    driver.send_user_text("please compact the context")
    assert "GOAL ENVELOPE" in driver._stdin.writes[-1].decode()


def test_a_leading_absolute_path_is_prose_not_a_command():
    """Why the guard matches the CLI's own list instead of a `/` prefix.

    "/home/alice/helios is where the bug is" opens with a slash and is
    ordinary prose. A prefix heuristic would strip the Goal envelope off a real
    user turn — a silent, invisible failure — every time someone pasted a path.
    """
    driver, _writes = _live_driver()
    _init(driver, slash_commands=["compact", "model"])
    assert driver.is_slash_command("/home/alice/helios is where the bug is") is False
    assert driver.is_slash_command("/model opus") is True
    assert driver.is_slash_command("  /compact  ") is True
    assert driver.is_slash_command("compact") is False


def test_terminal_only_commands_are_not_treated_as_commands():
    """`doctor` and `color` only work in the CLI's own TUI (measured, 2.1.238).

    Sending one through stream-json produces prose either way, so stripping the
    envelope for it would lose the goal for nothing.
    """
    driver, _writes = _live_driver()
    _init(driver, slash_commands=["compact", "doctor"], terminal_slash_commands=["doctor"])
    assert driver.is_slash_command("/compact") is True
    assert driver.is_slash_command("/doctor") is False


def test_nothing_is_a_command_before_init_lands():
    """Fail-closed direction: unknown name -> prose -> the pre-existing
    behaviour, never a silently-dropped envelope."""
    driver, _writes = _live_driver()
    assert driver.is_slash_command("/compact") is False
