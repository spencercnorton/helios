"""App-side queue for user messages typed while a turn is in flight.

Mirrors the Claude Code desktop app: the composer never locks, and a
message submitted mid-turn is held (visibly, as a "queued" row in the
transcript) and auto-sent when the current turn's `result` lands — one
queued message per turn, in order, so each gets its own full response.

The queue lives on the DRIVER, not the window: a background session can keep
draining after the user switches away, but every actual turn start rechecks
the owning Work's execution breaker. GTK-free on purpose — the logic is shared
by provider drivers and the tests exercise it on the slim CI image without gi.

Host-class contract (both CLI drivers satisfy it):
  * `send_user_text(text)` — sets `is_busy` True on success, emits `error`
    (without latching busy) on failure.
  * `is_busy` / `is_accepting_input` properties.
  * GObject `emit`, with a `queued-user-sent (int, str)` signal declared.
  * Call `_init_user_queue()` in __init__ and `_flush_user_queue()` after
    emitting each turn-completing `result`.

Stop semantics are the window's job: an explicit user Stop calls
`take_queued()` BEFORE `stop()`, so the abort's result finds an empty queue
and nothing auto-sends after an interrupt.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from helios.log import get_logger

_log = get_logger("message-queue")


class RequiredPromptContextError(RuntimeError):
    """Required Work context could not be verified before provider I/O.

    The public message is fixed so a provider/coordinator exception containing
    prompt or credential material can never cross into a toast or transcript.
    """

    user_message = (
        "Helios could not verify required Work context. "
        "The message was not sent."
    )

    def __init__(self) -> None:
        super().__init__(self.user_message)


@dataclass(slots=True)
class PreparedPrompt:
    """Wire text plus a commit hook for context with delivery semantics.

    A provider-neutral Work handoff advances a participant's event cursor only
    after the backend accepted the prompt.  Keeping that acknowledgement next
    to the prepared text avoids losing a collaboration delta when a subprocess
    spawn or stdin write fails.  Plain string wrappers remain supported.
    """

    text: str
    on_sent: Callable[[], None] | None = None

    def mark_sent(self) -> None:
        callback = self.on_sent
        if callback is None:
            return
        # Make acknowledgement idempotent: drivers may conservatively call it
        # from more than one success path, but a cursor must commit once.
        self.on_sent = None
        try:
            callback()
        except Exception as e:
            _log.warning("prompt context acknowledgement failed: %s", e)


PromptContextProvider = Callable[[object, str], str | PreparedPrompt]
ExecutionGuard = Callable[[object], str]
ExecutionAttemptAdmitter = Callable[[object], tuple[str, str]]
ExecutionAttemptFinisher = Callable[[object, str, str, str], None]


@dataclass(frozen=True, slots=True)
class ExecutionDispatchEvidence:
    """Exact provider-bound prompt identity persisted before external I/O."""

    wire_prompt_text: str
    provider_request_key: str = ""
    native_binding_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionAcceptanceEvidence:
    """Provider correlation returned after an explicit acceptance."""

    accepted_turn_id: str = ""
    provider_request_key: str = ""
    native_binding_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionStopEvidence:
    """Typed provider acknowledgement and the fate of pending queue rows."""

    acknowledgement: dict[str, object]
    queue_disposition: str = ""


TerminalEvidenceType = Literal[
    "local_abort",
    "verified_rejection",
    "provider_terminal",
    "cancellation_ack",
]


@dataclass(frozen=True, slots=True)
class ExecutionTerminalEvidence:
    """Causal evidence that is strong enough to release an execution slot.

    ``reason_code`` is deliberately machine-shaped. Provider error prose must
    never be persisted here: it can contain prompt or credential material.
    """

    evidence_type: TerminalEvidenceType
    status: str
    reason_code: str
    provider_status: str = ""
    request_id: str = ""
    turn_id: str = ""
    response_id: str = ""
    native_id: str = ""
    usage: dict[str, object] | None = None
    cost_micro_usd: int | None = None
    stop_acknowledgement: dict[str, object] | None = None
    queue_disposition: str = ""


ExecutionAttemptDispatchRecorder = Callable[
    [object, str, ExecutionDispatchEvidence], None
]
ExecutionAttemptAcceptanceRecorder = Callable[
    [object, str, ExecutionAcceptanceEvidence], None
]
ExecutionAttemptStopRecorder = Callable[[object, str, ExecutionStopEvidence], None]
ExecutionAttemptEvidenceFinisher = Callable[
    [object, str, ExecutionTerminalEvidence], None
]
ExecutionContributionRecorder = Callable[[object, object], object | None]


DeliveryState = Literal["accepted", "pending", "rejected", "uncertain"]


@dataclass(frozen=True, slots=True)
class MessageDelivery:
    """Explicit local handoff result, independent of a fast worker's state."""

    state: DeliveryState

    @property
    def accepted(self) -> bool:
        return self.state == "accepted"

    @property
    def pending(self) -> bool:
        return self.state == "pending"

    @property
    def rejected(self) -> bool:
        return self.state == "rejected"

    @property
    def uncertain(self) -> bool:
        return self.state == "uncertain"


_UNSET = object()


class UserMessageQueueMixin:
    """Ordered queue of (id, text) user messages awaiting their turn."""

    def _init_user_queue(self) -> None:
        self._user_queue: list[tuple[int, str]] = []
        self._user_queue_seq: int = 0
        # A queued head whose provider handoff may have crossed the wire is
        # not an ordinary unsent queue row. Keep its identity until provider
        # evidence accepts it or recovery quarantines it; never auto-replay it.
        self._uncertain_queue_delivery: tuple[int, str] | None = None
        # Synchronous driver errors can fire before ``send_user_text`` returns.
        # Let MainWindow distinguish that admission frame from an idle failure
        # so it cannot clear Stop/busy before an ambiguous queued handoff is
        # quarantined below.
        self._queue_dispatch_in_progress = False
        self._prompt_context_provider: PromptContextProvider | None = None
        self._execution_guard: ExecutionGuard | None = None
        self._execution_attempt_admitter: ExecutionAttemptAdmitter | None = None
        self._execution_attempt_finisher: ExecutionAttemptFinisher | None = None
        self._execution_attempt_dispatch_recorder: (
            ExecutionAttemptDispatchRecorder | None
        ) = None
        self._execution_attempt_acceptance_recorder: (
            ExecutionAttemptAcceptanceRecorder | None
        ) = None
        self._execution_attempt_stop_recorder: ExecutionAttemptStopRecorder | None = None
        self._execution_attempt_evidence_finisher: (
            ExecutionAttemptEvidenceFinisher | None
        ) = None
        self._execution_contribution_recorder: ExecutionContributionRecorder | None = None
        # Hold the exact objects until MainWindow's eventual ``turn-appended``
        # callback consumes them. OpenRouter schedules that signal on GLib
        # *after* terminal accounting, so clearing markers at terminal release
        # would double-write the contribution. Strong references also prevent
        # CPython from reusing an ``id`` for a different Turn in the gap.
        self._execution_recorded_contributions: dict[int, object] = {}
        self._execution_contribution_persistence_failed = False
        self._execution_attempt_id = ""
        self._execution_dispatch_recorded = False
        self._execution_dispatch_evidence: ExecutionDispatchEvidence | None = None

    # ── public API (window-facing) ──

    def set_prompt_context_provider(
        self,
        provider: PromptContextProvider | None,
    ) -> None:
        """Install an app-level prompt wrapper.

        Drivers call `_apply_prompt_context()` immediately before writing to
        their backend. Because queued messages are eventually sent through the
        same `send_user_text(text)` method, the context is resolved at actual
        send time rather than at queue time.
        """
        self._prompt_context_provider = provider

    def set_execution_guard(self, guard: ExecutionGuard | None) -> None:
        """Install a fail-closed gate checked immediately before a turn starts.

        The window performs the same check before accepting a direct submit,
        but queued messages start inside provider result callbacks. Keeping the
        final gate on the driver closes that asynchronous boundary and lets a
        Work-wide terminal state stop every provider in the family.
        """

        self._execution_guard = guard

    def set_execution_attempt_controller(
        self,
        admit: ExecutionAttemptAdmitter | None,
        finish: ExecutionAttemptFinisher | None,
        *,
        record_dispatch: ExecutionAttemptDispatchRecorder | None | object = _UNSET,
        record_acceptance: (
            ExecutionAttemptAcceptanceRecorder | None | object
        ) = _UNSET,
        record_stop: ExecutionAttemptStopRecorder | None | object = _UNSET,
        finish_with_evidence: (
            ExecutionAttemptEvidenceFinisher | None | object
        ) = _UNSET,
        record_contribution: ExecutionContributionRecorder | None | object = _UNSET,
    ) -> None:
        """Install the durable scoped-attempt admission lifecycle.

        Admission is separate from the inexpensive state guard: a driver calls
        it exactly at the last safe boundary before a provider side effect.
        Missing or failed accounting is fail-closed.
        """

        self._execution_attempt_admitter = admit
        self._execution_attempt_finisher = finish
        # Existing tests and third-party drivers replace only admit/finish.
        # Preserve already-installed recovery adapters unless the caller
        # explicitly supplies a new value (including ``None`` to clear it).
        if record_dispatch is not _UNSET:
            self._execution_attempt_dispatch_recorder = record_dispatch
        if record_acceptance is not _UNSET:
            self._execution_attempt_acceptance_recorder = record_acceptance
        if record_stop is not _UNSET:
            self._execution_attempt_stop_recorder = record_stop
        if finish_with_evidence is not _UNSET:
            self._execution_attempt_evidence_finisher = finish_with_evidence
        if record_contribution is not _UNSET:
            self._execution_contribution_recorder = record_contribution

    @property
    def execution_attempt_id(self) -> str:
        """Current durable attempt identifier, primarily for diagnostics."""

        return str(getattr(self, "_execution_attempt_id", "") or "")

    def _begin_execution_attempt(self, *, reuse_existing: bool = False) -> str:
        """Acquire the applicable durable execution lane; return a denial reason."""

        if self.execution_attempt_id:
            if reuse_existing:
                return ""
            return (
                "Helios has an unresolved execution attempt. "
                "New provider work remains blocked."
            )
        admitter = getattr(self, "_execution_attempt_admitter", None)
        if admitter is None:
            return (
                "Helios execution admission is unavailable. "
                "The message was not sent."
            )
        try:
            attempt_id, reason = admitter(self)
        except Exception as exc:
            _log.warning("execution admission failed: %s", exc)
            return (
                "Helios could not reserve the execution lane. "
                "The message was not sent."
            )
        attempt_id = str(attempt_id or "")
        reason = str(reason or "")
        if reason:
            return reason
        if not attempt_id:
            return (
                "Helios could not verify the execution reservation. "
                "The message was not sent."
            )
        self._execution_attempt_id = attempt_id
        self._execution_dispatch_recorded = False
        self._execution_dispatch_evidence = None
        self._execution_contribution_persistence_failed = False
        return ""

    def _record_execution_contribution(self, turn: object) -> bool:
        """Persist an assistant contribution before terminal slot release."""

        recorder = getattr(self, "_execution_contribution_recorder", None)
        if recorder is None:
            _log.error("execution contribution recorder is unavailable")
            self._execution_contribution_persistence_failed = True
            return False
        try:
            recorded = recorder(self, turn)
        except Exception as exc:
            _log.error("could not record execution contribution: %s", exc)
            self._execution_contribution_persistence_failed = True
            return False
        if recorded is None:
            _log.error("execution contribution recorder did not confirm persistence")
            self._execution_contribution_persistence_failed = True
            return False
        self._execution_recorded_contributions[id(turn)] = turn
        return True

    def _consume_recorded_contribution(self, turn: object) -> bool:
        """Let the UI signal path avoid duplicating a pre-terminal record."""

        marker = id(turn)
        recorded = self._execution_recorded_contributions.get(marker)
        if recorded is not turn:
            return False
        self._execution_recorded_contributions.pop(marker, None)
        return True

    def _record_execution_dispatch(
        self,
        evidence: ExecutionDispatchEvidence,
    ) -> bool:
        """Persist exact wire identity before the first provider side effect."""

        attempt_id = self.execution_attempt_id
        recorder = getattr(self, "_execution_attempt_dispatch_recorder", None)
        if not attempt_id or recorder is None:
            _log.error("execution dispatch recorder is unavailable")
            return False
        try:
            recorder(self, attempt_id, evidence)
        except Exception as exc:
            _log.error("could not record execution dispatch %s: %s", attempt_id, exc)
            return False
        self._execution_dispatch_recorded = True
        self._execution_dispatch_evidence = evidence
        return True

    def _record_execution_acceptance(
        self,
        evidence: ExecutionAcceptanceEvidence,
    ) -> bool:
        """Persist provider-native acceptance without releasing the slot."""

        attempt_id = self.execution_attempt_id
        recorder = getattr(self, "_execution_attempt_acceptance_recorder", None)
        if not attempt_id or recorder is None:
            _log.error("execution acceptance recorder is unavailable")
            return False
        try:
            recorder(self, attempt_id, evidence)
        except Exception as exc:
            _log.error("could not record execution acceptance %s: %s", attempt_id, exc)
            return False
        return True

    def _record_execution_stop(self, evidence: ExecutionStopEvidence) -> bool:
        """Persist cancellation acknowledgement without inferring terminality."""

        attempt_id = self.execution_attempt_id
        recorder = getattr(self, "_execution_attempt_stop_recorder", None)
        if not attempt_id or recorder is None:
            _log.error("execution stop recorder is unavailable")
            return False
        try:
            recorder(self, attempt_id, evidence)
        except Exception as exc:
            _log.error("could not record execution stop %s: %s", attempt_id, exc)
            return False
        return True

    def _finish_execution_with_evidence(
        self,
        evidence: ExecutionTerminalEvidence,
    ) -> bool:
        """Release only through a typed causal-evidence adapter."""

        attempt_id = self.execution_attempt_id
        if not attempt_id:
            return True
        if self._execution_contribution_persistence_failed:
            _log.error(
                "execution attempt %s has an unpersisted contribution", attempt_id
            )
            return False
        finisher = getattr(self, "_execution_attempt_evidence_finisher", None)
        if finisher is None:
            # Compatibility for lightweight test/third-party controllers.
            # Production installs the evidence adapter together with the
            # dispatch recorder; provider drivers that can reach external I/O
            # fail closed when that recorder is missing.
            legacy = getattr(self, "_execution_attempt_finisher", None)
            if legacy is None:
                _log.error("execution attempt %s has no evidence finisher", attempt_id)
                return False
            try:
                legacy(self, attempt_id, evidence.status, evidence.reason_code)
            except Exception as exc:
                _log.error(
                    "could not finish execution attempt %s: %s", attempt_id, exc
                )
                return False
            self._execution_attempt_id = ""
            self._execution_dispatch_recorded = False
            self._execution_dispatch_evidence = None
            self._execution_contribution_persistence_failed = False
            return True
        try:
            finisher(self, attempt_id, evidence)
        except Exception as exc:
            _log.error("could not finish execution attempt %s: %s", attempt_id, exc)
            return False
        self._execution_attempt_id = ""
        self._execution_dispatch_recorded = False
        self._execution_dispatch_evidence = None
        self._execution_contribution_persistence_failed = False
        return True

    def _finish_execution_attempt(
        self,
        status: str,
        terminal_reason: str = "",
    ) -> bool:
        """Persist terminal state before result delivery or queue flushing."""

        attempt_id = self.execution_attempt_id
        if not attempt_id:
            return True
        finisher = getattr(self, "_execution_attempt_finisher", None)
        if finisher is None:
            _log.error("execution attempt %s has no finisher", attempt_id)
            return False
        try:
            finisher(self, attempt_id, status, str(terminal_reason or ""))
        except Exception as exc:
            # Keep the id (and durable running row) so a later send cannot
            # silently create overlapping provider work.
            _log.error("could not finish execution attempt %s: %s", attempt_id, exc)
            return False
        self._execution_attempt_id = ""
        self._execution_dispatch_recorded = False
        self._execution_dispatch_evidence = None
        self._execution_contribution_persistence_failed = False
        return True

    def _finish_execution_from_result(self, result: object) -> bool:
        payload = result if isinstance(result, dict) else {}
        subtype = str(payload.get("subtype", "") or "").strip()
        normalized = subtype.lower()
        if normalized == "success":
            status = "completed"
        elif normalized in {"aborted", "interrupted", "cancelled", "canceled"}:
            status = "aborted"
        elif normalized in {"budgetlimited", "error_max_budget_usd"}:
            status = "budgetLimited"
        else:
            status = "failed"
        reason_code = {
            "completed": "provider_result.success",
            "aborted": "provider_result.aborted",
            "budgetLimited": "provider_result.budget_limited",
            "failed": "provider_result.failed",
        }[status]
        return self._finish_execution_with_evidence(
            ExecutionTerminalEvidence(
                evidence_type="provider_terminal",
                status=status,
                reason_code=reason_code,
                provider_status=subtype or "unknown",
                request_id=self.execution_attempt_id,
                native_id=self._confirmed_execution_native_id(),
                queue_disposition=(
                    "restored" if status == "budgetLimited" else "released"
                ),
            )
        )

    def _confirmed_execution_native_id(self) -> str:
        """Return only a provider identity that the UI already verified."""

        if getattr(self, "_helios_identity_confirmed", False) is not True:
            return ""
        return str(
            getattr(self, "session_id", "")
            or getattr(self, "_resume", "")
            or ""
        )

    def _execution_block_reason(self) -> str:
        guard = getattr(self, "_execution_guard", None)
        if guard is None:
            return ""
        try:
            reason = guard(self)
        except Exception as exc:
            _log.warning("execution guard failed: %s", exc)
            return (
                "Helios could not verify this Work's execution state. "
                "The message was not sent."
            )
        return str(reason or "")

    def _prepare_prompt_context(self, text: str) -> PreparedPrompt:
        provider = getattr(self, "_prompt_context_provider", None)
        if provider is None:
            return PreparedPrompt(text)
        try:
            wrapped = provider(self, text)
        except RequiredPromptContextError:
            # This is an admission boundary, not an optional presentation
            # wrapper. Every driver catches the typed error before its own
            # provider boundary and leaves direct/queued input unsent.
            raise
        except Exception as e:
            _log.warning("prompt context provider failed: %s", e)
            return PreparedPrompt(text)
        if isinstance(wrapped, PreparedPrompt):
            return wrapped if wrapped.text else PreparedPrompt(text)
        if isinstance(wrapped, str) and wrapped:
            return PreparedPrompt(wrapped)
        return PreparedPrompt(text)

    def _apply_prompt_context(self, text: str) -> str:
        """Compatibility helper for callers that only need transformed text.

        Real drivers use :meth:`_prepare_prompt_context` and acknowledge the
        returned object after a successful write.  This text-only form keeps
        existing lightweight consumers and third-party wrappers working.
        """
        return self._prepare_prompt_context(text).text

    def queue_user_text(self, text: str, *, first: bool = False) -> int:
        """Hold `text` until the in-flight turn completes. Returns a queue id
        the UI can use to remove the entry before it's sent.

        ``first`` re-inserts at the head: for a message that already owned the
        next slot when it was dispatched (a declined steer), whose position
        must survive submissions that queued while its dispatch was in flight.
        """
        self._user_queue_seq += 1
        qid = self._user_queue_seq
        if first:
            self._user_queue.insert(0, (qid, text))
        else:
            self._user_queue.append((qid, text))
        return qid

    def queued_messages(self) -> list[tuple[int, str]]:
        """Snapshot of pending (id, text) entries, send order."""
        return list(self._user_queue)

    def remove_queued(self, qid: int) -> bool:
        """Drop one pending entry (the ✕ on its transcript row)."""
        owned = {
            marker
            for marker in (
                getattr(self, "_uncertain_queue_delivery", None),
                getattr(self, "_pending_queue_delivery", None),
            )
            if marker is not None
        }
        for i, (q, _t) in enumerate(self._user_queue):
            if q == qid:
                if self._user_queue[i] in owned:
                    return False
                del self._user_queue[i]
                return True
        return False

    def take_queued(self) -> list[str]:
        """Return definitely-unsent texts while retaining an ambiguous head.

        Stop may run before a provider's late receipt.  The ambiguous row is a
        non-sendable quarantine marker, not a draft: keep it attached until
        provider evidence accepts it or terminal cleanup consumes it.
        """
        uncertain = self._uncertain_queue_delivery
        texts = [
            text
            for qid, text in self._user_queue
            if uncertain is None or (qid, text) != uncertain
        ]
        self._user_queue[:] = (
            [uncertain]
            if uncertain is not None and uncertain in self._user_queue
            else []
        )
        return texts

    def _quarantine_uncertain_queue_delivery(self) -> tuple[int, str] | None:
        """Consume an ambiguous queued row without making it replayable."""

        delivery, self._uncertain_queue_delivery = (
            self._uncertain_queue_delivery,
            None,
        )
        if delivery is not None:
            self._user_queue[:] = [row for row in self._user_queue if row != delivery]
        return delivery

    def _quarantine_pending_queue_delivery(self) -> tuple[int, str] | None:
        """Consume a provider-owned native head without restoring it."""

        delivery = getattr(self, "_pending_queue_delivery", None)
        if hasattr(self, "_pending_queue_delivery"):
            self._pending_queue_delivery = None
        if delivery is not None:
            self._user_queue[:] = [row for row in self._user_queue if row != delivery]
        return delivery

    def _accept_uncertain_queue_delivery(self) -> bool:
        """Promote one ambiguous queued head after provider-native evidence."""

        delivery, self._uncertain_queue_delivery = (
            self._uncertain_queue_delivery,
            None,
        )
        if delivery is None:
            return False
        qid, text = delivery
        self._user_queue[:] = [row for row in self._user_queue if row != delivery]
        self.emit("queued-user-sent", qid, text)
        return True

    # ── driver-side hook ──

    def _flush_user_queue(self) -> None:
        """Send the next queued message, if any. Called after each `result`.

        One per result: each queued message becomes its own turn. If the send
        fails (busy never latched — the driver emitted `error` instead), the
        message goes back to the head so the exit path can surface it."""
        if (
            not self._user_queue
            or not self.is_accepting_input
            or self._uncertain_queue_delivery is not None
        ):
            return
        block_reason = self._execution_block_reason()
        if block_reason:
            self.emit("error", block_reason)
            return
        qid, text = self._user_queue[0]
        self._queue_dispatch_in_progress = True
        try:
            outcome = self.send_user_text(text)
        finally:
            self._queue_dispatch_in_progress = False
        accepted = (
            outcome.accepted
            if isinstance(outcome, MessageDelivery)
            else bool(self.is_busy)
        )
        if accepted:
            self._user_queue.pop(0)
            self.emit("queued-user-sent", qid, text)
        elif isinstance(outcome, MessageDelivery) and outcome.uncertain:
            self._uncertain_queue_delivery = (qid, text)
