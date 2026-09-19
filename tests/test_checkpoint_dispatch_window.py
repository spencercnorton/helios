"""Deterministic coverage for the pre-turn checkpoint dispatch fence.

No provider process is started here.  The window methods run against small
stand-ins so the tests can control the exact worker/deadline/Stop ordering.
"""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios import main_window as main_window_module  # noqa: E402
from helios.backend.work_store import WorkEvent  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


def _work_event(text: str = "FIRST") -> WorkEvent:
    return WorkEvent(
        event_id="event-1",
        work_id="work-1",
        seq=1,
        event_type="user.message",
        payload={"text": text},
        provider="openai",
        emitting_participant_id="participant-1",
        control_plane=False,
        created_at="2026-08-04T00:00:00Z",
    )


class _Composer:
    def __init__(self) -> None:
        self.text = ""
        self.busy = False
        self.accepted_acks = 0

    def current_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text

    def set_busy(self, busy: bool) -> None:
        self.busy = busy

    def grab_input_focus(self) -> None:
        pass

    def acknowledge_accepted(self) -> None:
        self.accepted_acks += 1


class _Driver:
    def __init__(
        self,
        provider: str,
        *,
        session_id: str,
        identity_confirmed: bool,
        accept_send: bool = True,
    ) -> None:
        self.provider = provider
        self.session_id = session_id
        self._cwd = "/repo"
        self._helios_work_id = "work-1"
        self._helios_identity_confirmed = identity_confirmed
        self.is_busy = False
        self.is_accepting_input = True
        self.native_starting = False
        self._goal_pending_text = None
        self._helios_native_delivery_state = ""
        self._pending_queue_delivery = None
        self._uncertain_queue_delivery = None
        self._queue_dispatch_in_progress = False
        self.accept_send = accept_send
        self.sent: list[str] = []
        self.stops = 0
        self._queue: list[tuple[int, str]] = []

    def send_user_text(self, text: str) -> None:
        self.sent.append(text)
        if self.accept_send:
            self.is_busy = True

    def queue_user_text(self, text: str) -> int:
        qid = len(self._queue) + 1
        self._queue.append((qid, text))
        return qid

    def queued_messages(self) -> list[tuple[int, str]]:
        return list(self._queue)

    def take_queued(self) -> list[str]:
        uncertain = self._uncertain_queue_delivery
        texts = [row[1] for row in self._queue if row != uncertain]
        self._queue[:] = (
            [uncertain]
            if uncertain is not None and uncertain in self._queue
            else []
        )
        return texts

    def _quarantine_uncertain_queue_delivery(self):
        delivery = self._uncertain_queue_delivery
        self._uncertain_queue_delivery = None
        if delivery is not None:
            self._queue[:] = [row for row in self._queue if row != delivery]
        return delivery

    def remove_queued(self, qid: int) -> bool:
        owned = {
            marker
            for marker in (
                self._pending_queue_delivery,
                self._uncertain_queue_delivery,
            )
            if marker is not None
        }
        if any(row[0] == qid and row in owned for row in self._queue):
            return False
        self._queue[:] = [entry for entry in self._queue if entry[0] != qid]
        return True

    def _quarantine_pending_queue_delivery(self):
        delivery = self._pending_queue_delivery
        self._pending_queue_delivery = None
        if delivery is not None:
            self._queue[:] = [row for row in self._queue if row != delivery]
        return delivery

    def take_queued_successors_after_pending_delivery(self) -> list[str] | None:
        if self._pending_queue_delivery is None or self._queue[:1] != [
            self._pending_queue_delivery
        ]:
            return None
        successors = [text for _qid, text in self._queue[1:]]
        del self._queue[1:]
        return successors

    def stop(self) -> None:
        self.stops += 1


class _NativeDriver(_Driver):
    pass


def _driver_for(provider: str, *, accept_send: bool = True) -> _Driver:
    if provider == "openai":
        return _NativeDriver(
            provider,
            session_id="",
            identity_confirmed=False,
            accept_send=accept_send,
        )
    if provider == "anthropic":
        return _Driver(
            provider,
            session_id="",
            identity_confirmed=False,
            accept_send=accept_send,
        )
    return _Driver(
        provider,
        session_id="openrouter-1",
        identity_confirmed=True,
        accept_send=accept_send,
    )


def _window(
    monkeypatch,
    provider: str,
    *,
    accept_send: bool = True,
    established: bool = False,
):
    monkeypatch.setattr(
        main_window_module,
        "CodexAppServerDriver",
        _NativeDriver,
    )
    monkeypatch.setattr(
        MainWindow,
        "_execution_target_lock_reason",
        lambda _self: "",
    )
    monkeypatch.setattr(
        MainWindow,
        "_execution_guard_for_driver",
        lambda _self, _drv: "",
    )
    drv = _driver_for(provider, accept_send=accept_send)
    if established:
        drv.session_id = f"{provider}-existing"
        drv._helios_identity_confirmed = True
    composer = _Composer()
    captures: list[tuple[_Driver, int, str, object]] = []
    queued_rows: list[tuple[int, str]] = []
    removed_rows: list[int] = []
    visible_turns: list[str] = []
    stored: list[object] = []
    toasts: list[str] = []
    busy_clears: list[bool] = []

    window = types.SimpleNamespace(
        _destroyed=False,
        _driver=drv,
        _current_driver=drv,
        _restore_in_flight=False,
        _checkpoint_dispatch_generation=0,
        _pending_checkpoint_dispatches={},
        _provider_dispatch_in_progress=set(),
        _native_pending_user={},
        _identity_pending_user={},
        _native_unsent_drafts={},
        _native_recovery_anchors={},
        _next_chat=types.SimpleNamespace(
            work_id="work-1",
            project=types.SimpleNamespace(cwd="/repo"),
        ),
        _work_coordinator=None,
        _composer=composer,
        _chat_toolbar=types.SimpleNamespace(
            set_busy=lambda busy: setattr(composer, "busy", busy)
        ),
        _activity=types.SimpleNamespace(
            set_activity=lambda _state: None,
            clear=lambda: None,
        ),
        _transcript=types.SimpleNamespace(
            append_queued=lambda qid, text, _remove: queued_rows.append((qid, text)),
            remove_queued=removed_rows.append,
            clear_queued=lambda: removed_rows.append(-1),
            append_error=lambda _message: None,
        ),
        _execution_change_pending_for=lambda _drv: False,
        _driver_matches_selected_provider=lambda candidate: candidate is drv,
        _driver_matches_target_binding=lambda candidate: candidate is drv,
        _ensure_driver=lambda: True,
        _selected_provider=lambda: provider,
        _native_draft_key=MainWindow._native_draft_key,
        _drv_is_current=lambda candidate: candidate is window._current_driver,
        _make_queued_remover=lambda _drv: lambda _qid: None,
        _stop_following=lambda: None,
        _append_visible_user_turn=visible_turns.append,
        _store_checkpoint=lambda _drv, _sid, point: stored.append(point),
        _toast=lambda message, timeout=None: toasts.append(message),
    )

    def clear_busy() -> None:
        composer.busy = False
        busy_clears.append(True)

    def capture(candidate, text: str, dispatch) -> None:
        generation = MainWindow._begin_checkpoint_dispatch(window, candidate, text)
        captures.append((candidate, generation, text, dispatch))

    window._clear_busy_ui = clear_busy
    window._capture_checkpoint = capture
    for name in (
        "_drain_queue_to_composer",
        "_return_identity_pending_user",
        "_return_native_pending_user",
        "_return_native_locally_buffered_user",
    ):
        setattr(window, name, types.MethodType(getattr(MainWindow, name), window))
    return types.SimpleNamespace(
        window=window,
        driver=drv,
        composer=composer,
        captures=captures,
        queued_rows=queued_rows,
        removed_rows=removed_rows,
        visible_turns=visible_turns,
        stored=stored,
        toasts=toasts,
        busy_clears=busy_clears,
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openrouter"])
def test_second_submit_queues_while_checkpoint_owns_first(monkeypatch, provider):
    h = _window(monkeypatch, provider)

    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")

    assert len(h.captures) == 1
    assert h.driver.sent == []
    assert h.driver.queued_messages() == [(1, "SECOND")]
    assert h.queued_rows == [(1, "SECOND")]


def test_idle_callback_gap_cannot_overtake_an_older_queued_prompt(monkeypatch):
    h = _window(monkeypatch, "openrouter", established=True)
    h.driver.queue_user_text("OLDER")
    h.driver.is_busy = False

    MainWindow._on_composer_send(h.window, None, "NEWER")

    assert h.driver.sent == []
    assert h.captures == []
    assert h.driver.queued_messages() == [(1, "OLDER"), (2, "NEWER")]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openrouter"])
def test_stop_wins_and_late_checkpoint_callbacks_are_inert(monkeypatch, provider):
    h = _window(monkeypatch, provider)
    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")
    h.composer.text = "THIRD"
    drv, generation, _text, dispatch = h.captures[0]

    MainWindow._on_composer_stop(h.window)
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )
    MainWindow._checkpoint_deadline(h.window, drv, generation, dispatch)

    assert h.composer.text == "FIRST\n\nSECOND\n\nTHIRD"
    assert h.driver.sent == []
    assert h.driver.stops == 1
    assert h.stored == []
    assert not [toast for toast in h.toasts if "without a checkpoint" in toast]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openrouter"])
def test_checkpoint_release_is_exactly_once_across_provider_boundaries(
    monkeypatch,
    provider,
):
    h = _window(monkeypatch, provider)
    MainWindow._on_composer_send(h.window, None, "FIRST")
    drv, generation, _text, dispatch = h.captures[0]
    point = object()

    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        point,
        dispatch,
    )
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )
    MainWindow._checkpoint_deadline(h.window, drv, generation, dispatch)

    assert h.driver.sent == ["FIRST"]
    assert h.stored == [point]
    if provider == "anthropic":
        assert h.window._identity_pending_user[id(drv)] == (drv, "FIRST")
    elif provider == "openai":
        assert h.window._native_pending_user[id(drv)] == (drv, "FIRST")
    else:
        assert h.visible_turns == ["FIRST"]


def test_deadline_wins_then_late_capture_cannot_store_or_dispatch(monkeypatch):
    h = _window(monkeypatch, "openrouter")
    MainWindow._on_composer_send(h.window, None, "FIRST")
    drv, generation, _text, dispatch = h.captures[0]

    MainWindow._checkpoint_deadline(h.window, drv, generation, dispatch)
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert drv.sent == ["FIRST"]
    assert h.stored == []
    assert len([toast for toast in h.toasts if "without a checkpoint" in toast]) == 1


def test_stale_generation_cannot_release_a_newer_dispatch(monkeypatch):
    h = _window(monkeypatch, "openrouter")
    old_calls: list[str] = []
    new_calls: list[str] = []
    old_generation = MainWindow._begin_checkpoint_dispatch(
        h.window,
        h.driver,
        "OLD",
    )
    MainWindow._take_checkpoint_dispatch(h.window, h.driver, old_generation)
    new_generation = MainWindow._begin_checkpoint_dispatch(
        h.window,
        h.driver,
        "NEW",
    )

    MainWindow._checkpoint_captured(
        h.window,
        h.driver,
        old_generation,
        "session",
        object(),
        lambda: old_calls.append("sent"),
    )
    MainWindow._checkpoint_deadline(
        h.window,
        h.driver,
        old_generation,
        lambda: old_calls.append("sent"),
    )
    MainWindow._checkpoint_captured(
        h.window,
        h.driver,
        new_generation,
        "session",
        object(),
        lambda: new_calls.append("sent"),
    )

    assert old_calls == []
    assert new_calls == ["sent"]
    assert len(h.stored) == 1


def test_release_after_navigation_sends_only_the_captured_background_driver(
    monkeypatch,
):
    h = _window(monkeypatch, "openrouter")
    MainWindow._on_composer_send(h.window, None, "FIRST")
    drv, generation, _text, dispatch = h.captures[0]
    h.window._current_driver = object()

    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert drv.sent == ["FIRST"]
    assert h.visible_turns == []
    assert h.composer.text == ""


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_established_provider_admission_denial_restores_all_text(
    monkeypatch,
    provider,
):
    h = _window(
        monkeypatch,
        provider,
        accept_send=False,
        established=True,
    )
    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")
    h.composer.text = "THIRD"
    drv, generation, _text, dispatch = h.captures[0]

    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert drv.sent == ["FIRST"]
    assert h.composer.text == "FIRST\n\nSECOND\n\nTHIRD"
    assert h.visible_turns == []
    assert h.busy_clears == [True]


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_explicit_acceptance_beats_fast_worker_busy_race(monkeypatch, provider):
    h = _window(monkeypatch, provider, established=True)
    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")
    drv, generation, _text, dispatch = h.captures[0]

    def accept_then_finish(text: str):
        drv.sent.append(text)
        drv.is_busy = False
        return main_window_module.MessageDelivery("accepted")

    drv.send_user_text = accept_then_finish
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert drv.sent == ["FIRST"]
    assert h.visible_turns == ["FIRST"]
    assert h.window._identity_pending_user == {}
    assert drv.queued_messages() == [(1, "SECOND")]
    assert h.composer.text == ""


@pytest.mark.parametrize(
    "state",
    ["accepted", "pending", "rejected", "uncertain"],
)
def test_nonnative_local_handoff_never_claims_provider_acceptance(
    monkeypatch,
    state,
):
    h = _window(monkeypatch, "anthropic", established=True)
    recorded = []

    def record(_drv, text):
        recorded.append(text)
        return _work_event(text)

    h.window._work_coordinator = types.SimpleNamespace(
        record_user_message=record
    )
    MainWindow._on_composer_send(h.window, None, "FIRST")
    drv, generation, _text, dispatch = h.captures[0]

    def deliver(text: str):
        drv.sent.append(text)
        drv.is_busy = state in {"accepted", "pending", "uncertain"}
        return main_window_module.MessageDelivery(state)

    drv.send_user_text = deliver
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert h.composer.accepted_acks == 0
    assert recorded == (["FIRST"] if state == "accepted" else [])


def test_native_pending_send_acknowledges_only_after_causal_acceptance(monkeypatch):
    h = _window(monkeypatch, "openai")
    recorded = []
    def record(_drv, text):
        recorded.append(text)
        return _work_event(text)

    h.window._work_coordinator = types.SimpleNamespace(record_user_message=record)
    MainWindow._on_composer_send(h.window, None, "FIRST")
    drv, generation, _text, dispatch = h.captures[0]

    def pending(text: str):
        drv.sent.append(text)
        drv.is_busy = True
        return main_window_module.MessageDelivery("pending")

    drv.send_user_text = pending
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert h.composer.accepted_acks == 0
    assert recorded == []

    MainWindow._on_native_prompt_accepted(h.window, drv, "FIRST")

    assert recorded == ["FIRST"]
    assert h.composer.accepted_acks == 1


def test_direct_user_record_failure_has_no_flourish(monkeypatch):
    h = _window(monkeypatch, "anthropic", established=True)

    def fail_record(_drv, _text):
        raise RuntimeError("store unavailable")

    h.window._work_coordinator = types.SimpleNamespace(
        record_user_message=fail_record
    )
    h.window._identity_pending_user[id(h.driver)] = (h.driver, "FIRST")

    assert MainWindow._accept_identity_pending_user(h.window, h.driver) is True
    assert h.composer.accepted_acks == 0


def test_direct_user_record_returning_none_has_no_flourish(monkeypatch):
    h = _window(monkeypatch, "anthropic", established=True)
    recorded = []
    h.window._work_coordinator = types.SimpleNamespace(
        record_user_message=lambda _drv, text: recorded.append(text)
    )
    h.window._identity_pending_user[id(h.driver)] = (h.driver, "FIRST")

    assert MainWindow._accept_identity_pending_user(h.window, h.driver) is True
    assert recorded == ["FIRST"]
    assert h.composer.accepted_acks == 0


def test_native_acceptance_returning_none_has_no_flourish(monkeypatch):
    h = _window(monkeypatch, "openai")
    recorded = []
    h.window._native_pending_user[id(h.driver)] = (h.driver, "FIRST")
    h.window._work_coordinator = types.SimpleNamespace(
        record_user_message=lambda _drv, text: recorded.append(text)
    )

    MainWindow._on_native_prompt_accepted(h.window, h.driver, "FIRST")

    assert recorded == ["FIRST"]
    assert h.composer.accepted_acks == 0


def test_queued_user_record_returning_none_has_no_flourish(monkeypatch):
    h = _window(monkeypatch, "anthropic", established=True)
    recorded = []
    h.window._work_coordinator = types.SimpleNamespace(
        record_user_message=lambda _drv, text: recorded.append(text)
    )
    h.window._transcript.append_turn = lambda _turn: None
    h.window._plan = types.SimpleNamespace(append_turn=lambda _turn: None)

    MainWindow._on_queued_user_sent(h.window, h.driver, 7, "QUEUED")

    assert recorded == ["QUEUED"]
    assert h.composer.accepted_acks == 0


def test_uncertain_user_record_returning_none_has_no_flourish(monkeypatch):
    h = _window(monkeypatch, "anthropic", established=True)
    recorded = []
    h.window._work_coordinator = types.SimpleNamespace(
        record_user_message=lambda _drv, text: recorded.append(text)
    )
    h.window._uncertain_pending_user = {
        id(h.driver): (h.driver, "FIRST")
    }

    assert MainWindow._accept_uncertain_pending_user(h.window, h.driver) is True
    assert MainWindow._accept_uncertain_pending_user(h.window, h.driver) is False
    assert recorded == ["FIRST"]
    assert h.composer.accepted_acks == 0


@pytest.mark.parametrize(("destroyed", "background"), [(True, False), (False, True)])
def test_native_durable_acceptance_does_not_animate_closed_or_background_work(
    monkeypatch,
    destroyed,
    background,
):
    h = _window(monkeypatch, "openai")
    h.window._native_pending_user[id(h.driver)] = (h.driver, "FIRST")
    h.window._work_coordinator = types.SimpleNamespace(
        record_user_message=lambda _drv, text: _work_event(text)
    )
    h.window._destroyed = destroyed
    if background:
        h.window._current_driver = object()

    MainWindow._on_native_prompt_accepted(h.window, h.driver, "FIRST")

    assert h.composer.accepted_acks == 0


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
@pytest.mark.parametrize("visible", [True, False])
def test_explicit_nonnative_rejection_restores_fifo(
    monkeypatch,
    provider,
    visible,
):
    h = _window(monkeypatch, provider, established=True)
    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")
    h.composer.text = "THIRD"
    drv, generation, _text, dispatch = h.captures[0]
    if not visible:
        h.window._current_driver = object()

    def reject(text: str):
        drv.sent.append(text)
        drv.is_busy = False
        return main_window_module.MessageDelivery("rejected")

    drv.send_user_text = reject
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert drv.sent == ["FIRST"]
    assert h.visible_turns == []
    assert h.window._identity_pending_user == {}
    if visible:
        assert h.composer.text == "FIRST\n\nSECOND\n\nTHIRD"
        assert h.window._native_unsent_drafts == {}
    else:
        assert h.composer.text == "THIRD"
        assert h.window._native_unsent_drafts == {
            f"{provider}:work-1": ["FIRST", "SECOND"]
        }


def test_explicit_claude_uncertainty_quarantines_first_until_provider_proof(
    monkeypatch,
):
    h = _window(monkeypatch, "anthropic", established=True)
    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")
    h.composer.text = "THIRD"
    drv, generation, _text, dispatch = h.captures[0]

    def uncertain(text: str):
        drv.sent.append(text)
        drv.is_busy = True
        MainWindow._on_driver_error(h.window, drv, "stdin delivery uncertain")
        return main_window_module.MessageDelivery("uncertain")

    drv.send_user_text = uncertain
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert h.composer.text == "SECOND\n\nTHIRD"
    assert h.composer.busy is True
    assert h.busy_clears == []
    assert h.window._identity_pending_user == {}
    assert h.window._uncertain_pending_user == {id(drv): (drv, "FIRST")}

    assert MainWindow._accept_uncertain_pending_user(h.window, drv) is True
    assert MainWindow._accept_uncertain_pending_user(h.window, drv) is False
    assert h.window._uncertain_pending_user == {}
    assert h.visible_turns == ["FIRST"]


@pytest.mark.parametrize("state", ["rejected", "uncertain"])
def test_explicit_native_outcome_restores_only_definite_rejection(
    monkeypatch,
    state,
):
    h = _window(monkeypatch, "openai")
    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")
    h.composer.text = "THIRD"
    drv, generation, _text, dispatch = h.captures[0]

    def finish(text: str):
        drv.sent.append(text)
        drv.is_busy = False
        drv._helios_native_delivery_state = (
            "rejected" if state == "rejected" else "unknown"
        )
        return main_window_module.MessageDelivery(state)

    drv.send_user_text = finish
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    expected = (
        "FIRST\n\nSECOND\n\nTHIRD"
        if state == "rejected"
        else "SECOND\n\nTHIRD"
    )
    assert h.composer.text == expected
    assert h.window._native_pending_user == {}
    assert h.busy_clears == [True]


def test_unknown_queued_native_head_is_quarantined_before_successors(monkeypatch):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    first = drv.queue_user_text("FIRST")
    drv.queue_user_text("SECOND")
    drv._pending_queue_delivery = (first, "FIRST")
    drv._helios_native_delivery_state = "unknown"
    drv.is_busy = False
    h.composer.text = "THIRD"

    MainWindow._on_driver_error(h.window, drv, "transport exited")

    assert h.composer.text == "SECOND\n\nTHIRD"
    assert drv.queued_messages() == []
    assert drv._pending_queue_delivery is None
    assert first in h.removed_rows
    assert "FIRST" not in h.composer.text


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_synchronous_background_admission_error_keeps_first_before_queue(
    monkeypatch,
    provider,
):
    h = _window(monkeypatch, provider, established=True)
    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")
    h.composer.text = "THIRD"
    h.window._current_driver = object()
    drv, generation, _text, dispatch = h.captures[0]

    def reject_with_synchronous_error(text: str) -> None:
        drv.sent.append(text)
        MainWindow._on_driver_error(h.window, drv, "admission failed")

    drv.send_user_text = reject_with_synchronous_error
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )

    assert drv.sent == ["FIRST"]
    assert h.composer.text == "THIRD"
    assert h.window._native_unsent_drafts == {
        f"{provider}:work-1": ["FIRST", "SECOND"]
    }


@pytest.mark.parametrize("visible", [True, False])
def test_async_driver_error_cancels_pending_checkpoint_and_late_callbacks(
    monkeypatch,
    visible,
):
    h = _window(monkeypatch, "anthropic")
    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")
    h.composer.text = "THIRD"
    if not visible:
        h.window._current_driver = object()
    drv, generation, _text, dispatch = h.captures[0]

    MainWindow._on_driver_error(h.window, drv, "reader failed")
    MainWindow._checkpoint_captured(
        h.window,
        drv,
        generation,
        "session",
        object(),
        dispatch,
    )
    MainWindow._checkpoint_deadline(h.window, drv, generation, dispatch)

    assert MainWindow._checkpoint_dispatch_for(h.window, drv) is None
    assert drv.sent == []
    assert h.stored == []
    if visible:
        assert h.composer.text == "FIRST\n\nSECOND\n\nTHIRD"
        assert h.composer.busy is False
    else:
        assert h.composer.text == "THIRD"
        assert h.window._native_unsent_drafts == {
            "anthropic:work-1": ["FIRST", "SECOND"]
        }


def test_escape_consumes_pending_checkpoint_before_provider_dispatch(monkeypatch):
    from gi.repository import Gdk

    h = _window(monkeypatch, "openrouter")
    MainWindow._on_composer_send(h.window, None, "FIRST")
    h.window._on_composer_stop = types.MethodType(
        MainWindow._on_composer_stop,
        h.window,
    )

    handled = MainWindow._on_window_key(
        h.window,
        None,
        Gdk.KEY_Escape,
        0,
        0,
    )

    assert handled is True
    assert MainWindow._checkpoint_dispatch_for(h.window, h.driver) is None
    assert h.driver.sent == []
    assert h.driver.stops == 1


def test_rewind_refuses_while_checkpoint_dispatch_is_pending(monkeypatch):
    h = _window(monkeypatch, "openrouter")
    MainWindow._on_composer_send(h.window, None, "FIRST")
    h.window._agent_is_writing = lambda _drv: False

    MainWindow._on_restore_requested(h.window, None, object(), ["a.py"])

    assert MainWindow._checkpoint_dispatch_for(h.window, h.driver) is not None
    assert "writing files" in h.toasts[-1]


def test_emergency_stop_consumes_pending_checkpoint(monkeypatch):
    h = _window(monkeypatch, "openrouter")
    stopped: list[tuple[object, bool]] = []
    h.window._driver_manager = types.SimpleNamespace(
        drivers_for_shutdown=lambda: [h.driver],
        stop_driver=lambda drv, interrupt=True: stopped.append((drv, interrupt)),
    )
    MainWindow._on_composer_send(h.window, None, "FIRST")

    MainWindow._on_emergency_stop_all(h.window)

    assert MainWindow._checkpoint_dispatch_for(h.window, h.driver) is None
    assert h.composer.text == "FIRST"
    assert stopped == [(h.driver, True)]


def test_teardown_consumes_pending_checkpoint(monkeypatch):
    h = _window(monkeypatch, "openrouter")
    torn_down: list[tuple[object, bool]] = []
    h.window._resolve_questions_for_driver = lambda _drv: None
    h.window._sync_execution_control = lambda: None
    h.window._driver_manager = types.SimpleNamespace(
        teardown=lambda drv, interrupt=False: torn_down.append((drv, interrupt))
    )
    MainWindow._on_composer_send(h.window, None, "FIRST")

    MainWindow._teardown_driver(h.window, h.driver)

    assert MainWindow._checkpoint_dispatch_for(h.window, h.driver) is None
    assert h.composer.text == "FIRST"
    assert torn_down == [(h.driver, False)]


def test_visible_driver_exit_clears_observed_agent_projection(monkeypatch):
    h = _window(monkeypatch, "openai", established=True)
    resets = []
    h.window._forget_driver = lambda _drv: None
    h.window._sync_execution_control = lambda: None
    monkeypatch.setattr(
        MainWindow,
        "_reset_observed_agent_activity",
        lambda owner: resets.append(owner),
    )

    MainWindow._on_driver_exited(h.window, h.driver, 0)

    assert resets == [h.window]


def test_background_driver_exit_preserves_visible_agent_projection(monkeypatch):
    h = _window(monkeypatch, "openai", established=True)
    resets = []
    h.window._current_driver = object()
    h.window._forget_driver = lambda _drv: None
    monkeypatch.setattr(
        MainWindow,
        "_reset_observed_agent_activity",
        lambda owner: resets.append(owner),
    )

    MainWindow._on_driver_exited(h.window, h.driver, 0)

    assert resets == []


def test_visible_hard_teardown_clears_observed_agent_projection(monkeypatch):
    h = _window(monkeypatch, "openai", established=True)
    resets = []
    h.window._resolve_questions_for_driver = lambda _drv: None
    h.window._sync_execution_control = lambda: None
    h.window._driver_manager = types.SimpleNamespace(
        teardown=lambda _drv, interrupt=False: None
    )
    monkeypatch.setattr(
        MainWindow,
        "_reset_observed_agent_activity",
        lambda owner: resets.append(owner),
    )

    MainWindow._teardown_driver(h.window, h.driver)

    assert resets == [h.window]


@pytest.mark.parametrize("owner", ["checkpoint", "identity"])
def test_background_identity_rejection_recovers_first_before_queue(
    monkeypatch,
    owner,
):
    h = _window(monkeypatch, "anthropic")
    drv = h.driver
    if owner == "checkpoint":
        MainWindow._on_composer_send(h.window, None, "FIRST")
    else:
        h.window._identity_pending_user[id(drv)] = (drv, "FIRST")
    drv.queue_user_text("SECOND")
    h.composer.text = "THIRD"
    h.window._current_driver = object()
    torn_down: list[tuple[object, bool]] = []
    h.window._driver_provider = lambda candidate: candidate.provider
    h.window._resolve_questions_for_driver = lambda _drv: None
    h.window._teardown_driver = types.MethodType(
        MainWindow._teardown_driver,
        h.window,
    )
    h.window._driver_manager = types.SimpleNamespace(
        teardown=lambda candidate, interrupt=False: torn_down.append(
            (candidate, interrupt)
        )
    )

    MainWindow._reject_started_identity(h.window, drv, "mismatch")

    assert MainWindow._checkpoint_dispatch_for(h.window, drv) is None
    assert h.composer.text == "THIRD"
    assert h.window._native_unsent_drafts == {
        "anthropic:work-1": ["FIRST", "SECOND"]
    }
    assert torn_down == [(drv, False)]


@pytest.mark.parametrize(
    "case",
    ["claude-identity", "gpt-locally-buffered", "gpt-wire-in-flight"],
)
def test_background_sidebar_stop_distinguishes_unsent_from_wire_pending(
    monkeypatch,
    case,
):
    provider = "anthropic" if case == "claude-identity" else "openai"
    h = _window(monkeypatch, provider)
    drv = h.driver
    pending = (
        h.window._native_pending_user
        if provider == "openai"
        else h.window._identity_pending_user
    )
    pending[id(drv)] = (drv, "FIRST")
    if case == "gpt-locally-buffered":
        drv.native_starting = True
        drv._helios_native_delivery_state = "local"
    elif case == "gpt-wire-in-flight":
        drv._helios_native_delivery_state = "wire"
    drv.queue_user_text("SECOND")
    h.composer.text = "THIRD"
    h.window._current_driver = object()
    stopped: list[object] = []
    h.window._driver_manager = types.SimpleNamespace(
        driver_for_session=lambda _session_id: drv,
        stop_driver=stopped.append,
    )

    MainWindow._on_session_stop_requested(h.window, None, "session")

    assert h.composer.text == "THIRD"
    if case == "gpt-wire-in-flight":
        assert h.window._native_unsent_drafts == {
            "openai:work-1": ["SECOND"]
        }
        assert h.window._native_pending_user[id(drv)] == (drv, "FIRST")
    else:
        assert h.window._native_unsent_drafts == {
            f"{provider}:work-1": ["FIRST", "SECOND"]
        }
        assert pending == {}
    assert stopped == [drv]


@pytest.mark.parametrize("source", ["direct", "queued"])
@pytest.mark.parametrize("outcome", ["accepted", "rejected"])
@pytest.mark.parametrize("visible", [True, False])
def test_wire_resolution_uses_work_owned_successor_boundary(
    monkeypatch,
    source,
    outcome,
    visible,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    drv._helios_native_delivery_state = "wire"
    if source == "direct":
        h.window._native_pending_user[id(drv)] = (drv, "FIRST")
        drv.queue_user_text("SECOND")
    else:
        first_qid = drv.queue_user_text("FIRST")
        drv.queue_user_text("SECOND")
        drv._pending_queue_delivery = (first_qid, "FIRST")
    h.window._native_unsent_drafts["openai:work-1"] = ["OLD"]
    h.composer.text = "THIRD"
    h.window._current_driver = drv if visible else object()
    h.window._driver_manager = types.SimpleNamespace(
        driver_for_session=lambda _session_id: drv,
        stop_driver=lambda _drv: None,
    )

    MainWindow._on_session_stop_requested(h.window, None, "session")

    expected_staged = ["OLD", "SECOND"] + (["THIRD"] if visible else [])
    assert h.window._native_unsent_drafts == {"openai:work-1": expected_staged}
    assert h.composer.text == ("" if visible else "THIRD")
    assert MainWindow._native_recovery_anchor_for(h.window, drv) is not None
    if outcome == "accepted":
        drv._helios_native_delivery_state = "accepted"
        MainWindow._on_native_prompt_accepted(h.window, drv, "FIRST")
        if source == "queued":
            drv.remove_queued(1)
        if visible:
            assert h.composer.text == "OLD\n\nSECOND\n\nTHIRD"
            assert h.window._native_unsent_drafts == {}
        else:
            assert h.composer.text == "THIRD"
            assert h.window._native_unsent_drafts == {
                "openai:work-1": ["OLD", "SECOND"]
            }
    else:
        drv._helios_native_delivery_state = "rejected"
        drv.is_busy = False
        if source == "queued":
            drv._pending_queue_delivery = None
        MainWindow._on_driver_error(h.window, drv, "turn/start rejected")
        if visible:
            assert h.composer.text == "OLD\n\nFIRST\n\nSECOND\n\nTHIRD"
            assert h.window._native_unsent_drafts == {}
        else:
            assert h.composer.text == "THIRD"
            assert h.window._native_unsent_drafts == {
                "openai:work-1": ["OLD", "FIRST", "SECOND"]
            }
    assert MainWindow._native_recovery_anchor_for(h.window, drv) is None


@pytest.mark.parametrize("source", ["direct", "queued"])
@pytest.mark.parametrize("initial_state", ["accepted", "rejected", "unknown"])
def test_preclassified_stop_never_exposes_native_head(
    monkeypatch,
    source,
    initial_state,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    drv._helios_native_delivery_state = initial_state
    if source == "direct":
        h.window._native_pending_user[id(drv)] = (drv, "FIRST")
        drv.queue_user_text("SECOND")
    else:
        first_qid = drv.queue_user_text("FIRST")
        drv.queue_user_text("SECOND")
        drv._pending_queue_delivery = (first_qid, "FIRST")
    h.composer.text = "THIRD"
    h.window._driver_manager = types.SimpleNamespace(
        driver_for_session=lambda _session_id: drv,
        stop_driver=lambda _drv: None,
    )

    MainWindow._on_session_stop_requested(h.window, None, "session-a")

    assert h.composer.text == ""
    assert h.window._native_unsent_drafts == {
        "openai:work-1": ["SECOND", "THIRD"]
    }
    assert "FIRST" not in h.window._native_unsent_drafts["openai:work-1"]
    assert MainWindow._native_recovery_anchor_for(h.window, drv) is not None

    if initial_state == "accepted":
        MainWindow._on_native_prompt_accepted(h.window, drv, "FIRST")
        if source == "queued":
            drv.remove_queued(first_qid)
        assert h.composer.text == "SECOND\n\nTHIRD"
    elif initial_state == "rejected":
        drv.is_busy = False
        if source == "queued":
            drv._pending_queue_delivery = None
        MainWindow._on_driver_error(h.window, drv, "turn/start rejected")
        assert h.composer.text == "FIRST\n\nSECOND\n\nTHIRD"
    else:
        drv.is_busy = False
        MainWindow._on_driver_error(h.window, drv, "transport exited")
        assert h.composer.text == "SECOND\n\nTHIRD"


@pytest.mark.parametrize("source", ["direct", "queued"])
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "unknown"])
def test_stop_switch_keeps_recovery_owned_by_original_work(
    monkeypatch,
    source,
    outcome,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    drv._helios_native_delivery_state = "wire"
    if source == "direct":
        h.window._native_pending_user[id(drv)] = (drv, "FIRST")
        drv.queue_user_text("SECOND")
    else:
        first_qid = drv.queue_user_text("FIRST")
        drv.queue_user_text("SECOND")
        drv._pending_queue_delivery = (first_qid, "FIRST")
    h.window._native_unsent_drafts["openai:work-1"] = ["OLD"]
    h.composer.text = "THIRD"
    h.window._driver_manager = types.SimpleNamespace(
        driver_for_session=lambda _session_id: drv,
        stop_driver=lambda _drv: None,
    )

    MainWindow._on_session_stop_requested(h.window, None, "session-a")
    assert h.composer.text == ""

    # Work B becomes visible before App Server's authoritative outcome lands.
    h.window._current_driver = object()
    h.composer.text = "B-DRAFT"
    if outcome == "accepted":
        drv._helios_native_delivery_state = "accepted"
        MainWindow._on_native_prompt_accepted(h.window, drv, "FIRST")
        if source == "queued":
            drv.remove_queued(first_qid)
        expected = ["OLD", "SECOND", "THIRD"]
    elif outcome == "rejected":
        drv._helios_native_delivery_state = "rejected"
        drv.is_busy = False
        if source == "queued":
            drv._pending_queue_delivery = None
        MainWindow._on_driver_error(h.window, drv, "turn/start rejected")
        expected = ["OLD", "FIRST", "SECOND", "THIRD"]
    else:
        drv._helios_native_delivery_state = "unknown"
        drv.is_busy = False
        MainWindow._on_driver_error(h.window, drv, "transport exited")
        expected = ["OLD", "SECOND", "THIRD"]

    assert h.composer.text == "B-DRAFT"
    assert h.window._native_unsent_drafts == {"openai:work-1": expected}
    assert MainWindow._native_recovery_anchor_for(h.window, drv) is None

    # Reopening Work A restores only A's texts and preserves their chronology.
    h.window._current_driver = drv
    h.composer.text = ""
    MainWindow._restore_native_unsent_drafts(h.window, drv)
    assert h.composer.text == "\n\n".join(expected)
    assert h.window._native_unsent_drafts == {}


def test_rebind_before_wire_resolution_keeps_anchor_bucket_private(
    monkeypatch,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    drv._helios_native_delivery_state = "wire"
    drv.is_busy = True
    h.window._native_pending_user[id(drv)] = (drv, "FIRST")
    drv.queue_user_text("SECOND")
    h.composer.text = "THIRD"
    h.window._driver_manager = types.SimpleNamespace(
        driver_for_session=lambda _session_id: drv,
        stop_driver=lambda _drv: None,
    )
    MainWindow._on_session_stop_requested(h.window, None, "session-a")

    h.composer.text = "B-DRAFT"
    h.window._driver = object()
    h.window._current_driver = object()

    def bind_current(candidate):
        h.window._driver = candidate
        h.window._current_driver = candidate

    h.window._driver_manager = types.SimpleNamespace(bind_current=bind_current)
    h.window._goal_strip = types.SimpleNamespace(clear_native_goal=lambda: None)
    h.window._sync_effort_sensitivity = lambda: None
    h.window._sync_execution_control = lambda: None

    MainWindow._bind_visible_driver(h.window, drv)

    assert h.composer.text == "B-DRAFT"
    assert h.window._native_unsent_drafts == {
        "openai:work-1": ["SECOND", "THIRD"]
    }
    assert MainWindow._native_recovery_anchor_for(h.window, drv) is not None


@pytest.mark.parametrize("source", ["direct", "queued"])
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "unknown"])
def test_repeated_stop_keeps_original_boundary_and_appends_new_successors(
    monkeypatch,
    source,
    outcome,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    drv._helios_native_delivery_state = "wire"
    if source == "direct":
        h.window._native_pending_user[id(drv)] = (drv, "FIRST")
        drv.queue_user_text("SECOND")
    else:
        first_qid = drv.queue_user_text("FIRST")
        drv.queue_user_text("SECOND")
        drv._pending_queue_delivery = (first_qid, "FIRST")
    h.window._native_unsent_drafts["openai:work-1"] = ["OLD"]
    h.window._current_driver = object()
    h.window._driver_manager = types.SimpleNamespace(
        driver_for_session=lambda _session_id: drv,
        stop_driver=lambda _drv: None,
    )

    MainWindow._on_session_stop_requested(h.window, None, "session-a")
    original = MainWindow._native_recovery_anchor_for(h.window, drv)
    MainWindow._on_session_stop_requested(h.window, None, "session-a")
    assert MainWindow._native_recovery_anchor_for(h.window, drv) == original

    drv.queue_user_text("FOURTH")
    MainWindow._on_session_stop_requested(h.window, None, "session-a")
    assert MainWindow._native_recovery_anchor_for(h.window, drv) == original
    assert h.window._native_unsent_drafts == {
        "openai:work-1": ["OLD", "SECOND", "FOURTH"]
    }

    if outcome == "accepted":
        drv._helios_native_delivery_state = "accepted"
        MainWindow._on_native_prompt_accepted(h.window, drv, "FIRST")
        if source == "queued":
            drv.remove_queued(first_qid)
        expected = ["OLD", "SECOND", "FOURTH"]
    elif outcome == "rejected":
        drv._helios_native_delivery_state = "rejected"
        drv.is_busy = False
        if source == "queued":
            drv._pending_queue_delivery = None
        MainWindow._on_driver_error(h.window, drv, "turn/start rejected")
        expected = ["OLD", "FIRST", "SECOND", "FOURTH"]
    else:
        drv._helios_native_delivery_state = "unknown"
        drv.is_busy = False
        MainWindow._on_driver_error(h.window, drv, "transport exited")
        expected = ["OLD", "SECOND", "FOURTH"]
    assert h.window._native_unsent_drafts == {"openai:work-1": expected}


@pytest.mark.parametrize("state", ["idle", "rejected"])
def test_next_prewire_rejection_restores_once_before_newer_text(
    monkeypatch,
    state,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    drv._helios_native_delivery_state = state
    drv.is_busy = False
    h.window._native_pending_user[id(drv)] = (drv, "NEXT")
    drv.queue_user_text("AFTER")
    h.composer.text = "DRAFT"

    MainWindow._on_driver_error(h.window, drv, "pre-wire rejection")

    assert h.composer.text == "NEXT\n\nAFTER\n\nDRAFT"
    assert h.composer.text.count("NEXT") == 1
    assert h.window._native_pending_user == {}


@pytest.mark.parametrize("visible", [True, False])
@pytest.mark.parametrize("source", ["direct", "queued"])
def test_unknown_native_exit_quarantines_first_and_preserves_successors(
    monkeypatch,
    visible,
    source,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    drv._helios_native_delivery_state = "unknown"
    if source == "direct":
        h.window._native_pending_user[id(drv)] = (drv, "UNKNOWN_FIRST")
        drv.queue_user_text("SECOND")
    else:
        first_qid = drv.queue_user_text("UNKNOWN_FIRST")
        drv.queue_user_text("SECOND")
        drv._pending_queue_delivery = (first_qid, "UNKNOWN_FIRST")
    h.composer.text = "THIRD"
    h.window._current_driver = drv if visible else object()
    if not visible:
        h.window._native_unsent_drafts["openai:work-1"] = ["OLD"]
    h.window._forget_driver = lambda _drv: None
    h.window._sync_execution_control = lambda: None

    MainWindow._on_driver_exited(h.window, drv, 1)

    assert "UNKNOWN_FIRST" not in h.composer.text
    assert h.window._native_pending_user == {}
    if visible:
        assert h.composer.text == "SECOND\n\nTHIRD"
    else:
        assert h.composer.text == "THIRD"
        assert h.window._native_unsent_drafts == {
            "openai:work-1": ["OLD", "SECOND"]
        }


@pytest.mark.parametrize("visible", [True, False])
def test_driver_exit_recovers_first_then_queue_then_current_draft(
    monkeypatch,
    visible,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    h.window._native_pending_user[id(drv)] = (drv, "FIRST")
    drv.queue_user_text("SECOND")
    h.composer.text = "THIRD"
    h.window._current_driver = drv if visible else object()
    h.window._forget_driver = lambda _drv: None
    h.window._sync_execution_control = lambda: None

    MainWindow._on_driver_exited(h.window, drv, 0)

    if visible:
        assert h.composer.text == "FIRST\n\nSECOND\n\nTHIRD"
        assert h.removed_rows == [-1]
    else:
        assert h.composer.text == "THIRD"
        assert h.window._native_unsent_drafts == {
            "openai:work-1": ["FIRST", "SECOND"]
        }


@pytest.mark.parametrize("visible", [True, False])
def test_provider_error_recovers_pending_first_before_queued_second(
    monkeypatch,
    visible,
):
    h = _window(monkeypatch, "openai")
    drv = h.driver
    h.window._native_pending_user[id(drv)] = (drv, "FIRST")
    drv.queue_user_text("SECOND")
    h.composer.text = "THIRD"
    h.window._current_driver = drv if visible else object()

    MainWindow._on_driver_error(h.window, drv, "admission failed")

    if visible:
        assert h.composer.text == "FIRST\n\nSECOND\n\nTHIRD"
    else:
        assert h.composer.text == "THIRD"
        assert h.window._native_unsent_drafts == {
            "openai:work-1": ["FIRST", "SECOND"]
        }


def test_queued_uncertain_error_keeps_stop_busy_until_proof(monkeypatch):
    h = _window(monkeypatch, "anthropic", established=True)
    drv = h.driver
    first = drv.queue_user_text("MAYBE")
    drv.queue_user_text("LATER")
    drv.is_busy = True
    drv._queue_dispatch_in_progress = True

    MainWindow._on_driver_error(h.window, drv, "stdin delivery uncertain")
    drv._uncertain_queue_delivery = (first, "MAYBE")
    drv._queue_dispatch_in_progress = False

    assert drv.queued_messages() == [(first, "MAYBE"), (2, "LATER")]
    assert h.composer.text == ""
    assert h.composer.busy is True
    assert h.busy_clears == []


def test_queued_uncertain_exit_quarantines_head_and_recovers_successor(
    monkeypatch,
):
    h = _window(monkeypatch, "anthropic", established=True)
    drv = h.driver
    first = drv.queue_user_text("MAYBE")
    drv.queue_user_text("LATER")
    drv._uncertain_queue_delivery = (first, "MAYBE")
    h.composer.text = "DRAFT"
    h.window._forget_driver = lambda _drv: None
    h.window._sync_execution_control = lambda: None

    MainWindow._on_driver_exited(h.window, drv, 1)

    assert h.composer.text == "LATER\n\nDRAFT"
    assert "MAYBE" not in h.composer.text
    assert drv.queued_messages() == []
    assert any("unknown delivery" in toast for toast in h.toasts)


def test_nonterminal_cancel_keeps_late_provider_proof_promotable(monkeypatch):
    h = _window(monkeypatch, "anthropic", established=True)
    drv = h.driver
    first = drv.queue_user_text("QUEUED_MAYBE")
    drv.queue_user_text("LATER")
    drv._uncertain_queue_delivery = (first, "QUEUED_MAYBE")
    h.window._uncertain_pending_user = {id(drv): (drv, "DIRECT_MAYBE")}

    MainWindow._recover_driver_for_cancellation(
        h.window,
        drv,
        terminal=False,
    )

    assert h.window._uncertain_pending_user == {
        id(drv): (drv, "DIRECT_MAYBE")
    }
    assert drv._uncertain_queue_delivery == (first, "QUEUED_MAYBE")
    assert drv.queued_messages() == [(first, "QUEUED_MAYBE")]
    assert h.composer.text == "LATER"

    MainWindow._on_provider_delivery_confirmed(h.window, drv)
    assert h.window._uncertain_pending_user == {}
    assert h.visible_turns == ["DIRECT_MAYBE"]


def test_background_exit_preserves_more_than_twenty_unsent_messages(monkeypatch):
    h = _window(monkeypatch, "anthropic", established=True)
    expected = [f"queued-{index}" for index in range(25)]
    for text in expected:
        h.driver.queue_user_text(text)
    h.window._current_driver = object()
    h.window._forget_driver = lambda _drv: None
    h.window._sync_execution_control = lambda: None

    MainWindow._on_driver_exited(h.window, h.driver, 0)

    assert h.window._native_unsent_drafts == {
        "anthropic:work-1": expected,
    }


# --- mid-turn send: steer where supported, queue where not -----------------


def test_mid_turn_send_steers_instead_of_queueing_when_supported(monkeypatch):
    """A correction typed mid-run should reach the turn that is running, not
    the one after it. Providers with a steer contract take that path."""
    h = _window(monkeypatch, "openai", established=True)
    steered: list[str] = []
    h.driver.steer_user_text = lambda text: (steered.append(text) or True)

    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "actually, do it differently")

    assert steered == ["actually, do it differently"]
    # Steered, so no "Queued" row and nothing waiting for the next turn.
    assert h.queued_rows == []
    assert h.driver.queued_messages() == []
    # Committed on prompt-accepted, exactly like an ordinary native send.
    assert h.window._native_pending_user[id(h.driver)] == (
        h.driver,
        "actually, do it differently",
    )


def test_mid_turn_send_queues_when_the_driver_cannot_steer(monkeypatch):
    """Claude and OpenRouter have no steer contract, so the queue stays."""
    h = _window(monkeypatch, "anthropic", established=True)
    h.driver.steer_user_text = lambda _text: False

    MainWindow._on_composer_send(h.window, None, "FIRST")
    MainWindow._on_composer_send(h.window, None, "SECOND")

    assert h.driver.queued_messages() == [(1, "SECOND")]
    assert h.queued_rows == [(1, "SECOND")]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openrouter"])
def test_mid_turn_send_never_stops_the_running_driver(monkeypatch, provider):
    """Regression guard for "sending mid-turn interrupts the session".

    Whichever branch a mid-turn submit takes — steer, queue, or a bounced
    send — it must never tear down or interrupt the driver that is working.
    """
    h = _window(monkeypatch, provider, established=True)

    MainWindow._on_composer_send(h.window, None, "FIRST")
    for text in ("SECOND", "THIRD", "FOURTH"):
        MainWindow._on_composer_send(h.window, None, text)

    assert h.driver.stops == 0
    assert h.busy_clears == []
