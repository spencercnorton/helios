"""UserMessageQueueMixin — GTK-free queue logic shared by both drivers."""

import pytest

from helios.backend.process.message_queue import (
    ExecutionTerminalEvidence,
    MessageDelivery,
    PreparedPrompt,
    RequiredPromptContextError,
    UserMessageQueueMixin,
)


class _Host(UserMessageQueueMixin):
    """Minimal stand-in for a CLI driver: records sends + signal emissions."""

    def __init__(self) -> None:
        self._init_user_queue()
        self.sent: list[str] = []
        self.emitted: list[tuple] = []
        self.lifecycle: list[tuple[str, ...]] = []
        self.accepting = True
        self.send_ok = True
        self._busy = False
        self._attempt_seq = 0
        self.set_execution_attempt_controller(self._admit, self._finish)

    def _admit(self, _driver) -> tuple[str, str]:
        self._attempt_seq += 1
        attempt_id = f"attempt-{self._attempt_seq}"
        self.lifecycle.append(("admit", attempt_id))
        return attempt_id, ""

    def _finish(
        self,
        _driver,
        attempt_id: str,
        status: str,
        reason: str,
    ) -> None:
        self.lifecycle.append(("finish", attempt_id, status, reason))

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def is_accepting_input(self) -> bool:
        return self.accepting

    def send_user_text(self, text: str) -> MessageDelivery:
        block_reason = self._execution_block_reason()
        if block_reason:
            self.emit("error", block_reason)
            return MessageDelivery("rejected")
        prepared = self._prepare_prompt_context(text)
        admission_error = self._begin_execution_attempt()
        if admission_error:
            self.emit("error", admission_error)
            return MessageDelivery("rejected")
        if self.send_ok:
            self.sent.append(prepared.text)
            prepared.mark_sent()
            self._busy = True
            return MessageDelivery("accepted")
        else:
            self._finish_execution_attempt("failed", "simulated send failure")
            return MessageDelivery("rejected")
        # On failure the real drivers emit `error` and never latch busy.

    def emit(self, signal: str, *args) -> None:
        self.emitted.append((signal, *args))

    def finish_turn(self) -> None:
        """Simulate a turn-completing `result` (drivers flush right after)."""
        self._finish_execution_attempt("completed", "success")
        self._busy = False
        self._flush_user_queue()


def test_queue_ids_are_unique_and_ordered():
    h = _Host()
    a = h.queue_user_text("first")
    b = h.queue_user_text("second")
    assert a != b
    assert h.queued_messages() == [(a, "first"), (b, "second")]


def test_flush_sends_one_message_per_result_in_order():
    h = _Host()
    h._busy = True
    h.queue_user_text("first")
    h.queue_user_text("second")

    h.finish_turn()
    assert h.sent == ["first"]
    assert h.emitted[-1][0] == "queued-user-sent"
    assert h.emitted[-1][2] == "first"
    assert [t for _q, t in h.queued_messages()] == ["second"]

    h.finish_turn()
    assert h.sent == ["first", "second"]
    assert h.queued_messages() == []


def test_flush_noop_when_queue_empty():
    h = _Host()
    h.finish_turn()
    assert h.sent == []
    assert h.emitted == []


def test_flush_noop_when_not_accepting_input():
    h = _Host()
    h.queue_user_text("held")
    h.accepting = False
    h.finish_turn()
    assert h.sent == []
    # Message retained so the exit path can surface it.
    assert [t for _q, t in h.queued_messages()] == ["held"]


def test_remove_queued_drops_only_the_target():
    h = _Host()
    a = h.queue_user_text("keep me out")
    b = h.queue_user_text("send me")
    assert h.remove_queued(a) is True
    assert h.remove_queued(a) is False  # already gone
    assert h.queued_messages() == [(b, "send me")]
    h.finish_turn()
    assert h.sent == ["send me"]


def test_take_queued_clears_and_returns_texts():
    h = _Host()
    h.queue_user_text("one")
    h.queue_user_text("two")
    assert h.take_queued() == ["one", "two"]
    assert h.queued_messages() == []
    h.finish_turn()
    assert h.sent == []


def test_failed_send_requeues_at_head():
    h = _Host()
    h.queue_user_text("flaky")
    h.queue_user_text("later")
    h.send_ok = False
    h.finish_turn()
    assert h.sent == []
    assert all(sig != "queued-user-sent" for sig, *_ in h.emitted)
    assert [t for _q, t in h.queued_messages()] == ["flaky", "later"]
    # Recovery: a later flush (e.g. after respawn) sends it.
    h.send_ok = True
    h.finish_turn()
    assert h.sent == ["flaky"]


def test_explicit_acceptance_beats_fast_worker_busy_race():
    h = _Host()
    h.queue_user_text("fast")

    def fast_send(text: str) -> MessageDelivery:
        h.sent.append(text)
        # The worker completed before send_user_text returned. ``is_busy`` is
        # therefore false even though local delivery definitely succeeded.
        h._busy = False
        return MessageDelivery("accepted")

    h.send_user_text = fast_send
    h._flush_user_queue()

    assert h.sent == ["fast"]
    assert h.queued_messages() == []
    assert h.emitted[-1] == ("queued-user-sent", 1, "fast")


def test_uncertain_queued_head_is_never_replayed_with_successors():
    h = _Host()
    first = h.queue_user_text("maybe delivered")
    h.queue_user_text("definitely unsent")
    sends = []

    def uncertain_send(text: str) -> MessageDelivery:
        sends.append(text)
        return MessageDelivery("uncertain")

    h.send_user_text = uncertain_send
    h._flush_user_queue()
    h._flush_user_queue()

    assert sends == ["maybe delivered"]
    assert h._uncertain_queue_delivery == (first, "maybe delivered")
    assert h.take_queued() == ["definitely unsent"]
    assert h.queued_messages() == [(first, "maybe delivered")]
    assert h._uncertain_queue_delivery == (first, "maybe delivered")
    assert h._quarantine_uncertain_queue_delivery() == (
        first,
        "maybe delivered",
    )
    assert h.queued_messages() == []
    assert h._uncertain_queue_delivery is None


def test_provider_evidence_promotes_uncertain_queued_head_once():
    h = _Host()
    first = h.queue_user_text("maybe delivered")
    h.queue_user_text("later")
    h.send_user_text = lambda _text: MessageDelivery("uncertain")
    h._flush_user_queue()
    assert h.take_queued() == ["later"]
    assert h.queued_messages() == [(first, "maybe delivered")]

    assert h._accept_uncertain_queue_delivery() is True
    assert h._accept_uncertain_queue_delivery() is False
    assert h.emitted == [("queued-user-sent", first, "maybe delivered")]
    assert h.queued_messages() == []


def test_owned_queued_delivery_cannot_be_removed_until_resolved():
    h = _Host()
    first = h.queue_user_text("owned")
    h._uncertain_queue_delivery = (first, "owned")

    assert h.remove_queued(first) is False
    assert h.queued_messages() == [(first, "owned")]

    h._quarantine_uncertain_queue_delivery()
    assert h.queued_messages() == []


def test_prompt_context_applied_when_queued_message_is_sent():
    h = _Host()
    h.queue_user_text("queued")
    h.set_prompt_context_provider(lambda _drv, text: f"wrapped:{text}")

    h.finish_turn()

    assert h.sent == ["wrapped:queued"]
    assert h.emitted[-1] == ("queued-user-sent", 1, "queued")


def test_queued_prompt_context_is_resolved_at_flush_time():
    h = _Host()
    generation = {"value": "old"}
    h.set_prompt_context_provider(
        lambda _drv, text: f"{generation['value']}:{text}"
    )
    h.queue_user_text("queued")
    generation["value"] = "new"

    h.finish_turn()

    assert h.sent == ["new:queued"]


def test_prompt_context_failure_falls_back_to_original_text():
    h = _Host()

    def boom(_drv, _text):
        raise RuntimeError("bad wrapper")

    h.set_prompt_context_provider(boom)
    h.queue_user_text("plain")
    h.finish_turn()

    assert h.sent == ["plain"]


def test_required_prompt_context_failure_is_never_silently_unwrapped():
    h = _Host()
    h.set_prompt_context_provider(
        lambda _drv, _text: (_ for _ in ()).throw(RequiredPromptContextError())
    )

    with pytest.raises(RequiredPromptContextError):
        h._prepare_prompt_context("must not cross the wire raw")


def test_prepared_prompt_commits_only_after_successful_send():
    h = _Host()
    committed: list[str] = []
    h.set_prompt_context_provider(
        lambda _drv, text: PreparedPrompt(
            f"shared:{text}", lambda: committed.append(text)
        )
    )
    h.queue_user_text("retry me")

    h.send_ok = False
    h.finish_turn()
    assert committed == []
    assert [text for _qid, text in h.queued_messages()] == ["retry me"]

    h.send_ok = True
    h.finish_turn()
    assert h.sent == ["shared:retry me"]
    assert committed == ["retry me"]


def test_prepared_prompt_acknowledgement_is_idempotent():
    committed: list[bool] = []
    prepared = PreparedPrompt("wire", lambda: committed.append(True))

    prepared.mark_sent()
    prepared.mark_sent()

    assert committed == [True]


def test_execution_guard_holds_queue_at_terminal_work_boundary():
    h = _Host()
    h._busy = True
    h.queue_user_text("must remain unsent")
    h.set_execution_guard(lambda _driver: "Work budget exhausted")

    h.finish_turn()

    assert h.sent == []
    assert h.queued_messages() == [(1, "must remain unsent")]
    assert h.emitted[-1] == ("error", "Work budget exhausted")


def test_execution_guard_failure_is_fail_closed():
    h = _Host()
    h._busy = True
    h.queue_user_text("must remain unsent")

    def unavailable(_driver):
        raise RuntimeError("work store unavailable")

    h.set_execution_guard(unavailable)
    h.finish_turn()

    assert h.sent == []
    assert h.queued_messages() == [(1, "must remain unsent")]
    assert "could not verify" in h.emitted[-1][1]


def test_attempt_finishes_before_queued_message_reacquires_slot():
    h = _Host()
    h.send_user_text("first")
    h.queue_user_text("second")

    h.finish_turn()

    assert h.lifecycle == [
        ("admit", "attempt-1"),
        ("finish", "attempt-1", "completed", "success"),
        ("admit", "attempt-2"),
    ]
    assert h.sent == ["first", "second"]


def test_missing_attempt_controller_is_fail_closed_and_retains_queue():
    h = _Host()
    h.set_execution_attempt_controller(None, None)
    h.queue_user_text("must remain unsent")

    h.finish_turn()

    assert h.sent == []
    assert h.queued_messages() == [(1, "must remain unsent")]
    assert "admission is unavailable" in h.emitted[-1][1]


def test_failed_terminal_accounting_keeps_attempt_latched():
    h = _Host()

    def broken_finish(_driver, _attempt_id, _status, _reason):
        raise RuntimeError("database unavailable")

    h.set_execution_attempt_controller(h._admit, broken_finish)
    h.send_user_text("first")

    assert h._finish_execution_attempt("completed") is False
    assert h.execution_attempt_id == "attempt-1"
    assert "unresolved execution attempt" in h._begin_execution_attempt()
    assert h._begin_execution_attempt(reuse_existing=True) == ""
    assert h.lifecycle == [("admit", "attempt-1")]


def test_contribution_persists_before_typed_terminal_release():
    h = _Host()
    order = []
    h.set_execution_attempt_controller(
        h._admit,
        h._finish,
        record_contribution=lambda _driver, turn: (
            order.append(("contribution", turn)) or True
        ),
        finish_with_evidence=lambda _driver, _attempt, evidence: order.append(
            ("terminal", evidence.reason_code)
        ),
    )
    assert h._begin_execution_attempt() == ""

    turn = object()
    assert h._record_execution_contribution(turn)
    assert h._finish_execution_with_evidence(
        ExecutionTerminalEvidence(
            evidence_type="provider_terminal",
            status="completed",
            reason_code="provider.terminal",
            provider_status="success",
        )
    )

    assert order == [
        ("contribution", turn),
        ("terminal", "provider.terminal"),
    ]
    # OpenRouter emits ``turn-appended`` on a later GLib callback, after the
    # worker has committed terminal accounting. The marker must survive that
    # release and be consumed exactly once by the UI callback.
    assert h._consume_recorded_contribution(turn)
    assert not h._consume_recorded_contribution(turn)


def test_unpersisted_contribution_keeps_slot_running():
    h = _Host()

    def fail_contribution(_driver, _turn):
        raise RuntimeError("store unavailable")

    h.set_execution_attempt_controller(
        h._admit,
        h._finish,
        record_contribution=fail_contribution,
        finish_with_evidence=lambda *_args: None,
    )
    assert h._begin_execution_attempt() == ""
    assert not h._record_execution_contribution(object())


def test_unconfirmed_contribution_result_keeps_slot_running():
    h = _Host()
    h.set_execution_attempt_controller(
        h._admit,
        h._finish,
        record_contribution=lambda *_args: None,
        finish_with_evidence=lambda *_args: None,
    )
    assert h._begin_execution_attempt() == ""

    assert not h._record_execution_contribution(object())
    assert h._execution_contribution_persistence_failed is True
    assert not h._finish_execution_with_evidence(
        ExecutionTerminalEvidence(
            evidence_type="provider_terminal",
            status="completed",
            reason_code="provider.terminal",
        )
    )
    assert h.execution_attempt_id == "attempt-1"

    assert not h._finish_execution_with_evidence(
        ExecutionTerminalEvidence(
            evidence_type="provider_terminal",
            status="completed",
            reason_code="provider.terminal",
            provider_status="success",
        )
    )
    assert h.execution_attempt_id == "attempt-1"
