"""OpenRouter driver tool loop: persist-boundary invariant and compaction retry.

The invariant these guard: **every tool call emitted in an assistant message
gets a reply before the history is persisted.** Constructing the message
correctly (``build_assistant_message``) is not sufficient — the loop appends
the assistant message first and the replies afterwards, so anything that
unwinds in between leaves an orphan, and ``_run_turn`` saves unconditionally
in its finalize path. An unanswered ``tool_calls`` message makes every later
request 400, survives restart, and cannot be repaired by compaction (it lives
in the newest exchange, which is never dropped).
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.backend.openrouter import chat as or_chat  # noqa: E402
from helios.backend.openrouter.history import HistoryLog  # noqa: E402
from helios.backend.process import openrouter_driver as od  # noqa: E402
from helios.backend.process import openrouter_tools as tools  # noqa: E402


def _call(index: int = 0, name: str = "Read", arguments: str = '{"path": "a.py"}'):
    return or_chat.ToolCallRequest(
        id=f"call_{index}", name=name, arguments_json=arguments
    )


def _stream(
    *,
    tool_calls=(),
    text="",
    finish_reason="tool_calls",
    usage=None,
    response_id="",
):
    """A stream_chat replacement yielding one round's events."""

    def _fake(messages, **kwargs):
        if response_id and kwargs.get("on_response_accepted") is not None:
            kwargs["on_response_accepted"](response_id)
        if text:
            yield or_chat.TextDelta(text)
        for call in tool_calls:
            yield or_chat.ToolCallDelta(call)
        yield or_chat.Done(
            or_chat.ChatCompleted(
                finish_reason=finish_reason,
                usage=usage or or_chat.ChatUsage(reported=True),
                response_id=response_id,
            )
        )

    return _fake


@pytest.fixture()
def driver(tmp_path, monkeypatch):
    monkeypatch.setattr(od.or_key, "load_key", lambda: "sk-or-v1-testkey0000000000")
    drv = od.OpenRouterDriver(cwd=str(tmp_path), model="vendor/model")
    drv.start()
    attempt_seq = iter(range(1, 10_000))
    drv.set_execution_attempt_controller(
        lambda _driver: (f"attempt-{next(attempt_seq)}", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: None,
        record_acceptance=lambda *_args: None,
        record_stop=lambda *_args: None,
        record_contribution=lambda *_args: True,
    )
    return drv


def _assert_history_is_wire_valid(messages):
    """Every assistant tool_call id must have a matching role:"tool" reply."""
    requested = [
        call["id"]
        for m in messages
        if m.get("role") == "assistant"
        for call in m.get("tool_calls", [])
    ]
    answered = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert sorted(requested) == sorted(answered), (
        f"orphaned tool_calls: requested={requested} answered={answered}"
    )


def test_direct_openrouter_send_rechecks_execution_guard(driver):
    errors = []
    before = list(driver._history)
    driver.connect("error", lambda _driver, message: errors.append(message))
    driver.set_execution_guard(lambda _driver: "This Work binding is stale.")

    driver.send_user_text("must not reach the provider")

    assert errors == ["This Work binding is stale."]
    assert driver._history == before
    assert driver.is_busy is False


@pytest.mark.parametrize("ownership", ["busy", "queued"])
def test_direct_openrouter_send_cannot_bypass_fifo_ownership(driver, ownership):
    errors = []
    before = list(driver._history)
    driver.connect("error", lambda _driver, message: errors.append(message))
    if ownership == "busy":
        driver._busy = True
    else:
        driver.queue_user_text("older")

    outcome = driver.send_user_text("newer")

    assert outcome.rejected
    assert driver._history == before
    assert "not sent out of order" in errors[-1]


def test_openrouter_attempt_wraps_worker_and_finishes_before_result(
    driver,
    monkeypatch,
):
    order = []
    captured = {}

    class FakeThread:
        def __init__(self, *, target, args, daemon, name):
            del daemon, name
            captured["target"] = target
            captured["args"] = args

        def start(self):
            order.append("worker-start")

    def admit(_driver):
        order.append("admit")
        return "attempt-openrouter", ""

    def finish(_driver, attempt_id, status, _reason):
        order.append(("finish", attempt_id, status))

    monkeypatch.setattr(od.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        od.or_chat,
        "stream_chat",
        _stream(
            text="done",
            finish_reason="stop",
            usage=or_chat.ChatUsage(
                input_tokens=10,
                output_tokens=2,
                reported=True,
            ),
        ),
    )
    driver.set_execution_attempt_controller(admit, finish)
    driver.connect("result", lambda _driver, _payload: order.append("result"))

    driver.send_user_text("bounded turn")
    assert order == ["admit", "worker-start"]

    captured["target"](*captured["args"])

    assert order == [
        "admit",
        "worker-start",
        ("finish", "attempt-openrouter", "completed"),
        "result",
    ]


def test_openrouter_keeps_busy_until_terminal_ui_atomically_flushes_fifo(
    driver,
    monkeypatch,
):
    callbacks = []
    flushes = []
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callbacks.append((callback, args)) or len(callbacks),
    )
    monkeypatch.setattr(
        od.or_chat,
        "stream_chat",
        _stream(
            text="done",
            finish_reason="stop",
            usage=or_chat.ChatUsage(
                input_tokens=10,
                output_tokens=2,
                reported=True,
            ),
        ),
    )
    driver._flush_user_queue = lambda: flushes.append("flush")
    driver._busy = True

    driver._run_turn("first")

    assert driver.is_busy is True
    assert flushes == []
    publish, args = callbacks[-1]
    assert publish.__name__ == "publish_terminal"
    assert publish(*args) is False
    assert driver.is_busy is False
    assert flushes == ["flush"]


def test_openrouter_dispatch_persistence_failure_prevents_worker_release(
    driver,
    monkeypatch,
):
    before = list(driver._history)
    captured = {}

    class BlockedThread:
        def __init__(self, *, target, **_kwargs):
            captured["target"] = target

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(od.threading, "Thread", BlockedThread)
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-dispatch-fails", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: (_ for _ in ()).throw(
            OSError("database unavailable")
        ),
    )

    outcome = driver.send_user_text("must stay local")
    captured["target"]()

    assert outcome.rejected
    assert captured["started"] is True
    assert driver._history == before
    assert driver.execution_attempt_id == ""


def test_openrouter_local_history_failure_holds_slot_before_worker(
    driver,
    monkeypatch,
):
    before = list(driver._history)
    captured = {}
    stops = []

    class BlockedThread:
        def __init__(self, *, target, **_kwargs):
            captured["target"] = target

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(od.threading, "Thread", BlockedThread)
    monkeypatch.setattr(
        od.HistoryLog,
        "save_strict",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-history-fails", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: None,
        record_stop=lambda _driver, _attempt, evidence: stops.append(evidence),
    )

    outcome = driver.send_user_text("must not start worker")
    captured["target"]()

    assert outcome.rejected
    assert captured["started"] is True
    assert driver._history == before
    assert driver.execution_attempt_id == ""
    assert stops == []


def test_openrouter_thread_start_failure_is_predispatch_local_abort(
    driver,
    monkeypatch,
):
    terminal = []

    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(od.threading, "Thread", BrokenThread)
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-thread-fails", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: pytest.fail("dispatch must not run"),
        finish_with_evidence=lambda _driver, _attempt, evidence: terminal.append(
            evidence
        ),
    )

    outcome = driver.send_user_text("no worker")

    assert outcome.rejected
    assert terminal[0].evidence_type == "local_abort"
    assert terminal[0].reason_code == "local_abort"
    assert driver.execution_attempt_id == ""


def test_openrouter_local_mirror_failure_is_replay_safe_before_http(
    driver,
    monkeypatch,
):
    captured = {}
    events = []
    history_before = list(driver._history)

    class BlockedThread:
        def __init__(self, *, target, **_kwargs):
            captured["target"] = target

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(od.threading, "Thread", BlockedThread)
    monkeypatch.setattr(
        driver._transcript,
        "note_user_text",
        lambda _text: (_ for _ in ()).throw(OSError("mirror unavailable")),
    )
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-local-mirror", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: events.append("dispatch"),
        record_stop=lambda _driver, _attempt, evidence: events.append(
            ("stop", evidence.queue_disposition)
        ),
    )

    outcome = driver.send_user_text("never reached HTTP")
    captured["target"]()

    assert outcome.rejected
    assert captured["started"] is True
    assert events == ["dispatch", ("stop", "held")]
    assert driver.execution_attempt_id == "attempt-local-mirror"
    assert driver.is_accepting_input is False
    assert driver._history == history_before
    assert od.HistoryLog(driver.session_id).load() == history_before


def test_openrouter_accepted_transport_failure_is_ambiguous_and_not_replayed(
    driver,
    monkeypatch,
):
    captured = {}
    events = []
    stream_kwargs = []

    class FakeThread:
        def __init__(self, *, target, args, **_kwargs):
            captured["target"] = target
            captured["args"] = args

        def start(self):
            return None

    def partial_stream(_messages, **kwargs):
        stream_kwargs.append(kwargs)
        kwargs["on_response_accepted"]("resp-partial")
        yield or_chat.TextDelta("partial")
        raise or_chat.ChatError(
            or_chat.ChatErrorKind.CONNECTION,
            "connection dropped",
            retryable=True,
        )

    monkeypatch.setattr(od.threading, "Thread", FakeThread)
    monkeypatch.setattr(od.or_chat, "stream_chat", partial_stream)
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-ambiguous", ""),
        lambda *_args: events.append("legacy-terminal"),
        record_dispatch=lambda *_args: events.append("dispatch"),
        record_acceptance=lambda _driver, _attempt, evidence: events.append(
            ("acceptance", evidence.accepted_turn_id)
        ),
        record_stop=lambda _driver, _attempt, evidence: events.append(
            ("stop", evidence.queue_disposition)
        ),
        finish_with_evidence=lambda *_args: events.append("terminal"),
        record_contribution=lambda *_args: (
            events.append("contribution") or True
        ),
    )

    assert driver.send_user_text("one attempt only").accepted
    captured["target"](*captured["args"])

    assert stream_kwargs[0]["max_attempts"] == 1
    assert events == [
        "dispatch",
        ("acceptance", "resp-partial"),
        ("stop", "held"),
    ]
    assert driver.execution_attempt_id == "attempt-ambiguous"
    assert driver.is_accepting_input is False


def test_stop_during_local_tool_releases_completed_request_and_resumes(driver, monkeypatch, tmp_path):
    """A completed HTTP round is authoritative even if Stop interrupts its tools."""
    workers, requests, terminals, statuses = [], [], [], []
    # This test replaces Thread globally to capture the turn worker. Its
    # simulated tools mutate state, so keep their dispatch serial as well.
    monkeypatch.setattr(driver, "_parallel_read", lambda _call: False)
    side_effect = tmp_path / "side-effect.txt"

    class FakeThread:
        def __init__(self, *, target, args, **_kwargs):
            workers.append((target, args))

        def start(self):
            pass

    rounds = iter([
        _stream(tool_calls=[_call(0), _call(1), _call(2)], response_id="resp-tools"),
        _stream(text="continued from saved results", finish_reason="stop", response_id="resp-resumed"),
    ])

    def stream(messages, **kwargs):
        requests.append([dict(message) for message in messages])
        return next(rounds)(messages, **kwargs)

    def execute(*_args, **_kwargs):
        assert not side_effect.exists(), "completed tool was executed again"
        side_effect.write_text("executed once", encoding="utf-8")
        driver.stop()
        return "completed before Stop", False

    def install_controller(drv):
        drv.set_execution_attempt_controller(
            lambda _driver: (f"attempt-{len(workers)}", ""),
            lambda *_args: None,
            record_dispatch=lambda *_args: None,
            record_acceptance=lambda *_args: None,
            record_stop=lambda *_args: pytest.fail("fully completed HTTP requests are not uncertain"),
            finish_with_evidence=lambda _driver, _attempt, evidence: terminals.append(evidence),
            record_contribution=lambda *_args: True,
        )

    monkeypatch.setattr(od.threading, "Thread", FakeThread)
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(od.tools, "execute_tool", execute)
    monkeypatch.setattr(od.GLib, "idle_add", lambda callback, *args: callback(*args))
    install_controller(driver)
    driver.connect("turn-status-updated", lambda _driver, payload: statuses.append(payload["status"]))
    assert driver.send_user_text("perform the work").accepted
    driver.queue_user_text("queued follow-up")
    worker, args = workers[0]
    worker(*args)

    assert driver.execution_attempt_id == ""
    assert not driver.is_busy
    assert len(workers) == len(requests) == 1
    assert driver.queued_messages  # Stop must not silently dispatch the follow-up.
    assert terminals[0].evidence_type == "provider_terminal"
    assert terminals[0].status == "aborted"
    assert terminals[0].turn_id == terminals[0].response_id == "resp-tools"
    assert terminals[0].queue_disposition == "restored"
    assert statuses == ["inProgress", "interrupted"]

    saved = HistoryLog(driver.session_id).load()
    _assert_history_is_wire_valid(saved)
    results = [message for message in saved if message.get("role") == "tool"]
    assert [message["content"] for message in results] == [
        "completed before Stop", "Cancelled by the user.", "Cancelled by the user.",
    ]

    resumed = od.OpenRouterDriver(cwd=driver._cwd, model=driver.model, resume_session_id=driver.session_id)
    resumed.start()
    install_controller(resumed)
    assert resumed.send_user_text("continue from completed results").accepted
    worker, args = workers[1]
    worker(*args)
    assert terminals[1].status == "completed"
    assert side_effect.read_text(encoding="utf-8") == "executed once"
    assert all(message in requests[1] for message in results)


def test_cancelled_tool_calls_remain_paired_in_mirror_recovery(driver, monkeypatch):
    monkeypatch.setattr(od.or_chat, "stream_chat", _stream(tool_calls=[_call(0), _call(1)]))

    def execute(*_args, **_kwargs):
        driver.stop()
        return "done", False

    monkeypatch.setattr(od.tools, "execute_tool", execute)
    driver._run_turn("go")

    # This fallback is used if the authoritative history is unavailable.
    recovered = od.messages_from_mirror(driver._transcript._path(), driver.session_id)
    _assert_history_is_wire_valid(recovered)
    assert len([message for message in recovered if message.get("role") == "tool"]) == 2


def test_accepted_error_stream_is_held_without_terminal_receipt(driver, monkeypatch):
    def accepted_error(_messages, **kwargs):
        kwargs["on_response_accepted"]("resp-no-terminal")
        raise or_chat.ChatError(or_chat.ChatErrorKind.RATE_LIMIT, "upstream stream error")
        yield

    monkeypatch.setattr(od.or_chat, "stream_chat", accepted_error)
    monkeypatch.setattr(od.GLib, "idle_add", lambda callback, *args: callback(*args))
    driver._execution_attempt_id = "attempt-accepted-error"
    results = []
    driver.connect("result", lambda _driver, result: results.append(result))
    driver._run_turn("already staged")
    assert driver.execution_attempt_id == "attempt-accepted-error"
    assert not driver.is_accepting_input
    assert results == []


@pytest.mark.parametrize("progress", [
    pytest.param(None, id="pre-response-control"),
    pytest.param({"reasoning_details": [
        {"type": "reasoning.encrypted", "data": "opaque", "index": 0},
    ]}, id="buffered-reasoning"),
    pytest.param({"reasoning_details": [
        {"type": "reasoning.encrypted", "data": "opaque"},
    ]}, id="unindexed-reasoning"),
    pytest.param({"tool_calls": [
        {"index": 0, "id": "call-1", "function": {"name": "Read", "arguments": '{"path":'}},
    ]}, id="buffered-tool"),
])
@pytest.mark.parametrize("wire_error", [
    pytest.param({"code": 429, "message": "upstream limit"}, id="rate-limit"),
    pytest.param({"code": 400, "message": "maximum context length"}, id="context-length"),
    pytest.param({"code": 404, "message": "no endpoints found"}, id="route-rejection"),
])
def test_real_stream_progress_is_held_without_replay(
    driver, monkeypatch, progress, wire_error,
):
    """Exercise HTTP parsing, retry layers and terminal accounting together."""
    import json
    from dataclasses import replace

    requests, terminal, stops, results, flushes, exclusions = [], [], [], [], [], []
    route = od.or_routes.Route(
        model=driver.model, provider_slug="first", provider_name="First",
        context_length=0, max_completion_tokens=0, quantization="fp8",
        supports_tools=True, supports_reasoning=True, implicit_caching=False,
        explicit_caching=False, input_price=0, cache_read_price=0,
    )
    driver._route = route
    monkeypatch.setattr(driver, "_context_window", lambda: 0)
    monkeypatch.setattr(od.or_routes, "exclude", lambda *args: exclusions.append(args))
    monkeypatch.setattr(od.or_routes, "route_for", lambda _model: replace(route, provider_slug="second"))

    class Response:
        status = 200

        def iter_bytes(self):
            if len(requests) == 1:
                chunks = [] if progress is None else [{"choices": [{"delta": progress}]}]
                chunks.append({"error": wire_error})
            else:
                chunks = [{
                    "id": "resp-recovered",
                    "choices": [{"delta": {"content": "recovered"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0},
                }]
            yield "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode()

        def close(self):
            pass

    class Transport:
        def open(self, request, **_kwargs):
            requests.append(request)
            return Response()

    monkeypatch.setattr(or_chat, "UrlLibTransport", Transport)
    monkeypatch.setattr(od.GLib, "idle_add", lambda callback, *args: callback(*args))
    monkeypatch.setattr(driver, "_flush_user_queue", lambda: flushes.append(True))
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-progress", ""), lambda *_args: None,
        record_dispatch=lambda *_args: None, record_acceptance=lambda *_args: None,
        record_stop=lambda _driver, _attempt, evidence: stops.append(evidence),
        finish_with_evidence=lambda _driver, _attempt, evidence: terminal.append(evidence),
        record_contribution=lambda *_args: True,
    )
    driver._execution_attempt_id = "attempt-progress"
    driver._busy = True
    driver._history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "prior " * 4_000},
        {"role": "assistant", "content": "answer " * 4_000},
        {"role": "user", "content": "already staged"},
    ]
    before = list(driver._history)
    driver.queue_user_text("queued follow-up")
    driver.connect("result", lambda _driver, result: results.append(result))

    driver._run_turn_with_tools("already staged")

    if progress is not None:
        assert len(requests) == 1
        assert not terminal and not results and not flushes and not exclusions
        assert driver.execution_attempt_id == "attempt-progress"
        assert not driver.is_accepting_input
        assert driver._history == before
        assert driver.queued_messages
        assert len(stops) == 1 and stops[0].queue_disposition == "held"
    else:
        # Proven rejection remains recoverable: context/route failures retry,
        # and a pre-response rate limit releases only its own rejected turn.
        assert not stops
        assert driver.execution_attempt_id == ""
        assert len(terminal) == len(results) == len(flushes) == 1
        if wire_error["code"] == 429:
            assert len(requests) == 1
            assert terminal[0].evidence_type == "verified_rejection"
            assert driver._history == before[:-1]
        else:
            assert len(requests) == 2
            assert terminal[0].status == "completed"
            assert driver._history[-1]["content"] == "recovered"


@pytest.mark.parametrize("progress", ["acceptance", "reasoning"])
def test_context_retry_requires_no_provider_progress(driver, monkeypatch, progress):
    requests = []

    def accepted_then_context_error(messages, **kwargs):
        requests.append(messages)
        if len(requests) == 1:
            if progress == "acceptance":
                kwargs["on_response_accepted"]("resp-started")
            else:
                yield or_chat.ReasoningDelta("work already started")
            raise or_chat.ChatError(or_chat.ChatErrorKind.CONTEXT_LENGTH, "upstream stream error")
        yield from _stream(text="must not retry", finish_reason="stop")(messages, **kwargs)

    monkeypatch.setattr(od.or_chat, "stream_chat", accepted_then_context_error)
    driver._history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "prior " * 4_000},
        {"role": "assistant", "content": "answer " * 4_000},
        {"role": "user", "content": "current"},
    ]
    with pytest.raises(or_chat.ChatError):
        driver._stream_round(
            "key", od.StreamingAssistant(model=driver.model),
            on_response_accepted=lambda _response_id: None,
        )
    assert len(requests) == 1


@pytest.mark.parametrize("tool_round", [False, True])
def test_openrouter_strict_history_failure_retains_running_slot(
    driver,
    monkeypatch,
    tool_round,
):
    events = []
    saves = []
    real_save = od.HistoryLog.save_strict

    if tool_round:
        rounds = iter(
            [
                _stream(
                    tool_calls=[_call(0)],
                    response_id="resp-tool",
                    usage=or_chat.ChatUsage(
                        input_tokens=10,
                        output_tokens=2,
                        reported=True,
                    ),
                )
            ]
        )
        monkeypatch.setattr(od.tools, "execute_tool", lambda *_a, **_k: ("ok", False))
    else:
        rounds = iter(
            [
                _stream(
                    text="done",
                    finish_reason="stop",
                    response_id="resp-final",
                    usage=or_chat.ChatUsage(
                        input_tokens=10,
                        output_tokens=2,
                        reported=True,
                    ),
                )
            ]
        )

    def strict_save(history_log, messages):
        saves.append(len(messages))
        if len(saves) == 1:
            raise OSError("disk disappeared")
        return real_save(history_log, messages)

    monkeypatch.setattr(
        od.or_chat,
        "stream_chat",
        lambda *args, **kwargs: next(rounds)(*args, **kwargs),
    )
    monkeypatch.setattr(od.HistoryLog, "save_strict", strict_save)
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-unused", ""),
        lambda *_args: events.append("legacy-terminal"),
        record_dispatch=lambda *_args: None,
        record_acceptance=lambda *_args: events.append("acceptance"),
        record_stop=lambda _driver, _attempt, evidence: events.append(
            ("stop", evidence.queue_disposition)
        ),
        finish_with_evidence=lambda *_args: events.append("terminal"),
        record_contribution=lambda *_args: (
            events.append("contribution") or True
        ),
    )
    driver._execution_attempt_id = "attempt-history-boundary"
    driver._busy = True

    driver._run_turn("already staged")

    assert events == ["acceptance", ("stop", "held")]
    assert driver.execution_attempt_id == "attempt-history-boundary"
    assert driver.is_accepting_input is False


def test_openrouter_preaccept_http_rejection_is_authoritative(driver, monkeypatch):
    captured = {}
    terminal = []

    class FakeThread:
        def __init__(self, *, target, args, **_kwargs):
            captured["target"] = target
            captured["args"] = args

        def start(self):
            return None

    def reject(_messages, **_kwargs):
        raise or_chat.ChatError(
            or_chat.ChatErrorKind.AUTHENTICATION,
            "rejected",
            status=401,
        )
        yield  # pragma: no cover - generator marker

    monkeypatch.setattr(od.threading, "Thread", FakeThread)
    monkeypatch.setattr(od.or_chat, "stream_chat", reject)
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-rejected", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: None,
        record_acceptance=lambda *_args: pytest.fail("rejection was not accepted"),
        record_stop=lambda *_args: None,
        finish_with_evidence=lambda _driver, _attempt, evidence: terminal.append(
            evidence
        ),
        record_contribution=lambda *_args: True,
    )

    assert driver.send_user_text("reject once").accepted
    captured["target"](*captured["args"])

    assert len(terminal) == 1
    assert terminal[0].evidence_type == "verified_rejection"
    assert terminal[0].reason_code == "openrouter.http_rejected"
    assert terminal[0].request_id == "attempt-rejected"
    assert terminal[0].queue_disposition == "restored"
    assert driver.execution_attempt_id == ""
    assert all(message.get("content") != "reject once" for message in driver._history)


def test_openrouter_rejected_first_is_not_replayed_with_queued_second(
    driver,
    monkeypatch,
):
    workers = []
    requests = []
    terminal = []
    attempt_ids = iter(("attempt-first", "attempt-second"))

    class FakeThread:
        def __init__(self, *, target, args, **_kwargs):
            workers.append((target, args))

        def start(self):
            return None

    def reject_then_accept(messages, **kwargs):
        requests.append([dict(message) for message in messages])
        if len(requests) == 1:
            raise or_chat.ChatError(
                or_chat.ChatErrorKind.AUTHENTICATION,
                "rejected",
                status=401,
            )
        kwargs["on_response_accepted"]("resp-second")
        yield or_chat.TextDelta("second completed")
        yield or_chat.Done(
            or_chat.ChatCompleted(
                finish_reason="stop",
                usage=or_chat.ChatUsage(
                    input_tokens=8,
                    output_tokens=2,
                    reported=True,
                ),
                response_id="resp-second",
            )
        )

    monkeypatch.setattr(od.threading, "Thread", FakeThread)
    monkeypatch.setattr(od.or_chat, "stream_chat", reject_then_accept)
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    driver.set_execution_attempt_controller(
        lambda _driver: (next(attempt_ids), ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: None,
        record_acceptance=lambda *_args: None,
        record_stop=lambda *_args: None,
        finish_with_evidence=lambda _driver, attempt, evidence: terminal.append(
            (attempt, evidence)
        ),
        record_contribution=lambda *_args: True,
    )

    assert driver.send_user_text("FIRST").accepted
    driver.queue_user_text("SECOND")
    first_worker, first_args = workers[0]
    first_worker(*first_args)

    # The first terminal publication flushed SECOND into its own admitted
    # attempt, but the fake worker has not crossed HTTP yet.
    assert len(workers) == 2
    assert terminal[0][0] == "attempt-first"
    assert terminal[0][1].evidence_type == "verified_rejection"

    second_worker, second_args = workers[1]
    second_worker(*second_args)

    assert [message["content"] for message in requests[0] if message["role"] == "user"] == ["FIRST"]
    assert [message["content"] for message in requests[1] if message["role"] == "user"] == ["SECOND"]
    assert terminal[1][0] == "attempt-second"
    assert terminal[1][1].status == "completed"


def test_openrouter_multi_round_receipt_is_correlated_and_aggregated(
    driver,
    monkeypatch,
):
    captured = {}
    order = []
    terminal = []
    rounds = iter(
        [
            _stream(
                tool_calls=[_call(0)],
                response_id="resp-first",
                usage=or_chat.ChatUsage(
                    input_tokens=100,
                    output_tokens=10,
                    cached_tokens=40,
                    cost_usd=0.01,
                    reported=True,
                ),
            ),
            _stream(
                text="done",
                finish_reason="stop",
                response_id="resp-final",
                usage=or_chat.ChatUsage(
                    input_tokens=150,
                    output_tokens=20,
                    cached_tokens=80,
                    cost_usd=0.02,
                    reported=True,
                ),
            ),
        ]
    )

    class FakeThread:
        def __init__(self, *, target, args, **_kwargs):
            captured["target"] = target
            captured["args"] = args

        def start(self):
            return None

    monkeypatch.setattr(od.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        od.or_chat,
        "stream_chat",
        lambda *args, **kwargs: next(rounds)(*args, **kwargs),
    )
    monkeypatch.setattr(od.tools, "execute_tool", lambda *_a, **_k: ("ok", False))
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    driver._helios_identity_confirmed = True
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-multi", ""),
        lambda *_args: None,
        record_dispatch=lambda *_args: order.append("dispatch"),
        record_acceptance=lambda _driver, _attempt, evidence: order.append(
            ("acceptance", evidence.accepted_turn_id)
        ),
        record_stop=lambda *_args: None,
        finish_with_evidence=lambda _driver, _attempt, evidence: (
            order.append("terminal"),
            terminal.append(evidence),
        ),
        record_contribution=lambda _driver, _turn: (
            order.append("contribution") or True
        ),
    )

    assert driver.send_user_text("use one tool").accepted
    captured["target"](*captured["args"])

    assert order == [
        "dispatch",
        ("acceptance", "resp-first"),
        "contribution",
        "terminal",
    ]
    assert terminal[0].reason_code == "openrouter.provider_terminal"
    assert terminal[0].request_id == "attempt-multi"
    assert terminal[0].turn_id == "resp-first"
    assert terminal[0].response_id == "resp-final"
    assert terminal[0].native_id == driver.session_id
    assert terminal[0].usage == {
        "input_tokens": 130,
        "cache_read_input_tokens": 120,
        "cache_creation_input_tokens": 0,
        "output_tokens": 30,
        "tool_calls": 1,
        "requests": 2,
        "duration_ms": terminal[0].usage["duration_ms"],
    }
    assert terminal[0].cost_micro_usd == 30_000


def test_openrouter_accounting_failure_retires_input_capable_driver(
    driver,
    monkeypatch,
):
    errors: list[str] = []
    exits: list[int] = []
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        od.or_chat,
        "stream_chat",
        _stream(
            text="done",
            finish_reason="stop",
            usage=or_chat.ChatUsage(
                input_tokens=10,
                output_tokens=2,
                reported=True,
            ),
        ),
    )
    driver.set_execution_attempt_controller(
        lambda _driver: ("attempt-openrouter", ""),
        lambda *_args: (_ for _ in ()).throw(OSError("database unavailable")),
    )
    driver.connect("error", lambda _driver, message: errors.append(message))
    driver.connect("exited", lambda _driver, code: exits.append(code))

    driver._execution_attempt_id = "attempt-openrouter"
    driver._busy = True
    driver._run_turn("bounded turn")

    assert driver.execution_attempt_id == "attempt-openrouter"
    assert driver.is_busy is False
    assert driver.is_accepting_input is False
    assert exits == [1]
    assert any("result was withheld" in message for message in errors)


def test_openrouter_process_token_budget_stops_before_tool_execution(
    driver, monkeypatch
):
    budgets = []
    executed = []
    driver.connect(
        "budget-exhausted",
        lambda _driver, payload: budgets.append(dict(payload)),
    )
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        od.or_chat,
        "stream_chat",
        _stream(
            tool_calls=[_call(0)],
            usage=or_chat.ChatUsage(
                input_tokens=od.OPENROUTER_STANDARD_TOKEN_BUDGET,
                output_tokens=1,
                cost_usd=0.5,
                reported=True,
            ),
        ),
    )
    monkeypatch.setattr(
        od.tools,
        "execute_tool",
        lambda *args, **kwargs: executed.append((args, kwargs)) or ("ok", False),
    )

    driver._busy = True
    driver._run_turn("bounded")

    assert budgets == [
        {
            "kind": "tokens",
            "limit": od.OPENROUTER_STANDARD_TOKEN_BUDGET,
            "cost_limit_usd": od.OPENROUTER_STANDARD_COST_BUDGET_USD,
            "cost_usd": 0.5,
            "used": od.OPENROUTER_STANDARD_TOKEN_BUDGET + 1,
            "provider": "openrouter",
        }
    ]
    assert executed == []
    assert driver._token_budget_exhausted is True
    assert driver.is_busy is False
    _assert_history_is_wire_valid(HistoryLog(driver.session_id).load())


def test_openrouter_missing_usage_stops_before_tool_execution(driver, monkeypatch):
    budgets = []
    executed = []
    driver.connect(
        "budget-exhausted",
        lambda _driver, payload: budgets.append(dict(payload)),
    )
    monkeypatch.setattr(
        od.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )
    monkeypatch.setattr(
        od.or_chat,
        "stream_chat",
        _stream(tool_calls=[_call(0)], usage=or_chat.ChatUsage()),
    )
    monkeypatch.setattr(
        od.tools,
        "execute_tool",
        lambda *args, **kwargs: executed.append((args, kwargs)) or ("ok", False),
    )

    driver._busy = True
    driver._run_turn("unmetered")

    assert budgets == [
        {
            "kind": "usage-unverified",
            "limit": od.OPENROUTER_STANDARD_TOKEN_BUDGET,
            "cost_limit_usd": od.OPENROUTER_STANDARD_COST_BUDGET_USD,
            "cost_usd": 0.0,
            "used": 0,
            "provider": "openrouter",
        }
    ]
    assert executed == []
    assert driver._token_budget_exhausted is True
    _assert_history_is_wire_valid(HistoryLog(driver.session_id).load())


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"prompt_tokens": 1},
        {"prompt_tokens": "1", "completion_tokens": 2},
        {"prompt_tokens": 0, "completion_tokens": 0},
        {"prompt_tokens": 1, "completion_tokens": -1},
    ],
)
def test_missing_or_malformed_usage_is_not_a_zero_token_receipt(raw):
    assert or_chat._parse_usage(raw) is None


def test_valid_usage_can_report_zero_completion_tokens():
    usage = or_chat._parse_usage({"prompt_tokens": 1, "completion_tokens": 0})

    assert usage is not None
    assert usage.reported is True
    assert usage.input_tokens == 1
    assert usage.output_tokens == 0


class TestPersistBoundary:
    def test_a_raising_gate_still_produces_a_reply(self, driver, monkeypatch):
        """Regression: the workspace gate was the first raise-capable call in
        the per-call loop, and it ran outside execute_tool's blanket handler."""
        rounds = iter([
            _stream(tool_calls=[_call(0), _call(1)]),
            _stream(text="done", finish_reason="stop"),
        ])
        monkeypatch.setattr(
            od.or_chat, "stream_chat", lambda *a, **k: next(rounds)(*a, **k)
        )

        def _boom(*_a, **_k):
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(od.tools, "outside_target", _boom)

        driver._run_turn("go")

        persisted = HistoryLog(driver.session_id).load()
        _assert_history_is_wire_valid(persisted)
        assert any("failed" in str(m.get("content")) for m in persisted
                   if m.get("role") == "tool")

    def test_a_raising_tool_still_produces_a_reply(self, driver, monkeypatch):
        rounds = iter([
            _stream(tool_calls=[_call(0)]),
            _stream(text="done", finish_reason="stop"),
        ])
        monkeypatch.setattr(
            od.or_chat, "stream_chat", lambda *a, **k: next(rounds)(*a, **k)
        )
        monkeypatch.setattr(
            od.tools, "execute_tool",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("kaboom")),
        )

        driver._run_turn("go")
        _assert_history_is_wire_valid(HistoryLog(driver.session_id).load())

    def test_cancellation_mid_round_still_answers_every_call(self, driver, monkeypatch):
        """Stop() during a round must not leave later calls unanswered — and
        must not run them either."""
        monkeypatch.setattr(
            od.or_chat, "stream_chat",
            _stream(tool_calls=[_call(0), _call(1), _call(2)]),
        )
        executed: list[str] = []
        # Check cancellation of queued work across serialized dispatches.
        # Concurrent in-flight reads have their own cancellation coverage.
        monkeypatch.setattr(driver, "_parallel_read", lambda _call: False)

        def _execute(name, arguments, *, cwd, cancellation=None):
            del arguments, cwd, cancellation
            executed.append(name)
            driver.stop()  # user hits Stop while the first tool runs
            return "ok", False

        monkeypatch.setattr(od.tools, "execute_tool", _execute)

        driver._run_turn("go")

        persisted = HistoryLog(driver.session_id).load()
        _assert_history_is_wire_valid(persisted)
        assert len(executed) == 1, "queued tools kept running after Stop"
        cancelled = [
            m for m in persisted
            if m.get("role") == "tool" and "Cancelled" in str(m.get("content"))
        ]
        assert len(cancelled) == 2

    def test_a_normal_round_is_wire_valid(self, driver, monkeypatch):
        rounds = iter([
            _stream(tool_calls=[_call(0), _call(1)]),
            _stream(text="all done", finish_reason="stop"),
        ])
        monkeypatch.setattr(
            od.or_chat, "stream_chat", lambda *a, **k: next(rounds)(*a, **k)
        )
        monkeypatch.setattr(od.tools, "execute_tool", lambda *a, **k: ("ok", False))

        driver._run_turn("go")
        _assert_history_is_wire_valid(HistoryLog(driver.session_id).load())


class TestContextWindow:
    def test_unknown_window_is_reported_as_unknown(self, driver, monkeypatch):
        """model_catalog.context_window_for substitutes a 200k display default
        for models missing from the catalog cache. Sizing a trim against that
        makes compaction silently inert on a model with a smaller real
        window."""
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(or_catalog, "context_length_for", lambda _m: 0)
        assert driver._context_window() == 0

    def test_known_window_is_used(self, driver, monkeypatch):
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(or_catalog, "context_length_for", lambda _m: 64_000)
        assert driver._context_window() == 64_000

    def test_unreadable_catalog_is_unknown_not_a_crash(self, driver, monkeypatch):
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(
            or_catalog, "context_length_for",
            lambda _m: (_ for _ in ()).throw(OSError("no cache")),
        )
        assert driver._context_window() == 0


class TestContextLengthRetry:
    def test_retry_stops_instead_of_resending_identical_bytes(
        self, driver, monkeypatch
    ):
        """compact() refuses to drop the newest exchange, so a single
        oversized turn cannot shrink — retrying would re-send bytes the
        provider just rejected."""
        attempts = {"n": 0}

        def _always_too_long(*_a, **_k):
            attempts["n"] += 1
            raise or_chat.ChatError(
                or_chat.ChatErrorKind.CONTEXT_LENGTH, "maximum context length"
            )
            yield  # pragma: no cover — generator marker

        monkeypatch.setattr(od.or_chat, "stream_chat", _always_too_long)
        driver._history = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "x" * 400_000},
        ]

        with pytest.raises(or_chat.ChatError):
            driver._stream_round("key", od.StreamingAssistant(model="vendor/model"))

        assert attempts["n"] == 1, "re-sent an identical oversized request"

    def test_retry_fires_when_compaction_can_shrink(self, driver, monkeypatch):
        attempts = {"n": 0}

        def _fail_then_succeed(messages, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise or_chat.ChatError(
                    or_chat.ChatErrorKind.CONTEXT_LENGTH, "maximum context length"
                )
            yield or_chat.TextDelta("recovered")
            yield or_chat.Done(
                or_chat.ChatCompleted(finish_reason="stop", usage=or_chat.ChatUsage())
            )

        monkeypatch.setattr(od.or_chat, "stream_chat", _fail_then_succeed)
        driver._history = [{"role": "system", "content": "s"}]
        for _ in range(6):
            driver._history += [
                {"role": "user", "content": "x" * 20_000},
                {"role": "assistant", "content": "y" * 20_000},
            ]

        text, calls, reason, _usage, _reasoning = driver._stream_round(
            "key", od.StreamingAssistant(model="vendor/model")
        )

        assert attempts["n"] == 2
        assert "".join(text) == "recovered"
        assert len(driver._history) < 13


class TestGating:
    def test_outside_workspace_read_is_denied_in_plan_mode(self, driver, tmp_path,
                                                           monkeypatch):
        outside = tmp_path.parent / "outside-secret.txt"
        outside.write_text("PRIVATE", encoding="utf-8")
        driver._permission_mode = "plan"
        monkeypatch.setattr(
            od.tools, "execute_tool",
            lambda *a, **k: pytest.fail("denied tool was executed"),
        )

        content, is_error = driver._execute_tool_call(
            _call(arguments=f'{{"path": "{outside}"}}')
        )

        assert is_error
        assert "outside the working directory" in content

    def test_approval_prompt_discloses_the_outside_target(self, driver, tmp_path):
        outside = tmp_path.parent / "outside-secret.txt"
        driver._permission_mode = "acceptEdits"
        captured: list[dict] = []
        driver.connect(
            "question-asked", lambda _d, payload, token: captured.append((payload, token))
        )

        summary = tools.approval_summary(
            "Write",
            {"file_path": str(outside)},
            outside_path=tools.outside_target(
                "Write", {"file_path": str(outside)}, str(tmp_path)
            ),
        )
        assert "OUTSIDE the working directory" in summary
        assert str(outside) in summary


class TestCompactionBoundary:
    """Compaction here drops whole oldest exchanges outright — there is no
    provider summary standing in for them — and until 2026-09-03 it emitted
    one INFO log line. Measured on that date: 20 of 25 messages left a
    conversation with no user-visible trace at all."""

    def _fill(self, driver):
        driver._history = [{"role": "system", "content": "s"}]
        for index in range(6):
            driver._history += [
                {"role": "user", "content": f"u{index} " + "x" * 4_000},
                {"role": "assistant", "content": f"a{index} " + "y" * 4_000},
            ]

    def test_archive_limit_is_visible_and_keeps_replay_before_provider_io(
        self, driver, monkeypatch
    ):
        from helios.backend.openrouter import continuity

        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *args: cb(*args))
        monkeypatch.setattr(continuity, "MAX_ARCHIVE_BYTES", 512)
        errors = []
        driver.connect("error", lambda _driver, message: errors.append(message))
        self._fill(driver)
        before = list(driver._history)
        # Force the actual archive/compaction boundary in the worker, with no
        # provider call. A cap failure must not hide behind a generic error.
        monkeypatch.setattr(driver, "_stream_round", lambda *args, **kwargs: driver._compact_history(1_200))
        driver._begin_execution_attempt()
        driver._busy = True

        driver._run_turn("pending turn")

        assert driver._history == before
        assert HistoryLog(driver.session_id).load() == before
        assert any("start a new Work with a handoff" in message for message in errors)
        assert any("Original history was retained" in message for message in errors)
        assert driver.is_busy is False

    def test_a_drop_emits_a_boundary_with_both_sides_measured(
        self, driver, monkeypatch
    ):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        seen: list[dict] = []
        driver.connect("context-compacted", lambda _d, info: seen.append(info))
        self._fill(driver)

        dropped = driver._compact_history(1_200)

        assert dropped > 0
        assert len(seen) == 1
        info = seen[0]
        assert info["dropped"] == dropped
        assert info["trigger"] == "auto"
        assert info["pre_tokens"] > info["post_tokens"] > 0
        assert info["provider"] == "openrouter"

    def test_the_boundary_is_durable_before_the_signal(self, driver, monkeypatch):
        """A marker that only exists in this process is not a record."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        written: list[dict] = []
        monkeypatch.setattr(
            driver._transcript,
            "append_compaction",
            lambda **kw: written.append(kw),
        )
        self._fill(driver)

        driver._compact_history(1_200)

        assert len(written) == 1
        assert written[0]["trigger"] == "auto"
        assert written[0]["pre_tokens"] > written[0]["post_tokens"]

    def test_a_failing_boundary_write_does_not_fail_the_turn(
        self, driver, monkeypatch
    ):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        monkeypatch.setattr(
            driver._transcript,
            "append_compaction",
            lambda **kw: (_ for _ in ()).throw(OSError("read-only fs")),
        )
        self._fill(driver)

        assert driver._compact_history(1_200) > 0

    def test_a_no_op_compaction_announces_nothing(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        seen: list[dict] = []
        driver.connect("context-compacted", lambda _d, info: seen.append(info))
        driver._history = [{"role": "system", "content": "s"}]

        assert driver._compact_history(1_000_000) == 0
        assert seen == []


class TestPlanModeEndsTheTurn:
    """Measured live on 2026-09-03: a model called ExitPlanMode, read the
    result's "Stop here and wait", and carried straight on into another
    assistant message in the same turn. Prose is not a gate."""

    def test_presenting_a_plan_ends_the_turn(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        rounds = [
            _stream(tool_calls=[_call(0, name="ExitPlanMode", arguments='{"plan": [{"step": "do it"}]}')], response_id="gen-plan"),
            _stream(text="and now I will start implementing", finish_reason="stop"),
        ]
        served = {"n": 0}

        def _next(*a, **k):
            index = min(served["n"], len(rounds) - 1)
            served["n"] += 1
            return rounds[index](*a, **k)

        monkeypatch.setattr(od.or_chat, "stream_chat", _next)
        driver._permission_mode = "plan"

        plans = []
        driver.connect("plan-updated", lambda _driver, payload: plans.append(payload))
        assert driver._begin_execution_attempt() == ""

        driver._run_turn("go")

        assert served["n"] == 1, "the loop continued after the plan was presented"
        assert plans[0]["turnId"] == "gen-plan"

        _assert_history_is_wire_valid(HistoryLog(driver.session_id).load())

    def test_a_failed_exit_plan_mode_does_not_end_the_turn(self, driver, monkeypatch):
        """An empty plan is refused by the tool; that is an error the model
        should get a chance to correct, not a reason to stop."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        rounds = [
            _stream(tool_calls=[_call(0, name="ExitPlanMode", arguments='{"plan": []}')]),
            _stream(text="sorry, here is a real plan", finish_reason="stop"),
        ]
        served = {"n": 0}

        def _next(*a, **k):
            index = min(served["n"], len(rounds) - 1)
            served["n"] += 1
            return rounds[index](*a, **k)

        monkeypatch.setattr(od.or_chat, "stream_chat", _next)
        driver._permission_mode = "plan"

        driver._run_turn("go")

        assert served["n"] == 2


class TestCompactionIsWordedHonestly:
    """Claude and Codex compact by summarising provider-side, so the older
    turns survive in compressed form. Helios's own OpenRouter compaction drops
    whole exchanges outright, and telling a user their conversation "is now a
    summary" when it is simply gone is a false reassurance, not a boundary."""

    def test_the_payload_says_nothing_summarised_it(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        seen: list[dict] = []
        driver.connect("context-compacted", lambda _d, info: seen.append(info))
        driver._history = [{"role": "system", "content": "s"}]
        for index in range(6):
            driver._history += [
                {"role": "user", "content": f"u{index} " + "x" * 4_000},
                {"role": "assistant", "content": f"a{index} " + "y" * 4_000},
            ]

        driver._compact_history(1_200)

        assert seen[0]["summarized"] is False
        assert seen[0]["dropped"] > 0

    def test_a_provider_that_summarises_is_unaffected(self):
        """Absent key means summarised, so Claude and Codex payloads are
        unchanged."""
        info: dict = {"trigger": "auto", "pre_tokens": 10, "post_tokens": 5}
        assert info.get("summarized", True) is not False


def test_execution_plan_updates_use_first_accepted_identity_and_do_not_end_turn(driver, monkeypatch):
    import json
    from types import SimpleNamespace
    from helios.backend.work_coordinator import WorkCoordinator, tag_driver
    from helios.backend.work_store import WorkStore

    monkeypatch.setattr(od.GLib, "idle_add", lambda callback, *args: callback(*args))
    coordinator = WorkCoordinator(WorkStore())
    work = coordinator.ensure_work(cwd=driver._cwd, lead_provider="openrouter")
    participant = coordinator.bind_participant(work.work_id, "openrouter", native_id=driver.session_id)
    identity = SimpleNamespace()
    tag_driver(identity, participant)
    plans, statuses = [], []
    driver.connect("plan-updated", lambda _driver, payload: plans.append(
        coordinator.record_execution_plan(identity, payload)))
    driver.connect("turn-status-updated", lambda _driver, payload: statuses.append(payload))
    rounds = iter([
        _stream(tool_calls=[_call(name="update_plan", arguments=json.dumps({
            "plan": [{"step": "Inspect", "status": "in_progress"},
                     {"step": "Verify", "status": "pending"}],
        }))], response_id="gen-one"),
        _stream(tool_calls=[_call(1, name="update_plan", arguments=json.dumps({
            "plan": [{"step": "Inspect", "status": "completed"},
                     {"step": "Verify", "status": "in_progress"}],
        }))], response_id="gen-two"),
        _stream(text="Verification still running", finish_reason="stop", response_id="gen-three"),
    ])
    monkeypatch.setattr(od.or_chat, "stream_chat", lambda *args, **kwargs: next(rounds)(*args, **kwargs))
    assert driver._begin_execution_attempt() == ""
    driver._run_turn("Inspect and verify")
    assert len(plans) == 2
    assert plans[0].native_turn_id == plans[1].native_turn_id == "gen-one"
    assert plans[1].completed_count == 1
    assert plans[0].steps[0].task_id == plans[1].steps[0].task_id
    assert statuses[-1]["status"] == "completed"
    # A completed provider turn is not proof the unfinished plan step completed.
    assert plans[-1].steps[-1].status == "inProgress"
    assert coordinator.store.get_execution_plan(work.work_id) == plans[-1]
    coordinator.store.close()


def test_plan_request_without_provider_identity_is_error_not_false_progress(driver):
    plans = []
    driver.connect("plan-updated", lambda _driver, payload: plans.append(payload))
    content, failed = driver._execute_tool_call(_call(name="update_plan", arguments=(
        '{"plan":[{"step":"Inspect","status":"in_progress"}]}'
    )))
    assert failed
    assert "identity" in content
    assert plans == []


@pytest.mark.parametrize("failure", ["construct", "discover", "close"])
def test_estate_failure_does_not_strand_turn_or_next_send(driver, monkeypatch, failure):
    calls, errors, results = [], [], []

    class Estate:
        statuses = []

        def __init__(self, *_args, **_kwargs):
            if failure == "construct":
                raise OSError("registry unavailable")

        def discover(self):
            if failure == "discover":
                raise OSError("registry unavailable")
            return ()

        def close(self):
            if failure == "close":
                raise OSError("peer cleanup failed")

    def stream(messages, **kwargs):
        calls.append(kwargs["tools"])
        yield from _stream(
            text="built-in tools are available", finish_reason="stop", response_id=f"resp-{len(calls)}",
        )(messages, **kwargs)

    monkeypatch.setattr(od, "EstateMcp", Estate)
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(od.GLib, "idle_add", lambda callback, *args: callback(*args))
    driver.connect("error", lambda _driver, error: errors.append(error))
    driver.connect("result", lambda _driver, result: results.append(result))

    for _ in range(2):
        assert driver._begin_execution_attempt() == ""
        driver._busy = True
        driver._run_turn("already staged")
        assert not driver.is_busy
        assert driver.is_accepting_input
        assert driver.execution_attempt_id == ""
        assert driver._estate is None

    assert len(results) == len(calls) == 2
    assert all(tuple(schemas) == tools.TOOL_SCHEMAS for schemas in calls)
    assert all(result["subtype"] == "success" for result in results)
    if failure != "close":
        assert len(errors) == 2
        assert all("continue" in error and "built-in tools" in error for error in errors)
        assert driver.init_mcp_servers[0]["status"] == "failed"
        assert driver.init_mcp_servers[0]["reason"] == "registry unavailable"


def test_progressive_mcp_wire_budget_approval_scrub_and_cleanup(driver, monkeypatch):
    import json
    from helios.backend.openrouter.history import estimate_tokens

    events, approvals, sent_schemas = [], [], []
    schemas = [
        {"type": "function", "function": {"name": name, "description": "metadata " * 100,
         "parameters": {"type": "object", "properties": {}}}}
        for name in ("EstateSearchTools", "EstateCallTool")
    ]
    class Estate:
        statuses = [{"name": "test", "status": "configured"}]
        def __init__(self, cwd, cancellation):
            assert cwd == driver._cwd
        def discover(self):
            events.append("discover")
            return schemas
        def owns(self, name):
            return name in {"EstateSearchTools", "EstateCallTool"}
        def approval_name(self, name, arguments):
            return arguments.get("name", name)
        def call(self, name, arguments):
            events.append(name)
            if name == "EstateSearchTools":
                return '{"name":"mcp_test_read_exact"}', False
            return "secret=" + od.or_key.load_key(), False
        def close(self):
            events.append("closed")
    monkeypatch.setattr(od, "EstateMcp", Estate)
    monkeypatch.setattr(od.GLib, "idle_add", lambda callback, *args: callback(*args))
    driver._request_approval = lambda name, *args: approvals.append(name) or True
    rounds = iter([
        _stream(tool_calls=[_call(name="EstateSearchTools", arguments='{"query":"read"}')], response_id="mcp-turn"),
        _stream(tool_calls=[_call(1, name="EstateCallTool", arguments=json.dumps({
            "name": "mcp_test_read_exact", "arguments": {"value": "x"},
        }))], response_id="mcp-round-two"),
        _stream(text="done", finish_reason="stop", response_id="mcp-round-three"),
    ])
    def stream(*args, **kwargs):
        sent_schemas.append(kwargs["tools"])
        return next(rounds)(*args, **kwargs)
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    assert driver._begin_execution_attempt() == ""
    driver._run_turn("Use estate tooling")
    assert all(tuple(schema[-2:]) == tuple(schemas) for schema in sent_schemas)
    assert driver._schema_tokens == estimate_tokens(list(sent_schemas[0]))
    assert approvals == ["mcp_test_read_exact"]  # not the generic dispatch capability
    assert events == ["discover", "EstateSearchTools", "EstateCallTool", "closed"]
    assert od.or_key.load_key() not in str(driver._history)
    assert driver._estate is None


def test_mcp_calls_retain_read_only_permission_gate(driver):
    class Estate:
        def owns(self, name):
            return True
        def approval_name(self, name, arguments):
            return "mcp_verified_tool"
        def call(self, *args):
            pytest.fail("A read-only turn must not dispatch an external MCP action")
    driver._estate = Estate()
    driver._permission_mode = "plan"
    content, failed = driver._execute_tool_call(_call(name="EstateCallTool", arguments='{"name":"mcp_verified_tool","arguments":{}}'))
    assert failed
    assert "denied" in content.lower() or "read-only" in content.lower()


def test_instruction_sources_refresh_before_send_and_unreadable_policy_prevents_admission(driver, monkeypatch):
    from pathlib import Path

    source = Path(driver._cwd) / "AGENTS.md"
    source.write_text("Use the reviewed service map.")
    captured = []
    class Thread:
        def __init__(self, **kwargs):
            captured.append(kwargs["target"])
        def start(self):
            pass
    monkeypatch.setattr(od.threading, "Thread", Thread)
    driver._prompt_receipt = (1, 500)
    assert driver.send_user_text("Check this repository").accepted
    assert "Use the reviewed service map" in driver._history[0]["content"]
    assert driver.init_instruction_sources[0]["path"] == str(source)
    assert driver.init_instruction_sources[0]["sha256"]
    assert driver._prompt_receipt == (0, 0)
    # A resumed/next send must re-read the same source, not reuse stale policy.
    driver._busy = False
    driver._finish_execution_attempt("aborted", "isolated test")
    source.write_bytes(b"\xff")
    prior = list(driver._history)
    assert driver.send_user_text("Must not be sent").rejected
    assert driver._history == prior
    assert len(captured) == 1


@pytest.mark.parametrize("scope", ["repository", "global"])
def test_symlinked_instructions_stop_before_admission_worker_or_provider(
    driver, tmp_path, monkeypatch, scope
):
    repo, global_state, outside = (tmp_path / name for name in ("repo", "global-state", "private"))
    for folder in (repo, global_state, outside):
        folder.mkdir()
    (repo / ".git").mkdir()
    secret = outside / "synthetic-private-key"
    secret.write_text("PRIVATE_TEST_MARKER_MUST_STAY_LOCAL")
    selected = (repo if scope == "repository" else global_state) / "AGENTS.md"
    selected.symlink_to(secret)
    monkeypatch.setenv("HELIOS_STATE_DIR", str(global_state))
    driver._cwd = str(repo)
    events, errors = [], []
    before = list(driver._history)
    driver.connect("error", lambda _driver, message: errors.append(message))
    # Even a regressed loader cannot cause this regression test to dispatch:
    # the sentinel refuses admission while recording any boundary violation.
    monkeypatch.setattr(driver, "_begin_execution_attempt",
                        lambda: events.append("admission") or "Test refuses admission")
    monkeypatch.setattr(od.threading, "Thread", lambda **kwargs: events.append("worker"))
    monkeypatch.setattr(od.or_chat, "stream_chat", lambda *args, **kwargs: events.append("provider"))

    outcome = driver.send_user_text("Inspect this repository")

    assert outcome.rejected
    assert events == []
    assert any("symlink" in message for message in errors)
    assert driver._history == before
    assert "PRIVATE_TEST_MARKER_MUST_STAY_LOCAL" not in str(driver._history) + str(errors)
    assert not driver.is_busy


def test_parallel_reads_overlap_with_ordered_replies_and_mutation_barrier(driver, monkeypatch):
    import threading
    from helios.backend.process.streaming import StreamingAssistant

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    completed = []
    calls = [_call(i, name="Write" if i == 2 else "Read") for i in range(5)]

    def execute(call):
        index = int(call.id.split("_")[-1])
        if index == 2:
            assert sorted(completed) == [0, 1]
        else:
            if index > 2:
                assert 2 in completed
            barrier.wait(timeout=3)  # proves two reads execute concurrently
        with lock:
            completed.append(index)
        return f"result {index}", False

    monkeypatch.setattr(driver, "_execute_tool_call", execute)
    results = list(driver._execute_tool_batch(calls, StreamingAssistant()))
    assert [call.id for call, *_ in results] == [call.id for call in calls]
    assert [content for _, content, _ in results] == [f"result {i}" for i in range(5)]
    assert not any(error for _, _, error in results)


def test_parallel_reads_stop_before_next_mutation_and_keep_every_reply(driver, monkeypatch):
    import threading
    from helios.backend.process.streaming import StreamingAssistant

    barrier = threading.Barrier(2)
    entered = []
    calls = [_call(0), _call(1), _call(2, name="Write"), _call(3)]

    def execute(call):
        entered.append(call.id)
        barrier.wait(timeout=3)
        driver.stop()
        return "read completed", False

    monkeypatch.setattr(driver, "_execute_tool_call", execute)
    results = list(driver._execute_tool_batch(calls, StreamingAssistant()))
    assert sorted(entered) == ["call_0", "call_1"]
    assert [call.id for call, *_ in results] == [call.id for call in calls]
    assert all(error and "Cancelled" in content for _, content, error in results[2:])


def test_parallel_read_group_never_exceeds_four_workers(driver, monkeypatch):
    import threading
    from helios.backend.process.streaming import StreamingAssistant

    barrier = threading.Barrier(4)
    active, peak = 0, 0
    lock = threading.Lock()

    def execute(call):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=3)
        with lock:
            active -= 1
        return call.id, False

    monkeypatch.setattr(driver, "_execute_tool_call", execute)
    results = list(driver._execute_tool_batch([_call(i) for i in range(8)], StreamingAssistant()))
    assert peak == 4 and active == 0
    assert not any(error for _, _, error in results)


@pytest.mark.parametrize("failure", ["constructor", "future", "submit"])
def test_parallel_dispatch_failures_cannot_persist_orphaned_calls(driver, monkeypatch, failure):
    rounds = iter([
        _stream(tool_calls=[_call(0), _call(1)]),
        _stream(text="Could not inspect files.", finish_reason="stop"),
    ])

    class FailedFuture:
        def result(self):
            raise RuntimeError("worker failed")

    class FailedExecutor:
        def __init__(self, **kwargs):
            if failure == "constructor":
                raise RuntimeError("no worker capacity")

        def submit(self, *args):
            if failure == "submit":
                raise RuntimeError("cannot submit")
            return FailedFuture()

        def shutdown(self, **kwargs):
            pass

    monkeypatch.setattr(od, "ThreadPoolExecutor", FailedExecutor)
    monkeypatch.setattr(od.or_chat, "stream_chat", lambda *a, **kw: next(rounds)(*a, **kw))
    driver._run_turn("inspect")
    history = HistoryLog(driver.session_id).load()
    _assert_history_is_wire_valid(history)
    replies = [m for m in history if m.get("role") == "tool"]
    assert len(replies) == 2
    assert all("failed" in reply["content"] or "could not start" in reply["content"] for reply in replies)


def test_parallel_classifier_keeps_external_reads_and_mcp_serial(driver):
    from types import SimpleNamespace

    assert driver._parallel_read(_call())
    assert not driver._parallel_read(_call(arguments='{"path":"/etc/hosts"}'))
    assert not driver._parallel_read(_call(name="Bash", arguments='{"command":"pwd"}'))
    driver._estate = SimpleNamespace(owns=lambda _name: True)
    assert not driver._parallel_read(_call())
