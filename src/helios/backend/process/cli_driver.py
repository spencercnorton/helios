"""Drive a `claude` subprocess in stream-json mode.

The driver owns one subprocess per live session and emits parsed events
as GObject signals. Everything is async via `Gio.Subprocess` so the GTK
main loop stays responsive even when claude is producing tokens.

Wire-protocol crib-sheet (what we receive from claude on stdout):

  {"type":"system","subtype":"init",          ...}   first line; session_id, cwd, model, tools, ...
  {"type":"system","subtype":"status",        ...}   status: "requesting" | "compacting" | ...
  {"type":"system","subtype":"api_retry",     ...}   attempt, max_retries, retry_delay_ms, error_status
  {"type":"rate_limit_event",                 ...}
  {"type":"stream_event","event":{"type":"message_start",        ...}}
  {"type":"stream_event","event":{"type":"content_block_start",  ...}}  per-block (text|thinking|tool_use)
  {"type":"stream_event","event":{"type":"content_block_delta",  ...}}  incremental chunks
  {"type":"stream_event","event":{"type":"content_block_stop",   ...}}
  {"type":"stream_event","event":{"type":"message_delta",        ...}}
  {"type":"stream_event","event":{"type":"message_stop"}}
  {"type":"user","isReplay":true,             ...}   our input echoed back (--replay-user-messages)
  {"type":"assistant","message":{...},        ...}   canonical full assistant message
  {"type":"user","message":{...},             ...}   canonical full user message (e.g. tool_result wrapper)
  {"type":"result","subtype":"success",       ...}   turn complete, cost, duration, tokens

What we send on stdin (one JSON per line):

  {"type":"user","message":{"role":"user","content":[{"type":"text","text":"..."}]}}

Permission prompts travel through ``--permission-prompt-tool stdio``. Helios
serializes them through the same dialog queue as provider questions: explicit
Bypass auto-allows, Never ask/Read only deny, and interactive modes ask the user.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import signal
import time
import uuid
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

import shutil

from helios.backend.claude_binary import (
    find_claude_binary,
    supports_budget_family_enforcement,
    supports_effort_flag,
    supports_forward_subagent_text,
)
from helios.backend.codex_context import TRACKER_POLICY
from helios.backend.model_catalog import context_window_for
from helios.backend.process.spend_accounting import SpendAccumulator
from helios.backend.process.env_scrub import (
    CLAUDE_AUTH_ENV,
    INFISICAL_ENV,
    NORVI_TRACKER_ENV,
    scrub_helios_env,
)
from helios.backend.process.context_accounting import (
    main_model_context as _gtk_free_main_model_context,
    model_family as _gtk_free_model_family,
    prompt_tokens as _prompt_tokens,
)
from helios.backend.process.message_queue import (
    ExecutionDispatchEvidence,
    ExecutionStopEvidence,
    ExecutionTerminalEvidence,
    MessageDelivery,
    PreparedPrompt,
    RequiredPromptContextError,
    UserMessageQueueMixin,
)
from helios.backend.project_perms import (
    AUTONOMY_MODE,
    PERMISSION_MODES,
    effective_execution_mode,
    execution_mode_restriction_reason,
)
from helios.backend.router_client import dispatch_available, router_socket_path
from helios.backend.router_tools import (
    claude_mcp_config,
    router_mcp_launcher_path,
)
from helios.backend.sensitive_text import REDACTED, scrub_sensitive
from helios.backend.process.streaming import (  # noqa: F401 — re-exported names
    Block as _Block,
    StreamingAssistant,
    terminal_cost_micro_usd,
)
from helios.backend.transcript import QUESTION_DISMISSED_MESSAGE
from helios.log import get_logger

_log = get_logger("driver")

# Opt-in verbose wire tracing (set HELIOS_DEBUG=1): spawn argv/pid, every
# stdout record type, all stderr, and exit — for diagnosing "reply never
# comes". Always available as DEBUG-level logs; the flag just bumps the level.
_HELIOS_DEBUG = bool(os.environ.get("HELIOS_DEBUG"))

_ExecutionCallback = Callable[[bool, str], None]
_ControlCallback = Callable[[bool, str, dict], None]
_EFFORT_KEYS = frozenset({"off", "low", "medium", "high", "xhigh", "max"})
_CONTROL_REQUEST_TIMEOUT_MS = 10_000
CLAUDE_STANDARD_MAX_BUDGET_USD = 10.0

# Sentinel: "decide from the account's billing model" (vs an explicit None,
# which means "no cap", or an explicit float from a caller/test).
_RESOLVE_BUDGET: float = -1.0


def default_max_budget_usd() -> float | None:
    """The dollar cap for a fresh Claude process, or None when meaningless.

    `--max-budget-usd` only bounds anything on per-token billing. On a
    subscription the CLI's `cost_micro_usd` is an API-equivalent estimate, so a
    cap there terminates real work over a number that was never money — which
    is exactly what tripped a live Work at $10.07 on 2026-08-05.
    Fails closed to the standard cap when billing cannot be determined.

    ``refresh=True`` is required, not incidental. The cache is process-wide and
    Helios outlives a `claude auth login`, so a cached subscription answer
    would go on removing the cap for every later driver after the user switched
    to Console/API billing. Since the driver stores the result, this is still
    one probe per driver, not one per turn.
    """

    from helios.backend.claude_env import is_subscription_billing

    return (
        None
        if is_subscription_billing(refresh=True)
        else CLAUDE_STANDARD_MAX_BUDGET_USD
    )


def _dbg(msg: str) -> None:
    _log.debug(msg)


# Streaming aggregation shapes (Block / StreamingAssistant) live in
# backend/process/streaming.py so the codex driver and GTK-free tests share
# them; imported above with their historical local names.


# --- The driver ------------------------------------------------------------


#: Environment the INTERACTIVE spawn adds after the scrub. Task tools are off
#: by default on Fable/Opus 5/Sonnet 5; with them on, Claude keeps a task list
#: that Helios projects as the durable execution plan and X/Y chip, as Codex
#: already does. Helper spawns (title, archive) do not get this.
#:
#: Re-measured on 2.1.259 (2026-09-03), `--setting-sources ""`, per model:
#: fable with the env has TaskCreate/TaskUpdate, fable without it has none,
#: and haiku has them either way. So the default is per-model, not global —
#: do not conclude the flag is redundant from a haiku probe.
INTERACTIVE_CHILD_ENV: dict[str, str] = {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "1"}


class DriverSpawnError(RuntimeError):
    """start() could not spawn the subprocess (binary unrunnable, fork
    failure, …). Distinct from ClaudeBinaryNotFound so callers can clean up
    a driver they already registered handlers against."""


class ClaudeCliDriver(UserMessageQueueMixin, GObject.Object):
    """Owns one running `claude --print --output-format stream-json` subprocess.

    Signals (all carry a Turn or session metadata):

      session-started (session_id: str, cwd: str, model: str)
      assistant-streaming (StreamingAssistant)   -- emitted on every delta
      turn-appended (Turn)                       -- canonical user or assistant turn
      result (result_dict)                       -- turn complete
      queued-user-sent (qid: int, text: str)     -- a queued message auto-sent
      error (message: str)                       -- subprocess died / bad json / etc.
      exited (exit_code: int)
    """

    display_name = "claude"
    provider = "anthropic"

    # Tolerate sporadic undecodable stdout lines, but give up (surface an
    # error) once they're relentless — that's a broken pipe, not a bad byte.
    _MAX_STDOUT_ERRORS = 20

    __gsignals__ = {
        "session-started": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
        "assistant-streaming": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-appended": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "result": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # (tokens_used: int, context_window: int)
        "usage-updated": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        # The raw rate_limit_info dict from claude — keyed by rateLimitType.
        "rate-limit-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "budget-exhausted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Claude invoked the AskUserQuestion tool. Args: the parsed `input`
        # dict (shape `{questions: [{question, header, multiSelect, options:
        # [{label, description}, ...]}, ...]}`) and the tool_use_id we need
        # to reply to via answer_question(). If nobody answers, the CLI
        # returns "Answer questions?" upstream and the user sees
        # "looks like you dismissed the question".
        "question-asked": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        # A message queued mid-turn was auto-sent as its own turn (qid, text).
        "queued-user-sent": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
        # Provider-native activity proved an earlier uncertain stdin frame was
        # consumed. MainWindow can now promote its quarantined user turn.
        "delivery-confirmed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "exited": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        # Cumulative token spend for this process, split root vs delegated
        # Payload: a SpendSnapshot. Distinct from "usage-updated",
        # which is window occupancy for the request on the wire and excludes
        # subagent traffic by design — that exclusion is correct for a meter
        # and is why a fan-out could burn 89% of a budget unseen.
        "spend-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Observed subagent/workflow actors for the CURRENT root turn.
        # Same name and payload shape as the Codex driver's signal so the
        # window's existing handler and the Agent Dock need no per-provider
        # branch.
        "agents-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Auto-compaction happened. The CLI does it silently; without
        # this the user cannot tell whether an answer came from the transcript
        # or from a summary of it. Payload: {"trigger": ..., "pre_tokens": ...}.
        "context-compacted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # The CLI's own statement of what it supports, from the `initialize`
        # control_response. Payload: {models, commands, agents, account,
        # output_style, fast_mode_state, ...}.
        "capabilities-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Real per-category context occupancy from `get_context_usage`.
        # Answers before the first turn, so the meter need never show 0/0.
        "context-usage": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Provider-truth activity phases the stream itself cannot show: an
        # API retry and a compaction are both *silent* on the wire, so the
        # strip inferred "still thinking" through them. Same signal
        # name and payload convention as the Codex driver, so the window
        # needs no provider branch.
        "activity-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # The CLI's full slash-command inventory from `initialize.commands`,
        # normalized to [{name, description, argument_hint}]. The composer's
        # discovery popover is the consumer; the CLI itself resolves the
        # command when the text is sent.
        "commands-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # One `system/hook_started|hook_progress|hook_response` record with the
        # envelope stripped. A settings.json hook can block or rewrite a tool
        # call; without this the UI has no evidence it ran.
        "hook-event": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # The CLI withdrew a pending can_use_tool (control_cancel_request).
        # Arg: the UI token `question-asked` carried, so the dialog can close.
        "question-cancelled": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # The CLI itself changed the effective permission mode — a plan was
        # approved out of plan mode, or an approval carried `setMode`. Arg:
        # the new mode key. The window mirrors it into the toolbar and the
        # durable conversation settings.
        "permission-mode-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # Claude's task list (TaskCreate/TaskUpdate/TaskList, or TodoWrite on
        # older CLIs) projected into the SAME payload shape the Codex driver
        # emits for turn/plan/updated, so the window's durable execution plan
        # and the X/Y chip need no provider branch (parity plan Phase 3D).
        "plan-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        *,
        cwd: str,
        model: str = "",  # empty = let claude pick from settings
        # Safe-by-default: a caller that omits the mode gets "Ask", not full
        # bypass (SAFE_FALLBACK_MODE). MainWindow always passes an explicit
        # per-cwd mode; this default only guards stray/future constructions.
        permission_mode: str = "default",
        resume_session_id: str = "",
        # `None`  = omit the flag → use claude's default thinking budget
        # `0`     = explicit "off" → `--max-thinking-tokens 0` → no extended thinking
        # `>0`    = explicit budget in tokens (legacy fallback only)
        max_thinking_tokens: int | None = None,
        # effort: one of "low"|"medium"|"high"|"xhigh"|"max" — the levels
        # `claude --effort` accepts — passed straight through when the binary
        # supports the flag, else approximated via --max-thinking-tokens.
        # None = omit entirely (let claude use its default).
        effort: str | None = None,
        # Secondary provider-native breaker, meaningful only on per-token
        # billing. None = no dollar cap; resolved from the account's billing
        # model when left at the sentinel. The Work supervisor becomes
        # authoritative once family-wide accounting lands.
        max_budget_usd: float | None = _RESOLVE_BUDGET,
    ) -> None:
        super().__init__()
        self._cwd = cwd
        self._model = model
        # `_model` is overwritten by system/init with the resolved id, which
        # drops the `[1m]` marker. Keep the requested alias for window sizing.
        self._requested_model = model
        self._model_revision = 0
        self._permission_mode = effective_execution_mode(
            permission_mode, cwd, provider="anthropic"
        )
        self._resume = resume_session_id
        self._max_thinking_tokens = max_thinking_tokens
        self._effort = effort
        self._router_binding_id = f"claude_{uuid.uuid4().hex}"
        #: Spend for THIS process. `modelUsage` restarts at zero in a new
        #: process even when it resumes the same session (measured on claude
        #: 2.1.235), so the root counter has to restart with it — see the reset
        #: at the spawn site. A driver instance is one process (`start()`
        #: returns early once `_proc` is set, and `_proc` is never cleared), so
        #: "a respawn" means a NEW DRIVER, and the window drops this one.
        #: ponytail: per-process, not per-Work. execution_attempts already has
        #: usage_json/cost_micro_usd for durable per-Work totals when after-
        #: the-fact analysis is wanted; the ticket's pain was live blindness.
        self._spend = SpendAccumulator()
        if max_budget_usd is _RESOLVE_BUDGET:
            max_budget_usd = default_max_budget_usd()
        if max_budget_usd is None:
            # Subscription billing: a dollar cap measures a notional figure, not
            # money, so there is nothing to enforce. Deliberately not replaced
            # with a fabricated token/turn cap — the real ceiling is the account
            # rate limit, which arrives as `rate_limit_info`.
            self._max_budget_usd = None
        else:
            try:
                budget_usd = float(max_budget_usd)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Claude max_budget_usd must be a positive number or None"
                ) from exc
            if not math.isfinite(budget_usd) or budget_usd <= 0:
                raise ValueError(
                    "Claude max_budget_usd must be finite and greater than zero"
                )
            self._max_budget_usd = budget_usd
        self._init_user_queue()
        self._uncertain_prepared_prompt = None
        self._proc: Gio.Subprocess | None = None
        # True once we've launched claude via `setsid` so it leads its own
        # process group (pgid == pid). Lets stop() signal the WHOLE group with
        # killpg(), reaping the Bash-tool children claude spawned — which a
        # bare kill of claude's pid would orphan.
        self._own_group = False
        self._process_group_id: int | None = None
        self._stdin: Gio.OutputStream | None = None
        self._stdout: Gio.DataInputStream | None = None
        self._stderr: Gio.DataInputStream | None = None
        # Consecutive stdout decode/read errors; reset on any good read.
        self._stdout_err_count = 0

        self._streaming: StreamingAssistant | None = None
        self._session_id: str = ""
        self._closed = False
        # True once Helios itself asked this child to die (Stop button, app
        # close, reaping a detached driver, fresh chat). Exiting *after* we
        # asked is a confirmed cancellation; exiting on its own is not.
        self._stop_requested = False
        # True between sending a user turn and receiving its `result` — i.e.
        # claude is actively working. Lets the window reflect the right
        # busy/idle state when re-binding a background session as the visible
        # one (multi-session). Distinct from `is_running` (process alive).
        self._busy = False
        # Monotonic timestamp of the last meaningful wire activity (a send or
        # any stdout record). The window's idle-reaper + LRU cap use this to
        # pick which background drivers to wind down.
        self.last_activity: float = time.monotonic()
        # tool_use_id -> (control_request id, original input) for questions
        # awaiting an answer; the reply echoes the input with answers folded in.
        self._pending_questions: dict[str, tuple[str, dict]] = {}
        #: `initialize.commands`, normalized: [{name, description, argument_hint}].
        self._cli_commands: list[dict] = []
        #: The task plan Claude maintains with its task tools: task number ->
        #: {"step", "status"} in Codex vocabulary (pending|inProgress|completed).
        self._task_plan: dict[str, dict] = {}
        #: Task-tool calls awaiting their tool_result (which carries the id).
        self._pending_task_tools: dict[str, tuple] = {}
        #: Sentinel for an early result whose call FAILED: the registration
        #: must consume it and store nothing.
        #: Task-tool results that arrived BEFORE their call was registered.
        #: Measured on 2.1.258 (2026-09-03): the CLI emits the tool_result
        #: user records before the message_stop that finalizes the assistant
        #: message, so the result routinely precedes the registration.
        self._early_task_results: dict[str, object] = {}
        #: UI prompt token -> actor id, for prompts a subagent originated.
        self._prompt_actors: dict[str, str] = {}
        # observed delegation actors for the current root turn.
        # Keyed by the `Task`/`Workflow` tool_use_id, which is also the
        # `parent_tool_use_id` the child's own records carry, so every
        # transition below is CAUSAL: the id is provided by the provider, never
        # inferred. Cleared when a new root turn begins, because the projection
        # is turn-scoped like the Codex one.
        self._agents: dict[str, dict] = {}
        # tool_use_ids whose terminal comes from `system/task_notification`
        # rather than the root tool_result (see `_note_task_record`).
        self._task_managed_actors: set[str] = set()
        self._agents_root_turn_id = ""
        # UI token -> (control request id, original can_use_tool request,
        # {option label: action}). An action is {"allow": bool, "updates":
        # [PermissionUpdate...], "mode_after": str, "message": str}.
        self._pending_tool_approvals: dict[str, tuple[str, dict, dict]] = {}
        # Outbound control-protocol requests (initialize and live execution
        # settings) are correlated by request id.  Settings are committed only
        # after Claude ACKs them; otherwise the toolbar would advertise policy
        # that the current process is not actually using.
        self._control_seq = 0
        self._pending_control: dict[
            str,
            tuple[_ControlCallback | None, int],
        ] = {}
        self._control_initialized = False
        self._permission_change_pending = False
        self._effort_change_pending = False
        self._model_change_pending = False
        self._execution_restart_required = False

        # Live context-window occupancy, tracked per API request rather than
        # per turn. `result.usage` is the SUM over every request the turn made,
        # so a 3-tool-call turn reported ~3x the tokens actually resident in
        # the window. The last request's own usage IS the occupancy.
        self._ctx_window = 0
        self._ctx_request_input = 0  # prompt side of the in-flight request
        self._ctx_request_output = 0

        # Captured from the `system/init` line — the authoritative per-session
        # list of available tools and MCP servers. Empty until init lands.
        # MainWindow reads these on `session-started` to cache for the
        # settings "Tools" view (see helios.backend.claude_env).
        #: Background-subagent wake-ups held while a real turn is in flight.
        self._deferred_task_results: list[dict] = []
        #: The CLI's `initialize` payload, and the models[] inside it.
        self._cli_capabilities: dict = {}
        self._cli_models: list = []
        #: Latest `get_context_usage` payload.
        self._ctx_usage: dict = {}
        #: True once get_context_usage has given us a real window, after which
        #: modelUsage's contextWindow must not overwrite it — the two measure
        #: different things (usable-before-autocompact vs the hard model cap)
        #: and letting both write one field is what made the gauge change
        #: meaning between turn 1 and turn 2.
        self._ctx_window_authoritative = False
        self._init_tools: list = []
        self._init_mcp_servers: list = []
        #: Command names the CLI will accept as a leading `/token` this
        #: session, straight from `system/init`. Empty until init lands, which
        #: is the safe direction: an unknown name is treated as prose, so the
        #: worst case is the behaviour Helios already had.
        self._init_slash_commands: set[str] = set()

        # PyGObject footgun: async callbacks passed as bound methods can be
        # garbage-collected before the async completes — the C wrapper
        # holds only a weak ref. Pin them as instance attributes so they
        # outlive the read scheduling.
        self._cb_stdout = self._on_stdout_line
        self._cb_stderr = self._on_stderr_line
        self._cb_exit = self._on_exit
        self._cb_control_timeout = self._on_control_request_timeout

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def model(self) -> str:
        return self._model

    @property
    def init_tools(self) -> list:
        """Tool names available this session, from the `system/init` event."""
        return self._init_tools

    @property
    def init_mcp_servers(self) -> list:
        """MCP servers (with connection status) from the `system/init` event."""
        return self._init_mcp_servers

    @property
    def cli_commands(self) -> list[dict]:
        """The CLI's own command inventory, `[{name, description, argument_hint}]`."""
        return list(self._cli_commands)

    @property
    def init_slash_commands(self) -> set[str]:
        """Slash commands this session accepts, from the `system/init` event."""
        return set(self._init_slash_commands)

    def is_slash_command(self, text: str) -> bool:
        """Would the CLI read `text` as a command rather than as prose?

        Matched against the session's OWN advertised list, not a `/`-prefix
        heuristic. The heuristic is not merely imprecise, it is wrong in a way
        that matters: a message opening with an absolute path — `/home/<user>/
        helios has the bug` — is ordinary prose, and treating it as a command
        would silently drop the Goal envelope from a real user turn. Nothing
        matches until `system/init` has landed, so the failure direction is
        "behaves as it did before".
        """
        head = text.lstrip()
        if not head.startswith("/"):
            return False
        # The command is the first whitespace-delimited token; `/model opus`
        # and `/compact focus on the API` both carry arguments after it.
        token = head[1:].split(maxsplit=1)[0] if head[1:].split() else ""
        return token in self._init_slash_commands

    @property
    def is_running(self) -> bool:
        return self._proc is not None and not self._closed

    @property
    def is_busy(self) -> bool:
        """True while a turn is in flight (sent, no `result` yet)."""
        return self._busy

    @property
    def is_accepting_input(self) -> bool:
        """True when the process is alive AND stdin is still open — i.e. a
        send right now would reach claude. False after end_input()/stop(),
        including the window where the process is still flushing its final
        turn before exiting on EOF. Callers deciding whether to bind a live
        driver vs stage a --resume must check this, not just is_running."""
        return self.is_running and self._stdin is not None

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def effort_key(self) -> str:
        if self._effort == "off" or (
            self._effort is None and self._max_thinking_tokens == 0
        ):
            return "off"
        return self._effort or ""

    @property
    def execution_restart_required(self) -> bool:
        """Whether a live execution mutation left provider state uncertain."""

        return self._execution_restart_required

    # --- lifecycle ---

    def start(self) -> None:
        """Launch the subprocess.

        Raises ClaudeBinaryNotFound (no usable binary) or DriverSpawnError
        (binary found but the spawn itself failed). Either way the driver is
        inert afterwards — callers should disconnect their handlers and drop
        it rather than retry on the same instance.
        """
        if self._proc is not None:
            return

        binary = find_claude_binary()
        argv = [
            str(binary.path),
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",  # required when --print + stream-json
            "--include-partial-messages",
            "--replay-user-messages",
            # Hook lifecycle records. The operator's own PreToolUse guard can
            # deny a tool call; the transcript has to be able to say so.
            "--include-hook-events",
            "--permission-mode", self._permission_mode,
            # Makes Bypass available as a later control-protocol selection;
            # unlike --dangerously-skip-permissions, this does not enable it.
            "--allow-dangerously-skip-permissions",
            # Needed to receive `control_request` records for AskUserQuestion.
            # The CLI's IDE-integration protocol routes the question through
            # this channel; without it, the tool fails with "Answer questions?"
            # error and the user sees "looks like you dismissed the question."
            # For non-question tools, we auto-allow in _handle_control_request.
            "--permission-prompt-tool", "stdio",
            "--setting-sources", "user,project,local",
            # Parity with the Claude Code app, which auto-compacts. Helios runs
            # one long-lived stream-json session per Work, so without this a
            # full context window has no remedy but a new chat — the
            # Codex and OpenRouter drivers both already compact.
            "--autocompact", "auto",
            # Moves cwd/env/git-status out of the system prompt into the first
            # user message, so the cached prefix stops varying with them.
            # Measured on the development workstation 2026-08-05: ~296 fewer prompt tokens/turn
            # (37,180 vs 37,476). Small, but free and never negative; it does
            # NOT address the ~20k re-created per respawn, which is
            # cache TTL rather than prompt volatility. Verified not to pollute
            # the --replay-user-messages stream.
            "--exclude-dynamic-system-prompt-sections",
            # Claude reads CLAUDE.md natively, so this is the ONLY Helios-owned
            # instruction channel it has — and the tracker grant in env_scrub is
            # inert without it, because nothing else tells the session that
            # `norvi-work` exists. A constant string, so it does not re-break
            # the cached prefix the flag above just stabilised.
            "--append-system-prompt", TRACKER_POLICY,
        ]
        # Forward each child agent's own messages into the root stream, tagged
        # with `parent_tool_use_id`, so the Agent Dock can show what a subagent
        # is actually saying instead of a bare status square. Measured on
        # 2.1.241 (2026-08-25): children arrive as complete `assistant`/`user`
        # records, never stream deltas, so the per-record cost is bounded by
        # the child's message cadence. The dispatch loop routes every
        # parent-tagged record away from the root transcript/accumulators —
        # forwarding is display-only and must never look like root traffic.
        if supports_forward_subagent_text():
            argv.append("--forward-subagent-text")
        # The Helios Router MCP, advertised only when the broker can actually
        # dispatch. It used to be injected whenever the launcher script existed
        # in the repo — which is to say always, including through the whole
        # period the broker was structurally unable to dispatch anything. That
        # cost ~1.6k tokens of tool schemas and one Python subprocess per
        # session, and the MCP's own `instructions` tell the model to "use
        # delegate_task regularly", so it also spent turns on a tool whose only
        # possible answer was that the task was retained.
        #
        # `dispatch_available()` is a cached read, never a socket round trip:
        # this runs on the GTK main thread and a wedged broker must not be able
        # to stall a session opening. Its delegated inference spend still sits
        # outside Claude's native --max-budget-usd cap, so the Work supervisor
        # remains the follow-on that makes it accountable.
        router_launcher = router_mcp_launcher_path()
        if not router_launcher.is_file():
            _log.warning("Helios Router MCP launcher is missing: %s", router_launcher)
        elif dispatch_available():
            argv.extend(
                [
                    "--mcp-config",
                    claude_mcp_config(
                        self._router_binding_id,
                        socket_path=str(router_socket_path()),
                        launcher_path=str(router_launcher),
                    ),
                ]
            )
        if self._model:
            argv.extend(["--model", self._model])
        if self._resume:
            argv.extend(["--resume", self._resume])
        if self._max_budget_usd is not None:
            if supports_budget_family_enforcement():
                argv.extend(["--max-budget-usd", f"{self._max_budget_usd:g}"])
            else:
                raise DriverSpawnError(
                    "Claude's required process-family budget breaker is "
                    "unavailable. Helios requires Claude Code 2.1.217+ with "
                    "--max-budget-usd; update Claude Code or retry after "
                    "checking the configured binary."
                )

        # --- Effort / thinking budget ---
        # Priority order:
        #  1. effort str set → use --effort if binary supports it, else
        #     approximate with --max-thinking-tokens.
        #  2. max_thinking_tokens set (legacy / Off) → use --max-thinking-tokens.
        _EFFORT_LEVEL_TOKENS: dict[str, int] = {
            "low": 4000,
            "medium": 8000,
            "high": 16000,
            # The binary that predates --effort also predates max; its deepest
            # budget is the closest honest approximation.
            "xhigh": 32000,
            "max": 32000,
        }
        if self._effort is not None:
            if self._effort == "off":
                # "off" disables extended thinking via --max-thinking-tokens 0.
                argv.extend(["--max-thinking-tokens", "0"])
            elif supports_effort_flag():
                argv.extend(["--effort", self._effort])
            else:
                # Binary predates --effort; approximate via token budget.
                tokens = _EFFORT_LEVEL_TOKENS.get(self._effort)
                if tokens is not None:
                    argv.extend(["--max-thinking-tokens", str(tokens)])
        elif self._max_thinking_tokens is not None:
            # Legacy / explicit off path.
            # 0 IS a valid explicit value here — it disables extended thinking.
            argv.extend(["--max-thinking-tokens", str(self._max_thinking_tokens)])

        # Launch claude as its own session/group leader via `setsid` so we can
        # later signal the whole process group (claude + every Bash-tool child)
        # in one shot. `setsid <prog>` execs in-place when the caller isn't a
        # group leader (Gio.Subprocess children aren't), so the tracked pid
        # stays claude's and stdin/stdout/exit tracking are unaffected (verified
        # empirically). PyGObject doesn't expose set_child_setup(), so the argv
        # wrapper is the portable way to get a new session. Falls back to a
        # bare spawn (single-pid kill) if `setsid` isn't on PATH.
        setsid_path = shutil.which("setsid")
        using_setsid = bool(setsid_path)
        if setsid_path:
            argv = [setsid_path, *argv]

        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_PIPE
        )
        launcher.set_cwd(self._cwd)
        # Keep Helios-internal vars, unrelated credentials, and OpenAI/Codex
        # auth out of the child env — claude runs arbitrary Bash tools that
        # could read them. Claude's own auth env is forwarded, plus the one
        # operator-granted Infisical machine identity the interactive agent
        # needs to fetch every other secret at runtime, plus which Infisical to
        # ask — without the URL the CLI silently targets Cloud.
        # Housekeeping
        # spawns (title-gen, archival, probes) keep the narrower set.
        scrub_helios_env(
            launcher, keep=CLAUDE_AUTH_ENV | INFISICAL_ENV | NORVI_TRACKER_ENV
        )
        for key, value in INTERACTIVE_CHILD_ENV.items():
            launcher.setenv(key, value, True)

        _dbg(f"spawn argv={argv} cwd={self._cwd}")
        try:
            self._proc = launcher.spawnv(argv)
        except GLib.Error as e:
            _dbg(f"SPAWN FAILED: {e.message}")
            # Raise instead of emitting: emitting from inside start() runs
            # the window's handlers mid-_ensure_driver and previously left a
            # dead driver registered in _handlers forever (review M3).
            raise DriverSpawnError(f"Failed to spawn claude: {e.message}") from e
        _dbg(f"spawned pid={self._proc.get_identifier()}")
        # Spend is counted per PROCESS, and this is where a process begins, so
        # this is where the counter does. Today the assignment above is the
        # only one in the class and `start()` returns early when `_proc` is
        # set, so a driver is structurally one process and this line can only
        # ever run on a fresh accumulator. It is here anyway because the
        # alternative is a counter whose lifetime is bound to the *object* while
        # its semantics are bound to the *process* — and if anyone ever clears
        # `_proc` to allow a relaunch, `_root_tokens` would carry into a
        # `modelUsage` that restarted at zero, driving the split negative and
        # clamping it to nothing. That failure hides delegated spend, which is
        # the one thing this feature exists to show.
        self._spend = SpendAccumulator()

        try:
            spawned_pid = int(self._proc.get_identifier())
        except (GLib.Error, TypeError, ValueError):
            spawned_pid = 0
        self._process_group_id = _capture_process_group(
            spawned_pid,
            expected_new_session=using_setsid,
        )
        self._own_group = self._process_group_id is not None

        self._stdin = self._proc.get_stdin_pipe()
        self._stdout = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        self._stderr = Gio.DataInputStream.new(self._proc.get_stderr_pipe())

        # Claude accepts pipelined control requests, so initialize as soon as
        # stdin exists.  There is no need to delay normal reads or the first
        # live setting mutation while waiting for this ACK.
        self._initialize_control_protocol()
        # Ask what is already in the window. This is what stops the meter
        # reading 0/0 until the first turn lands: the CLI can answer before
        # any message is sent, and the answer costs no tokens.
        self.request_context_usage()

        # Start reading lines.
        self._read_next_stdout_line()
        self._read_next_stderr_line()

        # Watch exit.
        self._proc.wait_async(None, self._cb_exit, None)

    def send_user_text(
        self,
        text: str,
        *,
        with_context: bool = True,
    ) -> MessageDelivery:
        """Send a user message. Safe to call multiple times mid-turn.

        Uses `write_all` rather than `write_bytes` because the latter is not
        a guaranteed-write-all API — for large prompts (hundreds of KB) we
        could otherwise hand claude truncated JSON, which deadlocks the
        protocol while it waits for the rest.

        `with_context=False` sends `text` as the literal wire frame, skipping
        the prompt-context provider. That provider prepends the Helios Goal
        envelope (`session_goals`), and a slash command is only a command when
        it is the LEADING token — so `/compact` wrapped in an envelope reaches
        the model as prose and silently does nothing. Use it for commands
        Helios issues on the user's behalf, never for user-authored text: the
        envelope is what carries a Work's objective into the conversation.

        USER-TYPED commands get the same treatment automatically, below. They
        used to not: `_compact_current_chat` passed `with_context=False` for
        the one command Helios issues itself, so `/compact` worked from the
        toolbar button and silently did nothing when typed into the composer of
        a Work that had a goal — which is every tandem Work. The guard belongs
        here rather than at each caller, because "does the envelope survive"
        is a property of the wire frame, not of who built it.

        Everything else is deliberately unchanged. A command still runs a real
        turn and still returns a `result`, so it must keep the execution
        attempt, the busy flag and the dispatch evidence — bypassing those
        would leave the ledger open while the turn ran.
        """
        if not self.is_running or self._stdin is None:
            self.emit("error", "Cannot send: subprocess not running")
            return MessageDelivery("rejected")
        block_reason = self._execution_block_reason()
        if block_reason:
            self.emit("error", block_reason)
            return MessageDelivery("rejected")
        try:
            prepared = (
                self._prepare_prompt_context(text)
                if with_context and not self.is_slash_command(text)
                else PreparedPrompt(text)
            )
        except RequiredPromptContextError as exc:
            self.emit("error", exc.user_message)
            return MessageDelivery("rejected")
        payload = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prepared.text}],
            },
        }
        line = (json.dumps(payload) + "\n").encode("utf-8")
        admission_error = self._begin_execution_attempt()
        if admission_error:
            self.emit("error", admission_error)
            return MessageDelivery("rejected")
        attempt_id = self.execution_attempt_id
        if not self._record_execution_dispatch(
            ExecutionDispatchEvidence(
                wire_prompt_text=prepared.text,
                provider_request_key=attempt_id,
                native_binding_id=self._confirmed_execution_native_id(),
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
                "Helios could not persist the Claude dispatch identity. "
                "The message was not sent.",
            )
            return MessageDelivery("rejected")
        # Once dispatch is durable, any pipe failure is ambiguous: the kernel
        # may have accepted a prefix or the complete frame before surfacing an
        # error. Keep both local busy state and the durable slot until a native
        # result provides authoritative terminal evidence.
        self._busy = True
        # A new user turn owns a fresh actor projection; a background
        # wake-up (task-notification result) is NOT a new turn and keeps it.
        self._reset_observed_agents(attempt_id or uuid.uuid4().hex)
        try:
            # `write_all` blocks until every byte is written (or it errors).
            # The buffer is the inherited 64KB pipe; even multi-MB pastes
            # land in tens of milliseconds, so doing this synchronously
            # from the main loop is fine for now. If we ever see UI hitches
            # on huge prompts, swap to `write_all_async`.
            write_result = self._stdin.write_all(line, None)
            if isinstance(write_result, tuple):
                complete = bool(write_result[0])
                written = int(write_result[1]) if len(write_result) > 1 else 0
                if not complete or written != len(line):
                    raise OSError(
                        f"incomplete stdin write ({written}/{len(line)} bytes)"
                    )
            elif write_result is False:
                raise OSError("stdin write did not confirm completion")
        except Exception as exc:
            self._uncertain_prepared_prompt = prepared
            detail, _changed = scrub_sensitive(
                getattr(exc, "message", "") or str(exc) or "unknown I/O error"
            )
            self._record_execution_stop(
                ExecutionStopEvidence(
                    acknowledgement={},
                    queue_disposition="held",
                )
            )
            self.end_input()
            self.emit(
                "error",
                "Claude stdin delivery is uncertain; this Work remains blocked "
                f"until provider-backed recovery confirms the outcome ({detail}).",
            )
            return MessageDelivery("uncertain")
        prepared.mark_sent()
        self._uncertain_prepared_prompt = None
        self.last_activity = time.monotonic()
        return MessageDelivery("accepted")

    def _confirm_uncertain_delivery(self) -> None:
        """Acknowledge a handoff only after Claude proves it consumed input."""

        prepared, self._uncertain_prepared_prompt = (
            self._uncertain_prepared_prompt,
            None,
        )
        if prepared is not None:
            prepared.mark_sent()
        queue_confirmed = self._accept_uncertain_queue_delivery()
        if prepared is not None or queue_confirmed:
            self.emit("delivery-confirmed")

    def _confirm_matching_user_replay(self, obj: dict) -> None:
        """Confirm only an exact root echo of the ambiguous stdin frame."""

        prepared = self._uncertain_prepared_prompt
        if prepared is None or obj.get("parent_tool_use_id"):
            return
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            replayed = content
        elif isinstance(content, list):
            text_blocks = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            replayed = text_blocks[0] if len(text_blocks) == 1 else None
        else:
            replayed = None
        if replayed == prepared.text:
            self._confirm_uncertain_delivery()

    def respond_to_question(
        self,
        request_id: str,
        answer: object,
        input_payload: dict | None = None,
    ) -> None:
        """Reply to an AskUserQuestion control_request.

        Measured on claude 2.1.258 (2026-09-02): approving the tool with the
        answers folded into `updatedInput.answers` — `{question text: chosen
        label(s)}`, multi-select joined by ", ", a free-text "Other" verbatim —
        makes the CLI hand the model "The user answered: …" as the tool
        result, and the turn continues. That replaces the 2.1.145-era
        workaround (deny, then inject the answer as a raw user record), which
        bypassed execution admission and fabricated an extra root turn.

        `answer=None` means the user dismissed the dialog: deny, no follow-up.
        """
        if not self.is_running or self._stdin is None:
            self.emit("error", "Cannot answer question: subprocess not running")
            return

        if answer is None:
            self._write_stdin_line(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": request_id,
                        "response": {
                            "behavior": "deny",
                            "message": QUESTION_DISMISSED_MESSAGE,
                        },
                    },
                }
            )
            return

        original = copy.deepcopy(input_payload) if isinstance(input_payload, dict) else {}
        questions = [
            q for q in (original.get("questions") or []) if isinstance(q, dict)
        ]
        original["answers"] = _question_answers(questions, answer)
        self._write_stdin_line(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": {
                        "behavior": "allow",
                        "updatedInput": original,
                    },
                },
            }
        )

    def _write_stdin_line(self, obj: dict) -> bool:
        if self._stdin is None:
            return False
        line = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            self._stdin.write_all(line, None)
        except GLib.Error as e:
            self.emit("error", f"stdin write failed: {e.message}")
            return False
        return True

    @staticmethod
    def _notify_execution_callback(
        callback: _ExecutionCallback | None,
        success: bool,
        detail: str = "",
    ) -> None:
        if callback is None:
            return
        try:
            callback(success, detail)
        except Exception:
            _log.exception("execution-setting callback failed")

    def _send_control_request(
        self,
        request: dict,
        callback: _ControlCallback | None = None,
    ) -> bool:
        if self._stdin is None or self._closed:
            if callback is not None:
                callback(False, "the Claude process is not accepting input", {})
            return False
        self._control_seq += 1
        request_id = f"helios-{self._control_seq}"
        timeout_id = GLib.timeout_add(
            _CONTROL_REQUEST_TIMEOUT_MS,
            self._cb_control_timeout,
            request_id,
        )
        self._pending_control[request_id] = (callback, timeout_id)
        written = self._write_stdin_line(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": request,
            }
        )
        # Test doubles historically replace _write_stdin_line with list.append
        # (which returns None), so only an explicit False means write failure.
        if written is False:
            pending = self._pending_control.pop(request_id, None)
            if pending is not None:
                pending_callback, source_id = pending
                GLib.source_remove(source_id)
                if pending_callback is not None:
                    pending_callback(
                        False,
                        "could not write the Claude control request",
                        {"ambiguous": True},
                    )
            return False
        return True

    @staticmethod
    def _coerce_int(value: object) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    def _initialize_control_protocol(self) -> None:
        """Send the initialize handshake AND keep what it answers with.

        This frame was already being sent with no callback, so
        `_handle_control_response` dropped the payload on the floor — and that
        payload is the CLI stating its own capabilities: `models[]` with
        `supportsEffort` / `supportedEffortLevels` / `supportsFastMode` per
        model, plus `commands`, `agents`, `account` and `output_style`.
        Without it Helios substitutes a hardcoded six-stop effort list and a
        heuristic for which models even have a reasoning axis.

        Read-only and never fenced: if it fails or is slow, the existing
        constants remain correct for every model but the newest, so a miss
        costs nothing. That is why this deliberately does NOT go through the
        ambiguous-response teardown that settings mutations use.
        """

        if self._control_initialized:
            return

        def landed(ok: bool, _detail: str, payload: dict) -> None:
            if not ok or not isinstance(payload, dict):
                return
            models = payload.get("models")
            if isinstance(models, list):
                self._cli_models = models
            self._cli_capabilities = payload
            self.emit("capabilities-updated", payload)
            commands = _normalize_cli_commands(payload.get("commands"))
            if commands:
                self._cli_commands = commands
                self.emit("commands-updated", list(commands))

        if self._send_control_request(
            {
                "subtype": "initialize",
                "hooks": None,
                # Declared capability, accepted by 2.1.258 (measured
                # 2026-09-02): the dock offers a per-agent stop, so the
                # session-wide interrupt need not kill every background task.
                "perTaskStopAffordance": True,
            },
            landed,
        ):
            self._control_initialized = True

    def request_context_usage(self) -> None:
        """Ask the CLI what is actually in the context window.

        Answers BEFORE the first turn, at no token cost, with real per-category
        figures — which is what lets the meter stop reading 0/0 on open and
        stop deriving its largest segment as a residual.
        """

        model_revision = self._model_revision

        def landed(ok: bool, _detail: str, payload: dict) -> None:
            if not ok or not isinstance(payload, dict):
                return
            if self._model_revision != model_revision or self._model_change_pending:
                return
            if not payload.get("categories"):
                return
            # get_context_usage resolves aliases without an inference turn.
            # A live set_model ACK carries no resolved id or fresh system/init
            # record, so this is also the exact identity for effort lookup.
            resolved_model = payload.get("model")
            if isinstance(resolved_model, str) and resolved_model.strip():
                resolved_model = resolved_model.strip()
                if resolved_model != self._model:
                    self._model = resolved_model
                    self.emit("capabilities-updated", self._cli_capabilities)
            self._ctx_usage = payload
            window = self._coerce_int(payload.get("maxTokens"))
            if window:
                # Authoritative for this session, so it outranks both
                # modelUsage's contextWindow and the model-id heuristic.
                self._ctx_window = window
                self._ctx_window_authoritative = True
            self.emit("context-usage", payload)

        self._send_control_request({"subtype": "get_context_usage"}, landed)

    def supported_effort_levels(self) -> list[str]:
        """Effort stops the CURRENT model offers, or [] if the CLI did not say.

        [] means "no statement" and the caller must keep its own constants. It
        deliberately covers two cases that both have the same safe answer:
        the CLI never sent models[], and the CLI sent models[] but this model
        is not in it.

        NEVER returns another model's levels. An earlier cut fell back to the
        first effort-capable entry, so an unmatched model — or one explicitly
        marked `supportsEffort: false` — inherited someone else's capability.
        That is worse than not knowing, because the UI would offer stops the
        model does not have and the user could submit one.

        A model present with `supportsEffort: false` also returns [], so the
        caller shows its full constant set rather than an empty slider. That
        matches the behaviour before this method existed; narrowing the slider
        to nothing for such a model is a separate UI decision, not something
        to smuggle in here.
        """

        if not self._cli_models:
            return []
        # The requested alias may include [1m], while initialize lists the
        # bare alias and its resolved id. Only use the CLI-reported live id
        # as a second exact match; do not infer capabilities from a family.
        for target in (self._requested_model, self._model):
            target = (target or "").strip()
            if not target:
                continue
            for entry in self._cli_models:
                if not isinstance(entry, dict):
                    continue
                value = str(entry.get("value") or "")
                resolved = str(entry.get("resolvedModel") or "")
                if target not in (value, resolved):
                    continue
                if not entry.get("supportsEffort"):
                    return []
                levels = entry.get("supportedEffortLevels")
                if isinstance(levels, list) and levels:
                    return [str(x) for x in levels]
                return []
        return []

    def _handle_control_response(self, obj: dict) -> None:
        response = obj.get("response") or {}
        if not isinstance(response, dict):
            return
        request_id = str(response.get("request_id") or "")
        if request_id not in self._pending_control:
            return
        callback, source_id = self._pending_control.pop(request_id)
        GLib.source_remove(source_id)
        if callback is None:
            return
        if response.get("subtype") == "success":
            payload = response.get("response")
            callback(True, "", payload if isinstance(payload, dict) else {})
            return
        raw_error = response.get("error") or "Claude rejected the setting change"
        detail, _changed = scrub_sensitive(raw_error)
        callback(False, detail, {})

    def _on_control_request_timeout(self, request_id: str) -> bool:
        return self._expire_control_request(request_id, remove_source=False)

    def _expire_control_request(
        self,
        request_id: str,
        *,
        remove_source: bool = True,
    ) -> bool:
        """Expire one outbound request without waiting in tests or the UI."""

        pending = self._pending_control.pop(request_id, None)
        if pending is None:
            return False
        callback, source_id = pending
        # The actual timeout source is already dispatching and removes itself
        # when this callback returns False. Direct callers must remove it now.
        if remove_source:
            GLib.source_remove(source_id)
        if callback is not None:
            callback(
                False,
                "Claude did not acknowledge the setting change in time",
                {"ambiguous": True},
            )
        return False

    def _fail_pending_control(self, detail: str) -> None:
        pending, self._pending_control = self._pending_control, {}
        for callback, source_id in pending.values():
            GLib.source_remove(source_id)
            if callback is None:
                continue
            try:
                callback(False, detail, {})
            except Exception:
                _log.exception("control callback failed during teardown")

    def _fence_ambiguous_execution_change(self, detail: str) -> str:
        """Close input when Claude may have applied an unconfirmed change."""

        self._execution_restart_required = True
        self.end_input()
        reason = detail.rstrip(".") or "Claude could not reconcile execution settings"
        return (
            f"{reason}. The live process was closed; the next response will "
            "resume with the saved settings."
        )

    def set_permission_mode(
        self,
        mode: str,
        callback: _ExecutionCallback | None = None,
    ) -> bool:
        if mode not in PERMISSION_MODES:
            self._notify_execution_callback(callback, False, "unknown permission mode")
            return False
        restriction = execution_mode_restriction_reason(
            mode,
            self._cwd,
            provider="anthropic",
        )
        if restriction:
            self._notify_execution_callback(
                callback,
                False,
                restriction,
            )
            return False
        if mode == self._permission_mode:
            self._notify_execution_callback(callback, True)
            return True
        # No busy gate. `set_permission_mode` rides the control-request
        # channel, which is independent of the turn stream: measured against
        # claude 2.1.241, a request sent 6s into a live turn answered
        # `{"subtype":"success","response":{"mode":"plan"}}` before that turn's
        # `result`. Refusing here was Helios's own restriction, and it is what
        # made the toolbar go dead exactly when the user most wants to change
        # course.
        if not self.is_accepting_input:
            self._notify_execution_callback(
                callback,
                False,
                "the Claude process is not accepting input",
            )
            return False
        if self._permission_change_pending:
            self._notify_execution_callback(
                callback,
                False,
                "another permission change is pending",
            )
            return False

        self._permission_change_pending = True

        def applied(success: bool, detail: str, payload: dict) -> None:
            self._permission_change_pending = False
            if success:
                self._permission_mode = mode
            elif payload.get("ambiguous"):
                detail = self._fence_ambiguous_execution_change(detail)
            self._notify_execution_callback(callback, success, detail)

        return self._send_control_request(
            {"subtype": "set_permission_mode", "mode": mode},
            applied,
        )

    def set_model(
        self,
        model: str,
        callback: _ExecutionCallback | None = None,
    ) -> bool:
        """Switch the live process's model via the control channel.

        Verified on claude 2.1.241 (2026-08-25): `set_model` ACKs with
        `success` and the CLI echoes a `<local-command-stdout>` user record,
        which the dispatch loop drops. A definitive error leaves the process
        (and `self._model`) untouched; only an ambiguous outcome fences the
        process, so the next turn resumes on the saved settings.
        """
        if not isinstance(model, str) or not model.strip():
            self._notify_execution_callback(callback, False, "unknown model")
            return False
        model = model.strip()
        if model == self._model:
            self._notify_execution_callback(callback, True)
            return True
        if self._busy:
            self._notify_execution_callback(callback, False, "the conversation is busy")
            return False
        if not self.is_accepting_input:
            self._notify_execution_callback(
                callback,
                False,
                "the Claude process is not accepting input",
            )
            return False
        if self._model_change_pending:
            self._notify_execution_callback(
                callback,
                False,
                "another model change is pending",
            )
            return False

        self._model_change_pending = True

        def applied(success: bool, detail: str, payload: dict) -> None:
            self._model_change_pending = False
            if success:
                self._model = model
                self._requested_model = model
                self._model_revision += 1
                self.request_context_usage()
            elif payload.get("ambiguous"):
                detail = self._fence_ambiguous_execution_change(detail)
            self._notify_execution_callback(callback, success, detail)

        return self._send_control_request(
            {"subtype": "set_model", "model": model},
            applied,
        )

    def set_effort(
        self,
        key: str,
        callback: _ExecutionCallback | None = None,
    ) -> bool:
        if not isinstance(key, str) or key not in _EFFORT_KEYS:
            self._notify_execution_callback(callback, False, "unknown effort level")
            return False
        if key == self.effort_key:
            self._notify_execution_callback(callback, True)
            return True
        # No busy gate — same control channel as set_permission_mode. Both
        # `apply_flag_settings` and `set_max_thinking_tokens` (null and 0) were
        # measured returning success mid-turn, which matters because this path
        # sends two requests and fences the process if the second fails after
        # the first has landed.
        if not self.is_accepting_input:
            self._notify_execution_callback(
                callback,
                False,
                "the Claude process is not accepting input",
            )
            return False
        if self._effort_change_pending:
            self._notify_execution_callback(
                callback,
                False,
                "another effort change is pending",
            )
            return False

        # `ultracode` is cleared explicitly on every change: it is a sticky
        # boolean in the CLI's settings, so a process that was started with it
        # on (or inherited it from the user's settings.json) would otherwise
        # keep forcing Workflow-tool orchestration under a plain effort level.
        # "off" is a Helios concept, not an effortLevel the CLI accepts
        # (low|medium|high|xhigh|max) — sending it as one risks the whole
        # settings apply being rejected. Off is implemented purely by the
        # thinking-token override below; here it only clears ultracode.
        settings: dict = {"ultracode": False}
        if key != "off":
            settings["effortLevel"] = key
        requests = [{"subtype": "apply_flag_settings", "settings": settings}]
        # The max-token override is sticky within the Claude process.  Off
        # installs 0; every other effort must explicitly clear it with null or
        # a later Low/High/Max selection can remain silently disabled.
        requests.append(
            {
                "subtype": "set_max_thinking_tokens",
                "max_thinking_tokens": 0 if key == "off" else None,
            }
        )

        self._effort_change_pending = True
        state = {"finished": False, "provider_mutated": False}

        def fail(detail: str, *, ambiguous: bool) -> None:
            if state["finished"]:
                return
            state["finished"] = True
            self._effort_change_pending = False
            if state["provider_mutated"] or ambiguous:
                # These are two independent provider mutations. Once the
                # first has ACKed, or any response is ambiguous, retaining the
                # process would let a later turn run under state the toolbar
                # cannot truthfully identify. EOF fences new input; the next
                # response resumes this conversation with its last durable
                # settings on a fresh process.
                detail = self._fence_ambiguous_execution_change(detail)
            self._notify_execution_callback(callback, False, detail)

        def max_tokens_applied(success: bool, detail: str, payload: dict) -> None:
            if state["finished"]:
                return
            if not success:
                fail(detail, ambiguous=bool(payload.get("ambiguous")))
                return
            state["finished"] = True
            self._effort_change_pending = False
            self._effort = key
            self._max_thinking_tokens = 0 if key == "off" else None
            self._notify_execution_callback(callback, True)

        def flags_applied(success: bool, detail: str, payload: dict) -> None:
            if state["finished"]:
                return
            if not success:
                fail(detail, ambiguous=bool(payload.get("ambiguous")))
                return
            state["provider_mutated"] = True
            self._send_control_request(requests[1], max_tokens_applied)

        return self._send_control_request(requests[0], flags_applied)

    def _handle_control_request(self, obj: dict) -> None:
        """Route a control_request from the CLI.

        For AskUserQuestion: emit a `question-asked` signal so the UI can
        show the dialog; the UI then calls `respond_to_question(...)`.

        Other tools follow the selected permission mode. The request must
        always receive a response; leaving it unanswered stalls the CLI.
        """
        req_id = obj.get("request_id", "")
        req = obj.get("request") or {}
        subtype = str(req.get("subtype") or "")
        if subtype != "can_use_tool":
            # The docstring above is not decoration: an unanswered request
            # stalls the CLI. Bare-returning here is what made `elicitation`,
            # `hook_callback`, `mcp_message` and the OAuth/host-auth refresh
            # requests hang the session with no log line to find it by.
            # Refusing explicitly lets the CLI take its own failure path.
            _log.info("unhandled control_request subtype %r — refused", subtype)
            self._refuse_control_request(req_id, subtype)
            return
        tool_name = str(req.get("tool_name") or "")

        if tool_name == "AskUserQuestion":
            input_payload = req.get("input") or {}
            tool_use_id = str(req.get("tool_use_id") or req_id or "")
            # Keep the request id AND the original input: the reply echoes
            # the input back with the answers folded in. MainWindow uses the
            # tool_use_id as the dedup key.
            self._pending_questions[tool_use_id] = (req_id, input_payload)
            payload = _dialog_questions(input_payload)
            asker = self._note_prompt_asker(req, tool_use_id)
            if asker:
                payload["caption"] = asker
            self.emit("question-asked", payload, tool_use_id)
            return

        if tool_name == "ExitPlanMode":
            # The plan itself, asking for approval. The CLI routes it through
            # can_use_tool with `requires_user_interaction`; a blanket deny in
            # plan mode meant the plan could never be approved (measured on
            # 2.1.258, 2026-09-02). Always a dialog, whatever the mode.
            self._present_plan_review(req_id, req)
            return

        if self._permission_mode == AUTONOMY_MODE:
            self._respond_to_tool_approval(req_id, req, allow=True)
            return
        if self._permission_mode in {"dontAsk", "plan"}:
            # Plan mode is read-only: the CLI auto-allows reads, so anything
            # that reaches here is a write the plan has not been approved for.
            self._respond_to_tool_approval(req_id, req, allow=False)
            return
        self._present_tool_approval(req_id, req)

    def _present_tool_approval(self, request_id: str, req: dict) -> None:
        """Ask the user, offering what the CLI itself suggested.

        `permission_suggestions` carries the exact rule the CLI would write for
        "always allow" (`addRules` with a `ruleContent` per sub-command) or a
        `setMode` (acceptEdits for file writes). Those are the options the
        official harnesses show; Helios used to discard them for its own
        coarser prefix rule, which is kept only as the fallback.
        """
        tool_name = str(req.get("tool_name") or "")
        tool_use_id = str(req.get("tool_use_id") or request_id or tool_name)
        token = f"permission:{self.session_id}:{tool_use_id}"
        raw_input = req.get("input")
        suggestions = req.get("permission_suggestions")
        if not isinstance(suggestions, list):
            suggestions = []
        actions: dict[str, dict] = {}
        options: list[dict] = [
            {
                "label": "Allow once",
                "description": "Allow this exact tool request.",
            }
        ]
        actions["Allow once"] = {"allow": True}
        rules = _suggested_rules(suggestions)
        if rules:
            label = "Allow for this session"
            actions[label] = {
                "allow": True,
                "updates": [
                    {
                        "type": "addRules",
                        "behavior": "allow",
                        "destination": "session",
                        "rules": rules,
                    }
                ],
            }
            options.append(
                {
                    "label": label,
                    "description": (
                        "Also allow "
                        + "; ".join(_rule_text(rule) for rule in rules)
                        + " until this chat's Claude process exits. Nothing "
                        "is saved to disk."
                    ),
                }
            )
        else:
            rule = session_permission_rule(tool_name, raw_input)
            if rule:
                label = session_permission_label(rule)
                actions[label] = {
                    "allow": True,
                    "updates": [
                        {
                            "type": "addRules",
                            "behavior": "allow",
                            "destination": "session",
                            "rules": [rule],
                        }
                    ],
                }
                options.append(
                    {
                        "label": label,
                        # Say what ends the grant, because nothing else will:
                        # the rule lives in the CLI's session permission
                        # context and is never written to a settings file.
                        "description": (
                            "Stops asking until this chat's Claude process "
                            "exits. Nothing is saved to disk."
                        ),
                    }
                )
        if _suggested_mode(suggestions) == "acceptEdits":
            label = "Allow and auto-accept edits"
            actions[label] = {
                "allow": True,
                "updates": [
                    {
                        "type": "setMode",
                        "mode": "acceptEdits",
                        "destination": "session",
                    }
                ],
                "mode_after": "acceptEdits",
            }
            options.append(
                {
                    "label": label,
                    "description": (
                        "Switch this conversation to Auto-accept edits: file "
                        "edits stop asking, commands still do."
                    ),
                }
            )
        options.append(
            {
                "label": "Deny",
                "description": (
                    "Deny it and let Claude continue. Choose Other to tell "
                    "Claude why."
                ),
            }
        )
        actions["Deny"] = {"allow": False}
        self._pending_tool_approvals[token] = (request_id, req, actions)
        asker = self._note_prompt_asker(req, token)
        summary = _tool_approval_summary(tool_name, raw_input)
        description = str(req.get("description") or "").strip()
        question = summary
        if description and description not in summary:
            question = f"{description}\n{summary}"
        self.emit(
            "question-asked",
            {
                "allowOther": True,
                "requireExplicitChoice": True,
                "caption": " ".join(part for part in (asker, _decision_caption(req)) if part),
                "detail": _approval_detail(tool_name, raw_input),
                "questions": [
                    {
                        "header": f"Allow {tool_name}?" if tool_name else "Tool approval",
                        "question": question,
                        "multiSelect": False,
                        "allowOther": True,
                        "options": options,
                    }
                ],
            },
            token,
        )

    def _present_plan_review(self, request_id: str, req: dict) -> None:
        """Show the plan and let the user approve it, or send it back."""
        raw_input = req.get("input") if isinstance(req.get("input"), dict) else {}
        tool_use_id = str(req.get("tool_use_id") or request_id or "ExitPlanMode")
        token = f"plan:{self.session_id}:{tool_use_id}"
        plan = str(raw_input.get("plan") or "").strip()
        plan_path = str(raw_input.get("planFilePath") or "").strip()
        actions = {
            # Measured on 2.1.258: a plain allow leaves the CLI prompting for
            # writes again (default), allow + setMode stops it asking.
            "Approve plan": {"allow": True, "mode_after": "default"},
            "Approve and auto-accept edits": {
                "allow": True,
                "updates": [
                    {
                        "type": "setMode",
                        "mode": "acceptEdits",
                        "destination": "session",
                    }
                ],
                "mode_after": "acceptEdits",
            },
            "Keep planning": {
                "allow": False,
                "message": "The user wants changes to the plan before "
                "implementation. Revise it and call ExitPlanMode again.",
            },
        }
        self._pending_tool_approvals[token] = (request_id, req, actions)
        self.emit(
            "question-asked",
            {
                "allowOther": True,
                "requireExplicitChoice": True,
                "caption": (f"Saved to {plan_path}" if plan_path else ""),
                "detail": (
                    {"kind": "markdown", "title": "Proposed plan", "text": plan}
                    if plan
                    else None
                ),
                "questions": [
                    {
                        "header": "Plan review",
                        "question": (
                            "Claude proposes this plan. Approving starts the "
                            "implementation; choose Other to say what should "
                            "change."
                        ),
                        "multiSelect": False,
                        "allowOther": True,
                        "options": [
                            {
                                "label": "Approve plan",
                                "description": (
                                    "Start implementing; file edits and "
                                    "commands ask as usual."
                                ),
                            },
                            {
                                "label": "Approve and auto-accept edits",
                                "description": (
                                    "Start implementing with file edits "
                                    "auto-accepted for this conversation."
                                ),
                            },
                            {
                                "label": "Keep planning",
                                "description": "Send the plan back for revision.",
                            },
                        ],
                    }
                ],
            },
            token,
        )

    def _handle_control_cancel(self, obj: dict) -> None:
        """The CLI withdrew a pending can_use_tool: forget it and tell the UI."""
        request_id = str(obj.get("request_id") or "")
        if not request_id:
            return
        for token, (pending_id, _payload) in list(self._pending_questions.items()):
            if pending_id == request_id:
                del self._pending_questions[token]
                self._release_prompt_asker(token)
                self.emit("question-cancelled", token)
                return
        for token, entry in list(self._pending_tool_approvals.items()):
            if entry[0] == request_id:
                del self._pending_tool_approvals[token]
                self._release_prompt_asker(token)
                self.emit("question-cancelled", token)
                return

    def _note_prompt_asker(self, req: dict, token: str) -> str:
        """Name the subagent behind a prompt, and mark it as waiting on you.

        Measured on 2.1.258 (2026-09-03): a child-originated can_use_tool
        carries `agent_id`, which is the child's task id — the same value
        task_started gave the actor. Returns the caption, "" for the root.
        """
        agent_id = str(req.get("agent_id") or "")
        if not agent_id:
            return ""
        actor_id = next(
            (aid for aid, actor in self._agents.items() if actor.get("task_id") == agent_id),
            "",
        )
        if not actor_id:
            return "Asked by a subagent."
        actor = self._agents[actor_id]
        self._prompt_actors[token] = actor_id
        if actor.get("status") not in {"complete", "error", "stopped", "needs_input"}:
            actor["status"] = "needs_input"
            self.emit("agents-updated", copy.deepcopy(self._agents))
        return f"Asked by subagent: {actor.get('name') or actor_id}."

    def _release_prompt_asker(self, token: str) -> None:
        actor_id = self._prompt_actors.pop(token, None)
        if not actor_id or actor_id in self._prompt_actors.values():
            return  # another prompt from the same child is still open
        actor = self._agents.get(actor_id)
        if actor is not None and actor.get("status") == "needs_input":
            actor["status"] = "working"
            self.emit("agents-updated", copy.deepcopy(self._agents))

    # --- task plan (parity plan Phase 3D) -------------------------------------

    TASK_TOOLS = frozenset({"TaskCreate", "TaskUpdate", "TaskList", "TodoWrite"})

    def _note_task_tools(self, turn) -> None:
        """Register task-tool calls from a finished root message.

        TodoWrite carries the whole list and applies at once. TaskCreate's id
        and TaskUpdate's outcome only arrive in the tool_result, so those wait
        for `_note_task_tool_results`. Measured on 2.1.258 (2026-09-03).
        """
        for use in getattr(turn, "tool_uses", None) or ():
            name = str(getattr(use, "name", "") or "")
            if name not in self.TASK_TOOLS:
                continue
            use_id = str(getattr(use, "id", "") or "")
            inp = getattr(use, "input", None) or {}
            if not isinstance(inp, dict):
                continue
            if name == "TodoWrite":
                todos = inp.get("todos")
                if isinstance(todos, list):
                    self._task_plan = {
                        str(index): {
                            "step": str(todo.get("content") or ""),
                            "status": _plan_status(todo.get("status")),
                        }
                        for index, todo in enumerate(todos, 1)
                        if isinstance(todo, dict) and str(todo.get("content") or "")
                    }
                    self._emit_plan()
            elif name == "TaskCreate" and use_id:
                self._register_task_tool(
                    use_id,
                    ("create", str(inp.get("subject") or inp.get("description") or "")),
                )
            elif name == "TaskUpdate" and use_id:
                self._register_task_tool(
                    use_id,
                    (
                        "update",
                        str(inp.get("taskId") or ""),
                        str(inp.get("status") or ""),
                        str(inp.get("subject") or ""),
                    ),
                )
            elif name == "TaskList" and use_id:
                self._register_task_tool(use_id, ("list",))

    def _register_task_tool(self, use_id: str, pending: tuple) -> None:
        """Register a call; if its result already landed, apply it now."""
        if use_id not in self._early_task_results:
            self._pending_task_tools[use_id] = pending
            return
        early = self._early_task_results.pop(use_id)
        if early is _TASK_TOOL_FAILED:
            # The call errored before it was registered. Nothing to apply,
            # and nothing may be left pending — an unconsumed registration
            # never expires.
            return
        if self._apply_task_result(pending, early):
            self._emit_plan()

    def _remember_early_task_result(self, use_id: str, value: object) -> None:
        """Hold a result (or a failure tombstone) until its call registers."""
        self._early_task_results[use_id] = value
        while len(self._early_task_results) > 64:
            self._early_task_results.pop(next(iter(self._early_task_results)))

    def _apply_task_result(self, pending: tuple, text: str) -> bool:
        kind = pending[0]
        if kind == "create":
            match = _TASK_CREATED_RE.search(text)
            if match and pending[1]:
                self._task_plan[match.group(1)] = {"step": pending[1], "status": "pending"}
                return True
            return False
        if kind == "update":
            _kind, task_id, status, subject = pending
            task = self._task_plan.get(task_id)
            if task is None:
                return False
            if status.strip().lower() in {"deleted", "removed"}:
                del self._task_plan[task_id]
                return True
            if status:
                task["status"] = _plan_status(status)
            if subject:
                task["step"] = subject
            return True
        if kind == "list":
            parsed = _parse_task_list(text)
            if parsed:
                self._task_plan = parsed
                return True
        return False

    def _note_task_tool_results(self, turn) -> None:
        changed = False
        for res in getattr(turn, "tool_results", None) or ():
            use_id = str(getattr(res, "tool_use_id", "") or "")
            if not use_id:
                continue
            pending = self._pending_task_tools.pop(use_id, None)
            if getattr(res, "is_error", False):
                # A failed TaskCreate/TaskUpdate changes nothing, but its
                # registration must not sit in the pending map forever — and
                # the result can precede the registration, so leave a
                # tombstone for it to consume.
                if pending is None:
                    self._remember_early_task_result(use_id, _TASK_TOOL_FAILED)
                continue
            text = getattr(res, "content", "")
            text = text if isinstance(text, str) else json.dumps(text)
            if pending is None:
                # Unknown yet: keep it if it reads like a task-tool result, so
                # the registration that follows message_stop can claim it.
                if _looks_like_task_result(text):
                    self._remember_early_task_result(use_id, text)
                continue
            if self._apply_task_result(pending, text):
                changed = True
        if changed:
            self._emit_plan()

    def _emit_plan(self) -> None:
        def order(key: str) -> tuple[int, str]:
            return (int(key), "") if key.isdigit() else (10**9, key)

        steps = [
            {"step": task["step"], "status": task["status"]}
            for _key, task in sorted(self._task_plan.items(), key=lambda kv: order(kv[0]))
        ]
        self.emit(
            "plan-updated",
            {
                "threadId": self._session_id,
                "turnId": self._agents_root_turn_id or self._session_id,
                "explanation": None,
                "plan": steps,
                "source": "turn",
                "authoritative": True,
            },
        )

    def _refuse_control_request(self, request_id: str, subtype: str) -> None:
        """Answer a control_request Helios does not implement.

        An error response is the honest answer and lets the CLI degrade on its
        own terms; silence just stalls it. Deliberately not an empty `success`:
        claiming to have handled `hook_callback` would be worse than refusing.
        """
        self._write_stdin_line(
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": (
                        f"Helios does not implement control_request {subtype!r}"
                    ),
                },
            }
        )

    def _respond_to_tool_approval(
        self,
        request_id: str,
        request: dict,
        *,
        allow: bool,
        session_rule: dict | None = None,
        permission_updates: list | None = None,
        message: str = "",
    ) -> None:
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": "allow" if allow else "deny",
                },
            },
        }
        if allow:
            response["response"]["response"]["updatedInput"] = (
                request.get("input") or {}
            )
            updates: list = []
            if session_rule:
                updates.append(
                    {
                        "type": "addRules",
                        "behavior": "allow",
                        "destination": "session",
                        "rules": [session_rule],
                    }
                )
            if permission_updates:
                updates.extend(copy.deepcopy(permission_updates))
            if updates:
                # The deny path never carries a rule: a refusal must not be
                # able to widen permissions, whatever a caller passes.
                response["response"]["response"]["updatedPermissions"] = updates
        else:
            response["response"]["response"]["message"] = (
                message or "The user declined this tool request."
            )
        self._write_stdin_line(response)

    def _set_local_permission_mode(self, mode: str) -> None:
        """The CLI changed mode on its own (plan approved, setMode granted).

        Mirror it immediately so the very next can_use_tool is judged by the
        mode the CLI is actually in, then tell the window so the toolbar and
        the durable conversation settings follow.
        """
        mode = str(mode or "")
        if not mode or mode == self._permission_mode:
            return
        self._permission_mode = mode
        self.emit("permission-mode-changed", mode)

    def stop_task(self, actor_id: str) -> bool:
        """Stop one background subagent without interrupting the session.

        Measured on 2.1.258 (2026-09-02): `{"subtype":"stop_task","task_id"}`
        ACKs `{}`, the CLI emits `task_updated {status: killed}` and a
        `task_notification` with status `stopped`, which is where the actor's
        terminal comes from — nothing is marked here.
        """
        actor = self._agents.get(str(actor_id or ""))
        if not actor or actor.get("status") in {"complete", "error", "stopped"}:
            return False
        task_id = str(actor.get("task_id") or "")
        if not task_id:
            return False

        def landed(ok: bool, detail: str, _payload: dict) -> None:
            if not ok and not self._stop_requested:
                self.emit("error", f"Could not stop the subagent: {detail or 'no reply'}")

        return self._send_control_request(
            {"subtype": "stop_task", "task_id": task_id}, landed
        )

    # --- observed delegation actors ---------------------------------
    #
    # Every transition here is keyed on an id the PROVIDER supplied: the
    # `Task`/`Workflow` tool_use_id, which the child's own stream and terminal
    # records carry back as `parent_tool_use_id`. Nothing is inferred from
    # elapsed time or from the mere existence of a tool call, which is the
    # distinction the dormant adapter's docstring drew — a tool_use alone proved
    # only that delegation was *requested*.

    #: Root tools that delegate. Lowercased at the comparison site.
    # Match the name on the WIRE, not the one `system/init` advertises. CLI
    # 2.1.223 still lists the tool as `Task` in the init inventory but emits
    # `"name": "Agent"` in the tool_use block, so matching the inventory alone
    # silently seeded no actors at all. Measured 2026-08-06.
    DELEGATION_TOOLS = frozenset({"task", "workflow", "agent"})

    @property
    def observed_agent_root_turn_id(self) -> str:
        """The root turn the current actor projection belongs to.

        A property, matching the Codex driver: `_observed_agent_scope` reads it
        with `getattr`, so a plain method would hand the scope a
        ``"<bound method …>"`` string that passes the non-empty `is_valid` check.
        """
        return self._agents_root_turn_id

    def observed_agent_snapshot(self) -> dict[str, dict]:
        """Metadata-only snapshot of this turn's observed actors."""
        return copy.deepcopy(self._agents)

    def _reset_observed_agents(self, root_turn_id: str) -> None:
        """Start a fresh turn-scoped projection.

        Emits even when clearing, so a dock showing the previous turn's actors
        does not keep them on screen after a new turn begins.
        """
        had_any = bool(self._agents)
        self._agents = {}
        self._task_managed_actors = set()
        self._agents_root_turn_id = root_turn_id
        if had_any:
            self.emit("agents-updated", {})

    def _note_delegation_requests(self, turn) -> None:
        """Seed actors from the root turn's Task/Workflow tool_use blocks."""
        seeded = False
        for use in getattr(turn, "tool_uses", None) or ():
            name = str(getattr(use, "name", "") or "")
            if name.lower() not in self.DELEGATION_TOOLS:
                continue
            actor_id = str(getattr(use, "id", "") or "")
            if not actor_id or actor_id in self._agents:
                continue
            inp = getattr(use, "input", None) or {}
            self._agents[actor_id] = {
                # "starting" is the honest state: the request is provider-proven,
                # but nothing from the child has been observed yet.
                "status": "starting",
                "name": str(inp.get("description") or inp.get("subagent_type") or name),
                "role": str(inp.get("subagent_type") or name),
                "message": "",
                "tool": name,
                "task_id": "",
            }
            seeded = True
        if seeded:
            self.emit("agents-updated", copy.deepcopy(self._agents))

    def _note_child_progress(self, parent_tool_use_id: str) -> None:
        """A child produced output, so it is genuinely running."""
        actor = self._agents.get(parent_tool_use_id)
        if actor is None or actor.get("status") == "working":
            return
        # Never walk a terminal actor back to working. A late child stream event
        # after its own terminal record must not resurrect it; the model's
        # regression fence would drop it anyway, but not emitting is cheaper and
        # keeps the driver's own state honest. A child waiting on the user
        # (needs_input) stays waiting: its own output cannot answer the prompt.
        if actor.get("status") in {"complete", "error", "stopped", "needs_input"}:
            return
        actor["status"] = "working"
        self.emit("agents-updated", copy.deepcopy(self._agents))

    #: Keep an actor's surfaced tail short enough for a dock detail row.
    _CHILD_SNIPPET_CHARS = 120

    @staticmethod
    def _child_message_snippet(obj: dict) -> str:
        """Tail of a forwarded child message, for the actor's detail line.

        Prefers what the child *said* (text blocks) over what it *thought*
        (thinking blocks); a child that has only thought so far still gets a
        live line rather than nothing. Whitespace collapses so a multi-line
        message stays a one-line detail.
        """
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            texts, thoughts = [content], []
        elif isinstance(content, list):
            texts, thoughts = [], []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    texts.append(str(block.get("text") or ""))
                elif btype == "thinking":
                    thoughts.append(str(block.get("thinking") or ""))
        else:
            return ""
        joined = " ".join(" ".join(part.split()) for part in (texts or thoughts))
        joined = joined.strip()
        limit = ClaudeCliDriver._CHILD_SNIPPET_CHARS
        if len(joined) > limit:
            joined = "…" + joined[-limit:]
        return joined

    def _note_child_message(self, parent_tool_use_id: str, obj: dict) -> None:
        """A forwarded child message: promote to working, surface its tail.

        Same fences as `_note_child_progress`: an unknown parent id creates no
        actor, and a terminal actor is never resurrected or repainted by a
        late child record. Emission is bounded by the child's own message
        cadence — forwarded records are complete messages, not deltas.
        """
        actor = self._agents.get(parent_tool_use_id)
        if actor is None or actor.get("status") in {"complete", "error", "stopped"}:
            return
        changed = False
        if actor.get("status") not in {"working", "needs_input"}:
            actor["status"] = "working"
            changed = True
        snippet = self._child_message_snippet(obj)
        if snippet and snippet != actor.get("message"):
            actor["message"] = snippet
            changed = True
        if changed:
            self.emit("agents-updated", copy.deepcopy(self._agents))

    def _flush_deferred_task_results(self) -> None:
        """Deliver background wake-ups held during a live turn.

        Delivered ONLY from the ordinary root terminal, where the composer
        legitimately becomes idle. The budget-limit and persistence-failure
        paths withhold the real terminal and fence the driver, so they CLEAR
        instead: emitting there would make a background completion the only
        `result` a failed turn produced, re-enabling the composer and undoing
        the blocked state. Process death clears too. Either way nothing is
        left attached, because a held record drained by some later, unrelated
        turn is exactly the cross-turn confusion this guard exists to prevent.
        """

        deferred, self._deferred_task_results = self._deferred_task_results, []
        for held in deferred:
            self.emit("result", held)

    def _note_child_terminal(self, parent_tool_use_id: str, subtype: str) -> None:
        """The child's own `result` record — provider-native proof it finished.

        UNREACHED ON CLI 2.1.223, AND DELIBERATELY KEPT. No child `result`
        records arrive at all on this version;
        child terminals come through `system/task_notification` instead, which
        `_note_task_record` handles. The guard at the `result` branch is
        live, so this is not dead code — it is the correct handler for a record
        the current CLI happens not to emit. Right handler if they return,
        dead rather than wrong. Re-check when the pinned version moves.
        """
        actor = self._agents.get(parent_tool_use_id)
        if actor is None:
            return
        normalized = (subtype or "").strip().lower()
        if normalized == "success":
            actor["status"] = "complete"
        elif normalized in {"aborted", "interrupted", "cancelled", "canceled"}:
            actor["status"] = "stopped"
        else:
            # Includes the error_* subtypes and anything unrecognized. Failing
            # toward "error" is right for a terminal we cannot classify: the
            # child is definitely done, and silently calling it success would be
            # the misleading direction.
            actor["status"] = "error"
            actor["message"] = normalized
        self.emit("agents-updated", copy.deepcopy(self._agents))

    def _note_task_record(self, subtype: str, obj: dict) -> None:
        """Drive an actor from the `system/task_*` lifecycle.

        These records are keyed on the same `tool_use_id` as the delegation
        request, so this stays causal by construction — nothing is inferred from
        ordering or elapsed time.

        This channel exists because the root-level tool_result is NOT a terminal
        for a backgrounded child. Measured 2026-08-06: a background `Agent` gets
        its tool_result immediately ("Async agent launched successfully"), 36
        records before the child actually finished. Closing on that would show
        every subagent as complete the instant it launched — the exact opposite
        of what a fan-out needs to be visible. So a task-managed actor takes its
        terminal only from `task_notification`, and `_note_delegation_results`
        skips it. Both foreground and background children emit this lifecycle.
        """
        actor_id = str(obj.get("tool_use_id") or "")
        if not actor_id:
            return
        actor = self._agents.get(actor_id)
        if actor is None:
            if subtype != "task_started":
                return
            # task_started can land before the root message_stop that seeds
            # the actor from the tool_use block (2026-09-02 audit, gap 10).
            # Seed provisionally from the record itself; the later seeding
            # pass skips ids it already knows.
            actor = self._agents[actor_id] = {
                "status": "starting",
                "name": str(
                    obj.get("description") or obj.get("subagent_type") or "agent"
                ),
                "role": str(obj.get("subagent_type") or obj.get("task_type") or ""),
                "message": "",
                "tool": "Agent",
                "task_id": "",
            }
        task_id = str(obj.get("task_id") or "")
        if task_id:
            # What `stop_task` needs; only the task lifecycle carries it.
            actor["task_id"] = task_id
        # Tracked outside the actor dict: the emitted payload must stay
        # byte-compatible with the Codex driver's, so the dock needs no branch.
        self._task_managed_actors.add(actor_id)
        if actor.get("status") in {"complete", "error", "stopped"}:
            # Never resurrect a terminal actor with a late record.
            return
        if subtype == "task_notification":
            status = str(obj.get("status") or "").strip().lower()
            # task_notification is terminal by definition; classify the
            # vocabulary by stem so a new spelling (measured: "stopped" on
            # 2.1.258) does not paint a healthy fan-out red.
            if any(stem in status for stem in ("complet", "success", "done")):
                actor["status"] = "complete"
            elif any(
                stem in status
                for stem in ("stop", "abort", "interrupt", "cancel", "kill")
            ):
                actor["status"] = "stopped"
            else:
                actor["status"] = "error"
                actor["message"] = status
        else:
            # task_started proves the child was dispatched; task_progress proves
            # it produced something. Neither is terminal.
            if actor.get("status") == "working":
                return
            actor["status"] = "working"
        self.emit("agents-updated", copy.deepcopy(self._agents))

    def _note_delegation_results(self, turn) -> None:
        """Close actors from the root turn's tool_result blocks.

        Belt and braces alongside `_note_child_terminal`: the child's own
        terminal record is the richer signal, but the root-level tool_result is
        the one guaranteed to arrive, because it is what unblocks the root model.

        Skips task-managed actors — for those the tool_result can arrive while
        the child is still running (see `_note_task_record`).
        """
        changed = False
        for res in getattr(turn, "tool_results", None) or ():
            actor_id = str(getattr(res, "tool_use_id", "") or "")
            actor = self._agents.get(actor_id)
            if actor is None or actor.get("status") in {"complete", "error", "stopped"}:
                continue
            if actor_id in self._task_managed_actors:
                continue
            actor["status"] = "error" if getattr(res, "is_error", False) else "complete"
            changed = True
        if changed:
            self.emit("agents-updated", copy.deepcopy(self._agents))

    def answer_question(self, tool_use_id: str, answer: object) -> None:
        """Public method used by MainWindow once the dialog closes.

        Questions carry the dialog's answer map (`{"0": {"answers": [...]}}`)
        or a plain string; approvals carry the chosen option label, the free
        text the user typed as a reason, or None when the dialog was
        dismissed. Anything unrecognised denies — a refusal must be the
        default outcome of an answer Helios cannot interpret.
        """
        self._release_prompt_asker(tool_use_id)
        pending = self._pending_questions.pop(tool_use_id, None)
        if pending is not None:
            request_id, input_payload = pending
            self.respond_to_question(request_id, answer, input_payload)
            return
        approval = self._pending_tool_approvals.pop(tool_use_id, None)
        if approval is None:
            return
        request_id, request, actions = approval
        label = answer.strip() if isinstance(answer, str) else ""
        action = actions.get(label)
        if action is None:
            self._respond_to_tool_approval(
                request_id,
                request,
                allow=False,
                message=(
                    f"The user declined this tool request: {label}"
                    if label
                    else ""
                ),
            )
            return
        self._respond_to_tool_approval(
            request_id,
            request,
            allow=bool(action.get("allow")),
            permission_updates=action.get("updates"),
            message=str(action.get("message") or ""),
        )
        if action.get("allow") and action.get("mode_after"):
            self._set_local_permission_mode(str(action["mode_after"]))

    # Stop is staged so we don't SIGKILL a process that was happy to clean
    # up if asked. SIGINT → 800ms grace → SIGTERM → 800ms grace → SIGKILL.
    # Most claude turns react to SIGINT within a couple hundred ms.
    _STOP_TERM_AFTER_MS = 800
    _STOP_KILL_AFTER_MS = 1600

    def end_input(self) -> None:
        """Close stdin so a persistent stream-json claude finishes its current
        turn (if any) and exits on EOF — fully graceful, no SIGINT, no
        "Request interrupted by user" injected into the JSONL.

        Used when detaching a driver we no longer drive but whose in-flight
        turn should still complete. Without this the process blocks on
        `read(stdin)` forever (verified: idle orphans accumulate), because in
        `--input-format stream-json` mode claude does NOT self-exit after a
        turn — it waits for the next message. EOF tells it there won't be one.
        """
        if self._stdin is None or self._closed:
            return
        self._fail_pending_control("the Claude process stopped before acknowledging the change")
        try:
            self._stdin.close(None)
        except GLib.Error:
            pass
        self._stdin = None

    def stop(self, *, interrupt: bool = True) -> None:
        """Best-effort shutdown.

        Two flavours, selected by `interrupt`:

        * `interrupt=True` (the explicit Stop button): send SIGINT first so
          claude aborts the in-flight turn the way Ctrl-C does. This is the
          ONLY path that should ever use SIGINT, because claude reacts to
          SIGINT by writing a `[Request interrupted by user]` user-record into
          the session JSONL. That marker is correct when the *user* asked to
          stop — and wrong everywhere else.

        * `interrupt=False` (app close, reaping a detached driver, starting a
          fresh chat): close stdin (EOF) and SIGTERM immediately, then SIGKILL
          as a backstop. No SIGINT → no spurious `[Request interrupted by
          user]` lands in a transcript the user never touched. claude writes
          whole JSONL records, so terminating between records leaves the file
          at a clean boundary, not half a line.

        Either way: if claude exits before the scheduled stages fire, the
        alive-checks noop.

        When we launched claude under `setsid` (the normal case, see start()),
        the SIGTERM/SIGKILL escalation stages signal the whole process GROUP —
        so claude's Bash-tool children get cleaned up too instead of orphaning.
        SIGINT still goes to claude alone: it's the interactive-interrupt path
        and claude tears down its own in-flight tool on Ctrl-C.
        """
        if self._proc is None or self._closed:
            return
        self._closed = True
        self._stop_requested = True
        self._fail_pending_control("the Claude process stopped before acknowledging the change")
        try:
            if self._stdin is not None:
                self._stdin.close(None)
        except GLib.Error:
            pass
        self._stdin = None

        pid = None
        try:
            ident = self._proc.get_identifier()
            pid = int(ident) if ident is not None else None
        except (GLib.Error, ValueError, TypeError):
            pid = None

        if pid is None:
            # No PID to signal — fall back to immediate force_exit.
            try:
                self._proc.force_exit()
            except GLib.Error:
                pass
            return

        process_group_id = self._process_group_id

        # Background subagents run inside the CLI process, so the SIGTERM /
        # SIGKILL stages below end them with it; no per-child stop_task is
        # needed here (stdin is already closed anyway). stop_task is the
        # SELECTIVE path, from the dock, while the session keeps running.
        # Stage 1: SIGINT only on explicit user interrupt (to claude alone, so
        # it writes the interrupt marker and unwinds its own tool). Otherwise
        # go straight to SIGTERM of the whole group — no marker injected, and
        # Bash-tool children die with claude. stdin is already closed above, so
        # a terminated turn also hits EOF.
        if interrupt:
            _safe_kill(pid, signal.SIGINT)
        else:
            _signal_process_tree(process_group_id, pid, signal.SIGTERM)

        # Stage 2 + 3 scheduled — if claude exits cleanly before they fire,
        # the alive-check noops.
        proc = self._proc

        def _stage_term() -> bool:
            if _is_process_tree_alive(process_group_id, pid):
                _signal_process_tree(process_group_id, pid, signal.SIGTERM)
            return False  # don't repeat

        def _stage_kill() -> bool:
            if _is_process_tree_alive(process_group_id, pid):
                # Reap the whole group, then force_exit the tracked pid as a
                # backstop in case the group signal missed a re-parented child.
                _signal_process_tree(process_group_id, pid, signal.SIGKILL)
                try:
                    proc.force_exit()
                except GLib.Error:
                    _safe_kill(pid, signal.SIGKILL)
            return False

        GLib.timeout_add(self._STOP_TERM_AFTER_MS, _stage_term)
        GLib.timeout_add(self._STOP_KILL_AFTER_MS, _stage_kill)

    # --- async readers ---

    def _read_next_stdout_line(self) -> None:
        # Note: deliberately NOT gated on `_closed`. After stop(), claude's
        # interrupt unwinding still writes the "[Request interrupted]" turn
        # and the final `result` — dropping them left the on-screen
        # transcript missing its ending (review M4). The loop terminates
        # naturally at EOF instead.
        if self._stdout is None:
            return
        self._stdout.read_line_async(
            GLib.PRIORITY_DEFAULT, None, self._cb_stdout, None
        )

    def _on_stdout_line(self, src: Gio.DataInputStream, res, _user_data) -> None:
        if self._stdout is None:
            return
        try:
            line, _len = src.read_line_finish_utf8(res)
        except GLib.Error as e:
            # A single undecodable line (e.g. a tool dumping a non-UTF-8 byte)
            # must not permanently stall the stream — the old code returned
            # here without rescheduling, leaving the session hung mid-reply.
            # Skip it and keep reading; bail out only if errors are relentless
            # (a genuine broken pipe) to avoid a hot loop.
            self._stdout_err_count += 1
            if self._stdout_err_count > self._MAX_STDOUT_ERRORS:
                self.emit("error", f"stdout read failed: {e.message}")
                return
            _log.warning("stdout read error (skipping line): %s", e.message)
            self._read_next_stdout_line()
            return
        self._stdout_err_count = 0
        if line is None:
            # EOF — wait for exit callback
            return
        line = line.strip()
        if line:
            if _HELIOS_DEBUG:
                try:
                    _o = json.loads(line)
                    _dbg(f"stdout type={_o.get('type')} subtype={_o.get('subtype')}")
                except json.JSONDecodeError:
                    _dbg(f"stdout NON-JSON: {line[:120]!r}")
            self._handle_stdout_line(line)
        # Continue reading.
        self._read_next_stdout_line()

    def _read_next_stderr_line(self) -> None:
        if self._stderr is None:
            return
        self._stderr.read_line_async(
            GLib.PRIORITY_DEFAULT_IDLE, None, self._cb_stderr, None
        )

    def _on_stderr_line(self, src: Gio.DataInputStream, res, _user_data) -> None:
        if self._stderr is None:
            return
        try:
            line, _len = src.read_line_finish_utf8(res)
        except GLib.Error:
            return
        if line is None:
            return
        # Most stderr is noise (debug, deprecation warnings). We surface
        # only lines that look like errors.
        s = line.strip()
        if _HELIOS_DEBUG and s:
            _dbg(f"stderr: {s[:200]}")
        if s and ("Error" in s or "FATAL" in s or "panic" in s):
            self.emit("error", f"stderr: {s}")
        self._read_next_stderr_line()

    def _on_exit(self, proc: Gio.Subprocess, _res, _data) -> None:
        self._closed = True
        self._fail_pending_control("the Claude process exited before acknowledging the change")
        # If we killed it via signal, get_exit_status would assert. Check
        # whether it exited normally first.
        if proc.get_if_exited():
            code = proc.get_exit_status()
        elif proc.get_if_signaled():
            code = 128 + proc.get_term_sig()
        else:
            code = -1
        self._busy = False
        # The process is gone and the session is over: a held wake-up is moot
        # and emitting a `result` for a dead driver would report a completion
        # against a session being torn down. Drop rather than deliver.
        self._deferred_task_results.clear()
        if (
            self.execution_attempt_id
            and self._execution_dispatch_recorded
            and self._stop_requested
        ):
            # We asked this child to stop and then watched it exit, so the
            # cancellation is confirmed even though Claude never sent a
            # terminal receipt. Record only that — aborted, no receipt, no
            # usage — and release the lane. Holding it here instead bricks the
            # Work: nothing survives to produce the receipt, so per-Work
            # admission refuses every later message with "already executing"
            # until Helios restarts and the startup sweep reclaims it.
            native_id = self._confirmed_execution_native_id()
            acknowledgement: dict[str, object] = {
                "acknowledged": True,
                "cancellation_confirmed": True,
                "request_id": self.execution_attempt_id,
            }
            if native_id:
                acknowledgement["native_id"] = native_id
            if not self._finish_execution_with_evidence(
                ExecutionTerminalEvidence(
                    evidence_type="cancellation_ack",
                    status="aborted",
                    reason_code="claude.stopped_by_helios",
                    request_id=self.execution_attempt_id,
                    native_id=native_id,
                    stop_acknowledgement=acknowledgement,
                    queue_disposition="restored",
                )
            ):
                self.emit(
                    "error",
                    "Helios could not release this Work's execution slot after "
                    "the stop; new provider work remains blocked.",
                )
        elif self.execution_attempt_id and self._execution_dispatch_recorded:
            # Nobody asked: claude died on its own. Process death cannot tell
            # us whether it consumed the frame, executed tools, or committed a
            # terminal result before the pipe vanished. Preserve the durable
            # running row for reconciliation.
            self._record_execution_stop(
                ExecutionStopEvidence(
                    acknowledgement={},
                    queue_disposition="held",
                )
            )
            self.emit(
                "error",
                "Claude exited without a provider-terminal receipt; this Work "
                "remains blocked until provider-backed recovery confirms the outcome.",
            )
        elif not self._finish_execution_with_evidence(
            ExecutionTerminalEvidence(
                evidence_type="local_abort",
                status="aborted",
                reason_code="local_abort",
                queue_disposition="restored",
            )
        ):
            self.emit(
                "error",
                "Helios could not persist execution completion; "
                "new provider work remains blocked.",
            )
        _dbg(f"EXITED code={code}")
        self.emit("exited", code)

    # --- event dispatch ---

    def _handle_stdout_line(self, line: str) -> None:
        self.last_activity = time.monotonic()
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return  # ignore garbage; subprocess sometimes prints stray text
        try:
            self._dispatch_record(obj)
        except Exception:
            # A malformed/unexpected record (claude CLI schema drift, a corrupt
            # line) must never escape this GLib stdout callback — that would
            # kill the read loop and strand the session mid-reply with no error
            # shown. Log it and keep streaming.
            _log.exception(
                "error handling stdout record (type=%r)",
                obj.get("type") if isinstance(obj, dict) else None,
            )

    def _dispatch_record(self, obj: dict) -> None:
        rtype = obj.get("type")

        if rtype == "system":
            sub = obj.get("subtype")
            if sub == "compact_boundary":
                # `--autocompact auto` is on (see the spawn argv), so this
                # arrives unprompted mid-session. The CLI reports it and Helios
                # used to drop it on the floor, which is why compaction was
                # invisible here while the terminal shows it.
                meta = obj.get("compactMetadata") or {}
                self.emit(
                    "context-compacted",
                    {
                        # "auto" when the window filled, "manual" when the user
                        # asked. Both are worth showing; only one is a surprise.
                        "trigger": str(meta.get("trigger") or "auto"),
                        "pre_tokens": int(meta.get("pre_tokens") or 0),
                    },
                )
                return
            if sub == "init":
                self._session_id = obj.get("session_id") or ""
                self._model = obj.get("model") or self._model
                # Stash the authoritative tool/MCP inventory before emitting so
                # `session-started` handlers can read it off the driver.
                self._init_tools = obj.get("tools") or []
                self._init_mcp_servers = obj.get("mcp_servers") or []
                # `terminal_slash_commands` is the CLI's own list of names that
                # only work in its interactive TUI (measured on 2.1.238:
                # `doctor` and `color`). Subtracting it means Helios never
                # strips the Goal envelope for a token that was going to be
                # prose anyway.
                self._init_slash_commands = {
                    str(name)
                    for name in (obj.get("slash_commands") or [])
                    if str(name)
                } - {
                    str(name) for name in (obj.get("terminal_slash_commands") or [])
                }
                self.emit(
                    "session-started",
                    self._session_id,
                    obj.get("cwd") or "",
                    obj.get("model") or "",
                )
                dispatch = getattr(self, "_execution_dispatch_evidence", None)
                if (
                    self.execution_attempt_id
                    and dispatch is not None
                    and not dispatch.native_binding_id
                    and self._session_id
                    and self._confirmed_execution_native_id() == self._session_id
                    and not self._record_execution_dispatch(
                        ExecutionDispatchEvidence(
                            wire_prompt_text=dispatch.wire_prompt_text,
                            provider_request_key=dispatch.provider_request_key,
                            native_binding_id=self._session_id,
                        )
                    )
                ):
                    self.emit(
                        "error",
                        "Helios could not persist Claude's native session "
                        "identity; new provider work remains blocked.",
                    )
            elif sub in {"task_started", "task_progress", "task_notification"}:
                self._note_task_record(sub, obj)
            elif sub == "api_retry":
                # The one record that distinguishes "the API is failing and
                # will try again" from "the model is taking a while". Nothing
                # else streams during a retry, so without this the strip sits
                # on its last state and the session looks dead.
                self.emit(
                    "activity-updated",
                    {
                        "category": "retry",
                        "attempt": obj.get("attempt"),
                        "max_retries": obj.get("max_retries"),
                        "retry_delay_ms": obj.get("retry_delay_ms"),
                        # int | null — null means a connection-level failure
                        # (timeouts are not retried with a status).
                        "error_status": obj.get("error_status"),
                    },
                )
            elif sub == "status":
                phase = str(obj.get("status") or "")
                if phase:
                    self.emit(
                        "activity-updated",
                        {"category": "phase", "phase": phase},
                    )
            elif sub == "thinking_tokens":
                tokens = obj.get("estimated_tokens")
                if isinstance(tokens, int) and tokens > 0:
                    self.emit(
                        "activity-updated",
                        {"category": "thinking", "tokens": tokens},
                    )
            elif sub in {"hook_started", "hook_progress", "hook_response"}:
                self.emit(
                    "hook-event",
                    {
                        key: value
                        for key, value in obj.items()
                        if key not in {"type", "uuid", "session_id"}
                    },
                )
            # remaining system subtypes are informational

        elif rtype == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            if isinstance(info, dict) and info.get("rateLimitType"):
                self.emit("rate-limit-updated", info)

        elif rtype == "control_request":
            # The CLI emits this when `--permission-prompt-tool stdio` is set
            # and a tool wants permission. AskUserQuestion and tool approvals
            # both route through the serialized UI broker below.
            self._handle_control_request(obj)

        elif rtype == "control_response":
            # Responses to Helios-originated initialize / live-setting
            # requests. Incoming permission answers use the same top-level
            # record type in the opposite direction but are only written, not
            # dispatched here.
            self._handle_control_response(obj)

        elif rtype == "control_cancel_request":
            # The CLI withdrew a can_use_tool it had asked (its own interrupt,
            # a timeout, or the turn ending). Leaving the entry pending would
            # keep a dialog open for a request nobody will read the answer to.
            self._handle_control_cancel(obj)

        elif rtype == "stream_event":
            parent_id = str(obj.get("parent_tool_use_id") or "")
            if parent_id:
                # Child-agent activity is not evidence that the root Claude
                # process consumed the current root prompt, and it must not
                # mutate the root streaming accumulator. It IS evidence that
                # that specific child is running, which is the one thing the
                # actor projection may take from it.
                self._note_child_progress(parent_id)
                return
            ev = obj.get("event") or {}
            etype = ev.get("type")
            # Subagent (Task tool) traffic is its own window — never fold it
            # into the main conversation's meter.
            self._track_stream_usage(ev, etype)
            if etype == "message_start":
                self._streaming = StreamingAssistant()
                self._streaming.apply_stream_event(ev)
                # NOT a projection reset. A turn that delegates emits a second
                # root message after the launch tool_results come back, and
                # resetting here wiped the dock exactly while the children
                # were running (2026-09-02 audit, gap 3). The projection is
                # scoped to the USER turn, reset in send_user_text.
            elif self._streaming is not None:
                self._streaming.apply_stream_event(ev)
                if etype == "message_stop":
                    # Message is done — finalize it from the streaming
                    # aggregator (which already collected every text /
                    # thinking / tool_use block via the deltas) and emit a
                    # SINGLE turn-appended for the whole message. Previously
                    # we relied on the canonical `assistant` records, but
                    # claude emits one per `content_block_stop` — each
                    # containing just ONE block of a multi-block message —
                    # so we'd post 3 separate bubbles for a single
                    # "thinking + tool_use + tool_use" response.
                    final_turn = self._streaming.to_turn()
                    if final_turn.has_content:
                        if self.execution_attempt_id and not (
                            self._record_execution_contribution(final_turn)
                        ):
                            self.emit(
                                "error",
                                "Helios could not persist Claude's contribution; "
                                "new provider work remains blocked.",
                            )
                        self.emit("turn-appended", final_turn)
                    # After the turn is on screen: any Task/Workflow block in it
                    # is a provider-proven delegation request.
                    self._note_delegation_requests(final_turn)
                    self._note_task_tools(final_turn)
                    self._streaming = None
                else:
                    self.emit("assistant-streaming", self._streaming)

        elif rtype == "assistant":
            parent_id = str(obj.get("parent_tool_use_id") or "")
            if parent_id:
                # A forwarded child message (--forward-subagent-text). It
                # belongs to the child's own window: never the root transcript,
                # never the root accumulators. It IS the child speaking, which
                # is the one thing the actor projection may surface.
                self._note_child_message(parent_id, obj)
                return
            # Per-block-stop canonical snapshot — ignored. The streaming
            # aggregator already has equivalent content from the
            # stream_event deltas, and we synthesize the final turn on
            # `message_stop` above. Surfacing these here would create one
            # bubble per block of a multi-block message.
            pass

        elif rtype == "user":
            parent_id = str(obj.get("parent_tool_use_id") or "")
            if parent_id:
                # A forwarded child-side user record (its prompt or its tool
                # results). Rendering it would put the subagent's inner
                # conversation into the root transcript as a "You" turn — the
                # trap measured on 2.1.241 the day --forward-subagent-text was
                # wired. It is still causal proof the child advanced.
                self._note_child_progress(parent_id)
                return
            # Could be a real user echo (--replay) or a synthetic tool_result
            # wrapper. We only want tool_result wrappers in the UI — drop
            # echoes, since the UI already added the user's message on send.
            if obj.get("isReplay"):
                self._confirm_matching_user_replay(obj)
                return
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, str) and content.lstrip().startswith(
                "<local-command-stdout>"
            ):
                # The CLI's local echo of an applied command (set_model, slash
                # commands). Command feedback, not conversation — rendering it
                # would fabricate a user bubble the user never typed.
                return
            from helios.backend.transcript import turn_from_record

            turn = turn_from_record(obj)
            if turn is not None and turn.has_content:
                self.emit("turn-appended", turn)
            if turn is not None:
                # The root-level tool_result for a Task/Workflow is what
                # unblocks the root model, so it is the arrival-guaranteed
                # "done" signal.
                self._note_delegation_results(turn)
                self._note_task_tool_results(turn)

        elif rtype == "result":
            child_id = str(obj.get("parent_tool_use_id") or "")
            if child_id:
                # A child-agent terminal belongs to its own context window.
                # It cannot finish the root execution attempt or drain the
                # root user's queue — but it is provider-native proof that
                # that child is done, and carries its outcome.
                self._note_child_terminal(child_id, str(obj.get("subtype") or ""))
                return
            # Cumulative spend for this process, split root vs delegated
            # Folded in before the task-notification branch below on
            # purpose: those records ARE roots, and for a background fan-out
            # they are exactly when the children's cost lands. Skipping them
            # would leave the burn invisible for the case that caused the
            # ticket.
            spend = self._spend.observe(obj)
            if spend is not None:
                self.emit("spend-updated", spend)
            origin = obj.get("origin")
            if isinstance(origin, dict) and origin.get("kind") == "task-notification":
                # A background subagent finishing WAKES THE ROOT for a turn
                # Helios never sent a message for (measured: one
                # user message produced three such records). It carries no
                # `parent_tool_use_id` — it IS the root — so it used to fall
                # through and close whichever execution attempt was open,
                # which after a queue flush is the NEXT user message, still
                # in flight.
                #
                # It owns no attempt, so it must not confirm delivery, flip
                # `_busy`, book terminal accounting or drain the queue. But it
                # IS a real turn with real content, and for a BACKGROUND
                # session `_on_turn_result` is the only thing that tells the
                # user their Work finished — so the signal is kept.
                #
                # Deferred rather than emitted while `_busy`, because that
                # signal also re-enables the composer and clears the activity
                # strip for the *current* driver — a straggler arriving
                # mid-turn would otherwise report a live turn as finished.
                # Deferred, NOT dropped: discarding it would lose the only
                # notification a background Work produced, which is the whole
                # reason the signal is kept at all.
                used, window = self._main_model_context(obj)
                if window > 0:
                    if not self._ctx_window_authoritative:
                        self._ctx_window = window
                    self.emit("usage-updated", used, self._ctx_window or window)
                if self._busy:
                    self._deferred_task_results.append(obj)
                else:
                    self.emit("result", obj)
                return
            # A terminal record is provider-native proof even when no streamed
            # assistant message preceded it (for example an early error).
            self._confirm_uncertain_delivery()
            self._busy = False
            used, window = self._main_model_context(obj)
            if window > 0:
                # modelUsage.contextWindow is the HARD model cap (1,000,000 on
                # sonnet). get_context_usage.maxTokens is the usable budget
                # before autocompact fires (967,000 = cap minus the 33,000
                # autocompact buffer, measured 2026-08-07 on 2.1.224). Both
                # were being written into this one field, which is why the
                # gauge could change meaning between turn 1 and turn 2. The
                # actionable number wins; the cap only fills in when the
                # control frame has not answered.
                if not self._ctx_window_authoritative:
                    self._ctx_window = window
                self.emit("usage-updated", used, self._ctx_window or window)
            # Re-read after every turn: the categories move as the
            # conversation grows, and this is free.
            self.request_context_usage()
            provider_status = str(obj.get("subtype") or "unknown")
            normalized_status = provider_status.lower()
            if normalized_status == "success":
                terminal_status = "completed"
            elif normalized_status in {
                "aborted",
                "interrupted",
                "cancelled",
                "canceled",
            }:
                terminal_status = "aborted"
            elif normalized_status in {"budgetlimited", "error_max_budget_usd"}:
                terminal_status = "budgetLimited"
            else:
                terminal_status = "failed"
            if not self._finish_execution_with_evidence(
                ExecutionTerminalEvidence(
                    evidence_type="provider_terminal",
                    status=terminal_status,
                    reason_code="claude.provider_terminal",
                    provider_status=provider_status,
                    request_id=self.execution_attempt_id,
                    native_id=self._confirmed_execution_native_id(),
                    usage=_terminal_usage(obj),
                    cost_micro_usd=_terminal_cost_micro_usd(obj),
                    queue_disposition=(
                        "restored"
                        if terminal_status == "budgetLimited"
                        else "released"
                    ),
                )
            ):
                self.emit(
                    "error",
                    "Helios could not persist execution completion; "
                    "the result was withheld and new provider work remains blocked.",
                )
                # DISCARD, not deliver. This path withholds the real terminal
                # and fences the driver; pushing a held wake-up through the
                # ordinary `result` signal would re-enable the composer and
                # clear the activity strip, undoing the blocked state — and it
                # would be the only `result` this failed turn ever emitted.
                self._deferred_task_results.clear()
                self.end_input()
                return
            if obj.get("subtype") == "error_max_budget_usd":
                self.emit(
                    "budget-exhausted",
                    {
                        "kind": "usd",
                        "limit": self._max_budget_usd,
                        "provider": self.provider,
                    },
                )
                self.emit(
                    "error",
                    "Claude reached this Work's native budget limit. "
                    "Queued messages were left unsent; start a new bounded Work.",
                )
                # Same reasoning as the persistence-failure path above: the
                # breaker has tripped and must stay tripped.
                self._deferred_task_results.clear()
                self.end_input()
                return
            self.emit("result", obj)
            # Any background-subagent wake-up that landed mid-turn was held
            # rather than dropped. The live turn has now reported, so the
            # composer is idle and these can be delivered without a completed
            # background Work reading as the current turn finishing.
            self._flush_deferred_task_results()
            # Turn complete — auto-send the next queued message, if any. The
            # explicit-Stop path drains the queue (take_queued) BEFORE stop(),
            # so an interrupt's result never auto-sends.
            self._flush_user_queue()

    # These names stay on the driver for compatibility with the UI and tests,
    # but the implementation is GTK-free so the slim CI lane executes it.
    _model_family = staticmethod(_gtk_free_model_family)

    def _track_stream_usage(self, ev: dict, etype: str) -> None:
        """Update the context meter from the request currently on the wire.

        `message_start` carries the prompt side of THIS request (uncached
        input + cached prefix + newly-cached prefix) — i.e. exactly what is
        resident in the window right now. `message_delta` carries the running
        output count. Emitting on both means the meter moves during a long
        agentic turn instead of jumping once at the end.
        """
        if etype == "message_start":
            usage = (ev.get("message") or {}).get("usage") or {}
            self._ctx_request_input = _prompt_tokens(usage)
            self._ctx_request_output = int(usage.get("output_tokens") or 0)
        elif etype == "message_delta":
            usage = ev.get("usage") or {}
            if "output_tokens" not in usage:
                return
            self._ctx_request_output = int(usage.get("output_tokens") or 0)
        else:
            return
        window = self._ctx_window or context_window_for(
            self._requested_model or self._model
        )
        if window > 0 and self._ctx_request_input > 0:
            self.emit(
                "usage-updated",
                self._ctx_request_input + self._ctx_request_output,
                window,
            )

    _main_model_context = _gtk_free_main_model_context


_TERMINAL_USAGE_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)



def _terminal_usage(result: dict) -> dict[str, object] | None:
    """Copy only finite, nonnegative integer counters from a native result."""

    raw = result.get("usage")
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


def _terminal_cost_micro_usd(result: dict) -> int | None:
    """Kept as the driver's name for it; the parser lives in streaming.py so
    the slim CI lane and the transcript footer share one implementation."""
    return terminal_cost_micro_usd(result)


#: Shell metacharacters that make a Bash command more than one thing. A rule
#: derived from the first token of `touch a && rm -rf b` would be labelled
#: "touch" and is not a grant anyone consented to, so compound commands get no
#: session option at all — Allow once / Deny, exactly as before.
_SHELL_COMPOUND = (";", "&", "|", "`", "$(", "\n", ">", "<")

#: A plain executable name. Anything with a quote, glob or variable in it is
#: not something we can name in a button, so it gets no session option.
_PLAIN_COMMAND = re.compile(r"^[A-Za-z0-9._/-]+$")


def session_permission_rule(tool_name: str, raw_input: object) -> dict | None:
    """The narrowest session-scoped allow rule that covers this exact request.

    Returns a `rules[]` entry for a `can_use_tool` response's
    `updatedPermissions`, or None when no grant can be named precisely enough
    to put on a button.

    Verified end-to-end against claude 2.1.245 rather than assumed:
    answering one `can_use_tool` with

        {"behavior": "allow", "updatedInput": {...},
         "updatedPermissions": [{"type": "addRules", "behavior": "allow",
                                 "destination": "session",
                                 "rules": [{"toolName": "Bash",
                                            "ruleContent": "touch:*"}]}]}

    let the next two `touch` calls through with no prompt and **still
    prompted for `rm -f`** — the CLI's own matcher does the scoping, and no
    settings file was written (destination "session" is memory-only; the
    CLI's accept-set for a host-supplied update is exactly
    {"localSettings", "session"}).

    Bash is the tool where a blanket grant is worst, so it is the one tool
    scoped by command rather than tool-wide.
    """

    name = str(tool_name or "").strip()
    if not name:
        return None
    if name != "Bash":
        # ponytail: tool-wide for everything else. Read/Edit/Write rule content
        # is a path-pattern dialect of Claude's own; deriving it here would
        # duplicate the matcher we are deliberately delegating to. Narrow these
        # per-tool only if a blanket session grant proves too coarse in use.
        return {"toolName": name}
    command = ""
    if isinstance(raw_input, dict):
        command = str(raw_input.get("command") or "").strip()
    if not command or any(tok in command for tok in _SHELL_COMPOUND):
        return None
    head = command.split()[0]
    if not _PLAIN_COMMAND.match(head):
        return None
    return {"toolName": "Bash", "ruleContent": f"{head}:*"}


def session_permission_label(rule: dict) -> str:
    """The button text. It must name the grant, not just its duration."""

    content = str(rule.get("ruleContent") or "")
    if content.endswith(":*"):
        return f"Allow all {content[:-2]} commands for this session"
    return f"Allow all {rule.get('toolName')} calls for this session"


def _normalize_cli_commands(raw: object) -> list[dict]:
    """`initialize.commands` → `[{name, description, argument_hint}]`."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lstrip("/")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "description": str(entry.get("description") or "").strip(),
                "argument_hint": str(
                    entry.get("argumentHint") or entry.get("argument_hint") or ""
                ).strip(),
                "source": "claude",
            }
        )
    return out


#: An early result whose task-tool call failed. Distinct from "absent" so a
#: later registration consumes it instead of waiting forever.
_TASK_TOOL_FAILED = object()

_TASK_CREATED_RE = re.compile(r"Task #(\d+) created")
_TASK_LIST_LINE_RE = re.compile(r"^#(\d+)\s+\[([A-Za-z_ -]+)\]\s+(.*?)\s*$")


def _plan_status(value: object) -> str:
    """Claude's task vocabulary -> the Codex plan vocabulary the window uses."""
    text = "".join(ch for ch in str(value or "").lower() if ch.isalnum())
    if text in {"inprogress", "active", "running", "started"}:
        return "inProgress"
    if text in {"completed", "complete", "done"}:
        return "completed"
    return "pending"


def _looks_like_task_result(text: str) -> bool:
    head = str(text or "")[:400]
    return bool(
        _TASK_CREATED_RE.search(head)
        or "Updated task #" in head
        or _TASK_LIST_LINE_RE.match(head.strip().splitlines()[0] if head.strip() else "")
    )


def _parse_task_list(text: str) -> dict[str, dict]:
    """`TaskList` output: one `#N [status] subject` line per task."""
    plan: dict[str, dict] = {}
    for line in str(text or "").splitlines():
        match = _TASK_LIST_LINE_RE.match(line.strip())
        if match:
            plan[match.group(1)] = {
                "step": match.group(3),
                "status": _plan_status(match.group(2)),
            }
    return plan


def _dialog_questions(input_payload: object) -> dict:
    """The AskUserQuestion input as the dialog wants it: ids per question.

    The dialog returns an answer map only when every question has a unique
    id; the CLI's questions have none, so number them. The ids never go back
    on the wire — `respond_to_question` maps them onto question text.
    """
    payload = copy.deepcopy(input_payload) if isinstance(input_payload, dict) else {}
    questions = payload.get("questions")
    if isinstance(questions, list):
        for index, question in enumerate(questions):
            if isinstance(question, dict):
                question["id"] = str(index)
    return payload


def _question_answers(questions: list[dict], answer: object) -> dict[str, str]:
    """`{question text: answer text}` from the dialog's answer map or string."""
    texts = [str(q.get("question") or "").strip() for q in questions]
    answers: dict[str, str] = {}
    if isinstance(answer, dict):
        for index, text in enumerate(texts):
            entry = answer.get(str(index))
            chosen = entry.get("answers") if isinstance(entry, dict) else entry
            if isinstance(chosen, (list, tuple)):
                joined = ", ".join(str(item) for item in chosen if str(item))
            else:
                joined = str(chosen or "")
            if text and joined:
                answers[text] = joined
        return answers
    text_answer = str(answer or "").strip()
    if not text_answer:
        return answers
    if len(texts) == 1:
        answers[texts[0]] = text_answer
        return answers
    # Legacy multi-question string ("Header: a\nHeader2: b"): match by header.
    headers = {str(q.get("header") or "").strip(): t for q, t in zip(questions, texts)}
    for line in text_answer.splitlines():
        head, sep, rest = line.partition(":")
        if sep and head.strip() in headers and rest.strip():
            answers[headers[head.strip()]] = rest.strip()
    if not answers and texts:
        answers[texts[0]] = text_answer
    return answers


def _suggested_rules(suggestions: list) -> list[dict]:
    """The CLI's own allow rules from `permission_suggestions`."""
    rules: list[dict] = []
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        if suggestion.get("type") != "addRules" or suggestion.get("behavior") != "allow":
            continue
        for rule in suggestion.get("rules") or []:
            if not isinstance(rule, dict):
                continue
            tool = str(rule.get("toolName") or "").strip()
            if not tool:
                continue
            entry = {"toolName": tool}
            content = rule.get("ruleContent")
            if isinstance(content, str) and content.strip():
                entry["ruleContent"] = content.strip()
            rules.append(entry)
    return rules


def _suggested_mode(suggestions: list) -> str:
    for suggestion in suggestions:
        if isinstance(suggestion, dict) and suggestion.get("type") == "setMode":
            return str(suggestion.get("mode") or "")
    return ""


def _rule_text(rule: dict) -> str:
    content = rule.get("ruleContent")
    return f"{rule['toolName']}({content})" if content else str(rule["toolName"])


def _decision_caption(req: dict) -> str:
    reason = str(req.get("decision_reason_type") or "").strip()
    return {
        "subcommandResults": "Asked because one of the sub-commands is not allowed yet.",
        "rule": "Asked by a permission rule.",
        "mode": "Asked by the current permission mode.",
        "classifier": "The auto-mode classifier could not approve this on its own.",
    }.get(reason, f"Reason: {reason}" if reason else "")


_DETAIL_CHARS = 20_000


def _approval_detail(tool_name: str, raw_input: object) -> dict | None:
    """What the user is approving, in full: a diff, file content, a command."""
    if not isinstance(raw_input, dict):
        return None
    path = str(raw_input.get("file_path") or raw_input.get("notebook_path") or "")
    if tool_name in {"Edit", "MultiEdit", "Write", "NotebookEdit"}:
        diff = None
        try:
            from helios.backend.tool_diff import edit_diff

            diff = edit_diff(tool_name, raw_input)
        except ImportError:
            diff = None
        if diff:
            return {"kind": "diff", "title": path, "text": diff}
        text = (
            raw_input.get("content")
            or raw_input.get("new_string")
            or raw_input.get("new_source")
        )
        if isinstance(text, str) and text:
            return {"kind": "code", "title": path, "text": text[:_DETAIL_CHARS]}
        return None
    if tool_name == "Bash":
        command = raw_input.get("command")
        if isinstance(command, str) and ("\n" in command or len(command) > 160):
            return {"kind": "code", "title": "Command", "text": command[:_DETAIL_CHARS]}
    return None


def _tool_approval_summary(tool_name: str, raw_input: object) -> str:
    """Describe the exact Claude action without echoing recognizable secrets."""

    name = str(tool_name or "this tool")
    summary = f"Allow Claude to use {name}?"
    if not isinstance(raw_input, dict):
        return summary
    detail_keys = (
        ("command", "Command"),
        ("file_path", "Path"),
        ("path", "Path"),
        ("notebook_path", "Notebook"),
        ("url", "URL"),
        ("query", "Query"),
        ("pattern", "Pattern"),
    )
    details: list[str] = []
    seen: set[str] = set()
    for key, label in detail_keys:
        value = raw_input.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        clean, _changed = scrub_sensitive(value.strip())
        clean = clean[:1200]
        identity = f"{label}:{clean}"
        if identity in seen:
            continue
        seen.add(identity)
        details.append(f"{label}: {clean}")
    cwd = raw_input.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        clean_cwd, _changed = scrub_sensitive(cwd.strip())
        details.append(f"Working directory: {clean_cwd[:500]}")
    if not details and raw_input:
        try:
            rendered = json.dumps(
                _redact_tool_input(raw_input),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        except (TypeError, ValueError):
            rendered = str(raw_input)
        clean_input, _changed = scrub_sensitive(rendered)
        details.append(f"Input: {clean_input[:1200]}")
    if details:
        summary += "\n\n" + "\n".join(details)
    return summary


def _redact_tool_input(value: object, field_name: str = "") -> object:
    normalized = field_name.lower().replace("-", "_")
    if any(
        marker in normalized
        for marker in ("password", "passwd", "secret", "token", "api_key", "authorization")
    ):
        return REDACTED
    if isinstance(value, dict):
        return {
            str(key): _redact_tool_input(item, str(key))
            for key, item in list(value.items())[:30]
        }
    if isinstance(value, list):
        return [_redact_tool_input(item) for item in value[:30]]
    if isinstance(value, str):
        return scrub_sensitive(value)[0]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


# ── Process-signal helpers ───────────────────────────────────────────────


def _safe_kill(pid: int, sig: signal.Signals) -> None:
    """Best-effort `os.kill` — never raises; quietly noops if the target is
    gone."""
    try:
        os.kill(pid, sig)
    except (OSError, ProcessLookupError):
        pass


def _capture_process_group(
    pid: int,
    *,
    expected_new_session: bool,
) -> int | None:
    """Capture the actual PGID after ``setsid`` has completed.

    Never infer ``pgid == pid`` from the argv wrapper. The wrapper and exec can
    race this process immediately after spawn, and a guessed group could be
    Helios's own. A short bounded wait is paid once per Claude process.
    """

    if not expected_new_session or pid <= 1:
        return None
    try:
        parent_group = os.getpgrp()
    except OSError:
        parent_group = -1
    deadline = time.monotonic() + 0.1
    while True:
        try:
            pgid = os.getpgid(pid)
            sid = os.getsid(pid)
        except (OSError, ProcessLookupError):
            return None
        if pgid > 1 and pgid != parent_group and sid == pgid:
            return pgid
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.005)


def _signal_process_tree(
    process_group_id: int | None,
    pid: int,
    sig: signal.Signals,
) -> None:
    """Signal a captured process group, falling back to the tracked leader."""

    if process_group_id is not None:
        try:
            os.killpg(process_group_id, sig)
            return
        except (OSError, ProcessLookupError):
            pass
    _safe_kill(pid, sig)


def _is_process_tree_alive(process_group_id: int | None, pid: int) -> bool:
    """Check the captured group even after its original leader has exited."""

    if process_group_id is not None:
        try:
            os.killpg(process_group_id, 0)
            return True
        except (OSError, ProcessLookupError):
            pass
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _signal_group(pid: int, sig: signal.Signals, own_group: bool) -> None:
    """Compatibility wrapper for the retired ``codex exec`` driver."""

    _signal_process_tree(pid if own_group else None, pid, sig)


def _is_alive(pid: int) -> bool:
    """Compatibility single-process liveness probe."""

    return _is_process_tree_alive(None, pid)
