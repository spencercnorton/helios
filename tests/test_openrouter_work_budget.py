"""Actual driver loop with WorkStore accounting and controlled provider replies."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from helios.backend.openrouter import chat
from helios.backend.openrouter.history import HistoryLog
from helios.backend.openrouter.routes import Route
from helios.backend.process import openrouter_driver as od
from helios.backend.process.streaming import StreamingAssistant
from helios.backend.work_store import WorkStore


@pytest.fixture
def bound_driver(tmp_path, monkeypatch):
    monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *args: cb(*args))
    monkeypatch.setattr(od.or_key, "load_key", lambda: "test-key")
    route = Route(
        model="deepseek/test", provider_slug="test", provider_name="Test",
        context_length=1_000_000, max_completion_tokens=393216,
        quantization="fp8", supports_tools=True, supports_reasoning=True,
        implicit_caching=False, explicit_caching=True,
        input_price=0.134e-6, cache_read_price=0.0268e-6, output_price=0.268e-6,
    )
    monkeypatch.setattr(od.or_routes, "route_for", lambda *a, **kw: route)
    store = WorkStore(tmp_path / "work.db")
    driver = od.OpenRouterDriver(cwd=str(tmp_path), model=route.model, permission_mode="auto")
    driver.start()
    driver._helios_identity_confirmed = True
    work = store.create_work(cwd=str(tmp_path), lead_provider="openrouter")
    participant = store.bind_participant(work.work_id, "openrouter", native_session_id=driver.session_id)
    attempt = store.start_execution_attempt(
        work.work_id, participant.participant_id, provider="openrouter",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        attempt.attempt_id, wire_prompt_text="bounded", provider_request_key=attempt.attempt_id,
    )

    def finish(_driver, attempt_id, evidence):
        receipt = {key: getattr(evidence, key) for key in (
            "evidence_type", "provider_status", "request_id", "turn_id", "response_id", "native_id",
        ) if getattr(evidence, key)}
        store.finish_execution_attempt(
            attempt_id, status=evidence.status, terminal_reason=evidence.reason_code,
            terminal_receipt=receipt, usage=evidence.usage,
            cost_micro_usd=evidence.cost_micro_usd, queue_disposition=evidence.queue_disposition,
            stop_acknowledgement=evidence.stop_acknowledgement,
        )

    driver.set_execution_attempt_controller(
        lambda _driver: (attempt.attempt_id, ""), lambda *args: None,
        record_acceptance=lambda _driver, aid, evidence: store.record_execution_acceptance(
            aid, accepted_turn_id=evidence.accepted_turn_id,
            provider_request_key=evidence.provider_request_key,
        ),
        record_stop=lambda *args: None, record_contribution=lambda *args: True,
        finish_with_evidence=finish,
    )
    driver.set_work_budget_store(store)
    driver._execution_attempt_id = attempt.attempt_id
    driver._history = [{"role": "system", "content": "Use tools."}, {"role": "user", "content": "bounded"}]
    driver._busy = True
    try:
        yield driver, store, attempt
    finally:
        store.close()


@pytest.mark.parametrize("durable", [False, True])
def test_cached_replay_crosses_200k_and_finishes_file_work(bound_driver, monkeypatch, tmp_path, durable):
    driver, store, attempt = bound_driver
    if not durable:
        driver.set_work_budget_store(None)  # original process-token policy control
    calls = []

    def stream(messages, **kwargs):
        # The reservation must already be on disk before the HTTP boundary.
        if durable:
            assert store._conn.execute("SELECT COUNT(*) FROM openrouter_request_budgets").fetchone()[0] == len(calls) + 1
        calls.append(kwargs["max_tokens"])
        kwargs["on_response_accepted"](f"response-{len(calls)}")
        if len(calls) < 12:
            yield chat.ToolCallDelta(chat.ToolCallRequest(
                id=f"write-{len(calls)}", name="Write",
                arguments_json=json.dumps({"file_path": "result.txt", "content": str(len(calls))}),
            ))
        else:
            yield chat.TextDelta("Verified result.txt contains 11.")
        yield chat.Done(chat.ChatCompleted(
            finish_reason="tool_calls" if len(calls) < 12 else "stop",
            usage=chat.ChatUsage(input_tokens=20_000, cached_tokens=18_000, output_tokens=50,
                                 cost_usd=0.001, reported=True),
        ))

    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    errors = []
    driver.connect("error", lambda _d, message: errors.append(message))
    driver._run_turn_with_tools("bounded")
    if not durable:
        assert len(calls) == 9
        assert driver._budget_trip_kind == "tokens-projected"
        assert store.get_execution_attempt(attempt.attempt_id).status == "budgetLimited"
        return
    assert len(calls) == 12
    assert driver._lifetime_tokens_used == 240_600
    assert (tmp_path / "result.txt").read_text() == "11"
    assert not errors
    assert not driver._token_budget_exhausted
    assert store.get_execution_attempt(attempt.attempt_id).status == "completed"
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) == 12_000


def test_spent_work_stops_before_http(bound_driver, monkeypatch):
    driver, store, attempt = bound_driver
    store.reserve_openrouter_request(attempt.attempt_id, "older-request", 5_000_000)
    monkeypatch.setattr(od.or_chat, "stream_chat", lambda *a, **kw: pytest.fail("HTTP after spend cap"))
    errors = []
    driver.connect("error", lambda _d, message: errors.append(message))
    driver._run_turn_with_tools("bounded")
    assert driver._budget_trip_kind == "cost-projected"
    assert any("$5.00" in error and "Work" in error for error in errors)
    assert store.get_execution_attempt(attempt.attempt_id).status == "budgetLimited"


@pytest.mark.parametrize("progress", [False, True])
def test_rejection_releases_only_without_response_progress(bound_driver, monkeypatch, progress):
    driver, store, attempt = bound_driver
    def stream(*args, **kwargs):
        raise chat.ChatError(chat.ChatErrorKind.RATE_LIMIT, "limited", response_started=progress)
        yield
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    with pytest.raises(chat.ChatError):
        driver._stream_round("test-key", StreamingAssistant())
    rows = store._conn.execute("SELECT * FROM openrouter_request_budgets").fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == ("reserved" if progress else "rejected")
    assert (store.openrouter_spend_micro_usd(attempt.attempt_id) > 0) == progress


@pytest.mark.parametrize("kind", [chat.ChatErrorKind.CONNECTION, chat.ChatErrorKind.CANCELLED,
                                  chat.ChatErrorKind.TIMEOUT, chat.ChatErrorKind.PROTOCOL])
def test_uncertain_transport_preserves_reservation(bound_driver, monkeypatch, kind):
    driver, store, attempt = bound_driver
    def stream(*args, **kwargs):
        raise chat.ChatError(kind)
        yield
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    with pytest.raises(chat.ChatError):
        driver._stream_round("test-key", StreamingAssistant())
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) > 0


def test_reroute_reprices_and_reserves_again(bound_driver, monkeypatch):
    driver, store, attempt = bound_driver
    costly = replace(driver._route, provider_slug="expensive", output_price=0.001)
    monkeypatch.setattr(od.or_routes, "route_for", lambda *args, **kwargs: costly)
    monkeypatch.setattr(od.or_routes, "exclude", lambda *args: None)
    requested = []
    def stream(*args, **kwargs):
        requested.append((kwargs["provider_only"], kwargs["max_tokens"]))
        if len(requested) == 1:
            raise chat.ChatError(chat.ChatErrorKind.NO_ELIGIBLE_ENDPOINT)
        yield chat.Done(chat.ChatCompleted(finish_reason="stop", usage=chat.ChatUsage(
            input_tokens=1, output_tokens=1, cost_usd=0.001, reported=True,
        )))
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    driver._stream_round("test-key", StreamingAssistant())
    assert requested[0][1] > requested[1][1]
    assert requested[1][0] == "expensive"
    assert requested[1][1] < 5000
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) == 1000
    assert [row[0] for row in store._conn.execute(
        "SELECT state FROM openrouter_request_budgets ORDER BY created_at",
    )] == ["rejected", "reported"]


@pytest.mark.parametrize("cost", [None, float("nan"), -1])
def test_missing_or_invalid_cost_stops_before_tool_execution(bound_driver, monkeypatch, cost):
    driver, store, attempt = bound_driver
    def stream(*args, **kwargs):
        yield chat.ToolCallDelta(chat.ToolCallRequest(id="x", name="Write", arguments_json='{}'))
        yield chat.Done(chat.ChatCompleted(finish_reason="tool_calls", usage=chat.ChatUsage(
            input_tokens=10, output_tokens=1, cost_usd=cost, reported=True,
        )))
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(od.tools, "execute_tool", lambda *a, **kw: pytest.fail("Unmetered tool execution"))
    driver._run_turn_with_tools("bounded")
    assert driver._budget_trip_kind == "cost-unverified"
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) > 0
    assert store.get_execution_attempt(attempt.attempt_id).status == "budgetLimited"


def test_unreported_finite_cost_retains_reservation_and_stops_tools(bound_driver, monkeypatch):
    driver, store, attempt = bound_driver
    requests = []
    def stream(*args, **kwargs):
        requests.append(kwargs)
        yield chat.ToolCallDelta(chat.ToolCallRequest(id="x", name="Write", arguments_json='{}'))
        yield chat.Done(chat.ChatCompleted(finish_reason="tool_calls", usage=chat.ChatUsage(
            input_tokens=10, output_tokens=1, cost_usd=0.001, reported=False,
        )))
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(od.tools, "execute_tool", lambda *a, **kw: pytest.fail("Unmetered tool execution"))
    driver._run_turn_with_tools("bounded")
    assert len(requests) == 1
    assert driver._budget_trip_kind == "usage-unverified"
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) > 0
    assert store._conn.execute("SELECT state FROM openrouter_request_budgets").fetchone()[0] == "reserved"
    terminal = store.get_execution_attempt(attempt.attempt_id)
    assert terminal.status == "budgetLimited"
    assert terminal.cost_micro_usd is None


def test_small_context_does_not_latch_a_money_limit(bound_driver, monkeypatch):
    driver, store, attempt = bound_driver
    driver._route = replace(driver._route, context_length=100)
    monkeypatch.setattr(od.or_chat, "stream_chat", lambda *a, **kw: pytest.fail("Tiny context HTTP"))
    driver._run_turn_with_tools("bounded")
    assert not driver._token_budget_exhausted
    assert store.get_execution_attempt(attempt.attempt_id).status == "failed"
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) == 0


@pytest.mark.parametrize("kind,status,released", [
    (chat.ChatErrorKind.PROVIDER_UNAVAILABLE, 503, False),
    (chat.ChatErrorKind.HTTP, 500, False),
    (chat.ChatErrorKind.HTTP, 408, False),
    (chat.ChatErrorKind.HTTP, 400, True),
])
def test_http_failure_is_not_automatically_zero_spend(bound_driver, monkeypatch, kind, status, released):
    driver, store, attempt = bound_driver
    def stream(*args, **kwargs):
        raise chat.ChatError(kind, status=status)
        yield
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    with pytest.raises(chat.ChatError):
        driver._stream_round("test-key", StreamingAssistant())
    assert (store.openrouter_spend_micro_usd(attempt.attempt_id) == 0) == released


def _tool_response(index):
    yield chat.ToolCallDelta(chat.ToolCallRequest(
        id=f"write-{index}", name="Write",
        arguments_json=json.dumps({"file_path": "result.txt", "content": str(index)}),
    ))
    yield _receipt()


def _receipt(*, cost=0.001, reported=True, finish="stop"):
    return chat.Done(chat.ChatCompleted(
        finish_reason=finish, usage=chat.ChatUsage(
            input_tokens=1000, output_tokens=100, cost_usd=cost, reported=reported,
        ),
    ))


@pytest.mark.parametrize("handoff", ["text", "unsolicited-tool", "empty", "truncated"])
def test_round_limit_handoff_is_bounded_paired_and_resumable(
    bound_driver, monkeypatch, tmp_path, handoff,
):
    driver, store, attempt = bound_driver
    monkeypatch.setattr(od, "_MAX_TOOL_ROUNDS", 2)
    monkeypatch.setattr(od, "_MAX_PRODUCTIVE_TOOL_ROUNDS", 2)
    requests, errors, results, turns = [], [], [], []
    original_tools = driver._turn_tools
    original_system = dict(driver._history[0])

    def stream(messages, **kwargs):
        requests.append((list(messages), kwargs))
        # Every request, including the final handoff, was reserved before I/O.
        rows = store._conn.execute("SELECT state FROM openrouter_request_budgets").fetchall()
        assert len(rows) == len(requests) and rows[-1][0] == "reserved"
        kwargs["on_response_accepted"](f"response-{len(requests)}")
        if len(requests) <= 2:
            assert kwargs["allow_tool_calls"]
            yield from _tool_response(len(requests))
        else:
            assert not kwargs["allow_tool_calls"]
            assert 256 <= kwargs["max_tokens"] <= 4096
            if handoff != "empty":
                yield chat.TextDelta("Wrote result.txt; validation remains unfinished.")
            if handoff == "unsolicited-tool":
                yield chat.ToolCallDelta(chat.ToolCallRequest(
                    id="forbidden", name="Write",
                    arguments_json='{"file_path":"forbidden.txt","content":"must not execute"}',
                ))
            yield _receipt(finish="length" if handoff == "truncated" else "stop")

    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(driver, "_flush_user_queue", lambda: pytest.fail("Automatic continuation after pause"))
    driver.connect("error", lambda _d, message: errors.append(message))
    driver.connect("result", lambda _d, result: results.append(result))
    driver.connect("turn-appended", lambda _d, turn: turns.append(turn))
    driver._run_turn_with_tools("bounded")

    assert len(requests) == 3
    assert "2 of 2 tool rounds remain" in requests[0][0][-1]["content"]
    assert "1 of 2 tool rounds remain" in requests[1][0][-1]["content"]
    assert "Tool calls are disabled" in requests[2][0][-1]["content"]
    assert all(messages[0] == original_system for messages, _ in requests)
    assert driver._turn_tools == original_tools  # approval identities stay stable
    assert (tmp_path / "result.txt").read_text() == "2"
    assert not (tmp_path / "forbidden.txt").exists()
    assert len(turns[-1].tool_results) == 2
    assert "Send a follow-up message in this Work" in turns[-1].text
    assert not errors and not driver._token_budget_exhausted
    assert results[-1]["subtype"] == "interrupted"
    assert results[-1]["stop_reason"] == "tool_round_limit"
    terminal = store.get_execution_attempt(attempt.attempt_id)
    assert terminal.status == "aborted"
    assert terminal.terminal_reason == "openrouter.tool_round_limit"
    assert terminal.queue_disposition == "restored"
    assert terminal.cost_micro_usd == 3000
    assert terminal.usage["requests"] == 3
    assert store.get_work(attempt.work_id).status == "active"
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) == 3000
    persisted = HistoryLog(driver.session_id).load()
    assert persisted == driver._history
    assert "Helios turn budget:" not in json.dumps(persisted)
    requested = [c["id"] for m in persisted for c in m.get("tool_calls", [])]
    assert requested == [m["tool_call_id"] for m in persisted if m["role"] == "tool"]

    # A newly admitted turn can reuse this Work and its persisted conversation;
    # the handoff did not renew or clear the Work's dollar accounting.
    renewed = store.start_execution_attempt(
        attempt.work_id, attempt.participant_id, provider="openrouter",
        expected_participant_generation=attempt.participant_generation,
    )
    store.record_execution_dispatch(
        renewed.attempt_id, wire_prompt_text="continue", provider_request_key=renewed.attempt_id,
    )
    driver._execution_attempt_id = renewed.attempt_id
    driver._history = persisted + [{"role": "user", "content": "continue"}]
    driver._busy = True
    flushed = []
    monkeypatch.setattr(driver, "_flush_user_queue", lambda: flushed.append(True))

    def finish(messages, **kwargs):
        assert "2 of 2 tool rounds remain" in messages[-1]["content"]
        assert kwargs["allow_tool_calls"]
        kwargs["on_response_accepted"]("response-resumed")
        yield chat.TextDelta("Verified result.txt contains 2.")
        yield _receipt()

    monkeypatch.setattr(od.or_chat, "stream_chat", finish)
    driver._run_turn_with_tools("continue")
    assert store.get_execution_attempt(renewed.attempt_id).status == "completed"
    assert store.openrouter_spend_micro_usd(renewed.attempt_id) == 4000
    assert flushed == [True]


@pytest.mark.parametrize("stop", ["cancel", "spend"])
def test_last_tool_round_cannot_force_a_handoff_request(bound_driver, monkeypatch, stop):
    driver, store, attempt = bound_driver
    monkeypatch.setattr(od, "_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(od, "_MAX_PRODUCTIVE_TOOL_ROUNDS", 1)
    requests = []

    def stream(messages, **kwargs):
        requests.append(messages)
        assert len(requests) == 1
        kwargs["on_response_accepted"]("response-tool")
        yield from _tool_response(1)

    def execute(_call):
        if stop == "cancel":
            driver._cancel_token = od.CancellationToken()
            driver._cancel_token.cancel()
        else:
            store.reserve_openrouter_request(attempt.attempt_id, "uncertain", 4_999_000)
        return "finished local tool", False

    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(driver, "_execute_tool_call", execute)
    driver._run_turn_with_tools("bounded")
    terminal = store.get_execution_attempt(attempt.attempt_id)
    assert len(requests) == 1
    assert terminal.status == ("aborted" if stop == "cancel" else "budgetLimited")


@pytest.mark.parametrize("failure", ["missing-cost", "unreported", "transport"])
def test_handoff_failure_keeps_spend_and_existing_recovery_rules(bound_driver, monkeypatch, failure):
    driver, store, attempt = bound_driver
    monkeypatch.setattr(od, "_MAX_TOOL_ROUNDS", 1)
    monkeypatch.setattr(od, "_MAX_PRODUCTIVE_TOOL_ROUNDS", 1)
    requests = []

    def stream(messages, **kwargs):
        requests.append(messages)
        kwargs["on_response_accepted"](f"response-{len(requests)}")
        if len(requests) == 1:
            yield from _tool_response(1)
        elif failure == "transport":
            raise chat.ChatError(chat.ChatErrorKind.CONNECTION, response_started=True)
        else:
            yield _receipt(cost=None if failure == "missing-cost" else 0.001,
                           reported=failure != "unreported")

    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    driver._run_turn_with_tools("bounded")
    assert len(requests) == 2
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) > 1000
    assert [row[0] for row in store._conn.execute(
        "SELECT state FROM openrouter_request_budgets ORDER BY created_at",
    )] == ["reported", "reserved"]
    terminal = store.get_execution_attempt(attempt.attempt_id)
    assert terminal.status == ("running" if failure == "transport" else "budgetLimited")


def test_round_guidance_is_priced_before_http_and_not_persisted(bound_driver, monkeypatch):
    driver, store, attempt = bound_driver
    context = od._round_context(1)
    expected_prompt = driver._prompt_cost() + (
        od.math.ceil(od.estimate_tokens(context) * od._PROMPT_ESTIMATE_MARGIN)
        * driver._effective_prices()[0]
    )

    def stream(messages, **kwargs):
        assert messages[-1] == context[0]
        reserved = store.openrouter_spend_micro_usd(attempt.attempt_id)
        assert reserved == od.micro_usd(
            expected_prompt + kwargs["max_tokens"] * driver._effective_prices()[1],
        )
        yield _receipt()

    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    before = list(driver._history)
    driver._stream_round("test-key", StreamingAssistant(), round_context=context, handoff=True)
    assert driver._history == before


@pytest.mark.parametrize("view", ["visible", "background", "closing"])
@pytest.mark.parametrize("pause", ["tool_round_limit", "tool_stalled", "output_limit"])
def test_round_pause_returns_queued_drafts_before_the_next_send(bound_driver, monkeypatch, view, pause):
    from helios.main_window import MainWindow

    driver, store, attempt = bound_driver
    ceiling = 25 if pause == "tool_stalled" else 1
    monkeypatch.setattr(od, "_MAX_TOOL_ROUNDS", ceiling)
    monkeypatch.setattr(od, "_MAX_PRODUCTIVE_TOOL_ROUNDS", ceiling)
    monkeypatch.setattr(MainWindow, "_refresh_capabilities", lambda _window: None)
    composer = SimpleNamespace(text="existing draft")
    composer.current_text = lambda: composer.text
    composer.set_text = lambda text: setattr(composer, "text", text)
    composer.set_busy = lambda _busy: None
    composer.grab_input_focus = lambda: None
    removed, notified = [], []
    window = SimpleNamespace(
        _destroyed=view == "closing", _drv_is_current=lambda _drv: view == "visible",
        _composer=composer, _transcript=SimpleNamespace(remove_queued=removed.append),
        _push_background_activity=lambda *args: None, _notify_session_finished=notified.append,
        _refresh_openrouter_credits=lambda **kwargs: None,
        _chat_toolbar=SimpleNamespace(set_busy=lambda _busy: None),
        _remember_activity=lambda *args: None, _activity=SimpleNamespace(clear=lambda: None),
        _sessions=SimpleNamespace(reload=lambda **kwargs: None),
    )
    first = driver.queue_user_text("first queued draft")
    second = driver.queue_user_text("second queued draft")
    requests = []

    def stream(messages, **kwargs):
        requests.append(messages)
        kwargs["on_response_accepted"](f"response-{len(requests)}")
        if pause == "output_limit":
            yield chat.TextDelta("Response was cut short")
            yield _receipt(finish="length")
        elif len(requests) <= (3 if pause == "tool_stalled" else 1):
            yield from _tool_response(1)
        else:
            yield chat.TextDelta("File updated; verification remains.")
            yield _receipt()

    def publish(_driver, result):
        assert store.get_execution_attempt(attempt.attempt_id).status == "aborted"
        assert result["stop_reason"] == pause
        MainWindow._on_turn_result(window, driver, result)

    driver.connect("result", publish)
    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(driver, "_flush_user_queue", lambda: pytest.fail("Auto-send after round pause"))
    driver._run_turn_with_tools("bounded")
    assert len(requests) == {"tool_round_limit": 2, "tool_stalled": 4, "output_limit": 1}[pause]
    assert not driver.queued_messages()
    if view == "visible":
        assert removed == [first, second]
        assert composer.text == "first queued draft\n\nsecond queued draft\n\nexisting draft"
    else:
        assert window._native_unsent_drafts[MainWindow._native_draft_key(driver)] == [
            "first queued draft", "second queued draft",
        ]
        assert composer.text == "existing draft"
        assert notified == ([driver] if view == "background" else [])


def test_productive_tool_rounds_extend_with_same_work_allowance(bound_driver, monkeypatch, tmp_path):
    driver, store, attempt = bound_driver
    monkeypatch.setattr(od, "_MAX_TOOL_ROUNDS", 2)
    monkeypatch.setattr(od, "_MAX_PRODUCTIVE_TOOL_ROUNDS", 4)
    requests = []

    def stream(messages, **kwargs):
        requests.append((messages, kwargs))
        kwargs["on_response_accepted"](f"response-{len(requests)}")
        assert kwargs["allow_tool_calls"]
        if len(requests) <= 3:
            yield from _tool_response(len(requests))
        else:
            yield chat.TextDelta("Finished the requested changes.")
            yield _receipt()

    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    driver._run_turn_with_tools("bounded")
    assert len(requests) == 4
    assert "2 of 4 tool rounds remain" in requests[2][0][-1]["content"]
    assert (tmp_path / "result.txt").read_text() == "3"
    terminal = store.get_execution_attempt(attempt.attempt_id)
    assert terminal.status == "completed"
    assert terminal.cost_micro_usd == store.openrouter_spend_micro_usd(attempt.attempt_id) == 4000


def test_identical_rounds_pause_early_with_truthful_handoff(bound_driver, monkeypatch):
    driver, store, attempt = bound_driver
    requests, results = [], []

    def stream(messages, **kwargs):
        requests.append(kwargs)
        kwargs["on_response_accepted"](f"response-{len(requests)}")
        if len(requests) <= 3:
            yield from _tool_response(1)
        else:
            assert not kwargs["allow_tool_calls"]
            yield chat.TextDelta("Repeated the same write; the task remains incomplete.")
            yield _receipt()

    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(driver, "_flush_user_queue", lambda: pytest.fail("Unexpected continuation"))
    driver.connect("result", lambda _d, result: results.append(result))
    driver._run_turn_with_tools("bounded")
    assert len(requests) == 4
    terminal = store.get_execution_attempt(attempt.attempt_id)
    assert terminal.status == "aborted" and terminal.queue_disposition == "restored"
    assert terminal.terminal_reason == "openrouter.tool_stalled"
    assert results[-1]["stop_reason"] == "tool_stalled"
    assert store.get_work(attempt.work_id).status == "active"


@pytest.mark.parametrize("with_tool", [False, True])
def test_output_limit_is_interrupted_with_paired_history_and_retained_spend(
    bound_driver, monkeypatch, tmp_path, with_tool,
):
    driver, store, attempt = bound_driver
    results = []

    def stream(messages, **kwargs):
        kwargs["on_response_accepted"]("truncated")
        yield chat.TextDelta("An unfinished response")
        if with_tool:
            yield chat.ToolCallDelta(chat.ToolCallRequest(
                id="partial", name="Write", arguments_json='{"file_path":"partial.txt","content":"x"}',
            ))
        yield _receipt(finish="length")

    monkeypatch.setattr(od.or_chat, "stream_chat", stream)
    monkeypatch.setattr(driver, "_flush_user_queue", lambda: pytest.fail("Unexpected continuation"))
    driver.connect("result", lambda _d, result: results.append(result))
    driver._run_turn_with_tools("bounded")
    terminal = store.get_execution_attempt(attempt.attempt_id)
    assert terminal.status == "aborted" and terminal.queue_disposition == "restored"
    assert terminal.terminal_reason == "openrouter.output_limit"
    assert results[-1]["subtype"] == "interrupted"
    assert results[-1]["stop_reason"] == "output_limit"
    assert not (tmp_path / "partial.txt").exists()
    assert not any(m.get("tool_calls") for m in HistoryLog(driver.session_id).load())
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) == 1000
