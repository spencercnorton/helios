"""Persistent Codex App Server driver with fail-closed transport policy.

The existing :class:`CodexCliDriver` remains available as a compatibility
implementation, but this driver never falls back to it automatically.  The
exec transport cannot currently prove parity for native developer
instructions, delegation disablement, or lifetime budgets, so degrading to it
would silently remove the P0 containment contract.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject  # noqa: E402

from helios.backend import codex_env, model_catalog, session_goals
from helios.backend.agent_activity import (
    TERMINAL_AGENT_STATUSES,
    AgentObservedStatus,
    normalize_agent_status,
)
from helios.backend.process import codex_app_events as app_events
from helios.backend.process import codex_protocol
from helios.backend.process.codex_app_contract import (
    APPROVAL_METHODS,
    DYNAMIC_TOOL_METHOD,
    MCP_ELICITATION_METHOD,
    PERMISSIONS_METHOD,
    USER_INPUT_METHOD,
    approval_question,
    build_review_settings_params,
    build_review_start_params,
    build_thread_fork_params,
    build_turn_steer_params,
    build_thread_params,
    build_turn_start_params,
    interaction_response,
    rate_limit_rows,
)
from helios.backend.router_client import (
    RouterClient,
    RouterError,
    RouterRejectedError,
)
from helios.backend.router_tools import CODEX_NAMESPACE, CONTRACT_VERSION, tool_names
from helios.backend.process.codex_app_hub import CodexAppServerHub, get_shared_hub
from helios.backend.process.codex_app_server import (
    CodexAppServerRpcError,
    ServerRequest,
)
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence,
    ExecutionDispatchEvidence,
    ExecutionStopEvidence,
    ExecutionTerminalEvidence,
    MessageDelivery,
    PreparedPrompt,
    RequiredPromptContextError,
)
from helios.backend.process.codex_driver import CodexCliDriver
from helios.backend.project_perms import (
    PERMISSION_MODES,
    execution_mode_restriction_reason,
)
from helios.backend.workflow_modes import (
    DEFAULT_WORKFLOW_MODE,
    PLAN_WORKFLOW_MODE,
    WORKFLOW_MODES,
    canonical_workflow_mode,
)
from helios.log import get_logger


_log = get_logger("codex-app-driver")

#: Notification methods already reported as unhandled, process-wide. A codex
#: rename fires on every notification of the renamed kind; one warning per
#: method per process is the whole point of the record.
_UNKNOWN_NOTIFICATION_METHODS: set[str] = set()
_UNSUPPORTED_NOTIFICATION_METHODS: set[str] = set()
_UNKNOWN_SERVER_REQUEST_METHODS: set[str] = set()

_METHOD_SERVER_REQUEST_RESOLVED = "serverRequest/resolved"
_METHOD_MCP_STATUS = "mcpServer/startupStatus/updated"
_METHOD_THREAD_GOAL_UPDATED = "thread/goal/updated"
_METHOD_THREAD_GOAL_CLEARED = "thread/goal/cleared"
_METHOD_THREAD_COMPACTED = "thread/compacted"
_METHOD_THREAD_COMPACT_START = "thread/compact/start"
_METHOD_REVIEW_START = "review/start"
_METHOD_THREAD_FORK = "thread/fork"
_METHOD_THREAD_SETTINGS_UPDATE = "thread/settings/update"
_METHOD_RUNTIME_WARNING = "warning"
_METHOD_CONFIG_WARNING = "configWarning"
_METHOD_GUARDIAN_WARNING = "guardianWarning"
_METHOD_DEPRECATION_NOTICE = "deprecationNotice"
_PROVIDER_NOTICE_METHODS = frozenset(
    {
        _METHOD_RUNTIME_WARNING,
        _METHOD_CONFIG_WARNING,
        _METHOD_GUARDIAN_WARNING,
        _METHOD_DEPRECATION_NOTICE,
    }
)

NATIVE_DELIVERY_IDLE = "idle"
NATIVE_DELIVERY_LOCAL = "local"
NATIVE_DELIVERY_WIRE = "wire"
NATIVE_DELIVERY_ACCEPTED = "accepted"
NATIVE_DELIVERY_REJECTED = "rejected"
NATIVE_DELIVERY_UNKNOWN = "unknown"
_METHOD_THREAD_STARTED = "thread/started"

_GOAL_STATUSES = {"active", "paused", "blocked", "complete"}
_MAX_ROUTER_TOOL_WORKERS = 4
_ROUTER_TOOL_SLOTS = threading.BoundedSemaphore(_MAX_ROUTER_TOOL_WORKERS)
CODEX_STANDARD_TOKEN_BUDGET = 200_000
CODEX_BUDGET_INTERRUPT_DEADLINE_MS = 12_000
_RESOLVE_TOKEN_BUDGET = -1


def _provider_notice_payload(
    method: str,
    params: dict[str, Any],
) -> dict[str, str] | None:
    """Normalize the four textual App Server notice schemas.

    Runtime and Guardian warnings use ``message``; configuration and
    deprecation notices use ``summary``.  Keep optional detail available to a
    future notice surface, while the current toast deliberately shows only the
    schema's concise user-facing field.
    """

    field = (
        "message"
        if method in {_METHOD_RUNTIME_WARNING, _METHOD_GUARDIAN_WARNING}
        else "summary"
    )
    raw_message = params.get(field)
    if method not in _PROVIDER_NOTICE_METHODS or not isinstance(raw_message, str):
        return None
    message = raw_message.strip()
    if not message:
        return None
    notice = {"method": method, "message": message}
    for key in ("threadId", "details", "path"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            notice[key] = value.strip()
    return notice


def _notification_turn_id(params: Any) -> str:
    """Extract the v2 turn id from either lifecycle notification shape."""

    if not isinstance(params, dict):
        return ""
    turn = params.get("turn")
    if isinstance(turn, dict):
        turn_id = turn.get("id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    turn_id = params.get("turnId")
    return turn_id if isinstance(turn_id, str) else ""


def default_token_budget() -> int | None:
    """The terminal family token cap, or ``None`` when tokens are not money.

    A lifetime token cap is a real spend control only on per-token billing.
    On a ChatGPT subscription it kills a Work over a figure nobody is billed
    for — the same defect the Claude dollar breaker had in v0.58.0, which
    terminally ended a live Work at $10.07 of API-*equivalent* estimate.

    This governs BOTH caps, because there are two and they are both terminal:
    the host-side family counter here, and the ``tokenBudget`` mirrored into
    Codex's own ``thread/goal/set`` — App Server enforces that one itself and
    reports ``sessionBudgetExceeded``. Capping one and not the other would just
    move which layer kills the Work. When this returns ``None`` the goal is
    sent without a ``tokenBudget`` and neither layer terminates.

    The subscription's real ceiling is the 5-hour/weekly meter, which already
    arrives as ``account/rateLimits/updated`` and is surfaced to the user. A
    provider-reported ``sessionBudgetExceeded`` is still honoured in both
    modes: that one is real, and is not Helios's synthetic cap.

    ``refresh=True`` is required, not incidental. The cache is process-wide and
    Helios outlives a ``codex login``, so a cached subscription answer would go
    on removing the cap for every later driver after the user switched to
    per-token auth. Since the driver stores the result, this is still one probe
    per driver, not one per turn.
    """

    from helios.backend.codex_env import is_subscription_billing

    return (
        None
        if is_subscription_billing(refresh=True)
        else CODEX_STANDARD_TOKEN_BUDGET
    )


@dataclass(frozen=True, slots=True)
class _TurnStartDispatch:
    """Immutable identity for one in-flight native model-turn request."""

    attempt_id: str
    queue_delivery: tuple[int, str] | None
    router_generation: int
    prepared: PreparedPrompt
    visible_text: str
    request_method: str
    operation_name: str
    accept_started_notification: bool
    authorize_router_tools: bool


@dataclass(frozen=True, slots=True)
class _ReviewPreparation:
    """A Review waiting for its read-only thread settings acknowledgement."""

    generation: int
    prepared: PreparedPrompt
    visible_text: str
    params: dict[str, Any]


class CodexAppServerDriver(CodexCliDriver):
    """One lightweight Helios client bound to a native Codex thread."""

    __gsignals__ = {
        # One-shot notice that the App Server's protocol moved under this
        # build: an unhandled notification method.
        "capability-drift": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "provider-notice": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "thread-forked": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "context-compacted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-status-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "plan-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "diff-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "activity-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "mcp-status-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "agents-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "native-goal-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "budget-exhausted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "interaction-resolved": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "prompt-accepted": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "transport-status-updated": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (object,),
        ),
    }

    display_name = "codex-app-server"

    def __init__(
        self,
        *args,
        hub: CodexAppServerHub | None = None,
        token_budget: int | None = _RESOLVE_TOKEN_BUDGET,
        workflow_mode: str = DEFAULT_WORKFLOW_MODE,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._workflow_mode = canonical_workflow_mode(workflow_mode)
        self._supported_workflow_modes: tuple[str, ...] = (DEFAULT_WORKFLOW_MODE,)
        self._hub: CodexAppServerHub | None = hub
        # At most one report in each protocol-gap class per driver; log lines
        # are deduplicated per method for the process lifetime.
        self._unknown_notification_reported = False
        self._unsupported_notification_reported = False
        self._unknown_request_reported = False
        # Resolved once per driver so one auth probe covers the session, and
        # so a test can pin either billing mode without touching the CLI.
        if token_budget is _RESOLVE_TOKEN_BUDGET:
            token_budget = default_token_budget()
        if token_budget is not None and token_budget <= 0:
            raise ValueError("Codex token_budget must be positive or None")
        self._token_budget: int | None = token_budget
        self._native_mode = False
        self._native_starting = False
        self._startup_pending_text: str | None = None
        self._native_goal_synced = False
        self._native_goal_pending = False
        self._goal_request_generation = 0
        self._goal_pending_text: str | None = None
        self._native_goal_reconcile_error = ""
        self._pending_queue_delivery: tuple[int, str] | None = None
        # One steer may await acknowledgement at a time. Without this latch a
        # second quick submission dispatches a concurrent turn/steer, and if
        # the first fails back to the queue while the second lands, the user's
        # own messages reorder — the exact property the pending-delivery guard
        # below protects. Refusing here sends the successor down the ordered
        # queue path instead. Set/read/cleared on the GTK main thread only.
        self._steer_inflight = False
        # MainWindow must distinguish definitely-local prompts from a
        # turn/start request whose acceptance is authoritative, rejected, or
        # unknowable. Never infer this from ``busy``: timeout/transport exit
        # can clear busy after the server accepted the turn.
        self._native_delivery_lock = threading.Lock()
        self._helios_native_delivery_state = NATIVE_DELIVERY_IDLE
        self._active_turn_start_dispatch: _TurnStartDispatch | None = None
        self._attempt_id_by_turn: dict[str, str] = {}
        self._promoted_turn_start_attempts: set[str] = set()
        self._turn_acceptance_lock = threading.Lock()
        self._recorded_turn_acceptance_attempts: set[str] = set()
        self._dispatching_queue_delivery = False
        self._interrupt_when_turn_known = False
        self._app_acc = app_events.CodexAppEventAccumulator(
            model=self._model,
            thread_id=self._acc.thread_id,
        )
        self._app_turn_id = ""
        self._app_acceptance_recorded = False
        # Manual native compaction is a dedicated App Server maintenance turn,
        # not a user/model execution attempt. Keep its identity separate so it
        # cannot mutate the Work ledger or the durable execution plan.
        self._manual_compaction_pending = False
        self._manual_compaction_generation = 0
        self._manual_compaction_turn_id = ""
        self._manual_compaction_item_id = ""
        self._manual_compaction_item_completed = False
        self._manual_compaction_interrupt_requested = False
        self._manual_compaction_interrupt_sent = False
        self._manual_compaction_pre_tokens = 0
        # Native Review is billable model work. Its read-only settings update
        # must be acknowledged before a durable execution slot crosses the
        # review/start boundary.
        self._review_generation = 0
        self._review_preparation: _ReviewPreparation | None = None
        self._review_cancel_requested = False
        # Fork is a control-plane operation: no model attempt, but its response
        # must still be reconciled exactly once so an accepted native branch is
        # never silently orphaned or retried after an ambiguous timeout.
        self._thread_fork_generation = 0
        self._thread_fork_pending = False
        self._thread_fork_stop_requested = False
        self._last_context_used = 0
        self._compaction_pre_tokens_by_item: dict[str, int] = {}
        self._recorded_compactions: set[str] = set()
        self._router_turn_lock = threading.Lock()
        self._router_authorized_turn_id = ""
        self._router_tool_generation = 0
        self._router_authorization_suppressed = False
        self._router_tool_requests: dict[int, ServerRequest] = {}
        # Process-global admission control prevents a burst from bypassing the
        # limit by spreading calls across several live session drivers.
        self._router_tool_slots = _ROUTER_TOOL_SLOTS
        # Existing native threads can retain previously persisted dynamic
        # tools even when thread/resume omits them. Keep execution disabled
        # until an explicit Parallel lease and family budget exist.
        self._delegation_enabled = False
        self._pending_interactions: dict[str, tuple[ServerRequest, str]] = {}
        self._interaction_token_by_id: dict[int | str, str] = {}
        self._interaction_seq = 0
        self._resolved_before_delivery: set[int | str] = set()
        self._answered_interaction_ids: set[int | str] = set()
        self._mcp_by_name: dict[str, dict[str, Any]] = {}
        self._agents: dict[str, dict[str, Any]] = {}
        self._agents_turn_id = ""
        self._rate_snapshots: dict[str, dict[str, Any]] = {}
        # App Server reports a cumulative counter per thread.  A native
        # session can own child threads, so retain the latest counter for each
        # member and enforce the budget against the whole thread family.
        self._lifetime_tokens_by_thread: dict[str, int] = {}
        self._lifetime_tokens_used = 0
        self._token_budget_exhausted = False
        self._budget_interrupt_required = False
        self._budget_interrupt_generation = 0
        self._budget_interrupt_source_id = 0
        self._budget_interrupt_request_turn_id = ""
        self._budget_interrupt_escalated = False
        self._fallback_reason = ""

    # -- window-facing capabilities ---------------------------------

    @property
    def session_id(self) -> str:
        if self._native_mode:
            return self._app_acc.thread_id
        return super().session_id

    @property
    def native_transport(self) -> bool:
        return self._native_mode

    @property
    def native_starting(self) -> bool:
        return self._native_starting

    @property
    def supports_native_goals(self) -> bool:
        return self._native_mode

    @property
    def native_goal_synced(self) -> bool:
        return self._native_goal_synced

    @property
    def fallback_reason(self) -> str:
        return self._fallback_reason

    @property
    def supports_manual_compaction(self) -> bool:
        return bool(
            self._native_mode
            and not self._closed
            and self._hub is not None
            and self.session_id
        )

    @property
    def supports_native_review(self) -> bool:
        return self.supports_manual_compaction

    @property
    def supports_native_fork(self) -> bool:
        return self.supports_manual_compaction

    @property
    def workflow_mode(self) -> str:
        return self._workflow_mode

    @property
    def supported_workflow_modes(self) -> tuple[str, ...]:
        return self._supported_workflow_modes

    def set_workflow_mode(
        self,
        mode: str,
        callback: Callable[[bool, str], None] | None = None,
    ) -> bool:
        if mode not in WORKFLOW_MODES:
            self._notify_execution_callback(callback, False, "unknown workflow mode")
            return False
        mode = canonical_workflow_mode(mode)
        if mode == PLAN_WORKFLOW_MODE and mode not in self._supported_workflow_modes:
            self._notify_execution_callback(
                callback,
                False,
                "This Codex App Server does not advertise native Plan mode.",
            )
            return False
        if not self._execution_change_ready(callback):
            return False
        self._workflow_mode = mode
        self._notify_execution_callback(callback, True)
        return True

    def set_permission_mode(
        self,
        mode: str,
        callback: Callable[[bool, str], None] | None = None,
    ) -> bool:
        if not self._native_mode:
            return super().set_permission_mode(mode, callback)
        if mode not in PERMISSION_MODES:
            self._notify_execution_callback(callback, False, "unknown permission mode")
            return False
        restriction = execution_mode_restriction_reason(
            mode,
            self._cwd,
            provider="openai",
        )
        if restriction:
            self._notify_execution_callback(
                callback,
                False,
                restriction,
            )
            return False
        if not self._execution_change_ready(callback):
            return False
        # TurnStartParams carries the sticky overrides on every native turn,
        # so changing this field while idle affects this same thread without
        # replacing or resuming the conversation.
        self._permission_mode = mode
        self._notify_execution_callback(callback, True)
        return True

    # -- lifecycle ---------------------------------------------------

    def start(self) -> None:
        """Start native discovery/binding off the GTK main loop."""

        if self._native_starting or self._native_mode or self._closed:
            return
        self._native_starting = True
        threading.Thread(
            target=self._start_worker,
            name="helios-codex-start",
            daemon=True,
        ).start()

    def _start_worker(self) -> None:
        try:
            super().start()  # binary discovery + auth validation, no process spawn
        except Exception as exc:
            GLib.idle_add(self._finish_start_error, str(exc))
            return

        # Emergency/ordinary Stop may have landed while binary/auth discovery
        # was running. Never create a new shared transport after that terminal
        # fence, even if the worker had not reached hub acquisition yet.
        if self._closed:
            self._native_starting = False
            return

        if os.environ.get("HELIOS_CODEX_TRANSPORT", "").strip().lower() == "exec":
            GLib.idle_add(
                self._finish_start_error,
                "HELIOS_CODEX_TRANSPORT=exec is disabled: codex exec cannot "
                "prove Helios developer-instruction, single-agent, and lifetime-"
                "budget guarantees. Unset it to use Codex App Server.",
            )
            return

        hub = self._hub or get_shared_hub()
        try:
            hub.acquire(self, binary=self._binary)
        except Exception as exc:
            GLib.idle_add(
                self._finish_start_error,
                "Codex App Server is unavailable. Helios will not fall back to "
                "an exec transport without equivalent developer instructions, "
                f"delegation controls, and lifetime budgets: {exc}",
            )
            return
        try:
            mode_result = hub.call("collaborationMode/list", {}, timeout=10.0)
            mode_rows = mode_result.get("data") if isinstance(mode_result, dict) else None
            if not isinstance(mode_rows, list):
                raise RuntimeError("invalid collaboration mode response")
            advertised = tuple(
                dict.fromkeys(
                    row.get("mode")
                    for row in mode_rows
                    if isinstance(row, dict)
                    and row.get("mode") in {DEFAULT_WORKFLOW_MODE, PLAN_WORKFLOW_MODE}
                )
            )
            self._supported_workflow_modes = tuple(
                dict.fromkeys((DEFAULT_WORKFLOW_MODE, *advertised))
            )
        except Exception as exc:
            self._supported_workflow_modes = (DEFAULT_WORKFLOW_MODE,)
            if self._workflow_mode == PLAN_WORKFLOW_MODE:
                hub.release(self)
                GLib.idle_add(
                    self._finish_start_error,
                    "Codex native Plan mode could not be verified; the session "
                    f"was not started: {exc}",
                )
                return
        if self._workflow_mode not in self._supported_workflow_modes:
            hub.release(self)
            GLib.idle_add(
                self._finish_start_error,
                "This Codex App Server does not advertise the selected native "
                f"workflow ({self._workflow_mode}).",
            )
            return
        if self._closed:
            self._native_starting = False
            hub.release(self)
            return

        resume_id = self._app_acc.thread_id
        method = "thread/resume" if resume_id else "thread/start"
        params = build_thread_params(
            cwd=self._cwd,
            model=self._model,
            permission_mode=self._permission_mode,
            workflow_mode=self._workflow_mode,
            thread_id=resume_id,
        )
        try:
            # Once written, a timeout is ambiguous. Never fall back/replay.
            result = hub.call(method, params, timeout=20.0)
        except Exception as exc:
            hub.release(self)
            GLib.idle_add(
                self._finish_start_error,
                f"Codex could not open its native thread: {exc}",
            )
            return

        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else ""
        if not isinstance(thread_id, str) or not thread_id:
            hub.release(self)
            GLib.idle_add(
                self._finish_start_error,
                "Codex App Server returned no thread id",
            )
            return
        GLib.idle_add(self._finish_native_start, hub, thread_id)

    def _finish_native_start(
        self,
        hub: CodexAppServerHub,
        thread_id: str,
    ) -> bool:
        if self._closed:
            hub.release(self)
            return False
        self._hub = hub
        self._native_mode = True
        self._native_starting = False
        self._app_acc.thread_id = thread_id
        self._app_acc._announced = True
        try:
            hub.bind_thread(self, thread_id)
        except Exception as exc:
            self._native_mode = False
            hub.release(self)
            self._hub = None
            return self._finish_start_error(
                f"Codex native thread is already active or could not bind: {exc}"
            )
        self._mirror.bind_thread(thread_id)
        pending, self._startup_pending_text = self._startup_pending_text, None
        self._busy = False
        self.emit("session-started", thread_id, self._cwd, self._model)
        # Session identity is a synchronous activation boundary.  A Helios
        # handler may reject and fully tear down this driver; never continue
        # loading state or dispatching the buffered first prompt afterward.
        if self._closed or getattr(self, "_helios_identity_rejected", False):
            self._finish_predispatch_attempt()
            return False
        self.emit("transport-status-updated", {"transport": "app-server"})
        self._load_native_account_state()
        self._load_native_mcp_state()
        if pending:
            self.send_user_text(pending, _reuse_execution_attempt=True)
            self._finish_deferred_attempt_if_unsent(
                "Codex startup prompt was rejected before turn dispatch"
            )
        return False

    def _finish_start_error(self, message: str) -> bool:
        if self._closed:
            return False
        self._native_starting = False
        self._startup_pending_text = None
        self._busy = False
        accounting_ok = self._finish_predispatch_attempt()
        self._closed = True
        if not accounting_ok:
            self.emit(
                "error",
                "Helios could not persist execution completion; "
                "new provider work remains blocked.",
            )
        self.emit("error", message)
        self.emit("exited", 1)
        return False

    def _finish_deferred_attempt_if_unsent(self, _reason: str) -> None:
        """Release an early reservation when deferred dispatch stayed local."""

        if self._busy or not self.execution_attempt_id:
            return
        if not self._finish_predispatch_attempt():
            self.emit(
                "error",
                "Helios could not persist execution completion; "
                "new provider work remains blocked.",
            )

    def _finish_predispatch_attempt(self) -> bool:
        """Release only a reservation proven not to have crossed provider I/O."""

        return self._finish_execution_with_evidence(
            ExecutionTerminalEvidence(
                evidence_type="local_abort",
                status="aborted",
                reason_code="local_abort",
                queue_disposition="restored",
            )
        )

    # -- provider-native maintenance --------------------------------

    def request_review(self, instructions: str = "") -> MessageDelivery:
        """Run Codex's native Review under an acknowledged read-only profile.

        Unlike compaction and fork, Review invokes a model and therefore owns
        the same durable Work admission, acceptance, contribution, budget, and
        terminal lifecycle as an ordinary turn. The preceding settings update
        is intentionally separate: if read-only cannot be confirmed, no review
        request crosses the provider boundary.
        """

        if not self._busy:
            self._transition_native_delivery_state(
                NATIVE_DELIVERY_ACCEPTED,
                NATIVE_DELIVERY_IDLE,
            )
        if not self.supports_native_review:
            self._mark_idle_prompt_rejected()
            self.emit(
                "error",
                "Native Codex Review is unavailable until this GPT "
                "conversation is connected.",
            )
            return MessageDelivery("rejected")
        if (
            self._busy
            or self._manual_compaction_pending
            or self._review_preparation is not None
            or self._thread_fork_pending
            or self._pending_queue_delivery is not None
            or bool(self._user_queue)
            or bool(self.execution_attempt_id)
        ):
            self._mark_idle_prompt_rejected()
            self.emit(
                "error",
                "Wait for the current provider operation to finish, then review.",
            )
            return MessageDelivery("rejected")
        if self._token_budget_exhausted:
            self._mark_idle_prompt_rejected()
            self.emit(
                "error",
                "This Work is budget limited; start a new Work before reviewing.",
            )
            return MessageDelivery("rejected")
        block_reason = self._execution_block_reason()
        if block_reason:
            self._mark_idle_prompt_rejected()
            self.emit("error", block_reason)
            return MessageDelivery("rejected")

        custom = str(instructions or "").strip()
        visible_text = f"/review {custom}" if custom else "/review"
        semantic_text = custom or (
            "Review the working tree, including staged, unstaged, and untracked "
            "changes. Do not modify files."
        )
        try:
            # The custom Review target is the actual native wire instruction,
            # which lets the required Work envelope and collaboration cursor
            # retain their normal accepted-delivery semantics.
            prepared = self._prepare_prompt_context(semantic_text)
            settings_params = build_review_settings_params(
                thread_id=self.session_id,
                model=self._model,
                effort=self._effort,
            )
            review_params = build_review_start_params(
                thread_id=self.session_id,
                instructions=prepared.text,
            )
        except RequiredPromptContextError as exc:
            self._mark_idle_prompt_rejected()
            self.emit("error", exc.user_message)
            return MessageDelivery("rejected")
        except Exception as exc:
            self._mark_idle_prompt_rejected()
            self.emit("error", f"Could not prepare Codex Review: {exc}")
            return MessageDelivery("rejected")

        self._review_generation += 1
        preparation = _ReviewPreparation(
            generation=self._review_generation,
            prepared=prepared,
            visible_text=visible_text,
            params=review_params,
        )
        self._review_preparation = preparation
        self._review_cancel_requested = False
        self._busy = True
        self._stop_requested = False
        self._set_native_delivery_state(NATIVE_DELIVERY_LOCAL)
        self.last_activity = time.monotonic()
        try:
            future = self._hub.request(
                _METHOD_THREAD_SETTINGS_UPDATE,
                settings_params,
                timeout=20.0,
            )
        except Exception as exc:
            self._review_preparation = None
            self._busy = False
            self._set_native_delivery_state(NATIVE_DELIVERY_REJECTED)
            self.emit(
                "error",
                "Codex Review was not started because read-only settings could "
                f"not be confirmed: {exc}",
            )
            return MessageDelivery("rejected")
        future.add_done_callback(
            lambda done: GLib.idle_add(
                self._finish_review_settings_request,
                done,
                preparation,
            )
        )
        return MessageDelivery("pending")

    def _finish_review_settings_request(
        self,
        future: Future[Any],
        preparation: _ReviewPreparation,
    ) -> bool:
        if self._review_preparation is not preparation or self._closed:
            return False
        try:
            result = future.result()
            if not isinstance(result, dict):
                raise RuntimeError("Codex returned an invalid settings response")
        except Exception as exc:
            self._reject_review_preparation(
                "Codex Review was not started because read-only settings could "
                f"not be confirmed: {exc}"
            )
            return False
        if self._review_cancel_requested:
            self._reject_review_preparation(
                "Codex Review was stopped before model execution began."
            )
            return False
        if self._token_budget_exhausted:
            self._reject_review_preparation(
                "This Work is budget limited; start a new Work before reviewing."
            )
            return False
        block_reason = self._execution_block_reason()
        if block_reason:
            self._reject_review_preparation(block_reason)
            return False

        admission_error = self._begin_execution_attempt()
        if admission_error:
            self._reject_review_preparation(admission_error)
            return False
        attempt_id = self.execution_attempt_id
        if not self._record_execution_dispatch(
            ExecutionDispatchEvidence(
                wire_prompt_text=preparation.prepared.text,
                provider_request_key=attempt_id,
                native_binding_id=self.session_id,
            )
        ):
            self._finish_predispatch_attempt()
            self._reject_review_preparation(
                "Helios could not persist the Codex Review dispatch identity. "
                "The review was not started."
            )
            return False

        self._review_preparation = None
        self._app_turn_id = ""
        self._app_acceptance_recorded = False
        self._interrupt_when_turn_known = False
        turn_generation = self._invalidate_router_tool_requests(
            suppress_authorization=True
        )
        dispatch = _TurnStartDispatch(
            attempt_id=attempt_id,
            queue_delivery=None,
            router_generation=turn_generation,
            prepared=preparation.prepared,
            visible_text=preparation.visible_text,
            request_method=_METHOD_REVIEW_START,
            operation_name="Codex Review",
            # Live protocol traces show an internal reviewer turn/started id
            # that differs from review/start's returned source-turn id.
            accept_started_notification=False,
            # Standard Work never authorizes dynamic Router delegation during
            # a read-only review, even if an old native thread restored tools.
            authorize_router_tools=False,
        )
        self._activate_turn_start_dispatch(dispatch)
        try:
            review_future = self._hub.request(
                _METHOD_REVIEW_START,
                preparation.params,
                timeout=60.0,
            )
        except Exception as exc:
            self._hold_ambiguous_turn_start(
                "Codex Review acceptance is unknown; closing this live binding "
                "without replaying the request.",
                detail=str(exc),
            )
            return False
        review_future.add_done_callback(
            lambda done: self._authorize_and_schedule_turn_start(done, dispatch)
        )
        return False

    def _reject_review_preparation(self, message: str) -> None:
        """Return a Review proven not to have crossed ``review/start``."""

        self._review_preparation = None
        self._review_cancel_requested = False
        self._busy = False
        self._set_native_delivery_state(NATIVE_DELIVERY_REJECTED)
        self.emit("error", message)
        if self._close_after_turn and not self._closed:
            self._finalize_native(0)

    def request_fork(self) -> MessageDelivery:
        """Create a persisted native branch without mutating the source thread."""

        if not self.supports_native_fork:
            self.emit(
                "error",
                "Native Codex Fork is unavailable until this GPT conversation "
                "is connected.",
            )
            return MessageDelivery("rejected")
        if (
            self._busy
            or self._manual_compaction_pending
            or self._review_preparation is not None
            or self._thread_fork_pending
            or self._pending_queue_delivery is not None
            or bool(self._user_queue)
            or bool(self.execution_attempt_id)
        ):
            self.emit(
                "error",
                "Wait for the current provider operation to finish, then fork.",
            )
            return MessageDelivery("rejected")
        try:
            params = build_thread_fork_params(
                thread_id=self.session_id,
                cwd=self._cwd,
                permission_mode=self._permission_mode,
                model=self._model,
            )
        except Exception as exc:
            self.emit("error", f"Could not prepare Codex Fork: {exc}")
            return MessageDelivery("rejected")

        self._thread_fork_generation += 1
        generation = self._thread_fork_generation
        self._thread_fork_pending = True
        self._thread_fork_stop_requested = False
        self._busy = True
        self.last_activity = time.monotonic()
        try:
            future = self._hub.request(_METHOD_THREAD_FORK, params, timeout=30.0)
        except Exception as exc:
            self._thread_fork_pending = False
            self._busy = False
            self.emit(
                "error",
                "Codex Fork outcome is unknown; no local branch was created and "
                f"the request was not replayed: {exc}",
            )
            return MessageDelivery("uncertain")
        future.add_done_callback(
            lambda done: GLib.idle_add(
                self._finish_thread_fork_request,
                done,
                generation,
                self.session_id,
            )
        )
        return MessageDelivery("pending")

    def _finish_thread_fork_request(
        self,
        future: Future[Any],
        generation: int,
        source_thread_id: str,
    ) -> bool:
        if (
            not self._thread_fork_pending
            or generation != self._thread_fork_generation
            or self._closed
        ):
            return False
        self._thread_fork_pending = False
        self._busy = False
        stop_requested = self._thread_fork_stop_requested
        self._thread_fork_stop_requested = False
        try:
            result = future.result()
            thread = result.get("thread") if isinstance(result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else ""
            forked_from_id = (
                thread.get("forkedFromId") if isinstance(thread, dict) else ""
            )
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError("Codex returned no forked thread id")
            if thread_id == source_thread_id:
                raise RuntimeError("Codex returned the source thread as its own fork")
            if forked_from_id and forked_from_id != source_thread_id:
                raise RuntimeError("Codex returned a fork from a different source")
            if thread.get("ephemeral") is True:
                raise RuntimeError("Codex returned an ephemeral fork")
        except CodexAppServerRpcError as exc:
            self.emit("error", f"Codex declined conversation Fork: {exc}")
            if self._close_after_turn:
                self._finalize_native(1)
            elif not self._token_budget_exhausted:
                self._flush_user_queue()
            return False
        except Exception as exc:
            self.emit(
                "error",
                "Codex Fork outcome is unknown; no local branch was created and "
                f"the request was not replayed: {exc}",
            )
            if self._close_after_turn:
                self._finalize_native(1)
            elif not self._token_budget_exhausted:
                self._flush_user_queue()
            return False

        turns = thread.get("turns") if isinstance(thread, dict) else None
        self.emit(
            "thread-forked",
            {
                "source_thread_id": source_thread_id,
                "thread_id": thread_id,
                "forked_from_id": str(forked_from_id or source_thread_id),
                "turn_count": len(turns) if isinstance(turns, list) else None,
                "stop_requested": stop_requested,
            },
        )
        if self._close_after_turn and not self._closed:
            self._finalize_native(0)
        elif not self._closed and not self._token_budget_exhausted:
            self._flush_user_queue()
        return False

    def request_compaction(self) -> bool:
        """Start Codex's native context compaction without creating a prompt.

        App Server models this as a dedicated maintenance turn. It therefore
        owns normal turn/item wire events but deliberately owns no Helios
        execution attempt, user transcript contribution, or execution-plan
        lifecycle.
        """

        if not self.supports_manual_compaction:
            self.emit(
                "error",
                "Native Codex compaction is unavailable until this GPT "
                "conversation is connected.",
            )
            return False
        if (
            self._busy
            or self._manual_compaction_pending
            or self._pending_queue_delivery is not None
            or bool(self._user_queue)
            or bool(self.execution_attempt_id)
        ):
            self.emit(
                "error",
                "Wait for the current provider operation to finish, then compact.",
            )
            return False
        if self._token_budget_exhausted:
            self.emit(
                "error",
                "This Work is budget limited; start a new Work before compacting.",
            )
            return False
        block_reason = self._execution_block_reason()
        if block_reason:
            self.emit("error", block_reason)
            return False

        self._manual_compaction_generation += 1
        generation = self._manual_compaction_generation
        self._manual_compaction_pending = True
        self._manual_compaction_turn_id = ""
        self._manual_compaction_item_id = ""
        self._manual_compaction_item_completed = False
        self._manual_compaction_interrupt_requested = False
        self._manual_compaction_interrupt_sent = False
        self._manual_compaction_pre_tokens = self._last_context_used
        self._busy = True
        self.last_activity = time.monotonic()
        self._emit_compaction_activity("compacting", trigger="manual")

        try:
            future = self._hub.request(
                _METHOD_THREAD_COMPACT_START,
                {"threadId": self.session_id},
                timeout=20.0,
            )
        except Exception as exc:
            self._fail_manual_compaction_request(
                f"Could not request Codex compaction: {exc}",
                close_binding=False,
            )
            return False
        future.add_done_callback(
            lambda done: GLib.idle_add(
                self._finish_manual_compaction_request,
                done,
                generation,
            )
        )
        return True

    def _finish_manual_compaction_request(
        self,
        future: Future[Any],
        generation: int,
    ) -> bool:
        if (
            not self._manual_compaction_pending
            or generation != self._manual_compaction_generation
            or self._closed
        ):
            return False
        try:
            result = future.result()
            if not isinstance(result, dict):
                raise RuntimeError("Codex returned an invalid compaction response")
        except CodexAppServerRpcError as exc:
            self._fail_manual_compaction_request(
                f"Codex declined context compaction: {exc}",
                close_binding=False,
            )
        except Exception as exc:
            # A timeout/transport failure can race a successful context
            # mutation. Do not retry or permit a new turn on an uncertain
            # in-memory history; close this binding so the next resume reads
            # Codex's persisted authority.
            self._fail_manual_compaction_request(
                "Codex compaction acceptance is unknown; the conversation "
                f"will be reopened before more work is sent: {exc}",
                close_binding=True,
            )
        return False

    def _fail_manual_compaction_request(
        self,
        message: str,
        *,
        close_binding: bool,
    ) -> None:
        if not self._manual_compaction_pending:
            return
        self._clear_manual_compaction_state()
        self._busy = False
        self._emit_compaction_activity("idle", trigger="manual")
        self.emit("error", message)
        if close_binding:
            self._finalize_native(1)
        elif not self._closed:
            self._flush_user_queue()

    def _clear_manual_compaction_state(self) -> None:
        self._manual_compaction_pending = False
        self._manual_compaction_turn_id = ""
        self._manual_compaction_item_id = ""
        self._manual_compaction_item_completed = False
        self._manual_compaction_interrupt_requested = False
        self._manual_compaction_interrupt_sent = False
        self._manual_compaction_pre_tokens = 0

    def _emit_compaction_activity(self, phase: str, *, trigger: str) -> None:
        self.emit(
            "activity-updated",
            {
                "threadId": self.session_id,
                "turnId": self._manual_compaction_turn_id,
                "itemId": self._manual_compaction_item_id,
                "itemType": "contextCompaction",
                "category": "phase",
                "phase": phase,
                "trigger": trigger,
            },
        )

    def _request_manual_compaction_interrupt(self) -> None:
        if (
            not self._manual_compaction_pending
            or self._manual_compaction_interrupt_sent
            or not self._manual_compaction_turn_id
            or self._hub is None
        ):
            return
        generation = self._manual_compaction_generation
        turn_id = self._manual_compaction_turn_id
        self._manual_compaction_interrupt_sent = True
        try:
            future = self._hub.request(
                "turn/interrupt",
                {
                    "threadId": self.session_id,
                    "turnId": self._manual_compaction_turn_id,
                },
                timeout=10.0,
            )
        except Exception as exc:
            self._manual_compaction_interrupt_sent = False
            self.emit("error", f"Could not stop context compaction: {exc}")
            return
        future.add_done_callback(
            lambda done: self._on_manual_compaction_interrupt_response(
                done,
                generation,
                turn_id,
            )
        )

    def _on_manual_compaction_interrupt_response(
        self,
        future: Future[Any],
        generation: int,
        turn_id: str,
    ) -> None:
        try:
            future.result()
        except Exception as exc:
            GLib.idle_add(
                self._report_manual_compaction_interrupt_failure,
                str(exc),
                generation,
                turn_id,
            )

    def _report_manual_compaction_interrupt_failure(
        self,
        detail: str,
        generation: int,
        turn_id: str,
    ) -> bool:
        if (
            self._manual_compaction_pending
            and not self._closed
            and generation == self._manual_compaction_generation
            and turn_id == self._manual_compaction_turn_id
        ):
            self._manual_compaction_interrupt_sent = False
            self.emit("error", f"Could not stop context compaction: {detail}")
        return False

    def end_input(self) -> None:
        if self._native_starting:
            self._startup_pending_text = None
            self._busy = False
            self._native_starting = False
            if not self._finish_predispatch_attempt():
                self.emit(
                    "error",
                    "Helios could not persist execution completion; "
                    "new provider work remains blocked.",
                )
            self._closed = True
            self.emit("exited", 0)
            return
        if not self._native_mode:
            super().end_input()
            return
        if self._closed:
            return
        if self._busy:
            self._close_after_turn = True
            return
        self._finalize_native(0)

    def stop(self, *, interrupt: bool = True) -> None:
        if self._native_starting:
            self._startup_pending_text = None
            self._busy = False
            # A stop while startup is racing hub acquisition is terminal. If
            # this stayed open, Emergency Stop All could abort an empty hub and
            # the worker could acquire a fresh transport immediately after.
            if not self._finish_predispatch_attempt():
                self.emit(
                    "error",
                    "Helios could not persist execution completion; "
                    "new provider work remains blocked.",
                )
            self._closed = True
            self.emit("exited", 0)
            return
        if not self._native_mode:
            super().stop(interrupt=interrupt)
            return
        if self._closed:
            return
        if self._review_preparation is not None:
            if not interrupt:
                self._close_after_turn = True
            else:
                # thread/settings/update has no cancellation method. It is a
                # safe narrowing, so wait for its bounded acknowledgement and
                # refuse to cross review/start afterward.
                self._review_cancel_requested = True
            return
        if self._thread_fork_pending:
            if not interrupt:
                self._close_after_turn = True
            else:
                # thread/fork has no cancellation method and may already have
                # created the branch. Reconcile its one response; never replay.
                self._thread_fork_stop_requested = True
            return
        if self._manual_compaction_pending:
            if not interrupt:
                self._close_after_turn = True
                return
            self._manual_compaction_interrupt_requested = True
            self._request_manual_compaction_interrupt()
            return
        if not interrupt:
            self._close_after_turn = False
            self._finalize_native(0)
            return
        if self._goal_pending_text is not None and not self._app_turn_id:
            # The user stopped while the prompt was waiting behind Goal sync;
            # nothing reached Codex, so cancel locally and keep the thread open.
            self._goal_pending_text = None
            self._pending_queue_delivery = None
            self._busy = False
            self._stop_requested = False
            self._interrupt_when_turn_known = False
            if not self._finish_predispatch_attempt():
                self.emit(
                    "error",
                    "Helios could not persist execution completion; "
                    "new provider work remains blocked.",
                )
            return
        if not self._busy or self._hub is None:
            return
        self._stop_requested = True
        self._invalidate_router_tool_requests()
        if not self._app_turn_id:
            self._interrupt_when_turn_known = True
            return
        self._request_interrupt()

    def _request_interrupt(self) -> None:
        if self._hub is None or not self._app_turn_id:
            return
        attempt_id = self.execution_attempt_id
        turn_id = self._app_turn_id
        native_id = self._confirmed_execution_native_id()
        budget_interrupt = self._budget_interrupt_required
        if budget_interrupt:
            generation = self._arm_budget_interrupt_watchdog()
            if self._budget_interrupt_request_turn_id == turn_id:
                return
            self._budget_interrupt_request_turn_id = turn_id
        else:
            generation = 0
        self._interrupt_when_turn_known = False
        try:
            future = self._hub.request(
                "turn/interrupt",
                {"threadId": self.session_id, "turnId": turn_id},
                timeout=10.0,
            )
            future.add_done_callback(
                lambda done: self._on_interrupt_response(
                    done,
                    attempt_id=attempt_id,
                    turn_id=turn_id,
                    native_id=native_id,
                    budget_interrupt=budget_interrupt,
                    generation=generation,
                )
            )
        except Exception as exc:
            self._handle_interrupt_failure(
                str(exc),
                budget_interrupt,
                generation,
            )

    def _on_interrupt_response(
        self,
        future: Future[Any],
        *,
        attempt_id: str,
        turn_id: str,
        native_id: str,
        budget_interrupt: bool,
        generation: int,
    ) -> None:
        try:
            future.result()
        except Exception as exc:
            GLib.idle_add(
                self._handle_interrupt_failure,
                str(exc),
                budget_interrupt,
                generation,
            )
            return
        GLib.idle_add(
            self._record_interrupt_acknowledgement,
            attempt_id,
            turn_id,
            native_id,
        )

    def _record_interrupt_acknowledgement(
        self,
        attempt_id: str,
        turn_id: str,
        native_id: str,
    ) -> bool:
        """Record RPC receipt without claiming the turn is cancelled."""

        if (
            attempt_id
            and self.execution_attempt_id == attempt_id
            and self._app_turn_id == turn_id
        ):
            self._record_execution_stop(
                ExecutionStopEvidence(
                    acknowledgement={
                        "acknowledged": True,
                        "cancellation_confirmed": False,
                        "request_id": attempt_id,
                        "turn_id": turn_id,
                        "native_id": native_id,
                    },
                    queue_disposition="held",
                )
            )
        return False

    def _handle_interrupt_failure(
        self,
        detail: str,
        budget_interrupt: bool,
        generation: int,
    ) -> bool:
        if not self._closed:
            self.emit("error", f"Could not interrupt Codex turn: {detail}")
        if (
            budget_interrupt
            and generation == self._budget_interrupt_generation
            and self._budget_interrupt_required
            and self._busy
        ):
            self._escalate_budget_interrupt(
                "the token-budget interrupt request failed",
                generation,
            )
        return False

    def _arm_budget_interrupt_watchdog(self) -> int:
        """Start one deadline for a budget-triggered interrupt."""

        if self._budget_interrupt_source_id or self._budget_interrupt_escalated:
            return self._budget_interrupt_generation
        self._budget_interrupt_generation += 1
        generation = self._budget_interrupt_generation
        try:
            self._budget_interrupt_source_id = GLib.timeout_add(
                CODEX_BUDGET_INTERRUPT_DEADLINE_MS,
                self._on_budget_interrupt_deadline,
                generation,
            )
        except Exception:
            _log.exception("could not arm Codex budget-interrupt watchdog")
            self._escalate_budget_interrupt(
                "Helios could not arm the token-budget interrupt deadline",
                generation,
            )
        return generation

    def _cancel_budget_interrupt_watchdog(self) -> None:
        source_id, self._budget_interrupt_source_id = (
            self._budget_interrupt_source_id,
            0,
        )
        self._budget_interrupt_generation += 1
        self._budget_interrupt_required = False
        self._budget_interrupt_request_turn_id = ""
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass

    def _on_budget_interrupt_deadline(self, generation: int) -> bool:
        if generation != self._budget_interrupt_generation:
            return False
        self._budget_interrupt_source_id = 0
        if (
            self._budget_interrupt_required
            and self._token_budget_exhausted
            and self._busy
            and not self._closed
            and self._native_mode
            and self._hub is not None
        ):
            self._escalate_budget_interrupt(
                "the token-budget interrupt deadline expired",
                generation,
            )
        return False

    def _escalate_budget_interrupt(self, reason: str, generation: int) -> None:
        if (
            generation != self._budget_interrupt_generation
            or self._budget_interrupt_escalated
            or not self._budget_interrupt_required
            or not self._busy
            or self._closed
        ):
            return
        hub = self._hub
        if hub is None:
            return
        self._budget_interrupt_escalated = True
        self._cancel_budget_interrupt_watchdog()
        self.emit(
            "error",
            f"Codex execution could not be stopped safely because {reason}. "
            "Helios is shutting down the shared Codex App Server.",
        )
        worker = threading.Thread(
            target=self._abort_shared_app_server,
            args=(hub,),
            name="helios-codex-budget-abort",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            # Thread creation failure must not turn a containment breaker into
            # a best-effort warning. The hub stop itself remains bounded.
            self._abort_shared_app_server(hub)

    @staticmethod
    def _abort_shared_app_server(hub: CodexAppServerHub) -> None:
        abort = getattr(hub, "abort_transport", None)
        if callable(abort):
            try:
                abort(returncode=-1)
                return
            except Exception:
                _log.exception("Codex safety-abort API failed")
        # Compatibility for injected hubs; the production hub exposes the
        # notifying safety-abort API above.
        try:
            hub.shutdown()
        except Exception:
            _log.exception("Codex shared transport shutdown failed")

    def _finalize_native(self, code: int) -> None:
        if self._closed:
            return
        self._cancel_budget_interrupt_watchdog()
        self._invalidate_router_tool_requests()
        self._clear_manual_compaction_state()
        self._review_preparation = None
        self._review_cancel_requested = False
        self._thread_fork_pending = False
        self._thread_fork_stop_requested = False
        self._closed = True
        self._busy = False
        for token in self._drain_interactions(respond=True):
            self.emit("interaction-resolved", token)
        hub, self._hub = self._hub, None
        if hub is not None:
            hub.release(self)
        self.emit("exited", code)

    # -- turns -------------------------------------------------------

    def _native_delivery_state_snapshot(self) -> str:
        with self._native_delivery_lock:
            return self._helios_native_delivery_state

    def _set_native_delivery_state(self, state: str) -> None:
        with self._native_delivery_lock:
            self._helios_native_delivery_state = state

    def _transition_native_delivery_state(
        self,
        expected: str,
        state: str,
    ) -> bool:
        """Compare-and-set delivery state without downgrading wire outcomes."""

        with self._native_delivery_lock:
            if self._helios_native_delivery_state != expected:
                return False
            self._helios_native_delivery_state = state
            return True

    def _activate_turn_start_dispatch(self, dispatch: _TurnStartDispatch) -> None:
        """Publish one request identity together with its WIRE state."""

        with self._native_delivery_lock:
            self._active_turn_start_dispatch = dispatch
            self._helios_native_delivery_state = NATIVE_DELIVERY_WIRE

    def _pin_turn_start_outcome(
        self,
        dispatch: _TurnStartDispatch,
        state: str,
    ) -> bool:
        """Pin an outcome only while it still belongs to the active request."""

        with self._native_delivery_lock:
            if self._active_turn_start_dispatch is not dispatch:
                return False
            if (
                self._helios_native_delivery_state == NATIVE_DELIVERY_ACCEPTED
                and state != NATIVE_DELIVERY_ACCEPTED
            ):
                # A correlated turn/started notification is stronger native
                # acceptance evidence than a missing/late JSON-RPC response.
                # Never let transport ambiguity or contradictory rejection
                # downgrade a provider turn that is already running.
                return False
            self._helios_native_delivery_state = state
            return True

    def _turn_start_dispatch_is_current(
        self,
        dispatch: _TurnStartDispatch,
    ) -> bool:
        with self._native_delivery_lock:
            return (
                self._active_turn_start_dispatch is dispatch
                and self.execution_attempt_id == dispatch.attempt_id
            )

    def _retire_turn_start_dispatch(
        self,
        dispatch: _TurnStartDispatch | None = None,
    ) -> _TurnStartDispatch | None:
        """Retire one callback identity without touching a newer request."""

        with self._native_delivery_lock:
            active = self._active_turn_start_dispatch
            if dispatch is not None and active is not dispatch:
                return None
            self._active_turn_start_dispatch = None
            return active

    def _bind_turn_to_dispatch(
        self,
        turn_id: str,
        dispatch: _TurnStartDispatch,
    ) -> bool:
        """Correlate a native turn only with the request that created it."""

        if not turn_id:
            return False
        with self._native_delivery_lock:
            if (
                self._active_turn_start_dispatch is not dispatch
                or self.execution_attempt_id != dispatch.attempt_id
            ):
                return False
            existing = self._attempt_id_by_turn.get(turn_id)
            if existing is not None and existing != dispatch.attempt_id:
                return False
            self._attempt_id_by_turn[turn_id] = dispatch.attempt_id
            return True

    def _bind_turn_to_current_dispatch(
        self,
        turn_id: str,
    ) -> _TurnStartDispatch | None:
        with self._native_delivery_lock:
            dispatch = self._active_turn_start_dispatch
            if (
                not turn_id
                or dispatch is None
                or self.execution_attempt_id != dispatch.attempt_id
            ):
                return None
            existing = self._attempt_id_by_turn.get(turn_id)
            if existing is not None and existing != dispatch.attempt_id:
                return None
            self._attempt_id_by_turn[turn_id] = dispatch.attempt_id
            return dispatch

    def _record_dispatch_acceptance(
        self,
        dispatch: _TurnStartDispatch,
        turn_id: str,
    ) -> bool:
        """Persist one captured request/turn correlation without global IDs."""

        if not self._bind_turn_to_dispatch(turn_id, dispatch):
            return False
        with self._turn_acceptance_lock:
            if dispatch.attempt_id in self._recorded_turn_acceptance_attempts:
                return True
            recorder = getattr(self, "_execution_attempt_acceptance_recorder", None)
            if recorder is None:
                _log.error("execution acceptance recorder is unavailable")
                return False
            evidence = ExecutionAcceptanceEvidence(
                accepted_turn_id=turn_id,
                provider_request_key=dispatch.attempt_id,
                native_binding_id=self.session_id,
            )
            try:
                recorder(self, dispatch.attempt_id, evidence)
            except Exception as exc:
                _log.error(
                    "could not record execution acceptance %s: %s",
                    dispatch.attempt_id,
                    exc,
                )
                return False
            self._recorded_turn_acceptance_attempts.add(dispatch.attempt_id)
            return True

    def _promote_turn_start_dispatch(
        self,
        dispatch: _TurnStartDispatch,
        turn_id: str,
        *,
        acceptance_recorded: bool,
    ) -> bool:
        """Commit one provider-accepted prompt exactly once on GTK."""

        if not self._turn_start_dispatch_is_current(dispatch):
            return False
        self._set_native_delivery_state(NATIVE_DELIVERY_ACCEPTED)
        self._app_acceptance_recorded = (
            self._app_acceptance_recorded or acceptance_recorded
        )
        self._app_turn_id = turn_id
        self._app_acc.turn_id = turn_id
        self._begin_native_turn(turn_id)
        already_promoted = dispatch.attempt_id in self._promoted_turn_start_attempts
        if not acceptance_recorded and not already_promoted:
            self.emit(
                "error",
                "Codex accepted the turn, but Helios could not persist its "
                "native identity. New provider work remains blocked until the "
                "authoritative terminal event is recorded.",
            )
        if already_promoted:
            return True
        self._promoted_turn_start_attempts.add(dispatch.attempt_id)
        dispatch.prepared.mark_sent()
        self._mirror.note_user_text(dispatch.visible_text)
        self.emit("prompt-accepted", dispatch.visible_text)
        delivery = dispatch.queue_delivery
        if delivery is not None and self._pending_queue_delivery == delivery:
            self._pending_queue_delivery = None
            if self._user_queue[:1] == [delivery]:
                self._user_queue.pop(0)
                self.emit("queued-user-sent", delivery[0], delivery[1])
        self.last_activity = time.monotonic()
        if self._interrupt_when_turn_known:
            self._request_interrupt()
        return True

    # -- steering ----------------------------------------------------

    def steer_user_text(self, text: str) -> bool:
        """Inject ``text`` into the turn that is already running.

        This is the difference between a correction landing now and landing
        after the model has finished going the wrong way. App Server applies
        the steer to the live turn, so nothing is interrupted and no second
        turn is started. Returns False when steering is not possible, which
        leaves the caller free to fall back to the queue.
        """

        if (
            not self._native_mode
            or self._closed
            or self._hub is None
            or not self._busy
            or not self.session_id
            or not self._app_turn_id
        ):
            return False
        # Another delivery already owns the next slot; steering around it
        # would reorder the user's own messages against each other. An
        # outstanding steer owns it just as much as a queued delivery does —
        # and so does anything already WAITING in the ordinary queue: a steer
        # dispatched while the queue holds messages would put a later
        # submission into the turn ahead of an earlier one.
        if (
            self._steer_inflight
            or self._pending_queue_delivery is not None
            or self._user_queue
            or self._goal_pending_text is not None
            or self._native_goal_pending
        ):
            return False
        turn_id = self._app_turn_id
        try:
            prepared = self._prepare_prompt_context(text)
        except RequiredPromptContextError as exc:
            self.emit("error", exc.user_message)
            return False
        params = build_turn_steer_params(
            thread_id=self.session_id,
            turn_id=turn_id,
            text=prepared.text,
        )
        try:
            future = self._hub.request("turn/steer", params, timeout=30.0)
        except Exception as exc:
            _log.warning("turn/steer dispatch failed: %s", exc)
            return False
        self._steer_inflight = True
        future.add_done_callback(
            lambda done: self._on_steer_response(done, prepared, text, turn_id)
        )
        self.last_activity = time.monotonic()
        return True

    def _on_steer_response(
        self,
        future: Future[Any],
        prepared: PreparedPrompt,
        visible_text: str,
        turn_id: str,
    ) -> None:
        try:
            future.result()
        except CodexAppServerRpcError as exc:
            # App Server answered and said no — authoritative, so requeueing
            # cannot duplicate anything.
            GLib.idle_add(self._finish_steer_failure, str(exc), visible_text)
            return
        except Exception as exc:
            # Timeout or transport loss proves nothing: the steer may have
            # been applied while its response was lost. The normal native
            # send guards this exact case with acceptance evidence; a steer
            # has no idempotency key, so the only duplicate-safe move is to
            # not resend and say so.
            GLib.idle_add(self._finish_steer_unknown, str(exc), visible_text)
            return
        GLib.idle_add(self._finish_steer_accepted, prepared, visible_text, turn_id)

    def _finish_steer_accepted(
        self,
        prepared: PreparedPrompt,
        visible_text: str,
        turn_id: str,
    ) -> bool:
        self._steer_inflight = False
        if self._closed:
            return False
        prepared.mark_sent()
        self._mirror.note_user_text(visible_text)
        self.emit("prompt-accepted", visible_text)
        return False

    def _finish_steer_failure(self, detail: str, visible_text: str) -> bool:
        """A refused steer must not lose the message — fall back to the queue.

        The turn usually finished between dispatch and delivery, so the text
        becomes the next turn instead, which is what would have happened
        without steering at all.
        """

        self._steer_inflight = False
        if self._closed:
            return False
        # No new signal: the ordinary queue path already renders and drains
        # this. Connecting a signal a driver does not declare is what killed
        # every GPT chat in v0.69.0.
        #
        # Head insert, not append: the steer guard only dispatches when the
        # queue is EMPTY, so anything in it now arrived while this dispatch
        # was in flight — later submissions. Appending would deliver them
        # ahead of this earlier one.
        self.queue_user_text(visible_text, first=True)
        _log.info("turn/steer declined (%s) — message queued instead", detail)
        if not self._busy:
            self._flush_user_queue()
        return False

    def _finish_steer_unknown(self, detail: str, visible_text: str) -> bool:
        """An unconfirmed steer is never resent — the user decides.

        App Server may have applied it while the response was lost, and a
        steer carries no idempotency key, so an automatic resend risks the
        same contribution landing twice. Loss is not silent either: the
        declared ``error`` signal names the text and tells the user exactly
        when to send it again.
        """

        self._steer_inflight = False
        if self._closed:
            return False
        _log.warning("turn/steer outcome unknown (%s) — not re-sent", detail)
        tail = visible_text if len(visible_text) <= 80 else visible_text[:79] + "…"
        self.emit(
            "error",
            "A mid-turn correction could not be confirmed as delivered and "
            f"was not re-sent, to avoid a duplicate: “{tail}” — send it "
            "again if the response ignores it.",
        )
        return False

    def _terminal_turn_matches_current_attempt(self, turn_id: str) -> bool:
        """Reject late/duplicate terminal notifications from older turns."""

        attempt_id = self.execution_attempt_id
        if not attempt_id or not turn_id:
            return False
        with self._native_delivery_lock:
            mapped_attempt = self._attempt_id_by_turn.get(turn_id)
            dispatch = self._active_turn_start_dispatch
        if mapped_attempt is not None:
            return mapped_attempt == attempt_id
        return bool(
            dispatch is not None
            and dispatch.attempt_id == attempt_id
            and self._app_turn_id == turn_id
        )

    def send_user_text(
        self,
        text: str,
        *,
        _reuse_execution_attempt: bool = False,
    ) -> MessageDelivery:
        # ``accepted`` describes the previous active turn, not this new
        # admission attempt.  Clear it before any pre-wire guard can reject
        # the new text; otherwise MainWindow can mistake a definitely-unsent
        # NEXT prompt for the already-accepted prior prompt and quarantine it.
        if not self._busy:
            self._transition_native_delivery_state(
                NATIVE_DELIVERY_ACCEPTED,
                NATIVE_DELIVERY_IDLE,
            )
        if self._token_budget_exhausted:
            self._mark_idle_prompt_rejected()
            limit = self._token_budget
            self.emit(
                "error",
                (
                    f"Codex reached this session's {limit:,}-token safety limit. "
                    "Start a new bounded Work to continue."
                    if limit is not None
                    else "Codex reported this account's session budget as "
                    "exhausted. Start a new Work to continue."
                ),
            )
            return MessageDelivery("rejected")
        block_reason = self._execution_block_reason()
        if block_reason:
            self._mark_idle_prompt_rejected()
            self.emit("error", block_reason)
            return MessageDelivery("rejected")
        if self._native_starting:
            if self._closed:
                self._mark_idle_prompt_rejected()
                self.emit("error", "Cannot send: Codex session was closed")
                return MessageDelivery("rejected")
            if self._startup_pending_text is not None:
                self.emit("error", "Codex is still preparing this session.")
                return MessageDelivery("rejected")
            admission_error = self._begin_execution_attempt(
                reuse_existing=_reuse_execution_attempt
            )
            if admission_error:
                self.emit("error", admission_error)
                return MessageDelivery("rejected")
            self._startup_pending_text = text
            self._set_native_delivery_state(NATIVE_DELIVERY_LOCAL)
            self._busy = True
            self.last_activity = time.monotonic()
            return MessageDelivery("pending")
        if not self._native_mode:
            self._mark_idle_prompt_rejected()
            self.emit(
                "error",
                "Codex App Server is not connected. Exec fallback is disabled "
                "because it cannot enforce this Work's containment contract.",
            )
            return MessageDelivery("rejected")
        if self._closed:
            self._mark_idle_prompt_rejected()
            self.emit("error", "Cannot send: Codex session was closed")
            return MessageDelivery("rejected")
        if self._hub is None or not self.session_id:
            self._mark_idle_prompt_rejected()
            self.emit("error", "Codex App Server is not connected")
            return MessageDelivery("rejected")
        if self._native_goal_reconcile_error:
            self._mark_idle_prompt_rejected()
            self.emit("error", self._native_goal_reconcile_error)
            return MessageDelivery("rejected")
        if self._native_goal_pending:
            if (
                self._pending_queue_delivery is not None
                and not self._dispatching_queue_delivery
            ):
                self.emit(
                    "error",
                    "An earlier queued message is waiting for Goal "
                    "reconciliation; this message was not sent.",
                )
                return MessageDelivery("rejected")
            if self._goal_pending_text is not None:
                self.emit("error", "Codex is still synchronizing this Work goal.")
                return MessageDelivery("rejected")
            if self._busy:
                self.emit(
                    "error", "Codex is still working — queue this message instead."
                )
                return MessageDelivery("rejected")
            admission_error = self._begin_execution_attempt(
                reuse_existing=_reuse_execution_attempt
            )
            if admission_error:
                self.emit("error", admission_error)
                return MessageDelivery("rejected")
            self._goal_pending_text = text
            self._set_native_delivery_state(NATIVE_DELIVERY_LOCAL)
            self._busy = True
            self.last_activity = time.monotonic()
            return MessageDelivery("pending")
        if self._busy:
            self.emit("error", "Codex is still working — queue this message instead.")
            return MessageDelivery("rejected")

        try:
            prepared = self._prepare_prompt_context(text)
        except RequiredPromptContextError as exc:
            self.emit("error", exc.user_message)
            return MessageDelivery("rejected")
        # App Server receives Helios policy through typed application context
        # and loads AGENTS.md natively. Keep user input as user input;
        # cross-provider role text here caused the 2026-08-03 runaway.
        prompt = prepared.text
        try:
            # Validate every provider-owned field before reserving a durable
            # slot. The attempt id is inserted only after admission below.
            params = build_turn_start_params(
                thread_id=self.session_id,
                text=prompt,
                permission_mode=self._permission_mode,
                cwd=self._cwd,
                model=self._model,
                effort=self._effort,
                workflow_mode=self._workflow_mode,
            )
        except Exception as exc:
            self.emit("error", f"Could not prepare Codex turn: {exc}")
            return MessageDelivery("rejected")
        admission_error = self._begin_execution_attempt(
            reuse_existing=_reuse_execution_attempt
        )
        if admission_error:
            self.emit("error", admission_error)
            return MessageDelivery("rejected")
        attempt_id = self.execution_attempt_id
        if not self._record_execution_dispatch(
            ExecutionDispatchEvidence(
                wire_prompt_text=prompt,
                provider_request_key=attempt_id,
                native_binding_id=self.session_id,
            )
        ):
            self._finish_execution_with_evidence(
                ExecutionTerminalEvidence(
                    evidence_type="local_abort",
                    status="aborted",
                    reason_code="local_abort",
                    queue_disposition="restored",
                )
            )
            self.emit(
                "error",
                "Helios could not persist the Codex dispatch identity. "
                "The message was not sent.",
            )
            return MessageDelivery("rejected")
        # The durable attempt id is also the idempotency/correlation key on the
        # native request. A replay with a fresh UUID could create a second turn
        # after an ambiguous response.
        params["clientUserMessageId"] = attempt_id
        self._busy = True
        self._stop_requested = False
        self._interrupt_when_turn_known = False
        self._app_turn_id = ""
        self._app_acceptance_recorded = False
        turn_generation = self._invalidate_router_tool_requests(
            suppress_authorization=False
        )
        queue_delivery = (
            self._pending_queue_delivery
            if self._dispatching_queue_delivery
            else None
        )
        dispatch = _TurnStartDispatch(
            attempt_id=attempt_id,
            queue_delivery=queue_delivery,
            router_generation=turn_generation,
            prepared=prepared,
            visible_text=text,
            request_method="turn/start",
            operation_name="Codex turn",
            accept_started_notification=True,
            authorize_router_tools=True,
        )
        self.last_activity = time.monotonic()
        self._activate_turn_start_dispatch(dispatch)
        try:
            future = self._hub.request("turn/start", params, timeout=60.0)
        except Exception as exc:
            self._invalidate_router_tool_requests()
            # ``hub.request`` normally returns a Future even when the stdio
            # write fails. A synchronous failure still races shared-transport
            # teardown, so retain the post-dispatch slot conservatively.
            self._hold_ambiguous_turn_start(
                "Codex turn dispatch failed before an authoritative response; "
                "the Work remains blocked without replaying the request.",
                detail=str(exc),
            )
            return MessageDelivery("uncertain")
        future.add_done_callback(
            lambda done: self._authorize_and_schedule_turn_start(
                done,
                dispatch,
            )
        )
        return MessageDelivery("pending")

    def _mark_idle_prompt_rejected(self) -> None:
        """Make an idle pre-wire denial explicitly safe to replay.

        Never overwrite LOCAL/WIRE/ACCEPTED for an already-owned prompt.  The
        caller only changes IDLE, which is the state of this unsent admission.
        """

        self._transition_native_delivery_state(
            NATIVE_DELIVERY_IDLE,
            NATIVE_DELIVERY_REJECTED,
        )

    def _authorize_and_schedule_turn_start(
        self,
        future: Future[Any],
        dispatch: _TurnStartDispatch,
    ) -> None:
        """Persist and pin native acceptance before later wire callbacks."""

        # Future callbacks run on the transport thread (or synchronously for
        # an already-complete Future). Pin the authoritative outcome before a
        # process-exit callback or higher-priority Stop event can run on GTK.
        # A later explicit response is allowed to refine exit's UNKNOWN; exit
        # must never downgrade an already ACCEPTED/REJECTED response.
        if not self._pin_turn_start_outcome(
            dispatch,
            self._classify_turn_start_delivery(future, dispatch),
        ):
            # A provider terminal notification may release FIRST and auto-send
            # SECOND before FIRST's JSON-RPC Future callback runs.  The stale
            # callback owns neither the new attempt nor its queued head.
            return
        acceptance_recorded = self._record_turn_start_acceptance(future, dispatch)
        # In-memory authorization is a transport-order safety boundary, not a
        # durability claim. Pin it whenever the response carries a valid turn
        # so early notifications/tool requests are not dropped; persistence
        # success remains a separate fail-closed flag for later slot release.
        if dispatch.authorize_router_tools:
            self._record_authorized_turn_from_result(
                future,
                expected_generation=dispatch.router_generation,
            )
        GLib.idle_add(
            self._finish_turn_start_request,
            future,
            dispatch,
            acceptance_recorded,
        )

    def _turn_id_from_request_result(
        self,
        future: Future[Any],
        dispatch: _TurnStartDispatch,
    ) -> str:
        """Validate a model-turn response and return its authoritative id."""

        result = future.result()
        if not isinstance(result, dict):
            raise RuntimeError("Codex App Server returned an invalid response")
        if dispatch.request_method == _METHOD_REVIEW_START:
            review_thread_id = result.get("reviewThreadId")
            if review_thread_id != self.session_id:
                raise RuntimeError(
                    "Codex App Server returned Review on a different thread"
                )
        turn = result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else ""
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeError("Codex App Server returned no turn id")
        return turn_id

    def _classify_turn_start_delivery(
        self,
        future: Future[Any],
        dispatch: _TurnStartDispatch,
    ) -> str:
        try:
            self._turn_id_from_request_result(future, dispatch)
        except CodexAppServerRpcError:
            return NATIVE_DELIVERY_REJECTED
        except Exception:
            return NATIVE_DELIVERY_UNKNOWN
        return NATIVE_DELIVERY_ACCEPTED

    def _record_turn_start_acceptance(
        self,
        future: Future[Any],
        dispatch: _TurnStartDispatch,
    ) -> bool:
        """Bind the server turn id while still on the transport thread."""

        try:
            turn_id = self._turn_id_from_request_result(future, dispatch)
        except Exception:
            return False
        return self._record_dispatch_acceptance(dispatch, turn_id)

    def _record_authorized_turn_from_result(
        self,
        future: Future[Any],
        *,
        expected_generation: int | None = None,
    ) -> None:
        try:
            result = future.result()
            turn = result.get("turn") if isinstance(result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else ""
        except Exception:
            return
        if isinstance(turn_id, str) and turn_id:
            with self._router_turn_lock:
                generation_matches = (
                    expected_generation is None
                    or expected_generation == self._router_tool_generation
                )
                if (
                    generation_matches
                    and not self._closed
                    and self._native_mode
                    and not self._router_authorization_suppressed
                ):
                    self._router_authorized_turn_id = turn_id

    def _finish_turn_start_request(
        self,
        future: Future[Any],
        dispatch: _TurnStartDispatch,
        acceptance_recorded: bool = True,
    ) -> bool:
        if not self._turn_start_dispatch_is_current(dispatch):
            return False
        if self._closed:
            # App exit may win the GTK queue before the transport Future is
            # classified. A later authoritative JSON-RPC rejection still
            # terminalizes the held durable slot; success/transport ambiguity
            # remains held because neither is provider-terminal evidence.
            try:
                future.result()
            except CodexAppServerRpcError:
                self._settle_verified_turn_start_rejection(dispatch)
            except Exception:
                pass
            return False
        try:
            turn_id = self._turn_id_from_request_result(future, dispatch)
        except CodexAppServerRpcError as exc:
            self._invalidate_router_tool_requests()
            accounting_ok = self._settle_verified_turn_start_rejection(dispatch)
            if not accounting_ok:
                self.emit(
                    "error",
                    "Helios could not persist execution completion; "
                    "new provider work remains blocked.",
                )
            self.emit("error", f"Could not start {dispatch.operation_name}: {exc}")
            if self._close_after_turn:
                self._finalize_native(1)
            return False
        except Exception as exc:
            # Timeout, transport death, and a success-shaped response without
            # a turn id are all ambiguous. None proves rejection.
            self._hold_ambiguous_turn_start(
                f"{dispatch.operation_name} acceptance is unknown; closing this "
                "live binding without replaying the request.",
                detail=str(exc),
            )
            return False

        promoted = self._promote_turn_start_dispatch(
            dispatch,
            turn_id,
            acceptance_recorded=acceptance_recorded,
        )
        if promoted and not dispatch.accept_started_notification:
            # Live Review traces can emit a reviewer-internal turn/started id
            # before review/start returns the source turn that owns Stop,
            # terminality, and durable plan scope. Publish only that validated
            # identity to the UI.
            self.emit(
                "turn-status-updated",
                {
                    "threadId": self.session_id,
                    "turnId": turn_id,
                    "status": "inProgress",
                    "durationMs": None,
                    "error": None,
                },
            )
        return False

    def _settle_verified_turn_start_rejection(
        self,
        dispatch: _TurnStartDispatch,
    ) -> bool:
        """Terminalize one authoritative App Server JSON-RPC rejection."""

        if not self._turn_start_dispatch_is_current(dispatch):
            return True
        self._set_native_delivery_state(NATIVE_DELIVERY_REJECTED)
        self._busy = False
        if (
            dispatch.queue_delivery is not None
            and self._pending_queue_delivery == dispatch.queue_delivery
        ):
            self._pending_queue_delivery = None
        accounting_ok = self._finish_execution_with_evidence(
            ExecutionTerminalEvidence(
                evidence_type="verified_rejection",
                status="failed",
                reason_code="codex.rpc_rejected",
                provider_status="rejected",
                request_id=dispatch.attempt_id,
                native_id=self._confirmed_execution_native_id(),
                queue_disposition="restored",
            )
        )
        self._retire_turn_start_dispatch(dispatch)
        return accounting_ok

    def _hold_ambiguous_turn_start(self, message: str, *, detail: str = "") -> None:
        """Close one binding while preserving its unresolved durable slot."""

        self._invalidate_router_tool_requests()
        self._transition_native_delivery_state(
            NATIVE_DELIVERY_WIRE,
            NATIVE_DELIVERY_UNKNOWN,
        )
        self._busy = False
        self._interrupt_when_turn_known = False
        self._record_execution_stop(
            ExecutionStopEvidence(
                acknowledgement={},
                queue_disposition="held",
            )
        )
        self.emit("error", message)
        if detail:
            with self._native_delivery_lock:
                dispatch = self._active_turn_start_dispatch
            method = dispatch.request_method if dispatch is not None else "model turn"
            _log.warning("ambiguous %s outcome: %s", method, detail)
        self._finalize_native(1)

    def _flush_user_queue(self) -> None:
        """Keep native queued rows pending until ``turn/start`` is accepted."""

        if not self._native_mode:
            super()._flush_user_queue()
            return
        if (
            not self._user_queue
            or not self.is_accepting_input
            or self._pending_queue_delivery is not None
        ):
            return
        block_reason = self._execution_block_reason()
        if block_reason:
            self.emit("error", block_reason)
            return
        delivery = self._user_queue[0]
        self._pending_queue_delivery = delivery
        self._dispatching_queue_delivery = True
        try:
            outcome = self.send_user_text(delivery[1])
        finally:
            self._dispatching_queue_delivery = False
        if outcome.rejected:
            self._pending_queue_delivery = None

    def take_queued_successors_after_pending_delivery(self) -> list[str] | None:
        """Detach definitely-unsent successors while retaining wire head.

        The pending delivery must remain at ``_user_queue[0]`` until App Server
        authoritatively accepts or rejects it; removing it early loses the
        normal queued-user-sent promotion/accounting callback.
        """

        delivery = self._pending_queue_delivery
        if delivery is None or self._user_queue[:1] != [delivery]:
            return None
        successors = [text for _qid, text in self._user_queue[1:]]
        del self._user_queue[1:]
        return successors

    # -- transport callbacks (transport thread -> GLib) -------------

    def on_app_notification(self, method: str, params: Any) -> None:
        if method == app_events.METHOD_TURN_STARTED and isinstance(params, dict):
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else ""
            thread_id = params.get("threadId")
            if (
                isinstance(turn_id, str)
                and turn_id
                and (not thread_id or thread_id == self.session_id)
            ):
                # Only a Helios-dispatched user turn may authorize dynamic
                # tools. App Server also emits turn/started for maintenance
                # operations such as native compaction.
                with self._native_delivery_lock:
                    dispatched_user_turn = (
                        self._active_turn_start_dispatch is not None
                    )
                with self._router_turn_lock:
                    if (
                        dispatched_user_turn
                        and not self._closed
                        and self._native_mode
                        and not self._router_authorization_suppressed
                    ):
                        self._router_authorized_turn_id = turn_id
        elif method == app_events.METHOD_TURN_COMPLETED and isinstance(params, dict):
            thread_id = params.get("threadId")
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            turn_id = turn_id or params.get("turnId")
            requests_to_reject: tuple[ServerRequest, ...] = ()
            with self._router_turn_lock:
                if (
                    (not thread_id or thread_id == self.session_id)
                    and isinstance(turn_id, str)
                    and turn_id
                    and turn_id == self._router_authorized_turn_id
                ):
                    requests_to_reject = self._retire_router_tool_requests_locked(
                        suppress_authorization=True
                    )
            self._reject_router_tool_requests(requests_to_reject)
        GLib.idle_add(self._consume_app_notification, method, copy.deepcopy(params))

    def on_app_request(self, request: ServerRequest) -> None:
        if request.method == DYNAMIC_TOOL_METHOD:
            if not self._delegation_enabled:
                request.respond_error(
                    -32000,
                    "Helios delegation is disabled for standard Work",
                )
                return
            # Inference can take tens of seconds.  Never run it on the transport
            # reader or GTK main loop: either would stall every Codex session.
            if not self._router_tool_slots.acquire(blocking=False):
                request.respond_error(
                    -32000,
                    "Helios Router dynamic-tool capacity is in use",
                )
                return
            with self._router_turn_lock:
                generation = self._router_tool_generation
                self._router_tool_requests[id(request)] = request
            try:
                threading.Thread(
                    target=self._run_dynamic_tool_request_guarded,
                    args=(request, generation),
                    name="helios-codex-router-tool",
                    daemon=True,
                ).start()
            except Exception:
                with self._router_turn_lock:
                    self._router_tool_requests.pop(id(request), None)
                self._router_tool_slots.release()
                _log.exception("could not start Helios Router dynamic-tool worker")
                request.respond_error(
                    -32000,
                    "Helios Router dynamic-tool worker is unavailable",
                )
            return
        GLib.idle_add(self._consume_app_request, request)

    def on_app_exit(self, returncode: int | None) -> None:
        # Set this before either queued GTK callback can win. The CAS and the
        # authoritative Future classification share a lock, so exit can never
        # downgrade a response already pinned as ACCEPTED or REJECTED.
        self._transition_native_delivery_state(
            NATIVE_DELIVERY_WIRE,
            NATIVE_DELIVERY_UNKNOWN,
        )
        GLib.idle_add(self._consume_app_exit, returncode)

    def _consume_app_exit(self, returncode: int | None) -> bool:
        if not self._native_mode or self._closed:
            return False
        self._cancel_budget_interrupt_watchdog()
        self._transition_native_delivery_state(
            NATIVE_DELIVERY_WIRE,
            NATIVE_DELIVERY_UNKNOWN,
        )
        self._hub = None
        self._busy = False
        self._invalidate_router_tool_requests()
        self._clear_manual_compaction_state()
        self._review_preparation = None
        self._review_cancel_requested = False
        self._thread_fork_pending = False
        self._thread_fork_stop_requested = False
        self._closed = True
        for token in self._drain_interactions(respond=False):
            self.emit("interaction-resolved", token)
        if self.execution_attempt_id:
            with self._native_delivery_lock:
                state = self._helios_native_delivery_state
                dispatch = self._active_turn_start_dispatch
            if not self._execution_dispatch_recorded:
                # Startup/Goal-local text never crossed the provider boundary.
                # App Server exit is therefore a typed local abort, not an
                # unresolved provider attempt.
                accounting_ok = self._finish_predispatch_attempt()
                self._pending_queue_delivery = None
            elif state == NATIVE_DELIVERY_REJECTED and dispatch is not None:
                # The transport callback pins an authoritative JSON-RPC
                # rejection before either GTK callback can run. If process
                # exit wins the GTK queue, settle that evidence here so the
                # later turn/start callback cannot leak the durable slot.
                accounting_ok = self._settle_verified_turn_start_rejection(dispatch)
            else:
                accounting_ok = self._record_execution_stop(
                    ExecutionStopEvidence(
                        acknowledgement={},
                        queue_disposition="held",
                    )
                )
                self.emit(
                    "error",
                    "Codex App Server exited without a provider-terminal receipt; "
                    "this Work remains blocked until provider-backed recovery "
                    "confirms the outcome.",
                )
            if not accounting_ok:
                self.emit(
                    "error",
                    "Helios could not persist the App Server exit outcome; "
                    "new provider work remains blocked.",
                )
        self.emit(
            "error",
            "Codex App Server exited unexpectedly"
            + (f" (code {returncode})" if returncode is not None else ""),
        )
        self.emit("exited", returncode if returncode is not None else -1)
        return False

    #: Notification methods this driver consumes outside the accumulator.
    #: The GTK-free registry owns the complete schema classification.
    _DRIVER_NOTIFICATION_METHODS = (
        codex_protocol.DRIVER_HANDLED_NOTIFICATION_METHODS
    )

    def _app_server_version(self) -> str:
        hub = self._hub
        try:
            return str(getattr(hub, "server_version", "") or "") if hub else ""
        except Exception:
            return ""

    def _note_unknown_notification(self, method: str) -> None:
        """Classify a notification and surface real protocol gaps.

        Generated-schema methods are either handled, deliberately ignored, or
        reported as a known unsupported capability. Only a method absent from
        the pinned schema is drift. Reports are bounded per driver and log
        lines per method so a high-volume notification cannot create a storm.
        """

        disposition = codex_protocol.classify_notification(method)
        if disposition in {"handled", "ignored"}:
            return
        if disposition == "unsupported":
            if method not in _UNSUPPORTED_NOTIFICATION_METHODS:
                _UNSUPPORTED_NOTIFICATION_METHODS.add(method)
                _log.warning(
                    "Codex App Server sent known but unsupported notification %r: %s",
                    method,
                    codex_protocol.unsupported_notification_message(method),
                )
            if not self._unsupported_notification_reported:
                self._unsupported_notification_reported = True
                self.emit(
                    "capability-drift",
                    {
                        "provider": "codex",
                        "version": self._app_server_version(),
                        "degraded": [
                            codex_protocol.unsupported_notification_message(method)
                            + f" ({method})"
                        ],
                    },
                )
            return
        # The process-wide record deduplicates the *log line* only: a second
        # driver still owes its own window a drift report.
        if method not in _UNKNOWN_NOTIFICATION_METHODS:
            _UNKNOWN_NOTIFICATION_METHODS.add(method)
            _log.warning(
                "Codex App Server sent an unhandled notification %r — this Helios "
                "build does not understand it; some detail may be missing",
                method,
            )
        if not self._unknown_notification_reported:
            self._unknown_notification_reported = True
            self.emit(
                "capability-drift",
                {
                    "provider": "codex",
                    "version": self._app_server_version(),
                    "degraded": [
                        f"an event this build does not understand ({method})"
                    ],
                },
            )

    def _note_unknown_server_request(self, method: str) -> None:
        """Report a server request absent from the pinned generated schema."""

        if method not in _UNKNOWN_SERVER_REQUEST_METHODS:
            _UNKNOWN_SERVER_REQUEST_METHODS.add(method)
            _log.warning(
                "Codex App Server sent an unknown server request %r",
                method,
            )
        if not self._unknown_request_reported:
            self._unknown_request_reported = True
            self.emit(
                "capability-drift",
                {
                    "provider": "codex",
                    "version": self._app_server_version(),
                    "degraded": [
                        f"a server request this build does not understand ({method})"
                    ],
                },
            )

    def _consume_manual_compaction_notification(
        self,
        method: str,
        params: Any,
    ) -> bool:
        """Consume the maintenance turn without touching execution state."""

        if not self._manual_compaction_pending or not isinstance(params, dict):
            return False
        thread_id = params.get("threadId")
        if (
            isinstance(thread_id, str)
            and thread_id
            and thread_id != self.session_id
        ):
            return False
        turn_id = _notification_turn_id(params)

        if method == app_events.METHOD_TURN_STARTED:
            if not turn_id:
                return False
            if (
                self._manual_compaction_turn_id
                and self._manual_compaction_turn_id != turn_id
            ):
                return False
            self._manual_compaction_turn_id = turn_id
            self._emit_compaction_activity("compacting", trigger="manual")
            if self._manual_compaction_interrupt_requested:
                self._request_manual_compaction_interrupt()
            return True

        if method in {
            app_events.METHOD_ITEM_STARTED,
            app_events.METHOD_ITEM_COMPLETED,
        }:
            if not turn_id:
                return False
            item = params.get("item")
            if (
                not self._manual_compaction_turn_id
                and isinstance(item, dict)
                and item.get("type") == "contextCompaction"
            ):
                # A missed/delayed turn/started must not strand the maintenance
                # latch when the typed item itself proves its owning turn.
                self._manual_compaction_turn_id = turn_id
            if turn_id != self._manual_compaction_turn_id:
                return False
            if not isinstance(item, dict):
                return True
            if item.get("type") == "contextCompaction":
                item_id = item.get("id")
                if isinstance(item_id, str):
                    self._manual_compaction_item_id = item_id
                if method == app_events.METHOD_ITEM_COMPLETED:
                    self._manual_compaction_item_completed = True
                self._emit_compaction_activity("compacting", trigger="manual")
            # A maintenance turn has no user contribution or model output.
            # Skip every item in its scope so a future provider-side metadata
            # item cannot accidentally enter the active assistant stream.
            return True

        if method == app_events.METHOD_TURN_COMPLETED:
            if not turn_id:
                return False
            if not self._manual_compaction_turn_id:
                self._manual_compaction_turn_id = turn_id
            if turn_id != self._manual_compaction_turn_id:
                return False
            turn = params.get("turn")
            items = turn.get("items") if isinstance(turn, dict) else None
            if isinstance(items, list):
                compacted = next(
                    (
                        item
                        for item in items
                        if isinstance(item, dict)
                        and item.get("type") == "contextCompaction"
                    ),
                    None,
                )
                if compacted is not None:
                    item_id = compacted.get("id")
                    if isinstance(item_id, str):
                        self._manual_compaction_item_id = item_id
                    self._manual_compaction_item_completed = True
            status = (
                str(turn.get("status") or "")
                if isinstance(turn, dict)
                else ""
            )
            self._finish_manual_compaction_turn(status or "failed")
            return True
        if (
            turn_id
            and turn_id == self._manual_compaction_turn_id
            and method != app_events.METHOD_THREAD_TOKEN_USAGE
        ):
            # Future App Server versions may add turn-scoped metadata around
            # compaction. Keep the whole maintenance scope out of model/plan/
            # tool projection by default. A typed error remains user-visible,
            # while token usage is the one event deliberately allowed through
            # so the context gauge and aggregate breaker stay authoritative.
            if method == app_events.METHOD_ERROR:
                error = params.get("error")
                message = (
                    error.get("message")
                    if isinstance(error, dict)
                    else ""
                )
                if message and not self._manual_compaction_interrupt_requested:
                    self.emit("error", str(message))
            return True
        return False

    def _consume_legacy_context_compacted(self, params: Any) -> None:
        """Accept 0.152's deprecated boundary notification as a fallback."""

        if not isinstance(params, dict):
            return
        thread_id = params.get("threadId")
        if (
            isinstance(thread_id, str)
            and thread_id
            and thread_id != self.session_id
        ):
            return
        turn_id = _notification_turn_id(params)
        if not turn_id:
            return
        if (
            self._manual_compaction_pending
            and turn_id
            and (
                not self._manual_compaction_turn_id
                or turn_id == self._manual_compaction_turn_id
            )
        ):
            self._manual_compaction_turn_id = turn_id
            self._manual_compaction_item_completed = True
            return
        self._record_context_compaction(
            trigger="auto",
            turn_id=turn_id,
            item_id="",
            pre_tokens=self._last_context_used,
            post_tokens=0,
        )

    def _finish_manual_compaction_turn(self, status: str) -> None:
        pre_tokens = self._manual_compaction_pre_tokens
        post_tokens = self._last_context_used
        turn_id = self._manual_compaction_turn_id
        item_id = self._manual_compaction_item_id
        item_completed = self._manual_compaction_item_completed
        was_stopped = (
            self._manual_compaction_interrupt_requested
            or status == "interrupted"
        )

        self._clear_manual_compaction_state()
        self._busy = False
        if self._budget_interrupt_required:
            self._cancel_budget_interrupt_watchdog()
        self._emit_compaction_activity("idle", trigger="manual")

        if item_completed:
            self._record_context_compaction(
                trigger="manual",
                turn_id=turn_id,
                item_id=item_id,
                pre_tokens=pre_tokens,
                post_tokens=post_tokens,
            )
        elif was_stopped:
            self.emit("error", "Context compaction was stopped.")
        else:
            self.emit(
                "error",
                "Codex ended the compaction turn without a completed "
                f"contextCompaction item ({status}).",
            )

        if self._close_after_turn:
            self._finalize_native(0 if item_completed else 1)
        elif not self._token_budget_exhausted:
            self._flush_user_queue()

    def _record_context_compaction(
        self,
        *,
        trigger: str,
        turn_id: str,
        item_id: str,
        pre_tokens: int,
        post_tokens: int,
    ) -> None:
        """Persist and emit one deduplicated provider-owned boundary."""

        if not turn_id and not item_id:
            return
        key = f"turn:{turn_id}" if turn_id else f"item:{item_id}"
        if key in self._recorded_compactions:
            return
        if len(self._recorded_compactions) >= 128:
            self._recorded_compactions.clear()
        self._recorded_compactions.add(key)

        try:
            pre = max(0, int(pre_tokens or 0))
        except (TypeError, ValueError):
            pre = 0
        try:
            post = max(0, int(post_tokens or 0))
        except (TypeError, ValueError):
            post = 0
        if pre and post == pre:
            # No provider update arrived between start and completion; do not
            # claim the pre-compaction figure is also the result.
            post = 0
        append_boundary = getattr(self._mirror, "append_compaction", None)
        if callable(append_boundary):
            append_boundary(
                trigger=trigger,
                pre_tokens=pre,
                post_tokens=post,
                turn_id=turn_id,
                item_id=item_id,
            )
        self.emit(
            "context-compacted",
            {
                "trigger": "manual" if trigger == "manual" else "auto",
                "pre_tokens": pre,
                "post_tokens": post,
                "turn_id": turn_id,
                "item_id": item_id,
                "source": "codex-app-server",
            },
        )

    def _consume_app_notification(self, method: str, params: Any) -> bool:
        if self._closed or not self._native_mode:
            return False
        self.last_activity = time.monotonic()
        self._note_unknown_notification(method)
        if method in _PROVIDER_NOTICE_METHODS:
            notice = _provider_notice_payload(
                method,
                params if isinstance(params, dict) else {},
            )
            if notice is not None:
                self.emit("provider-notice", notice)
            else:
                _log.warning(
                    "Codex App Server sent malformed %s notification",
                    method,
                )
            return False
        if method == _METHOD_THREAD_COMPACTED:
            self._consume_legacy_context_compacted(params)
            return False
        if method == _METHOD_SERVER_REQUEST_RESOLVED:
            self._resolve_interaction_by_request_id(
                params.get("requestId") if isinstance(params, dict) else None
            )
            return False
        if method == _METHOD_MCP_STATUS and isinstance(params, dict):
            self._update_mcp_status(params)
        elif method in {_METHOD_THREAD_GOAL_UPDATED, _METHOD_THREAD_GOAL_CLEARED}:
            goal_thread_id = (
                str(params.get("threadId") or "") if isinstance(params, dict) else ""
            )
            if not goal_thread_id or goal_thread_id == self.session_id:
                self._native_goal_reconcile_error = ""
                self._native_goal_synced = method == _METHOD_THREAD_GOAL_UPDATED
                goal_payload = copy.deepcopy(params) if isinstance(params, dict) else {}
                goal_payload["cleared"] = method == _METHOD_THREAD_GOAL_CLEARED
                self.emit("native-goal-updated", goal_payload)
        elif method == _METHOD_THREAD_STARTED and isinstance(params, dict):
            self._capture_child_thread(params)

        # The root accumulator intentionally ignores notifications from other
        # thread IDs.  Usage is the exception: the hub routes child-thread
        # notifications to their owning root driver, and their counters must
        # contribute to the same lifetime containment budget.
        if method == app_events.METHOD_THREAD_TOKEN_USAGE and isinstance(params, dict):
            self._account_family_token_usage(params)

        # Native manual compaction owns a dedicated App Server maintenance
        # turn. Consume it before the user-turn accumulator and stale-attempt
        # fence so it cannot create a model contribution, mutate the durable
        # execution plan, or settle an execution attempt it never admitted.
        if self._consume_manual_compaction_notification(method, params):
            return False

        if method == app_events.METHOD_TURN_COMPLETED and isinstance(params, dict):
            turn = params.get("turn") or {}
            turn_id = (
                turn.get("id") if isinstance(turn, dict) else params.get("turnId")
            ) or params.get("turnId") or ""
            if not self._terminal_turn_matches_current_attempt(str(turn_id or "")):
                _log.warning(
                    "ignored stale Codex terminal notification for turn %s",
                    turn_id or "<unknown>",
                )
                return False

        for action in self._app_acc.feed_notification(
            method,
            params if isinstance(params, dict) else {},
        ):
            self._dispatch_app_action(action)
        return False

    # -- event dispatch ----------------------------------------------

    def _dispatch_app_action(self, action) -> None:
        kind, payload = action.kind, action.payload
        if kind == app_events.ACT_STREAMING:
            self.emit("assistant-streaming", payload)
        elif kind == app_events.ACT_TURN:
            self._pending_mirror_turn = payload
            if self.execution_attempt_id and not self._record_execution_contribution(
                payload
            ):
                self.emit(
                    "error",
                    "Helios could not persist Codex's contribution; new provider "
                    "work remains blocked.",
                )
            self.emit("turn-appended", payload)
        elif kind == app_events.ACT_RESULT:
            terminal_turn_id = str(
                payload.get("turnId")
                or self._app_acc.turn_id
                or self._app_turn_id
                or ""
            )
            if not self._terminal_turn_matches_current_attempt(terminal_turn_id):
                _log.warning(
                    "ignored stale Codex result for turn %s",
                    terminal_turn_id or "<unknown>",
                )
                return
            # A terminal notification can precede the JSON-RPC response for
            # this same turn. Retire its callback identity before releasing
            # the slot or auto-sending a queued successor.
            self._retire_turn_start_dispatch()
            budget_exceeded = _is_session_budget_exceeded(payload)
            provider_status = str(payload.get("subtype") or "unknown")
            if (
                self.execution_attempt_id
                and terminal_turn_id
                and not self._app_acceptance_recorded
            ):
                # Retry the idempotent acceptance write at the terminal seam.
                # A transient storage failure on the transport callback must
                # not leave a valid terminal receipt permanently unusable.
                self._app_acceptance_recorded = self._record_execution_acceptance(
                    ExecutionAcceptanceEvidence(
                        accepted_turn_id=terminal_turn_id,
                        provider_request_key=self.execution_attempt_id,
                        native_binding_id=self.session_id,
                    )
                )
            if budget_exceeded:
                terminal_status = "budgetLimited"
            elif provider_status == "success":
                terminal_status = "completed"
            elif provider_status in {"aborted", "interrupted", "cancelled"}:
                terminal_status = "aborted"
            else:
                terminal_status = "failed"
            accounting_ok = self._finish_execution_with_evidence(
                ExecutionTerminalEvidence(
                    evidence_type="provider_terminal",
                    status=terminal_status,
                    reason_code="codex.provider_terminal",
                    provider_status=provider_status,
                    request_id=self.execution_attempt_id,
                    turn_id=terminal_turn_id,
                    native_id=self._confirmed_execution_native_id(),
                    usage=_terminal_usage_receipt(payload),
                    queue_disposition=(
                        "restored"
                        if terminal_status == "budgetLimited"
                        else "released"
                    ),
                )
            )
            if not accounting_ok:
                self._busy = False
                self.emit(
                    "error",
                    "Helios could not persist execution completion; "
                    "the result was withheld and new provider work remains blocked.",
                )
                self._finalize_native(1)
                return
            if budget_exceeded:
                self._mark_token_budget_exhausted(interrupt_active=False)
            self._busy = False
            self._cancel_budget_interrupt_watchdog()
            self._stop_requested = False
            self._interrupt_when_turn_known = False
            self._app_turn_id = ""
            # The accepted prompt has reached a terminal result. A subsequent
            # idle admission owns a new delivery state and must not inherit it.
            self._set_native_delivery_state(NATIVE_DELIVERY_IDLE)
            self._invalidate_router_tool_requests()
            if self._pending_mirror_turn is not None:
                self._mirror.append_assistant(
                    self._pending_mirror_turn,
                    model=self._model,
                    result=payload,
                    turn_id=terminal_turn_id,
                )
                self._pending_mirror_turn = None
            self.emit("result", payload)
            if not self._token_budget_exhausted:
                self._flush_user_queue()
            if self._close_after_turn and not self._busy:
                self._finalize_native(0)
        elif kind == app_events.ACT_ERROR:
            if not self._stop_requested and not self._token_budget_exhausted:
                self.emit("error", payload)
        elif kind == app_events.ACT_USAGE_UPDATED:
            used = int(payload.get("usedTokens") or 0)
            window = int(payload.get("contextWindow") or 0)
            self._last_context_used = max(0, used)
            if not window:
                window = model_catalog.context_window_for(self._model)
            if used > 0 and window > 0:
                self.emit("usage-updated", used, window)
        elif kind == app_events.ACT_RATE_LIMIT_UPDATED:
            self._emit_rate_limit_snapshot(payload.get("rateLimits") or {})
        elif kind == app_events.ACT_TURN_STATUS:
            if _is_session_budget_exceeded(payload):
                # App Server can enforce its native tokenBudget before a final
                # usage notification arrives.  Latch this terminal condition
                # before ACT_RESULT gets a chance to flush queued work.
                self._mark_token_budget_exhausted(interrupt_active=False)
            status = str(payload.get("status") or "")
            if status == "inProgress":
                turn_id = str(payload.get("turnId") or "")
                with self._native_delivery_lock:
                    active_dispatch = self._active_turn_start_dispatch
                if (
                    active_dispatch is not None
                    and not active_dispatch.accept_started_notification
                ):
                    # Review emits a reviewer-internal turn/started identity;
                    # review/start's response is the authoritative source-turn
                    # correlation used for admission, Stop, terminality, and
                    # the visible plan/agent scope. Do not publish the internal
                    # identity; the validated response does that explicitly.
                    return
                dispatch = self._bind_turn_to_current_dispatch(turn_id)
                if dispatch is None:
                    _log.warning(
                        "ignored stale Codex turn-start notification for %s",
                        turn_id or "<unknown>",
                    )
                    return
                acceptance_recorded = self._record_dispatch_acceptance(
                    dispatch,
                    turn_id,
                )
                self._promote_turn_start_dispatch(
                    dispatch,
                    turn_id,
                    acceptance_recorded=acceptance_recorded,
                )
            self.emit("turn-status-updated", payload)
        elif kind == app_events.ACT_PLAN_UPDATED:
            self.emit("plan-updated", payload)
        elif kind == app_events.ACT_DIFF_UPDATED:
            self.emit("diff-updated", payload)
        elif kind == app_events.ACT_SUBAGENT_ACTIVITY:
            if self._apply_subagent_activity(payload):
                self.emit("activity-updated", payload)
        elif kind == app_events.ACT_MCP_ACTIVITY:
            self.emit("activity-updated", payload)
        elif kind == app_events.ACT_ACTIVITY:
            self.emit("activity-updated", payload)
        elif kind == app_events.ACT_CONTEXT_COMPACTED:
            self._dispatch_context_compaction_action(payload)

    def _dispatch_context_compaction_action(self, payload: dict[str, Any]) -> None:
        """Project one automatic provider-owned compaction item."""

        item_id = str(payload.get("itemId") or "")
        turn_id = str(payload.get("turnId") or "")
        lifecycle = str(payload.get("lifecycle") or "")
        if not item_id:
            return
        if lifecycle == "started":
            if len(self._compaction_pre_tokens_by_item) >= 64:
                self._compaction_pre_tokens_by_item.clear()
            self._compaction_pre_tokens_by_item[item_id] = self._last_context_used
            self.emit(
                "activity-updated",
                {
                    **payload,
                    "itemType": "contextCompaction",
                    "category": "phase",
                    "phase": "compacting",
                    "trigger": "auto",
                },
            )
            return
        if lifecycle != "completed":
            return
        pre_tokens = self._compaction_pre_tokens_by_item.pop(
            item_id,
            self._last_context_used,
        )
        self._record_context_compaction(
            trigger="auto",
            turn_id=turn_id,
            item_id=item_id,
            pre_tokens=pre_tokens,
            post_tokens=self._last_context_used,
        )
        # The surrounding user turn remains active after automatic
        # compaction. Move out of the compacting phase without claiming the
        # turn is complete; a later native activity item will refine it.
        self.emit(
            "activity-updated",
            {
                **payload,
                "itemType": "contextCompaction",
                "category": "phase",
                "phase": "requesting",
                "trigger": "auto",
            },
        )

    def _account_family_token_usage(self, params: dict[str, Any]) -> None:
        """Apply one cumulative per-thread counter to the family total."""

        token_usage = params.get("tokenUsage")
        total = token_usage.get("total") if isinstance(token_usage, dict) else None
        try:
            lifetime = max(
                0,
                int(total.get("totalTokens") or 0) if isinstance(total, dict) else 0,
            )
        except (TypeError, ValueError):
            return
        thread_id = str(params.get("threadId") or self.session_id or "")
        if not thread_id or lifetime <= 0:
            return
        previous = self._lifetime_tokens_by_thread.get(thread_id, 0)
        if lifetime <= previous:
            return
        self._lifetime_tokens_by_thread[thread_id] = lifetime
        self._lifetime_tokens_used = sum(self._lifetime_tokens_by_thread.values())
        # None = subscription billing: the family total is still tracked and
        # reported, it just does not terminate the Work. See default_token_budget.
        if (
            self._token_budget is not None
            and self._lifetime_tokens_used >= self._token_budget
        ):
            self._mark_token_budget_exhausted(interrupt_active=True)

    def _mark_token_budget_exhausted(self, *, interrupt_active: bool) -> None:
        """Latch the terminal family budget once and stop further dispatch."""

        if self._token_budget_exhausted:
            return
        self._token_budget_exhausted = True
        # `None` reaches here only from a provider-reported sessionBudgetExceeded,
        # so there is no Helios limit to round the counter up to.
        if self._token_budget is not None:
            self._lifetime_tokens_used = max(
                self._lifetime_tokens_used,
                self._token_budget,
            )
        if interrupt_active and self._busy:
            self._budget_interrupt_required = True
            self._arm_budget_interrupt_watchdog()
        self.emit(
            "budget-exhausted",
            {
                "kind": "tokens",
                "limit": self._token_budget,
                "provider": self.provider,
            },
        )
        self.emit(
            "error",
            (
                f"Codex reached this session family's "
                f"{self._token_budget:,}-token safety limit. "
                "The active turn was interrupted and queued messages were kept."
                if self._token_budget is not None
                else "Codex reported that this account's session budget is "
                "exhausted. The active turn was interrupted and queued "
                "messages were kept."
            ),
        )
        if not interrupt_active or self._budget_interrupt_escalated:
            return
        if self._manual_compaction_pending:
            self._manual_compaction_interrupt_requested = True
            self._request_manual_compaction_interrupt()
            return
        if self._app_turn_id:
            self._request_interrupt()
        elif self._busy:
            self._interrupt_when_turn_known = True

    def _begin_native_turn(self, turn_id: str) -> None:
        """Reset turn-scoped agent state exactly once for each native turn."""

        if not turn_id or turn_id == self._agents_turn_id:
            return
        self._agents_turn_id = turn_id
        self._agents.clear()
        self.emit("agents-updated", {})

    @property
    def observed_agent_root_turn_id(self) -> str:
        """Exact root turn owning the current in-memory agent observations."""

        return self._agents_turn_id

    def observed_agent_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a defensive copy for foreground rebind projection."""

        return copy.deepcopy(self._agents)

    # -- approvals and request_user_input ----------------------------

    def _retire_router_tool_requests_locked(
        self,
        *,
        suppress_authorization: bool,
    ) -> tuple[ServerRequest, ...]:
        self._router_authorized_turn_id = ""
        self._router_tool_generation += 1
        self._router_authorization_suppressed = suppress_authorization
        requests = tuple(self._router_tool_requests.values())
        self._router_tool_requests.clear()
        return requests

    @staticmethod
    def _reject_router_tool_requests(
        requests: tuple[ServerRequest, ...],
    ) -> None:
        for request in requests:
            try:
                request.respond_error(
                    -32000,
                    "Helios session or router turn is no longer active",
                )
            except Exception:
                # A broken shared transport must not strand the driver in an
                # uncloseable state or prevent the remaining requests from
                # being retired.
                _log.warning(
                    "could not retire Helios Router dynamic-tool request",
                    exc_info=True,
                )

    def _invalidate_router_tool_requests(
        self,
        *,
        suppress_authorization: bool = True,
    ) -> int:
        with self._router_turn_lock:
            requests = self._retire_router_tool_requests_locked(
                suppress_authorization=suppress_authorization
            )
            generation = self._router_tool_generation
        # ``respond_error`` atomically claims the transport request before its
        # bounded write.  If a worker response already won the transport race,
        # this is a no-op; otherwise Codex is resolved without stale content.
        self._reject_router_tool_requests(requests)
        return generation

    def _run_dynamic_tool_request_guarded(
        self,
        request: ServerRequest,
        generation: int,
    ) -> None:
        try:
            self._run_dynamic_tool_request(request, generation=generation)
        finally:
            with self._router_turn_lock:
                self._router_tool_requests.pop(id(request), None)
            self._router_tool_slots.release()

    def _run_dynamic_tool_request(
        self,
        request: ServerRequest,
        *,
        generation: int | None = None,
    ) -> None:
        """Execute one thread-bound Helios tool through the credential broker."""

        params = request.params if isinstance(request.params, dict) else {}
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        call_id = params.get("callId")
        namespace = params.get("namespace")
        tool = params.get("tool")
        arguments = params.get("arguments")
        with self._router_turn_lock:
            authorized_turn_id = self._router_authorized_turn_id
            current_generation = self._router_tool_generation
        if generation is None:
            generation = current_generation

        if (
            self._closed
            or not self._native_mode
            or generation != current_generation
            or thread_id != self.session_id
            or not isinstance(turn_id, str)
            or not turn_id
            or turn_id != authorized_turn_id
            or not isinstance(call_id, str)
            or not call_id
            or namespace != CODEX_NAMESPACE
            or tool not in tool_names()
            or not isinstance(arguments, dict)
        ):
            request.respond_error(
                -32602,
                "Invalid or unbound Helios dynamic tool request",
            )
            return

        binding = {
            "kind": "codex",
            "thread_id": thread_id,
            "turn_id": turn_id,
            "call_id": call_id,
        }
        try:
            result = RouterClient().call(tool, arguments, binding=binding)
        except RouterRejectedError as exc:
            # A policy refusal is a valid tool outcome: tell the primary model
            # to retain the task instead of making Codex retry the transport.
            result = {
                "contract_version": CONTRACT_VERSION,
                "accepted": False,
                "status": "rejected",
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "data": exc.data,
                },
            }
            success = True
        except RouterError as exc:
            result = {
                "contract_version": CONTRACT_VERSION,
                "accepted": False,
                "status": "unavailable",
                "error": {
                    "code": "ROUTER_UNAVAILABLE",
                    "message": str(exc),
                },
            }
            success = False
        except Exception:
            _log.exception("unexpected Helios Router dynamic-tool failure")
            result = {
                "contract_version": CONTRACT_VERSION,
                "accepted": False,
                "status": "failed",
                "error": {
                    "code": "ROUTER_INTERNAL_ERROR",
                    "message": "Helios Router failed unexpectedly",
                },
            }
            success = False
        else:
            success = True

        response = {
            "contentItems": [
                {
                    "type": "inputText",
                    "text": json.dumps(
                        result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            ],
            "success": success,
        }
        with self._router_turn_lock:
            response_is_current = (
                not self._closed
                and self._native_mode
                and generation == self._router_tool_generation
                and turn_id == self._router_authorized_turn_id
            )
        if response_is_current:
            # ``respond`` and lifecycle invalidation's ``respond_error`` compete
            # on the transport's exactly-once claim, so no lifecycle lock is
            # held across the potentially blocking pipe write.
            request.respond(response)
        else:
            request.respond_error(
                -32000,
                "Helios session or router turn is no longer active",
            )

    def _consume_app_request(self, request: ServerRequest) -> bool:
        if self._closed or not self._native_mode:
            request.respond_error(-32000, "Helios session is no longer active")
            return False
        if request.id in self._resolved_before_delivery:
            self._resolved_before_delivery.discard(request.id)
            request.abandon()
            return False
        self._answered_interaction_ids.discard(request.id)
        method = request.method
        params = request.params if isinstance(request.params, dict) else {}
        disposition = codex_protocol.classify_server_request(method)
        if disposition == "unknown":
            self._note_unknown_server_request(method)
            request.respond_error(-32601, f"Helios does not support {method}")
            return False
        if disposition == "denied":
            if method == MCP_ELICITATION_METHOD:
                request.respond({"action": "decline"})
            else:
                reason = codex_protocol.DENIED_SERVER_REQUEST_METHODS[method]
                request.respond_error(-32000, f"Helios denied {method}: {reason}")
            return False
        if method == "currentTime/read":
            request.respond({"currentTimeAt": int(time.time())})
            return False
        if method in APPROVAL_METHODS | {PERMISSIONS_METHOD}:
            # Non-interactive modes must not open a dialog if the server
            # unexpectedly asks. Bypass already grants native full access;
            # decline unexpected approvals instead of widening server policy.
            if (
                self._workflow_mode == PLAN_WORKFLOW_MODE
                or self._permission_mode in {"plan", "dontAsk", "bypassPermissions"}
            ):
                request.respond(interaction_response(method, None, params))
                return False
        if method not in APPROVAL_METHODS | {USER_INPUT_METHOD, PERMISSIONS_METHOD}:
            # A supported method reaching this GTK path means the registry and
            # dispatcher moved apart. Fail closed without calling it drift.
            request.respond_error(-32601, f"Helios does not support {method}")
            return False

        self._interaction_seq += 1
        token = (
            f"codex:{self.session_id}:{type(request.id).__name__}:"
            f"{request.id}:{self._interaction_seq}"
        )
        self._pending_interactions[token] = (request, method)
        self._interaction_token_by_id[request.id] = token
        payload = (
            approval_question(method, params)
            if method in APPROVAL_METHODS | {PERMISSIONS_METHOD}
            else copy.deepcopy(params)
        )
        payload["_heliosInteraction"] = {
            "provider": "openai",
            "method": method,
            "requestId": request.id,
        }
        self.emit("question-asked", payload, token)
        return False

    def answer_question(self, tool_use_id: str, answer: object | None) -> None:
        if not self._native_mode:
            super().answer_question(
                tool_use_id, answer if isinstance(answer, str) else None
            )
            return
        pending = self._pending_interactions.pop(tool_use_id, None)
        if pending is None:
            return
        request, method = pending
        self._interaction_token_by_id.pop(request.id, None)
        try:
            if request.respond(interaction_response(method, answer, request.params)):
                self._answered_interaction_ids.add(request.id)
                if len(self._answered_interaction_ids) > 512:
                    self._answered_interaction_ids.clear()
        except Exception as exc:
            self.emit("error", f"Could not answer Codex interaction: {exc}")

    def _resolve_interaction_by_request_id(self, request_id: object) -> None:
        if not isinstance(request_id, (int, str)) or isinstance(request_id, bool):
            return
        token = self._interaction_token_by_id.pop(request_id, None)
        if token is None:
            if request_id in self._answered_interaction_ids:
                self._answered_interaction_ids.discard(request_id)
            else:
                self._resolved_before_delivery.add(request_id)
                if len(self._resolved_before_delivery) > 512:
                    self._resolved_before_delivery.clear()
            return
        pending = self._pending_interactions.pop(token, None)
        if pending is not None:
            request, _method = pending
            request.abandon()
        self.emit("interaction-resolved", token)

    def _drain_interactions(self, *, respond: bool) -> list[str]:
        pending = list(self._pending_interactions.items())
        self._pending_interactions.clear()
        self._interaction_token_by_id.clear()
        self._resolved_before_delivery.clear()
        self._answered_interaction_ids.clear()
        for _token, (request, method) in pending:
            if not respond:
                continue
            try:
                request.respond(interaction_response(method, None, request.params))
            except Exception:
                pass
        return [token for token, _pending in pending]

    # -- goals -------------------------------------------------------

    def sync_goal(self, goal: Any) -> bool:
        if not self._native_mode or self._hub is None or not self.session_id:
            return False
        objective = str(getattr(goal, "objective", "") or "").strip()
        status = str(getattr(goal, "status", "active") or "active")
        if status not in _GOAL_STATUSES:
            status = (
                "blocked" if status in {"usageLimited", "budgetLimited"} else "active"
            )
        if not objective:
            return self.clear_native_goal()
        if len(objective) > session_goals.MAX_GOAL_OBJECTIVE_CHARS:
            # Preserve the full provider-neutral objective in the Work prompt
            # envelope. Clearing any old native goal is safer than truncating
            # and then claiming the two representations are synchronized.
            self._native_goal_synced = False
            _log.warning(
                "goal exceeds Codex native limit (%d > %d); using Work envelope",
                len(objective),
                session_goals.MAX_GOAL_OBJECTIVE_CHARS,
            )
            return self.clear_native_goal()
        goal_params: dict[str, Any] = {
            "threadId": self.session_id,
            "objective": objective,
            "status": status,
        }
        # Omitted entirely on subscription billing: App Server enforces this
        # key itself, so sending it would reinstate the terminal cap that
        # default_token_budget just declined to apply.
        if self._token_budget is not None:
            goal_params["tokenBudget"] = self._token_budget
        try:
            future = self._hub.request(
                "thread/goal/set",
                goal_params,
                timeout=10.0,
            )
        except Exception as exc:
            self._native_goal_synced = False
            self._native_goal_reconcile_error = (
                f"Could not set Codex native goal: {exc}. Retry the Goal change "
                "or reopen this GPT session before sending more work."
            )
            _log.warning("could not mirror Helios goal into Codex: %s", exc)
            return False
        self._native_goal_reconcile_error = ""
        self._native_goal_pending = True
        self._goal_request_generation += 1
        generation = self._goal_request_generation
        future.add_done_callback(
            lambda done: GLib.idle_add(
                self._finish_goal_request,
                done,
                True,
                generation,
            )
        )
        return True

    def clear_native_goal(self) -> bool:
        if not self._native_mode or self._hub is None or not self.session_id:
            return False
        try:
            future = self._hub.request(
                "thread/goal/clear",
                {"threadId": self.session_id},
                timeout=10.0,
            )
        except Exception as exc:
            self._native_goal_synced = False
            self._native_goal_reconcile_error = (
                f"Could not clear Codex native goal: {exc}. Retry clearing the "
                "Goal or reopen this GPT session before sending more work."
            )
            _log.warning("could not clear Codex native goal: %s", exc)
            return False
        self._native_goal_reconcile_error = ""
        self._native_goal_pending = True
        self._goal_request_generation += 1
        generation = self._goal_request_generation
        future.add_done_callback(
            lambda done: GLib.idle_add(
                self._finish_goal_request,
                done,
                False,
                generation,
            )
        )
        return True

    def _finish_goal_request(
        self,
        future: Future[Any],
        setting_goal: bool,
        generation: int,
    ) -> bool:
        if generation != self._goal_request_generation:
            return False
        success = True
        error_message = ""
        try:
            future.result()
        except Exception as exc:
            success = False
            action = "set" if setting_goal else "clear"
            _log.warning("could not %s Codex native goal: %s", action, exc)
            error_message = f"Could not {action} Codex native goal: {exc}"
        self._native_goal_pending = False
        if success:
            self._native_goal_reconcile_error = ""
            self._native_goal_synced = setting_goal
        else:
            self._native_goal_synced = False
            self._native_goal_reconcile_error = (
                f"{error_message}. Retry the Goal change or reopen this GPT "
                "session before sending more work."
            )
        pending, self._goal_pending_text = self._goal_pending_text, None
        if pending:
            self._busy = False
        if not success:
            # The old native goal may still be active. Do not send a prompt
            # under divergent canonical/native objectives; return it through
            # the normal pre-acceptance error path for explicit recovery.
            accounting_ok = self._finish_predispatch_attempt()
            if not self._closed:
                if not accounting_ok:
                    self.emit(
                        "error",
                        "Helios could not persist execution completion; "
                        "new provider work remains blocked.",
                    )
                self.emit("error", error_message)
            return False
        if pending and not self._closed:
            self.send_user_text(pending, _reuse_execution_attempt=True)
            self._finish_deferred_attempt_if_unsent(
                "Codex Goal-delayed prompt was rejected before turn dispatch"
            )
        elif (
            not self._closed
            and self._pending_queue_delivery is not None
            and self._user_queue[:1] == [self._pending_queue_delivery]
        ):
            self._pending_queue_delivery = None
            self._flush_user_queue()
        return False

    # -- account, MCP, and agent snapshots --------------------------

    def _load_native_account_state(self) -> None:
        if self._hub is None:
            return
        try:
            future = self._hub.request("account/rateLimits/read", {}, timeout=15.0)
            future.add_done_callback(
                lambda done: GLib.idle_add(self._apply_initial_rate_limits, done)
            )
        except Exception as exc:
            _log.debug("could not request Codex rate limits: %s", exc)

    def _apply_initial_rate_limits(self, future: Future[Any]) -> bool:
        try:
            result = future.result()
        except Exception:
            return False
        if not isinstance(result, dict):
            return False
        buckets = result.get("rateLimitsByLimitId")
        if isinstance(buckets, dict) and buckets:
            for snapshot in buckets.values():
                if isinstance(snapshot, dict):
                    self._emit_rate_limit_snapshot(snapshot)
        elif isinstance(result.get("rateLimits"), dict):
            self._emit_rate_limit_snapshot(result["rateLimits"])
        return False

    def _emit_rate_limit_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            return
        limit_id = str(snapshot.get("limitId") or "codex")
        merged = _merge_non_null(self._rate_snapshots.get(limit_id, {}), snapshot)
        self._rate_snapshots[limit_id] = merged
        for row in rate_limit_rows(merged):
            self.emit("rate-limit-updated", row)

    def _load_native_mcp_state(self) -> None:
        self._mcp_by_name = {}
        self._request_mcp_page(None)

    def _request_mcp_page(self, cursor: str | None) -> None:
        if self._hub is None:
            return
        params: dict[str, Any] = {
            "threadId": self.session_id,
            "detail": "toolsAndAuthOnly",
            "limit": 100,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            future = self._hub.request(
                "mcpServerStatus/list",
                params,
                timeout=20.0,
            )
            future.add_done_callback(
                lambda done: GLib.idle_add(self._apply_mcp_list, done)
            )
        except Exception as exc:
            _log.debug("could not request Codex MCP status: %s", exc)

    def _apply_mcp_list(self, future: Future[Any]) -> bool:
        try:
            result = future.result()
        except Exception:
            return False
        rows = result.get("data") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            return False
        for row in rows:
            if isinstance(row, dict) and row.get("name"):
                name = str(row["name"])
                self._mcp_by_name[name] = _merge_non_null(
                    self._mcp_by_name.get(name, {}),
                    row,
                )
        next_cursor = result.get("nextCursor")
        if isinstance(next_cursor, str) and next_cursor:
            self._request_mcp_page(next_cursor)
            return False
        self._publish_mcp_snapshot()
        return False

    def _update_mcp_status(self, params: dict[str, Any]) -> None:
        name = str(params.get("name") or "")
        if not name:
            return
        current = self._mcp_by_name.setdefault(name, {"name": name})
        current.update(copy.deepcopy(params))
        self._publish_mcp_snapshot()

    def _publish_mcp_snapshot(self) -> None:
        self._init_mcp_servers = [
            copy.deepcopy(self._mcp_by_name[name]) for name in sorted(self._mcp_by_name)
        ]
        try:
            codex_env.save_mcp_snapshot(self._init_mcp_servers)
        except Exception:
            pass
        self.emit("mcp-status-updated", copy.deepcopy(self._init_mcp_servers))

    def _capture_child_thread(self, params: dict[str, Any]) -> None:
        thread = params.get("thread")
        if not isinstance(thread, dict):
            return
        # Parent-thread identity alone cannot place a child in a root turn.
        # A delayed ``thread/started`` could otherwise repaint a newer Agent
        # Dock. The hub may still use an unscoped notification for routing;
        # the UI waits for turn-correlated lifecycle evidence.
        turn_id = str(params.get("turnId") or thread.get("turnId") or "")
        if not turn_id or turn_id != self._agents_turn_id:
            return
        parent = str(thread.get("parentThreadId") or "")
        child = str(thread.get("id") or "")
        if parent != self.session_id or not child:
            return
        self._merge_observed_agent_state(
            child,
            {
                "threadId": child,
                "parentThreadId": parent,
                "name": (
                    thread.get("agentNickname")
                    or thread.get("agentRole")
                    or child[:8]
                ),
                "role": thread.get("agentRole") or "subagent",
                "status": "running",
            },
        )
        self.emit("agents-updated", copy.deepcopy(self._agents))

    def _merge_observed_agent_state(
        self,
        actor_id: str,
        update: dict[str, Any],
    ) -> None:
        """Retain provider metadata without resurrecting a terminal actor."""

        current = self._agents.get(actor_id)
        if current is None:
            seeded = {
                "threadId": actor_id,
                "name": actor_id[:8],
                "role": "subagent",
            }
            seeded.update(copy.deepcopy(update))
            self._agents[actor_id] = seeded
            return

        incoming = copy.deepcopy(update)
        current_status = normalize_agent_status(
            current.get("status") or current.get("kind") or current.get("phase")
        )
        incoming_status = normalize_agent_status(
            incoming.get("status") or incoming.get("kind") or incoming.get("phase")
        )
        if (
            current_status in TERMINAL_AGENT_STATUSES
            and incoming_status is not current_status
        ):
            # A conflicting terminal event may enrich only stable identity.
            # Status-coupled explanation from that event is stale by
            # definition and would otherwise replace the correct detail after
            # a conversation rebind rebuilds the neutral model from this raw
            # snapshot.
            identity_keys = {
                "threadId",
                "parentThreadId",
                "name",
                "agentNickname",
                "agentRole",
                "role",
                "tool",
            }
            incoming = {
                key: value for key, value in incoming.items() if key in identity_keys
            }
        elif (
            current_status
            in {AgentObservedStatus.RUNNING, AgentObservedStatus.NEEDS_INPUT}
            and incoming_status is AgentObservedStatus.STARTING
        ):
            # Match AgentActivityModel's non-terminal regression fence. Detail
            # enrichment remains valid there; only the weaker status is stale.
            for key in ("status", "kind", "phase"):
                incoming.pop(key, None)
        current.update(incoming)

    def _apply_subagent_activity(self, payload: dict[str, Any]) -> bool:
        payload_turn_id = str(payload.get("turnId") or "")
        if not payload_turn_id or payload_turn_id != self._agents_turn_id:
            _log.warning(
                "ignored stale Codex agent activity for root turn %s (current %s)",
                payload_turn_id or "<unknown>",
                self._agents_turn_id or "<none>",
            )
            return False
        receiver_ids = payload.get("receiverThreadIds") or []
        states = payload.get("agentsStates")
        for child_id in receiver_ids if isinstance(receiver_ids, list) else []:
            child_id = str(child_id or "")
            if not child_id:
                continue
            state: Any = states.get(child_id) if isinstance(states, dict) else None
            update = {"threadId": child_id}
            if isinstance(state, dict):
                update.update(copy.deepcopy(state))
            update.setdefault(
                "status",
                str(payload.get("status") or payload.get("phase") or "running"),
            )
            self._merge_observed_agent_state(child_id, update)
        direct_id = str(payload.get("agentThreadId") or "")
        if direct_id:
            update = {
                "threadId": direct_id,
                "status": str(
                    payload.get("kind") or payload.get("status") or "running"
                ),
            }
            if payload.get("agentPath"):
                update["path"] = payload["agentPath"]
            self._merge_observed_agent_state(direct_id, update)
        self.emit("agents-updated", copy.deepcopy(self._agents))
        return True


def _is_session_budget_exceeded(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    return (
        isinstance(error, dict)
        and error.get("codexErrorInfo") == "sessionBudgetExceeded"
    )


_TERMINAL_USAGE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def _terminal_usage_receipt(payload: dict[str, Any]) -> dict[str, object] | None:
    """Copy only allowlisted integer counters from ``turn/completed`` state."""

    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    usage = {
        key: value
        for key in _TERMINAL_USAGE_KEYS
        if isinstance((value := raw.get(key)), int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return usage or None


def _merge_non_null(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(current)
    for key, value in update.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_non_null(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged
