"""OpenRouter chat driver — a GObject provider that runs the tool loop in-process.

Unlike the Claude/Codex drivers (which spawn a CLI subprocess that executes
tools internally), this driver holds its own OpenRouter API key, streams
chat completions over HTTPS, and executes model-requested tools directly in
Helios. The wire format is OpenAI-compatible (``/chat/completions`` with SSE
streaming and ``tool_calls``), translated into the same GObject signal
contract the window already consumes for Claude/Codex.

The agent loop (stream → tool calls → execute → re-stream) runs on a worker
thread; every GObject signal is emitted via ``GLib.idle_add`` so the GTK main
loop stays responsive and signals never fire from a background thread.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject  # noqa: E402

from helios.backend import model_catalog
from helios.backend.openrouter import chat as or_chat
from helios.backend.openrouter import key as or_key
from helios.backend.openrouter.gateway import CancellationToken
from helios.backend.openrouter.loop_policy import ToolRoundPolicy
from helios.backend.openrouter.budget import (
    WORK_COST_LIMIT_MICRO_USD, SpendLimitReached, micro_usd,
)
from helios.backend.openrouter import routes as or_routes
from helios.backend.openrouter.history import (
    HistoryLog,
    build_assistant_message,
    compact,
    estimate_tokens,
    messages_from_mirror,
    prune,
)
from helios.backend.process import openrouter_tools as tools
from helios.backend.process.cli_driver import DriverSpawnError, _approval_detail
from helios.backend.process.env_scrub import (
    ADOPTED_ENV,
    _is_credential_name,
    scrubbed_child_env,  # noqa: F401 — re-exported
)
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence,
    ExecutionDispatchEvidence,
    ExecutionStopEvidence,
    ExecutionTerminalEvidence,
    MessageDelivery,
    RequiredPromptContextError,
    UserMessageQueueMixin,
)
from helios.backend.project_perms import (
    effective_execution_mode,
    execution_mode_restriction_reason,
)
from helios.backend.process.openrouter_transcript import OpenRouterTranscriptWriter
from helios.backend.codex_context import TRACKER_POLICY
from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.sensitive_text import scrub_sensitive
from helios.backend.transcript import ToolResult
from helios.backend.openrouter.continuity import ContextArchive
from helios.backend.estate_mcp import EstateMcp
from helios.backend.provider_instructions import InstructionContextError, compile_instructions
from helios.log import get_logger

_log = get_logger("openrouter-driver")

_MAX_TOOL_ROUNDS = 25
_MAX_PRODUCTIVE_TOOL_ROUNDS = 100
_MAX_PARALLEL_READS = 4
_HANDOFF_MAX_TOKENS = 4096
# Compatibility ceiling for standalone controllers without WorkStore binding.
# The interactive application always installs durable dollar reservations.
OPENROUTER_STANDARD_TOKEN_BUDGET = 200_000

#: Per-Work dollar ceiling for interactive OpenRouter. Each HTTP request
#: reserves its projected prompt + maximum reply cost in WorkStore; reported
#: charges settle the reservation, and uncertain outcomes retain it. Prices
#: and token estimates are conservative projections, not provider price locks.
#: Removing cumulative replay tokens from this path does not alter endpoint
#: context limits, finite tool rounds, admission, cancellation or permissions.
OPENROUTER_STANDARD_COST_BUDGET_USD = WORK_COST_LIMIT_MICRO_USD / 1_000_000
_SYSTEM_PROMPT = (
    "You are an AI coding assistant integrated into Helios. "
    "You can read, write, and edit files, run shell commands, and search code "
    "in the user's working directory, which is {cwd}. Relative paths resolve "
    "against it. Use tools to accomplish tasks step by step. "
    "Prefer focused Read/Grep/Glob calls for project inspection, with only the "
    "needed lines and matches. These built-in project reads and searches do "
    "not need approval. Reserve Bash for operations that need a shell, such as "
    "validation and git commands. Group independent tool calls in one response. "
    "Use EstateSearchTools when available to discover relevant additional tools "
    "and follow their returned schemas. Respect denied actions; diagnose tool "
    "errors instead of repeating the same failed call. Verify changes before "
    "reporting success and distinguish completed work from unverified work. "
    "Once a check passes, reuse its result unless changes or new evidence "
    "justify running it again. Keep inspection focused on the requested "
    "deliverable and move to implementation once you have enough evidence. "
    "For substantial multi-step work use update_plan and maintain its statuses; "
    "simple answers need no plan. Before long context-heavy work, use "
    "checkpoint_context to retain decisions, evidence and unfinished work. "
    "Compaction retains bounded verbatim user excerpts, not a semantic summary; "
    "use read_context to recover omitted constraints or earlier evidence instead "
    "of guessing. Summarize what you did. "
    + TRACKER_POLICY
)


def _round_context(remaining: int, *, limit: int | None = None, pause_reason: str = "") -> list[dict]:
    """Request-only application guidance; never replay a stale countdown."""
    if remaining:
        text = (
            f"Helios turn budget: {remaining} of {limit or _MAX_TOOL_ROUNDS} tool rounds "
            "remain, including this response. A round can contain multiple "
            "independent tool calls. Focus on the requested deliverable; reuse "
            "passing checks unless changes or new evidence justify rerunning them. "
        )
        if remaining <= 5:
            text += (
                "Finish the current bounded milestone and verify it now. Avoid "
                "starting another investigation. Report any unfinished work honestly. "
            )
        text += (
            "A bounded extension is available only while tools produce new useful "
            "evidence, within the existing Work spend allowance. Repeating identical "
            "calls and results will pause the turn."
        )
    else:
        text = (
            ("Helios detected repeated identical tool rounds. " if pause_reason == "tool_stalled"
             else "Helios tool-round budget is exhausted. ")
            + "Tool calls are disabled for "
            "this final response. Give a concise handoff using evidence already "
            "available: what changed, what was verified, what remains unfinished, "
            "and the next concrete step. If nothing changed, say so. Do not claim "
            "completion or invent results. The user can continue in this Work."
        )
    return [{"role": "system", "content": text}]

# Share of the model's context window the replayed history may occupy. The rest
# is headroom for this turn's completion and for the chars/4 estimate being
# wrong in the unsafe direction; CONTEXT_LENGTH then halves this and retries for
# as long as compaction keeps removing something.
_HISTORY_BUDGET = 0.70
_APPROVAL_POLL_SECONDS = 0.5

#: Below this, the remaining spend budget cannot buy a useful reply, so the
#: breaker trips rather than sending a request that can only be truncated.
_MIN_USEFUL_COMPLETION = 256

#: Multiplier on the chars/4 token ESTIMATE when pricing a prompt in advance.
#:
#: chars/4 under-counts, and it under-counts most on exactly the content an
#: agent session is made of. Measured 2026-09-03 against the API's own
#: ``prompt_tokens`` for identical messages, four tokenizer families:
#: DeepSeek 1.27× on code and prose, 1.49× on JSON-shaped text, 1.38× on
#: numbered ``Read`` output; OpenAI 1.04–1.23×; Llama 1.38× on JSON; **Qwen
#: 1.55×** on numbered ``Read`` output. A projection that treats the estimate
#: as exact is therefore not an upper bound, and the spend ceiling it feeds is
#: not a ceiling (a review finding). The margin rounds the worst
#: measured ratio up. It is a measured bound over the tokenizers probed, not a
#: proof — see the ponytail note on ``history._CHARS_PER_TOKEN`` for the real
#: upgrade path, a per-vendor tokenizer.
_PROMPT_ESTIMATE_MARGIN = 1.6

#: Tokens the tool schemas add to every request. ``estimate_tokens`` only ever
#: saw the message array, so this fixed overhead was invisible to the
#: projection; measured at roughly 830 tokens on DeepSeek's tokenizer against
#: a chars/4 figure of 900, which the margin then covers.
_TOOL_SCHEMA_TOKENS = estimate_tokens(list(tools.TOOL_SCHEMAS))


class _RequestBudgetStop(Exception):
    """A pre-HTTP budget stop with a known local outcome."""

    def __init__(self, message: str, *, budget_limited: bool = True) -> None:
        super().__init__(message)
        self.budget_limited = budget_limited


def _grow_block(streaming: StreamingAssistant, kind: str, text: str) -> None:
    """Append ``text`` to the open block of ``kind``, or start one.

    A block per delta is exactly what the Claude path avoids: ``streaming.py``
    opens a block on ``content_block_start`` and accumulates every later delta
    into it. Appending one here per SSE event produced one ordered content
    span per event — measured 2026-09-03 at 200 spans for 200 deltas where
    the Claude path makes 1 — and every consumer inherited it. Each span is
    rendered as its own surface, so a markdown table split across two deltas
    could never render as a table, and one turn drew a separate collapsed
    "Reasoning summary" row for every fragment of the model's reasoning
    (31 of them, counted in the live widget).
    """
    if streaming.blocks and streaming.blocks[-1].type == kind:
        streaming.blocks[-1].text += text
    else:
        streaming.blocks.append(Block(type=kind, text=text))


_EGRESS_REDACTION_NOTE = (
    "\n\n[Helios redacted recognisable credentials from this result before "
    "sending it to the model provider.]"
)
_EGRESS_REDACTED = "[REDACTED BY HELIOS]"

#: A shell *reference* — the only thing a credential-shaped assignment may
#: hold without being redacted. Matched whole, never merely contained: a `$`
#: somewhere inside a value does not make the value a reference, and inside
#: single quotes it is not even an expansion.
_SHELL_REFERENCE = (
    r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\$\([^)\n]*\)|`[^`\n]*`)"
)

#: One complete environment-assignment line whose variable NAME is
#: credential-shaped and whose value is a literal: what ``env``, ``printenv``,
#: a ``.env`` file and a shell ``export`` all look like. The shape is what makes
#: it safe on machine output where the general ``name = value`` rule was not
#: (that one damaged 46 of 283 source files): source code puts spaces around
#: ``=`` and rarely writes an UPPER_CASE credential name as a bare assignment.
#:
#: Three corrections from review, each measured:
#:
#: * A **quoted** value is a literal — ``DATABASE_PASSWORD="correct-horse"``
#:   is a password, not an expansion (round 10).
#: * **Single quotes make everything literal**, `$` and backticks included, so
#:   ``DB_PASSWORD='pa$$word1'`` is a password too. Only an unquoted or
#:   double-quoted value can hold a reference at all (round 11).
#: * There is **no minimum length**. A short password is still a password, and
#:   measured over 319 repository files in raw, ``Read`` and ``Grep`` shapes,
#:   dropping the eight-character floor added *zero* false positives (round 11).
#:   A purely numeric value is exempt because no credential is one, and that is
#:   what keeps ``MAX_KEY_LEN=4096`` intact.
#:
#: The optional prefixes are the two shapes this driver's own tools emit:
#: ``Read`` numbers every line (``12: NAME=…``) and ``Grep`` prefixes
#: ``path:line:``; without them the rule missed exactly the tool output that
#: would carry a ``.env`` file upstream.
_ENV_CREDENTIAL_LINE = re.compile(
    r"(?m)^(?P<prefix>(?:\d+: |[^\n]*?:\d+:)?(?:export\s+)?)"
    r"(?P<name>[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|_KEY|_PAT|"
    r"APIKEY|CREDENTIAL|_PWD)[A-Z0-9_]*)"
    r"=(?P<value>"
    # A purely numeric value is never a credential, quoted or not — that is
    # what keeps `MAX_KEY_LEN=4096` and `MAX_KEY_LEN="4096"` intact.
    r"'(?![0-9]+')[^'\r\n]+'"                        # single quotes: always literal
    rf'|"(?!{_SHELL_REFERENCE}")(?![0-9]+")[^"\r\n]+"'  # double quotes: literal unless a whole reference
    rf"|(?!{_SHELL_REFERENCE}$)(?![0-9]+$)[^\s'\"][^\s]*"  # bare: not a reference, not a number
    r")$"
)

_MIN_KNOWN_VALUE_LEN = 16


def _known_secret_values() -> list[str]:
    """The exact secrets Helios itself put within the Bash tool's reach.

    Helios forwards a fixed allow-list of credentials into the interactive
    child environment (``env_scrub.ADOPTED_ENV``) and holds the OpenRouter key
    on disk where the shell can read it. It therefore *knows* these values, so
    redacting them by exact match is zero-false-positive by construction — a
    70-character random token does not occur in source by accident — and it
    catches the value wherever it appears, not only in ``NAME=value`` form.
    Read on every call: cheap, and a rotation takes effect without a restart.
    """
    values = [
        os.environ.get(name, "")
        for name in ADOPTED_ENV
        if _is_credential_name(name)
    ]
    values.append(or_key.load_key())
    return [v for v in values if len(v) >= _MIN_KNOWN_VALUE_LEN]


def _scrub_tool_output(content: str) -> str:
    """Remove recognisable credentials from tool output before it is replayed.

    This provider is the one path in Helios where tool results leave the
    machine: the array this feeds is POSTed to a third-party endpoint on every
    later round and is also persisted verbatim under
    ``~/.helios/openrouter-sessions/``. The estate's scrubber already runs over
    the durable Work ledger and over approval prompts; it did not run here, so
    one approved ``env`` uploaded whatever the Bash tool's environment holds —
    measured 2026-09-03, that includes an admin-scoped tracker token the
    session is deliberately granted.

    Three passes, from most to least certain:

    1. **Exact known values** — the credentials Helios itself forwarded or
       holds. Zero false positives; catches the concrete motivating leak in any
       surrounding text. a review finding was right that shape rules alone
       could not: that token is 70 random characters with no prefix.
    2. **Complete env-assignment lines** with a credential-shaped UPPER_CASE
       name — the ``env`` / ``.env`` / ``export`` shape — for credentials
       Helios does not know about.
    3. **Shape rules** (private-key blocks, Bearer headers, known token
       prefixes, credentials in a URL) via ``scrub_sensitive`` with the general
       ``name = value`` rule OFF. That rule is right for prose and wrong for
       machine output: measured over this repository, it damaged 46 of 283
       source files — ``token = object()`` became ``token = [REDACTED …]`` —
       so an agent reading its own code would have received nonsense.

    Scrubbed once, so the transcript the user reads and the array the model
    reads say the same thing; two versions of one tool result is a divergence
    that outlives whoever introduced it, and the mirror is the resume fallback.
    The model is told when something was removed so it does not read the gap as
    the file's real contents.

    ponytail: pattern matching plus the values Helios knows; it is a reduction
    in blast radius, not a boundary. The boundary is the approval prompt, which
    names the destination.
    """
    original = content
    for value in _known_secret_values():
        content = content.replace(value, _EGRESS_REDACTED)
    content = _ENV_CREDENTIAL_LINE.sub(
        lambda m: f"{m.group('prefix')}{m.group('name')}={_EGRESS_REDACTED}", content
    )
    content, _ = scrub_sensitive(content, named_pairs=False)
    return content + _EGRESS_REDACTION_NOTE if content != original else content


class _ContextContinuityError(RuntimeError):
    """A local archive failed before replay history could be compacted."""


class OpenRouterDriverSpawnError(DriverSpawnError):
    """start() could not begin (no API key, bad model, …).

    Subclasses ``DriverSpawnError`` so the window's existing spawn handler
    catches it. It previously only shared ``RuntimeError`` as a base, which
    made it a sibling — ``except DriverSpawnError`` did not catch it, and a
    missing key escaped as an unhandled exception inside a GTK callback.
    """


class OpenRouterDriver(UserMessageQueueMixin, GObject.Object):
    """Drives an OpenRouter chat session with in-process tool execution.

    Signals (identical to ClaudeCliDriver — the window connects the same set):
      session-started (session_id, cwd, model)
      assistant-streaming (StreamingAssistant)
      turn-appended (Turn)
      result (result_dict)
      usage-updated (tokens_used, context_window)
      rate-limit-updated (dict)
      plan-updated (dict)
      question-asked (payload, token)
      queued-user-sent (qid, text)
      error (message)
      exited (exit_code)
    """

    display_name = "openrouter"
    provider = model_catalog.PROVIDER_OPENROUTER

    __gsignals__ = {
        "session-started": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
        "assistant-streaming": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-appended": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "result": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "usage-updated": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        "rate-limit-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "plan-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-status-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "context-compacted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "budget-exhausted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "question-asked": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "queued-user-sent": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "exited": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    def __init__(
        self,
        *,
        cwd: str,
        model: str = "",
        permission_mode: str = "default",
        resume_session_id: str = "",
        effort: str = "",
    ) -> None:
        super().__init__()
        self._cwd = cwd
        self._model = model
        self._permission_mode = effective_execution_mode(permission_mode, cwd, provider="openrouter")
        self._resume = resume_session_id
        self._plan_turn_id = ""

        self._init_user_queue()
        self._session_id = ""
        self._started = False
        self._closed = False
        self._busy = False
        self._worker: threading.Thread | None = None
        self._cancel_token: CancellationToken | None = None
        self.last_activity: float = time.monotonic()
        self._init_tools: list = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
        self._init_mcp_servers: list = []
        self._instruction_sources: list[dict] = []
        self._estate: EstateMcp | None = None
        self._turn_tools = tools.TOOL_SCHEMAS
        self._schema_tokens = _TOOL_SCHEMA_TOKENS

        # tool approval: token -> (Event, allowed: bool | None)
        self._pending_approvals: dict[str, tuple[threading.Event, bool | None]] = {}

        self._history: list[dict] = []
        # Staged, not yet validated: the pinned endpoint is not known until
        # start() resolves a route, and an endpoint that does not declare
        # `reasoning` must never be sent one — `require_parameters: true`
        # would route the request away from it.
        self._effort_key = effort if or_chat.reasoning_for_effort(effort) else ""
        self._route: or_routes.Route | None = None
        self._transcript = OpenRouterTranscriptWriter(cwd)
        self._lifetime_tokens_used = 0
        self._lifetime_cost_usd = 0.0
        # Production binds the WorkStore before the first admitted turn.
        # Lightweight standalone controllers retain the legacy token guard.
        self._budget_store = None
        self._work_cost_usd = 0.0
        #: The provider's own count for the last prompt actually sent, and how
        #: long the history was when it was sent. A far better anchor for the
        #: next projection than chars/4 over the whole array: from the second
        #: round on, only the newly appended messages are estimated.
        self._prompt_receipt: tuple[int, int] = (0, 0)
        self._token_budget_exhausted = False
        #: Which ceiling actually tripped, so a later send says the true one.
        self._budget_trip_kind = ""
        #: Explicit tool/command grants; private to this driver lifetime.
        self._session_grants: set[str] = set()
        self._permission_epoch = 0
        self._approval_choices: dict[str, tuple[str, ...]] = {}
        #: Which allow option answered a pending approval, by token.
        self._session_answers: dict[str, str] = {}

    # ── properties ──────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    def set_work_budget_store(self, store) -> None:
        """Bind durable per-request reservations to the admission ledger."""
        self._budget_store = store

    @property
    def model(self) -> str:
        return self._model

    @property
    def init_tools(self) -> list:
        return self._init_tools

    @property
    def init_mcp_servers(self) -> list:
        return self._init_mcp_servers

    @property
    def init_instruction_sources(self) -> list[dict]:
        """Paths, hashes, precedence and freshness for this turn's loaded policy."""
        return [dict(source) for source in self._instruction_sources]

    @property
    def is_running(self) -> bool:
        return self._started and not self._closed

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def is_accepting_input(self) -> bool:
        return self.is_running

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def effort_key(self) -> str:
        return self._effort_key

    @property
    def execution_restart_required(self) -> bool:
        return False

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Validate the key, bind the session, and emit session-started."""
        if self._started:
            return
        if not or_key.load_key():
            raise OpenRouterDriverSpawnError("No OpenRouter API key — add one in Settings → Providers.")
        if not self._model or "/" not in self._model:
            raise OpenRouterDriverSpawnError(f"Invalid OpenRouter model: {self._model!r}")

        if self._resume:
            self._session_id = self._resume
            self._history = HistoryLog(self._session_id).load()
            if not self._history:
                self._history = self._load_history_from_mirror()
        else:
            self._session_id = uuid.uuid4().hex
            prune()

        self._transcript.bind_thread(self._session_id)
        # A history reconstructed from the display mirror carries no system
        # message (the mirror never stored one), so check the array rather than
        # only the empty case — otherwise a recovered session runs unprompted.
        if not self._history or self._history[0].get("role") != "system":
            self._history.insert(
                0, {"role": "system", "content": _SYSTEM_PROMPT.format(cwd=self._cwd)}
            )
            HistoryLog(self._session_id).save(self._history)

        # Resolve the endpoint once and hold it for the session. Re-resolving
        # per turn would defeat the point: a moved endpoint changes context
        # length and quantization, and discards the upstream prompt cache.
        self._route = or_routes.route_for(self._model)
        if self._route is not None and not self._route.pricing_known:
            # Said once, at session start: the dollar ceiling cannot be
            # enforced for an endpoint whose price the catalog did not give.
            _log.warning(
                "endpoint %s published no usable pricing for %s; the "
                "$%.2f spend ceiling cannot be enforced and this session is "
                "bounded by the %d-token budget instead",
                self._route.provider_slug, self._model,
                OPENROUTER_STANDARD_COST_BUDGET_USD,
                OPENROUTER_STANDARD_TOKEN_BUDGET,
            )
        if self._route is not None and not self._route.supports_tools:
            _log.warning(
                "pinned endpoint %s does not advertise tool support for %s",
                self._route.provider_slug, self._model,
            )
        if (
            self._effort_key
            and self._route is not None
            and not self._route.supports_reasoning
        ):
            # The catalog row advertised `reasoning` at model level, but this
            # endpoint does not. Drop the staged choice rather than sending a
            # parameter that would reroute the request; the toolbar reads
            # `effort_key` back, so the control reflects what is actually
            # in force.
            _log.info(
                "endpoint %s does not support reasoning effort for %s; cleared",
                self._route.provider_slug, self._model,
            )
            self._effort_key = ""

        self._started = True
        self.emit("session-started", self._session_id, self._cwd, self._model)

    def _load_history_from_mirror(self) -> list[dict]:
        try:
            from helios.backend import projects

            path = (
                projects.PROJECTS_DIR
                / projects.encode_project_dirname(self._cwd)
                / f"{self._session_id}.jsonl"
            )
            return messages_from_mirror(path, self._session_id)
        except Exception as e:  # noqa: BLE001
            _log.warning("mirror history reconstruction failed: %s", e)
            return []

    def send_user_text(self, text: str) -> MessageDelivery:
        """Send a user message and start the agent loop on a worker thread."""
        if not self.is_running:
            self.emit("error", "Cannot send: OpenRouter driver is not running")
            return MessageDelivery("rejected")
        if (
            (self._busy or self._user_queue)
            and not self._queue_dispatch_in_progress
        ):
            self.emit(
                "error",
                "OpenRouter already has active or queued input; the new "
                "message was not sent out of order.",
            )
            return MessageDelivery("rejected")
        if self._token_budget_exhausted:
            self.emit("error", self._budget_exhausted_message())
            return MessageDelivery("rejected")
        block_reason = self._execution_block_reason()
        if block_reason:
            self.emit("error", block_reason)
            return MessageDelivery("rejected")
        try:
            instructions = compile_instructions(self._cwd)
            prepared = self._prepare_prompt_context(text)
        except (RequiredPromptContextError, InstructionContextError) as exc:
            self.emit("error", exc.user_message if isinstance(exc, RequiredPromptContextError) else str(exc))
            return MessageDelivery("rejected")
        content = prepared.text
        admission_error = self._begin_execution_attempt()
        if admission_error:
            self.emit("error", admission_error)
            return MessageDelivery("rejected")
        self._busy = True
        self._plan_turn_id = ""
        self.last_activity = time.monotonic()

        worker_gate = threading.Event()
        release_worker = [False]

        def gated_turn() -> None:
            worker_gate.wait()
            if release_worker[0]:
                self._run_turn(prepared.text)

        def local_abort(message: str) -> MessageDelivery:
            release_worker[0] = False
            worker_gate.set()
            self._busy = False
            accounting_ok = self._finish_execution_with_evidence(
                ExecutionTerminalEvidence(
                    evidence_type="local_abort",
                    status="aborted",
                    reason_code="local_abort",
                    queue_disposition="restored",
                )
            )
            if not accounting_ok:
                self.end_input()
                self.emit(
                    "error",
                    "Helios could not persist OpenRouter's local abort; new "
                    "provider work remains blocked.",
                )
                # The worker gate never opened, so provider delivery is still
                # definitely absent even though accounting needs recovery.
                return MessageDelivery("rejected")
            self.emit("error", message)
            return MessageDelivery("rejected")

        if self._budget_store is not None:
            try:
                self._work_cost_usd = self._budget_store.initialize_openrouter_budget(
                    self.execution_attempt_id,
                ) / 1_000_000
                if self._effective_prices() is None:
                    return local_abort(
                        "OpenRouter could not verify this model's prices. "
                        "No request was sent; refresh the model catalog or choose a priced model."
                    )
            except Exception as exc:
                return local_abort(f"OpenRouter could not verify this Work's spend: {exc}")

        # Start a worker that cannot cross the HTTP boundary yet. If thread
        # creation itself fails, the attempt is still provably pre-dispatch and
        # can be released as a local abort instead of pinning the Work forever.
        try:
            self._cancel_token = CancellationToken()
            self._worker = threading.Thread(
                target=gated_turn,
                args=(),
                daemon=True,
                name=f"or-turn-{self._session_id[:8]}",
            )
            self._worker.start()
        except Exception as exc:
            return local_abort(f"Could not start OpenRouter worker: {exc}")

        history_before = list(self._history)
        history_log = HistoryLog(self._session_id)
        try:
            system = {"role": "system", "content": _SYSTEM_PROMPT.format(cwd=self._cwd)}
            if instructions.text:
                system["content"] += "\n\n" + instructions.text
            if self._history and self._history[0].get("role") == "system":
                self._history[0] = system
            else:
                self._history.insert(0, system)
            self._instruction_sources = list(instructions.sources)
            # The old receipt describes the previous instruction prefix and
            # schemas. Never price this changed prompt from that stale anchor.
            self._prompt_receipt = (0, 0)
            self._history.append({"role": "user", "content": content})
            history_log.save_strict(self._history)
        except Exception as exc:
            self._history = history_before
            try:
                history_log.save_strict(self._history)
            except Exception as rollback_exc:  # noqa: BLE001
                _log.warning("OpenRouter history rollback failed: %s", rollback_exc)
            return local_abort(
                "OpenRouter could not durably stage its local history; the "
                f"message was not sent ({exc})."
            )

        attempt_id = self.execution_attempt_id
        if not self._record_execution_dispatch(
            ExecutionDispatchEvidence(
                wire_prompt_text=content,
                provider_request_key=attempt_id,
                native_binding_id=self._confirmed_execution_native_id(),
            )
        ):
            self._history = history_before
            try:
                history_log.save_strict(self._history)
            except Exception as rollback_exc:  # noqa: BLE001
                _log.warning("OpenRouter history rollback failed: %s", rollback_exc)
            return local_abort(
                "Helios could not persist the OpenRouter dispatch identity. "
                "The message was not sent.",
            )

        # The display mirror is deliberately after durable dispatch: a
        # definitive pre-dispatch failure must not leave a false user bubble.
        try:
            self._transcript.note_user_text(prepared.text)
        except Exception as exc:
            release_worker[0] = False
            worker_gate.set()
            self._history = history_before
            try:
                history_log.save_strict(self._history)
            except Exception as rollback_exc:  # noqa: BLE001
                _log.warning("OpenRouter history rollback failed: %s", rollback_exc)
            self._record_execution_stop(
                ExecutionStopEvidence(
                    acknowledgement={},
                    queue_disposition="held",
                )
            )
            self._busy = False
            self.end_input()
            self.emit(
                "error",
                "OpenRouter's local transcript mirror failed before HTTP "
                "dispatch; the message was not sent. This Work remains "
                f"blocked until accounting recovery completes ({exc}).",
            )
            return MessageDelivery("rejected")

        prepared.mark_sent()
        release_worker[0] = True
        worker_gate.set()
        return MessageDelivery("accepted")

    def answer_question(self, token: str, answer: str | None) -> None:
        """Resolve a pending tool-approval prompt.

        Compares against the exact option strings this driver offered. Anything
        else — Deny, a dismissal, or a label from a future dialog this driver
        did not write — is a refusal, so an unrecognised answer can never widen
        permission.
        """
        entry = self._pending_approvals.pop(token, None)
        if entry is None:
            return
        event, _ = entry
        allowed = answer in self._approval_choices.get(token, ()) and answer != "Deny"
        if allowed:
            self._session_answers[token] = str(answer)
        self._pending_approvals[token] = (event, allowed)
        event.set()

    def set_permission_mode(
        self,
        mode: str,
        callback: Callable[[bool, str], None] | None = None,
    ) -> bool:
        from helios.backend.project_perms import PERMISSION_MODES

        if mode not in PERMISSION_MODES:
            self._notify(callback, False, "unknown permission mode")
            return False
        restriction = execution_mode_restriction_reason(
            mode,
            self._cwd,
            provider="openrouter",
        )
        if restriction:
            if callback is not None:
                callback(False, restriction)
            return False
        if mode == self._permission_mode:
            self._notify(callback, True)
            return True
        # No busy gate: both are plain fields consumed when the next request
        # is assembled, so a mid-turn change lands on the next turn.
        self._permission_mode = mode
        self._permission_epoch += 1
        self._session_grants.clear()
        self._notify(callback, True)
        return True

    def set_effort(
        self,
        key: str,
        callback: Callable[[bool, str], None] | None = None,
    ) -> bool:
        """Set the reasoning effort for subsequent turns.

        Mapped onto OpenRouter's unified ``reasoning`` parameter, which far more
        endpoints declare than the OpenAI-specific ``reasoning_effort``. Refused
        when the pinned endpoint does not declare it, rather than sending a
        parameter the provider will ignore — ``require_parameters`` would then
        route the request away or fail it outright.
        """
        if or_chat.reasoning_for_effort(key) is None:
            self._notify(callback, False, f"unknown effort level: {key}")
            return False
        if self._route is not None and not self._route.supports_reasoning:
            self._notify(
                callback, False,
                f"{self._route.provider_name} does not support reasoning effort",
            )
            return False
        # No busy gate: both are plain fields consumed when the next request
        # is assembled, so a mid-turn change lands on the next turn.
        self._effort_key = key
        self._notify(callback, True)
        return True

    def end_input(self) -> None:
        """Wind down without an interrupt marker."""
        self._closed = True
        self._resolve_all_approvals(False)

    def stop(self, *, interrupt: bool = True) -> None:
        """Cancel the in-flight turn and wind down."""
        if self._closed:
            return
        self._closed = True
        if self._cancel_token is not None:
            self._cancel_token.cancel()
        self._resolve_all_approvals(False)

    # ── agent loop (worker thread) ───────────────────────────────────────

    def _run_turn(self, _user_text: str) -> None:
        """Own progressive MCP discovery and cleanup entirely on the worker."""
        estate = None
        try:
            try:
                estate = EstateMcp(self._cwd, cancellation=self._cancel_token)
                self._estate = estate
                schemas = estate.discover()  # two small schemas; no server startup
                self._turn_tools = (*tools.TOOL_SCHEMAS, *schemas)
                self._init_mcp_servers = [dict(row) for row in estate.statuses]
            except Exception as exc:  # noqa: BLE001 — optional tools must not strand the worker
                self._estate = None
                self._turn_tools = tools.TOOL_SCHEMAS
                reason, _ = scrub_sensitive(str(exc))
                self._init_mcp_servers = [{"name": "estate", "status": "failed", "reason": reason}]
                _log.warning("Estate discovery failed; continuing with built-in tools: %s", reason)
                GLib.idle_add(
                    self.emit, "error",
                    "Estate tools could not be loaded. This turn will continue with "
                    f"Helios's built-in tools; retry after fixing the tool configuration. {reason}",
                )
            self._schema_tokens = estimate_tokens(list(self._turn_tools))
            self._prompt_receipt = (0, 0)
            self._init_tools = [schema["function"]["name"] for schema in self._turn_tools]
            if self._estate is not None:
                for row in self._init_mcp_servers:
                    if row.get("status") == "failed":
                        GLib.idle_add(self.emit, "error", f"Estate tool configuration unavailable: {row.get('reason', 'unknown failure')}")
            self._run_turn_with_tools(_user_text)
        finally:
            if estate is not None:
                try:
                    estate.close()
                except Exception as exc:  # noqa: BLE001
                    reason, _ = scrub_sensitive(str(exc))
                    _log.warning("Estate cleanup failed: %s", reason)
            if self._estate is estate:
                self._estate = None

    def _run_turn_with_tools(self, _user_text: str) -> None:
        """Stream → tool calls → execute → re-stream, then emit result."""
        started_at = time.monotonic()
        self._plan_turn_id = ""
        api_key = or_key.load_key()
        streaming = StreamingAssistant(model=self._model)
        tool_results: list[ToolResult] = []
        total_cost = 0.0
        cost_complete = True
        aggregate_input = 0
        aggregate_cached = 0
        aggregate_output = 0
        request_count = 0
        tool_call_count = 0
        last_usage = or_chat.ChatUsage()
        first_response_id = ""
        final_response_id = ""
        subtype = "success"
        error_message = ""
        ambiguous_detail = ""
        explicit_rejection: or_chat.ChatError | None = None
        blocks_before_round = 0
        streamed_chars_before_round = 0
        round_accepted = False
        plan_presented = False
        pause_reason = ""

        def response_accepted(response_id: str) -> None:
            nonlocal first_response_id, final_response_id, round_accepted
            round_accepted = True
            final_response_id = response_id
            if first_response_id:
                return
            first_response_id = response_id
            if not self._record_execution_acceptance(
                ExecutionAcceptanceEvidence(
                    accepted_turn_id=response_id,
                    provider_request_key=self.execution_attempt_id,
                    native_binding_id=self._confirmed_execution_native_id(),
                )
            ):
                raise RuntimeError(
                    "provider response identity could not be persisted"
                )
            self._plan_turn_id = response_id
            GLib.idle_add(self.emit, "turn-status-updated", {
                "threadId": self._session_id, "turnId": response_id, "status": "inProgress",
            })

        try:
            # Interactive Works get one metered, tools-disabled handoff after
            # the finite tool loop. Standalone legacy controllers keep their
            # existing process guard and request ceiling.
            handoff_enabled = self._budget_store is not None
            policy = ToolRoundPolicy(
                _MAX_TOOL_ROUNDS,
                _MAX_PRODUCTIVE_TOOL_ROUNDS if handoff_enabled else _MAX_TOOL_ROUNDS,
            )
            for _round in range(policy.hard_limit + int(handoff_enabled)):
                if self._is_cancelled():
                    if request_count:
                        # _stream_round returned a terminal receipt for every
                        # request so far. Stopping local tool work or between
                        # requests cannot leave an upstream request in flight.
                        subtype = "interrupted"
                    else:
                        ambiguous_detail = "cancellation had no provider-terminal receipt"
                    break

                pause_reason = policy.pause_reason() if handoff_enabled else ""
                handoff_round = bool(pause_reason)
                round_max_tokens = 0
                if self._budget_store is None:
                    # Project this round before sending it, prompt AND reply. A
                    # post-round check can only ever close the gate after the
                    # expensive event, and one 70%-full round on a 1M-context
                    # frontier model was measured at $21-22 — more than four times
                    # the whole process ceiling.
                    prompt_cost = self._prompt_cost()
                    round_max_tokens, binding_ceiling = (
                        self._affordable_completion_tokens(prompt_cost)
                    )
                    projected = self._projected_round_cost(prompt_cost, round_max_tokens)
                    over_spend = (
                        projected > 0
                        and self._lifetime_cost_usd + projected
                        > OPENROUTER_STANDARD_COST_BUDGET_USD
                    )
                    if (
                        not over_spend
                        and round_max_tokens < _MIN_USEFUL_COMPLETION
                        and binding_ceiling == "reserve"
                    ):
                        # Not a budget at all: this endpoint's own window leaves no
                        # room for a useful reply. Latching the process budget for
                        # that would permanently kill a session over a property of
                        # the model it picked (round 10).
                        subtype = "error"
                        error_message = (
                            f"{self._model} on "
                            f"{self._route.provider_name if self._route else 'this endpoint'} "
                            f"leaves only {max(0, round_max_tokens)} tokens for a reply, "
                            "which is too few to be useful. Choose a model with a "
                            "larger context window."
                        )
                        break
                    if over_spend or round_max_tokens < _MIN_USEFUL_COMPLETION:
                        # Name the ceiling that actually bound it. A session that
                        # ran out of *tokens* used to be latched and reported as
                        # cost-exhausted (round 8) — and then a *spend* trip could
                        # be reported as token exhaustion, because the label came
                        # from the clamp even when the dollar projection was what
                        # fired (round 9). A spend trip is a spend trip whatever
                        # the clamp said.
                        by_tokens = not over_spend and binding_ceiling == "tokens"
                        self._trip_runtime_budget(
                            kind="tokens-projected" if by_tokens else "cost-projected",
                            used=self._lifetime_tokens_used,
                        )
                        subtype = "budgetLimited"
                        error_message = (
                            "OpenRouter stopped before this request: it would have "
                            + (
                                "taken this process past its "
                                f"{OPENROUTER_STANDARD_TOKEN_BUDGET:,}-token safety limit."
                                if by_tokens
                                else "taken this process past its "
                                f"${OPENROUTER_STANDARD_COST_BUDGET_USD:.2f} spend limit."
                            )
                            + " Queued messages were kept."
                        )
                        break

                blocks_before_round = len(streaming.blocks)
                streamed_chars_before_round = sum(len(block.text) for block in streaming.blocks)
                round_accepted = False
                (
                    round_text,
                    round_tool_calls,
                    finish_reason,
                    usage,
                    round_reasoning,
                ) = self._stream_round(
                    api_key,
                    streaming,
                    max_tokens=round_max_tokens,
                    round_context=(
                        _round_context(
                            0 if handoff_round else policy.limit - _round,
                            limit=policy.limit, pause_reason=pause_reason,
                        )
                        if handoff_enabled else ()
                    ),
                    handoff=handoff_round,
                    on_response_accepted=response_accepted,
                )
                # Per-turn top-level usage is the prompt + output *resident in
                # the window* right now, never a cumulative sum — the same
                # convention cli_driver documents, and what session_state's
                # context-fill projection reads out of the transcript mirror.
                # Each round re-sends the whole history, so summing rounds
                # would multiply the reported fill by the round count. Cost is
                # cumulative and is accumulated separately.
                last_usage = usage
                request_count += 1
                tool_call_count += len(round_tool_calls)
                budget_tripped = False
                if usage.reported and usage.input_tokens > 0:
                    # The receipt also includes request-only round guidance.
                    # Keeping those tokens in the anchor overestimates the
                    # next request conservatively; never subtract estimates
                    # from an authoritative token count.
                    self._prompt_receipt = (len(self._history), usage.input_tokens)
                if not usage.reported:
                    cost_complete = False
                    budget_tripped = self._trip_runtime_budget(
                        kind="usage-unverified",
                        used=self._lifetime_tokens_used,
                    )
                    subtype = "budgetLimited"
                    error_message = (
                        "OpenRouter returned no verifiable token receipt. "
                        "The Work was stopped before any tool call ran."
                    )
                else:
                    cached = max(0, int(usage.cached_tokens or 0))
                    prompt = max(0, int(usage.input_tokens or 0))
                    aggregate_cached += min(prompt, cached)
                    aggregate_input += max(0, prompt - cached)
                    aggregate_output += max(0, int(usage.output_tokens or 0))
                    if usage.cost_usd is None or not math.isfinite(usage.cost_usd) or usage.cost_usd < 0:
                        cost_complete = False
                    else:
                        total_cost += usage.cost_usd
                        self._lifetime_cost_usd += max(0.0, usage.cost_usd)
                    self._lifetime_tokens_used += max(
                        0,
                        int(usage.input_tokens or 0)
                        + int(usage.output_tokens or 0),
                    )
                    if (
                        self._budget_store is None
                        and self._lifetime_tokens_used >= OPENROUTER_STANDARD_TOKEN_BUDGET
                    ):
                        budget_tripped = self._trip_runtime_budget(
                            kind="tokens",
                            used=self._lifetime_tokens_used,
                        )
                        subtype = "budgetLimited"
                        error_message = (
                            "OpenRouter reached this process's 200,000-token "
                            "safety limit. Queued messages were kept."
                        )
                    elif self._budget_store is not None and (
                        usage.cost_usd is None or not math.isfinite(usage.cost_usd)
                        or usage.cost_usd < 0
                    ):
                        budget_tripped = self._trip_runtime_budget(
                            kind="cost-unverified", used=self._lifetime_tokens_used,
                        )
                        subtype = "budgetLimited"
                        error_message = self._budget_exhausted_message()
                    elif (
                        (self._work_cost_usd if self._budget_store is not None else self._lifetime_cost_usd)
                        >= OPENROUTER_STANDARD_COST_BUDGET_USD
                    ):
                        budget_tripped = self._trip_runtime_budget(
                            kind="cost",
                            used=self._lifetime_tokens_used,
                        )
                        subtype = "budgetLimited"
                        error_message = (
                            "OpenRouter reached its "
                            f"${OPENROUTER_STANDARD_COST_BUDGET_USD:.2f} spend "
                            "limit. Queued messages were kept."
                        )

                self.last_activity = time.monotonic()

                assistant_msg, execute_calls = build_assistant_message(
                    "".join(round_text),
                    # Enforce locally even if an endpoint ignores tool_choice.
                    # Never execute or persist unanswered handoff tool calls.
                    () if handoff_round else round_tool_calls,
                    finish_reason,
                    round_reasoning,
                )
                if budget_tripped:
                    # Plain response text is safe to retain. Never persist a
                    # tool_calls assistant record without replies, and never
                    # execute a call after an unmetered/over-budget round.
                    if assistant_msg is not None and not execute_calls:
                        self._history.append(assistant_msg)
                    break
                if assistant_msg is not None:
                    self._history.append(assistant_msg)

                if handoff_round:
                    subtype = "interrupted"
                    note = (
                        f"Helios paused after {_round} tool rounds. "
                        + ("Repeated identical tool rounds made no new progress. "
                           if pause_reason == "tool_stalled" else "")
                        + ("No handoff text was returned. " if not "".join(round_text).strip() else "")
                        + "Send a follow-up message in this Work to continue. "
                        "Queued messages were kept."
                    )
                    streaming.blocks.append(Block(type="text", text=f"\n\n{note}"))
                    self._history.append({"role": "system", "content": note})
                    break

                if not execute_calls:
                    if finish_reason == "length":
                        subtype = "interrupted"
                        pause_reason = "output_limit"
                        error_message = (
                            "OpenRouter's response was cut off by the output limit. "
                            "No partial tool calls were executed. Send a follow-up "
                            "message in this Work to continue."
                        )
                        self._history.append({"role": "system", "content": error_message})
                    break

                # Execute tools and append results. Every call emitted in the
                # assistant message above MUST get a reply before this history
                # is persisted — an unanswered tool_call makes every later
                # request 400, and the array is written to disk and reloaded
                # verbatim on resume, so the session dies permanently. The loop
                # therefore appends a reply on *every* path: normal completion,
                # cancellation, and any exception the call site can raise.
                # Constructing the message correctly is not enough on its own;
                # the guarantee has to hold at the persist boundary.
                outcomes = []
                for call, content, is_error in self._execute_tool_batch(round_tool_calls, streaming):
                    outcomes.append((call.name, call.arguments_json, content, is_error))
                    if call.name == "ExitPlanMode" and not is_error:
                        plan_presented = True

                    tool_results.append(ToolResult(
                        tool_use_id=call.id,
                        content=content,
                        is_error=is_error,
                    ))
                    self._history.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": content,
                    })
                    self.last_activity = time.monotonic()

                policy.observe(outcomes)
                HistoryLog(self._session_id).save_strict(self._history)

                if self._is_cancelled():
                    subtype = "interrupted"
                    break

                if plan_presented:
                    # The plan is the turn's product, so presenting it ends the
                    # turn. Asking the model in prose to stop is not a gate:
                    # measured 2026-09-03, a model called ExitPlanMode, read
                    # "Stop here and wait", and carried straight on into
                    # another assistant message in the same turn.
                    break
            else:
                subtype = "error"
                error_message = f"Reached the {_MAX_TOOL_ROUNDS}-round tool limit."

        except _RequestBudgetStop as exc:
            subtype = "budgetLimited" if exc.budget_limited else "error"
            error_message = str(exc)
        except or_chat.ChatError as e:
            partial_current_response = (
                len(streaming.blocks) > blocks_before_round
                or sum(len(block.text) for block in streaming.blocks) != streamed_chars_before_round
            )
            if e.kind in {
                or_chat.ChatErrorKind.CANCELLED,
                or_chat.ChatErrorKind.CONNECTION,
                or_chat.ChatErrorKind.TIMEOUT,
                or_chat.ChatErrorKind.PROTOCOL,
            } or partial_current_response or round_accepted or e.response_started:
                ambiguous_detail = str(e.kind.value)
            else:
                explicit_rejection = e
                subtype = "error"
                error_message = e.message or str(e.kind.value)
        except Exception as e:  # noqa: BLE001
            ambiguous_detail = str(e) or type(e).__name__
            if isinstance(e, _ContextContinuityError):
                error_message = f"OpenRouter context could not be archived: {e}. "

        # A provider-authored rejection before any response identity proves
        # that this exact request was not accepted.  The user message was
        # staged in the replay array before the worker crossed HTTP, so remove
        # that exact newest exchange before releasing the attempt.  Otherwise
        # the FIFO flush below would send the rejected prompt again as context
        # for the next queued draft, outside the attempt that owned it.
        #
        # Compaction never drops the newest exchange, so anything other than
        # this exact tail is an ownership invariant failure.  Hold the attempt
        # for recovery instead of guessing which history entry to remove.
        if explicit_rejection is not None and not first_response_id:
            staged_user = {"role": "user", "content": _user_text}
            if self._history and self._history[-1] == staged_user:
                self._history.pop()
            else:
                ambiguous_detail = (
                    ambiguous_detail
                    or "pre-acceptance history ownership could not be verified"
                )

        # Finalize: build the Turn, write the transcript mirror, emit signals.
        turn = streaming.to_turn()
        for tr in tool_results:
            turn.tool_results.append(tr)

        duration_ms = int((time.monotonic() - started_at) * 1000)
        result = {
            "type": "result",
            "subtype": subtype,
            "provider": model_catalog.PROVIDER_OPENROUTER,
            "total_cost_usd": round(total_cost, 6),
            "duration_ms": duration_ms,
            "usage": {
                "input_tokens": last_usage.input_tokens,
                "output_tokens": last_usage.output_tokens,
                "cache_read_input_tokens": last_usage.cached_tokens,
                "cache_creation_input_tokens": 0,
            },
            # Every round of this turn, summed. ``usage`` above is the last
            # round alone on purpose — session_state.context_fill_for reads it
            # back out of the mirror as "what is resident in the window now" —
            # but the per-turn footer wants the turn, and rendering one round's
            # tokens beside three rounds' cumulative cost on the same line
            # described two different things as if they were one.
            "turn_usage": {
                "input_tokens": aggregate_input,
                "output_tokens": aggregate_output,
                "cache_read_input_tokens": aggregate_cached,
                "cache_creation_input_tokens": 0,
            },
            "modelUsage": {},
        }
        if pause_reason and subtype == "interrupted":
            result["stop_reason"] = pause_reason

        try:
            if turn.has_content or tool_results:
                self._transcript.append_assistant(
                    turn,
                    model=self._model,
                    result=result,
                    turn_id=first_response_id,
                )
            HistoryLog(self._session_id).save_strict(self._history)
        except Exception as exc:  # noqa: BLE001
            ambiguous_detail = ambiguous_detail or f"local persistence: {exc}"

        if ambiguous_detail:
            if first_response_id:
                GLib.idle_add(self.emit, "turn-status-updated", {
                    "threadId": self._session_id, "turnId": first_response_id,
                    "status": "interrupted" if self._is_cancelled() else "failed",
                })
            self._busy = False
            self._record_execution_stop(
                ExecutionStopEvidence(
                    acknowledgement={},
                    queue_disposition="held",
                )
            )
            self.end_input()
            _log.warning("ambiguous OpenRouter turn retained: %s", ambiguous_detail)
            GLib.idle_add(
                self.emit,
                "error",
                error_message + "OpenRouter ended without an authoritative terminal receipt; "
                "this Work remains blocked until provider-backed recovery "
                "confirms the outcome.",
            )
            GLib.idle_add(self.emit, "exited", 1)
            return

        has_contribution = turn.has_content or bool(tool_results)
        if has_contribution and not self._record_execution_contribution(turn):
            if first_response_id:
                GLib.idle_add(self.emit, "turn-status-updated", {
                    "threadId": self._session_id, "turnId": first_response_id, "status": "failed",
                })
            self._busy = False
            self.end_input()
            GLib.idle_add(
                self.emit,
                "error",
                "Helios could not persist OpenRouter's contribution; the "
                "result was withheld and new provider work remains blocked.",
            )
            GLib.idle_add(self.emit, "exited", 1)
            return

        if explicit_rejection is not None and not first_response_id:
            evidence = ExecutionTerminalEvidence(
                evidence_type="verified_rejection",
                status="failed",
                reason_code="openrouter.http_rejected",
                provider_status=str(explicit_rejection.kind.value),
                request_id=self.execution_attempt_id,
                native_id=self._confirmed_execution_native_id(),
                queue_disposition="restored",
            )
        else:
            terminal_status = {
                "success": "completed",
                "budgetLimited": "budgetLimited",
                "interrupted": "aborted",
            }.get(subtype, "failed")
            terminal_usage = {
                "input_tokens": aggregate_input,
                "cache_read_input_tokens": aggregate_cached,
                "cache_creation_input_tokens": 0,
                "output_tokens": aggregate_output,
                "tool_calls": tool_call_count,
                "requests": request_count,
                "duration_ms": duration_ms,
            }
            evidence = ExecutionTerminalEvidence(
                evidence_type="provider_terminal",
                status=terminal_status,
                reason_code=(
                    f"openrouter.{pause_reason}" if pause_reason and subtype == "interrupted"
                    else "openrouter.provider_terminal"
                ),
                provider_status=(
                    str(explicit_rejection.kind.value)
                    if explicit_rejection is not None
                    else subtype
                ),
                request_id=self.execution_attempt_id,
                turn_id=first_response_id,
                response_id=final_response_id,
                native_id=self._confirmed_execution_native_id(),
                usage=terminal_usage,
                cost_micro_usd=(
                    int(round(total_cost * 1_000_000))
                    if request_count and cost_complete
                    else None
                ),
                queue_disposition=(
                    "restored" if terminal_status in {"budgetLimited", "aborted"} else "released"
                ),
            )

        if not self._finish_execution_with_evidence(evidence):
            if first_response_id:
                GLib.idle_add(self.emit, "turn-status-updated", {
                    "threadId": self._session_id, "turnId": first_response_id, "status": "failed",
                })
            self._busy = False
            self.end_input()
            GLib.idle_add(
                self.emit,
                "error",
                "Helios could not persist execution completion; the result "
                "was withheld and new provider work remains blocked.",
            )
            GLib.idle_add(self.emit, "exited", 1)
            return

        context_window = self._context_window_total()

        def publish_terminal() -> bool:
            """Publish and advance the FIFO as one main-loop transaction."""

            if first_response_id:
                self.emit("turn-status-updated", {
                    "threadId": self._session_id, "turnId": first_response_id,
                    "status": (
                        "completed" if subtype == "success"
                        else "interrupted" if subtype == "interrupted" else "failed"
                    ),
                })
            self.emit("turn-appended", turn)
            self.emit("result", result)
            # prompt_tokens already includes the cached prefix; adding the
            # reply gives what the window actually holds now.
            self.emit(
                "usage-updated",
                last_usage.input_tokens + last_usage.output_tokens,
                context_window,
            )
            if error_message:
                self.emit("error", error_message)
            # Keep admission visibly busy until every terminal callback above
            # has run. A newly typed prompt must join the existing queue, and
            # the oldest row is promoted before any newer direct dispatch.
            self._busy = False
            if not self._token_budget_exhausted and subtype != "interrupted":
                self._flush_user_queue()
            if self._closed:
                self.emit("exited", 0)
            return False

        GLib.idle_add(publish_terminal)

    # ── streaming round ──────────────────────────────────────────────────

    def _stream_round(
        self,
        api_key: str,
        streaming: StreamingAssistant,
        *,
        max_tokens: int = 0,
        round_context=(),
        handoff: bool = False,
        on_response_accepted: Callable[[str], None] | None = None,
    ) -> tuple[list[str], list, str, or_chat.ChatUsage, tuple[dict, ...]]:
        """Compact, stream one model turn, and collect what it produced.

        Retries once on a context-length rejection with a harder trim. Without
        it a session that fills its window is permanently dead: nothing shrinks
        the history, so every later send re-submits the same oversized request
        and fails identically.
        """
        window = self._context_window()
        budget = _HISTORY_BUDGET
        context_tokens = (
            math.ceil(estimate_tokens(round_context) * _PROMPT_ESTIMATE_MARGIN)
            if round_context else 0
        )
        while True:
            if window > 0:
                self._compact_history(max(1, int(window * budget) - context_tokens))
            round_text: list[str] = []
            round_tool_calls: list[or_chat.ToolCallRequest] = []
            finish_reason = ""
            usage = or_chat.ChatUsage()
            reasoning_details: tuple[dict, ...] = ()
            accepted = False
            response_started = False
            reservation_id = ""
            if self._budget_store is not None:
                max_tokens, reservation_id = self._reserve_request_budget(
                    additional_prompt_tokens=context_tokens,
                    completion_limit=_HANDOFF_MAX_TOKENS if handoff else 0,
                )

            def accepted_response(response_id: str) -> None:
                nonlocal accepted
                accepted = True
                if on_response_accepted is not None:
                    on_response_accepted(response_id)

            try:
                for event in or_chat.stream_chat(
                    [*self._history, *round_context] if round_context else self._history,
                    model=self._model,
                    api_key=api_key,
                    tools=self._turn_tools,
                    allow_tool_calls=not handoff,
                    provider_only=self._route.provider_slug if self._route else "",
                    cache_prefix=bool(self._route and self._route.explicit_caching),
                    reasoning=(
                        or_chat.reasoning_for_effort(self._effort_key)
                        if self._effort_key
                        and (self._route is None or self._route.supports_reasoning)
                        else None
                    ),
                    max_tokens=max_tokens,
                    cancellation=self._cancel_token,
                    on_response_accepted=accepted_response,
                    max_attempts=1,
                ):
                    response_started = True
                    if isinstance(event, or_chat.TextDelta):
                        _grow_block(streaming, "text", event.text)
                        round_text.append(event.text)
                        self._emit_streaming(streaming)
                    elif isinstance(event, or_chat.ReasoningDelta):
                        _grow_block(streaming, "reasoning_summary", event.text)
                        self._emit_streaming(streaming)
                    elif isinstance(event, or_chat.ToolCallDelta):
                        round_tool_calls.append(event.call)
                    elif isinstance(event, or_chat.Done):
                        usage = event.completion.usage
                        finish_reason = event.completion.finish_reason
                        reasoning_details = event.completion.reasoning_details
            except or_chat.ChatError as e:
                response_started = response_started or e.response_started
                rejected_before_execution = e.kind in {
                    or_chat.ChatErrorKind.AUTHENTICATION, or_chat.ChatErrorKind.PAYMENT_REQUIRED,
                    or_chat.ChatErrorKind.RATE_LIMIT, or_chat.ChatErrorKind.CONTEXT_LENGTH,
                    or_chat.ChatErrorKind.NO_ELIGIBLE_ENDPOINT, or_chat.ChatErrorKind.MODEL_UNAVAILABLE,
                } or (e.kind is or_chat.ChatErrorKind.HTTP and e.status in {400, 404, 422})
                # An upstream 5xx/timeout can hide work already performed.
                # The execution lifecycle may terminate, but dollars stay held.
                if reservation_id and not accepted and not response_started and rejected_before_execution:
                    self._work_cost_usd = self._budget_store.settle_openrouter_request(
                        self.execution_attempt_id, reservation_id,
                        cost_micro_usd=0, rejected=True,
                    ) / 1_000_000
                if e.kind is or_chat.ChatErrorKind.RATE_LIMIT:
                    self._emit_rate_limit(e)
                # The pinned endpoint is ineligible under the request policy —
                # typically it trains on prompts, which data_collection="deny"
                # excludes. The endpoints API does not publish per-endpoint data
                # policy, so this is only discoverable by being told. Drop that
                # provider for good, re-route, and retry; an unpinned retry is
                # the floor, which is what the request did before pinning.
                if (
                    e.kind is or_chat.ChatErrorKind.NO_ELIGIBLE_ENDPOINT
                    and self._route is not None
                    and not accepted
                    and not response_started
                ):
                    rejected = self._route.provider_slug
                    or_routes.exclude(self._model, rejected)
                    self._route = or_routes.route_for(self._model)
                    _log.info(
                        "endpoint %s rejected by the request policy; re-routed to %s",
                        rejected,
                        self._route.provider_slug if self._route else "unpinned",
                    )
                    if self._route is None or self._route.provider_slug != rejected:
                        window = self._context_window()
                        continue
                    raise
                if (
                    e.kind is not or_chat.ChatErrorKind.CONTEXT_LENGTH
                    or accepted
                    or response_started
                ):
                    raise
                # Halve and retry only while compaction actually removes
                # something. A fixed retry floor is not enough: the window may
                # be unknown (see _context_window), and a retry that drops
                # nothing re-sends byte-identical bytes the provider just
                # rejected. compact() refuses to drop the newest exchange, so
                # this terminates on its own.
                budget /= 2
                target = (
                    max(1, int(window * budget) - context_tokens)
                    if window > 0
                    else int(estimate_tokens(self._history) * budget)
                )
                if self._compact_history(target) == 0:
                    raise
                _log.info("context-length rejection; retrying after a harder trim")
                continue
            if (
                reservation_id and usage.reported and usage.cost_usd is not None
                and math.isfinite(usage.cost_usd) and usage.cost_usd >= 0
            ):
                self._work_cost_usd = self._budget_store.settle_openrouter_request(
                    self.execution_attempt_id, reservation_id,
                    cost_micro_usd=micro_usd(usage.cost_usd),
                ) / 1_000_000
            return (
                round_text,
                round_tool_calls,
                finish_reason,
                usage,
                reasoning_details,
            )

    def _reserve_request_budget(
        self, *, additional_prompt_tokens: int = 0, completion_limit: int = 0,
    ) -> tuple[int, str]:
        """Compact first, then price and reserve every actual HTTP attempt.

        Retries can change endpoint and context size, so neither a previous
        reservation nor its max_tokens may authorize a later request.
        Cached input remains real context; reported dollar cost is what settles
        its cheaper replay. Preflight assumes a cache miss to bound the request.
        """
        used = self._budget_store.initialize_openrouter_budget(self.execution_attempt_id)
        self._work_cost_usd = used / 1_000_000
        prices = self._effective_prices()
        if prices is None:
            self._trip_runtime_budget(kind="cost-unverified", used=self._lifetime_tokens_used)
            raise _RequestBudgetStop(
                "OpenRouter could not verify the new endpoint's prices. "
                "Stopped before sending another request; queued messages were kept."
            )
        remaining = WORK_COST_LIMIT_MICRO_USD - used
        prompt_cost = self._prompt_cost() + additional_prompt_tokens * prices[0]
        reserve = (
            self._route.completion_reserve if self._route
            else self._unpinned_completion_reserve() or 4096
        )
        if reserve < _MIN_USEFUL_COMPLETION:
            raise _RequestBudgetStop(
                f"{self._model} leaves only {reserve} tokens for a reply. "
                "Choose a model with a larger context window.", budget_limited=False,
            )
        allowed = reserve
        if completion_limit > 0:
            allowed = min(allowed, completion_limit)
        if prices[1] > 0:
            # Leave one micro-dollar for upward rounding of the reservation.
            allowed = min(allowed, max(0, int(
                (remaining / 1_000_000 - prompt_cost - 0.000001) / prices[1]
            )))
        charge = micro_usd(self._projected_round_cost(prompt_cost, allowed))
        request_id = uuid.uuid4().hex
        try:
            if allowed < _MIN_USEFUL_COMPLETION or charge > remaining:
                raise SpendLimitReached()
            self._budget_store.reserve_openrouter_request(
                self.execution_attempt_id, request_id, charge,
            )
        except SpendLimitReached:
            self._trip_runtime_budget(kind="cost-projected", used=self._lifetime_tokens_used)
            raise _RequestBudgetStop(
                "OpenRouter stopped before this request: its maximum projected cost "
                f"would exceed this Work's ${OPENROUTER_STANDARD_COST_BUDGET_USD:.2f} "
                f"spend limit (${self._work_cost_usd:.4f} charged or reserved). "
                "Queued messages were kept."
            ) from None
        return allowed, request_id

    def _context_window(self) -> int:
        """Prompt budget in tokens for this session, or 0 when unknown.

        Prefers the *pinned endpoint's* budget: endpoints of one slug differ in
        context length and max output, and the model-level catalog figure is the
        maximum across all of them, so sizing against it overruns the smaller
        ones. Falls back to the catalog only when no route resolved.

        Deliberately not ``model_catalog.context_window_for``: that substitutes
        a 200k display default for any model missing from the on-disk cache,
        and sizing a trim against a fabricated window several times the real one
        makes compaction silently inert exactly when it is needed. Unknown is
        reported as unknown, and the CONTEXT_LENGTH path trims relative to the
        current size instead.
        """
        if self._route is not None and self._route.prompt_budget > 0:
            return self._route.prompt_budget
        from helios.backend.openrouter import catalog as or_catalog

        try:
            return int(or_catalog.context_length_for(self._model) or 0)
        except Exception:  # noqa: BLE001 — an unreadable cache is just unknown
            return 0

    def _context_window_total(self) -> int:
        """Denominator for the context ring: the PINNED endpoint's window.

        ``model_catalog.context_window_for`` reports the model-level maximum
        across every endpoint, which on a pinned session is a different number
        from the one in force — measured 2026-09-03, the catalog said
        1,048,576 for a session pinned to an endpoint serving 1,000,000, so
        the ring read low against the window compaction actually sizes to.
        Falls back to the catalog when no route resolved, because an unpinned
        request really is served by the model-level pool.
        """
        if self._route is not None and self._route.context_length > 0:
            return self._route.context_length
        return model_catalog.context_window_for(self._model)

    def _compact_history(self, max_tokens: int) -> int:
        """Trim the replayed history to ``max_tokens``. Returns messages dropped.

        Archive source exchanges before removing them, retain bounded quoted
        user evidence, and announce the boundary. A failed archive leaves the
        original replay untouched and surfaces actionable recovery guidance.
        """
        if max_tokens <= 0:
            return 0
        before = estimate_tokens(self._history)
        try:
            trimmed, dropped = compact(self._history, max_tokens=max_tokens,
                                       archive=ContextArchive(self._session_id))
        except (OSError, ValueError) as exc:
            raise _ContextContinuityError(str(exc)) from exc
        if dropped:
            self._history = trimmed
            self._prompt_receipt = (0, 0)  # the receipt described the old array
            HistoryLog(self._session_id).save_strict(self._history)
            after = estimate_tokens(self._history)
            _log.info("compacted history: dropped %d message(s)", dropped)
            # Durable first, then the live signal: a marker that only exists
            # in this process is not a record.
            try:
                self._transcript.append_compaction(
                    trigger="auto", pre_tokens=before, post_tokens=after
                )
            except Exception as exc:  # noqa: BLE001 — a marker must not fail a turn
                _log.warning("compaction boundary not recorded: %s", exc)
            GLib.idle_add(
                self.emit,
                "context-compacted",
                {
                    "trigger": "auto",
                    "pre_tokens": before,
                    "post_tokens": after,
                    "dropped": dropped,
                    # Extractive user context is retained, but no semantic
                    # summary was generated. Sources remain session-recoverable.
                    "summarized": False,
                    "archived": True,
                    "context_recovery_tool": "read_context",
                    "provider": self.provider,
                },
            )
        return dropped

    # ── tool execution + approval ────────────────────────────────────────

    def _parallel_read(self, call: or_chat.ToolCallRequest) -> bool:
        """Only built-in, in-project reads can share a dispatch batch.

        Approval UI, shell commands, MCP, archive and plan state are serialized.
        A mutation is a barrier, including between two independent read groups.
        """
        if call.name not in tools.READ_ONLY_TOOLS:
            return False
        try:
            if self._estate is not None and self._estate.owns(call.name):
                return False
            arguments = json.loads(call.arguments_json)
            return isinstance(arguments, dict) and not tools.outside_target(
                call.name, arguments, self._cwd,
            )
        except Exception:  # noqa: BLE001 — optimization must never orphan a call
            return False

    def _execute_tool_safely(self, call: or_chat.ToolCallRequest) -> tuple[str, bool]:
        if self._is_cancelled():
            return "Cancelled by the user.", True
        try:
            content, is_error = self._execute_tool_call(call)
            return _scrub_tool_output(content), is_error
        except Exception as exc:  # noqa: BLE001 — always pair every call
            _log.warning("tool dispatch failed: %s", exc)
            return _scrub_tool_output(f"{call.name} failed: {exc}"), True

    def _execute_tool_batch(self, calls, streaming):
        """Run bounded consecutive reads concurrently, emit replies in order."""
        index = 0
        while index < len(calls):
            group = [calls[index]]
            index += 1
            if self._parallel_read(group[0]):
                while (index < len(calls) and len(group) < _MAX_PARALLEL_READS
                       and self._parallel_read(calls[index])):
                    group.append(calls[index])
                    index += 1
            for call in group:
                streaming.blocks.append(Block(
                    type="tool_use", tool_use_name=call.name,
                    tool_use_id=call.id, tool_use_input_json=call.arguments_json,
                ))
            self._emit_streaming(streaming)
            if len(group) == 1:
                yield group[0], *self._execute_tool_safely(group[0])
                continue
            # Do not cross a mutation barrier until every read has settled.
            # These workers use only local built-ins; they never emit UI signals
            # or modify replay history. The turn worker owns both below.
            try:
                executor = ThreadPoolExecutor(max_workers=_MAX_PARALLEL_READS)
            except Exception as exc:  # noqa: BLE001 — pair even if no worker can start
                for call in group:
                    yield call, _scrub_tool_output(f"{call.name} could not start: {exc}"), True
                continue
            try:
                futures = []
                for call in group:
                    try:
                        futures.append(executor.submit(self._execute_tool_safely, call))
                    except Exception as exc:  # noqa: BLE001
                        futures.append(exc)
                for call, future in zip(group, futures):
                    if isinstance(future, Exception):
                        yield call, _scrub_tool_output(f"{call.name} failed: {future}"), True
                    else:
                        try:
                            content, is_error = future.result()
                        except Exception as exc:  # noqa: BLE001 — never replay a failed future
                            content, is_error = _scrub_tool_output(f"{call.name} failed: {exc}"), True
                        yield call, content, is_error
            finally:
                executor.shutdown(wait=True)

    def _execute_tool_call(self, call: or_chat.ToolCallRequest) -> tuple[str, bool]:
        """Gate one tool call through the permission mode, then execute."""
        try:
            arguments = json.loads(call.arguments_json) if call.arguments_json else {}
            if not isinstance(arguments, dict):
                arguments = {"_raw": call.arguments_json}
        except json.JSONDecodeError:
            arguments = {"_raw": call.arguments_json}

        # A plan is a UI event, not a filesystem effect: surface it before the
        # tool returns so the pane fills while the model is still explaining.
        if call.name in {"ExitPlanMode", "update_plan"}:
            try:
                payload = tools.execution_plan_payload(
                    arguments, thread_id=self._session_id, turn_id=self._plan_turn_id,
                    proposal=call.name == "ExitPlanMode",
                )
            except tools.ToolFailed as exc:
                return str(exc), True
            if self._is_cancelled():
                return "Tool execution cancelled by the user.", True
            GLib.idle_add(self.emit, "plan-updated", payload)
        if call.name in {"read_context", "checkpoint_context"}:
            if self._is_cancelled():
                return "Tool execution cancelled by the user.", True
            try:
                archive = ContextArchive(self._session_id)
                if call.name == "read_context":
                    return archive.read(arguments), False
                archive.checkpoint(_scrub_tool_output(arguments.get("summary", "")))
                return "Model checkpoint saved; it remains unverified evidence, not user requirements.", False
            except (OSError, ValueError) as exc:
                return f"{call.name} failed: {exc}", True

        estate_call = self._estate is not None and self._estate.owns(call.name)
        approval_name = self._estate.approval_name(call.name, arguments) if estate_call else call.name
        outside_path = tools.outside_target(call.name, arguments, self._cwd)
        decision = tools.decide(
            approval_name, self._permission_mode, outside_workspace=bool(outside_path)
        )
        if estate_call and call.name == "EstateSearchTools":
            # Explicit local inventory discovery, never a server-supplied hint.
            decision = "auto"

        if decision == "deny":
            return tools.denial_message(
                approval_name, self._permission_mode, outside_workspace=bool(outside_path)
            ), True

        if decision == "ask":
            if self._has_session_grant(approval_name, outside_path, arguments):
                _log.info("session grant covers %s", approval_name)
            else:
                allowed = self._request_approval(approval_name, arguments, outside_path)
                if not allowed:
                    return "The user declined this tool request.", True

        if estate_call:
            if self._is_cancelled():
                return "Tool execution cancelled by the user.", True
            outcome = self._estate.call(call.name, arguments)
            self._init_mcp_servers = [dict(row) for row in self._estate.statuses]
            return outcome

        return tools.execute_tool(
            call.name,
            arguments,
            cwd=self._cwd,
            cancellation=self._cancel_token,
        )

    def _prompt_tokens_upper_bound(self) -> int:
        """A conservative count for the prompt about to be sent.

        Anchored on the provider's own ``prompt_tokens`` receipt for the last
        request when one exists and the history has only grown since; only the
        messages appended after that receipt are estimated, and those carry the
        margin. With no receipt — the first round of a session, or after a
        compaction rewrote the array — the whole history is estimated with the
        margin, plus the tool-schema overhead the estimate never sees.

        The result is an upper bound at the worst tokenizer ratio measured, not
        an exact count; the ceiling it feeds is approximate by construction and
        is described that way.
        """
        anchor_len, anchor_tokens = self._prompt_receipt
        if anchor_tokens > 0 and 0 < anchor_len <= len(self._history):
            appended = self._history[anchor_len:]
            return anchor_tokens + int(
                estimate_tokens(appended) * _PROMPT_ESTIMATE_MARGIN
            )
        return int(
            (estimate_tokens(self._history) + self._schema_tokens)
            * _PROMPT_ESTIMATE_MARGIN
        )

    def _effective_prices(self) -> tuple[float, float] | None:
        """``(input, output)`` dollars per token for this session, or None.

        The pinned endpoint's own prices when it published them; otherwise the
        model-level catalog figure. Without that fallback an unpinned session
        (endpoint discovery failed — a supported path) or an endpoint with a
        mangled pricing payload had **no monetary bound at all**, only the
        token budget, and the token budget is not money: the same 200,000
        tokens is $0.02 on one model and $6.00 on another (a review finding, round
        12). Model-level prices are the maximum across that model's endpoints,
        so standing in with them is conservative in the right direction.

        None means no price is available anywhere, which the live catalog does
        not currently contain a single case of (measured: 420 of 420 models
        carry usable prices). Such a session is bounded by tokens alone and
        says so once at start.
        """
        if self._route is not None and self._route.pricing_known:
            return self._route.input_price, self._route.output_price
        try:
            from helios.backend.openrouter import catalog as or_catalog

            return or_catalog.pricing_for(self._model)
        except Exception:  # noqa: BLE001 — an unreadable cache is just unknown
            return None

    def _prompt_cost(self) -> float:
        """What replaying the current history would cost as input, in dollars.

        One figure, computed once and handed to both the affordability clamp
        and the projection, so the two can never disagree about the size of the
        same prompt. Conservative — see ``_prompt_tokens_upper_bound``.
        """
        prices = self._effective_prices()
        if prices is None:
            return 0.0  # no price anywhere: bounded in tokens, not dollars
        return self._prompt_tokens_upper_bound() * prices[0]

    def _affordable_completion_tokens(self, prompt_cost: float) -> tuple[int, str]:
        """The largest reply this round may ask for, and which ceiling bound it.

        One expression, every ceiling, every route. Seven rounds of review
        found seven next-order holes in this clamp and every one was a *branch*
        where a bound did not apply — including, in round 8, the branch where
        no route resolved at all: an unpinned request is a supported fallback
        (``start()`` does not require a route, ``_stream_round`` sends without
        a pin), and it was escaping both ceilings and the completion cap.

        So there are no branches on route availability. With no route there is
        no endpoint window to reserve against, so the model-level catalog
        supplies a conservative reserve; the token budget always applies; the
        dollar budget applies where the endpoint publishes real prices
        (``pricing_known`` separates a *confirmed* free endpoint from one whose
        price is missing, unparsable or a negative sentinel). Both budgets
        charge for the prompt first, because both count it.

        Returns ``(tokens, bound)`` where ``bound`` is ``"reserve"``,
        ``"tokens"`` or ``"cost"`` — the caller reports the ceiling that
        actually stopped it rather than always blaming spend (round 8), and
        distinguishes a *budget* it has spent from an *endpoint* that was
        never going to fit a useful reply (round 10).
        """
        reserve = (
            self._route.completion_reserve
            if self._route is not None and self._route.completion_reserve > 0
            else self._unpinned_completion_reserve()
        )

        # The token ceiling applies to every route, pinned or not, priced or
        # not. Free of charge is not free of budget.
        token_allowance = (
            OPENROUTER_STANDARD_TOKEN_BUDGET
            - self._lifetime_tokens_used
            - self._prompt_tokens_upper_bound()
        )
        if reserve > 0 and reserve <= token_allowance:
            allowed, bound = reserve, "reserve"
        else:
            allowed, bound = token_allowance, "tokens"

        # The dollar ceiling applies wherever a price can be established at
        # all — the pinned endpoint's, or the model-level catalog's when there
        # is no pinned endpoint or it published none.
        prices = self._effective_prices()
        if prices is not None and prices[1] > 0:
            remaining = (
                OPENROUTER_STANDARD_COST_BUDGET_USD
                - self._lifetime_cost_usd
                - max(0.0, prompt_cost)
            )
            by_cost = int(remaining / prices[1]) if remaining > 0 else 0
            if by_cost < allowed:
                allowed, bound = by_cost, "cost"
        return max(0, allowed), (bound or "tokens")

    def _unpinned_completion_reserve(self) -> int:
        """A conservative reply reserve when no endpoint resolved.

        Endpoint discovery can fail and the request then goes to the
        model-level pool unpinned. There is no endpoint window to size
        against, so the catalog's model-level figure stands in — it is the
        maximum across that model's endpoints, which is why the same eighth-of
        -the-window rule is applied to it rather than trusting it directly.
        """
        window = 0
        try:
            from helios.backend.openrouter import catalog as or_catalog

            window = int(or_catalog.context_length_for(self._model) or 0)
        except Exception:  # noqa: BLE001 — an unreadable cache is just unknown
            window = 0
        if window <= 0:
            return 0
        return min(max(4096, window // 8), window // 2)

    def _projected_round_cost(self, prompt_cost: float, completion_tokens: int) -> float:
        """Worst case for the next request: this prompt plus that whole reply.

        Priced from the pinned endpoint's own advertised rates. Zero when the
        endpoint publishes no prices, which is honest — an unpriced endpoint
        cannot be projected and the post-round check remains the backstop.
        """
        prices = self._effective_prices()
        if prices is None:
            return 0.0
        return prompt_cost + max(0, completion_tokens) * prices[1]

    def _trip_runtime_budget(self, *, kind: str, used: int) -> bool:
        """Latch once on the worker before any further model/tool side effect."""

        if self._token_budget_exhausted:
            return False
        self._token_budget_exhausted = True
        self._budget_trip_kind = kind
        GLib.idle_add(
            self.emit,
            "budget-exhausted",
            {
                "kind": kind,
                "limit": OPENROUTER_STANDARD_TOKEN_BUDGET,
                "cost_limit_usd": OPENROUTER_STANDARD_COST_BUDGET_USD,
                "cost_usd": round(
                    self._work_cost_usd if self._budget_store is not None else self._lifetime_cost_usd, 6,
                ),
                "used": max(0, int(used)),
                "provider": self.provider,
            },
        )
        return True

    def _budget_exhausted_message(self) -> str:
        """Name the ceiling that actually tripped.

        Four kinds set this latch now — the token budget, a projected round
        that would cross it, accumulated spend, and an unmetered round — and
        the message used to be hardcoded to the token one, so a session
        stopped for money was told it had run out of tokens.

        The token wording is the fall-through rather than a branch of its own:
        an explicit `tokens` branch was dead code, since it returned exactly
        what the default already did. A mutation check caught that — the test
        passed with the branch disabled — which is the only reason it is not
        still sitting here looking meaningful.
        """
        if self._budget_trip_kind == "cost-unverified":
            return (
                "OpenRouter returned no verifiable spend receipt. This Work was stopped; "
                "the request's maximum projected cost remains reserved. "
                "Start a new bounded Work to continue."
            )
        if self._budget_trip_kind.startswith("cost"):
            cost = self._work_cost_usd if self._budget_store is not None else self._lifetime_cost_usd
            scope = "Work" if self._budget_store is not None else "process"
            return (
                f"OpenRouter reached this {scope}'s "
                f"${OPENROUTER_STANDARD_COST_BUDGET_USD:.2f} spend limit "
                f"(${cost:,.4f} used). "
                "Start a new bounded Work to continue."
            )
        if self._budget_trip_kind == "usage-unverified":
            return (
                "OpenRouter returned no verifiable token receipt, so this "
                "process was stopped. Start a new bounded Work to continue."
            )
        # tokens / tokens-projected / unset: the token ceiling is the default
        # because it is the one that always applies (see
        # ``_affordable_completion_tokens``).
        return (
            f"OpenRouter reached this process's "
            f"{OPENROUTER_STANDARD_TOKEN_BUDGET:,}-token safety limit. "
            "Start a new bounded Work to continue."
        )

    def _emit_rate_limit(self, error: or_chat.ChatError) -> None:
        """Surface a 429 to the quota indicator, not just as a transient toast.

        The signal has been declared and connected since the driver landed but
        was never emitted, so an OpenRouter rate limit was invisible in the
        context popover where every other provider reports one. The payload
        matches what ``ChatToolbar.update_rate_limit`` consumes: it keys on
        ``rateLimitType`` and drops anything without it.
        """
        GLib.idle_add(self.emit, "rate-limit-updated", {
            "rateLimitType": "openrouter",
            "provider": self._route.provider_name if self._route else "OpenRouter",
            "status": "reached",
            "message": error.message or "rate limited",
        })

    def _is_cancelled(self) -> bool:
        return self._closed or (
            self._cancel_token is not None and self._cancel_token.cancelled
        )

    #: Built-in tool-wide grants stay workspace-scoped. Shell consent is
    #: exact-command only; estate consent is bound to its verified definition.
    _SESSION_GRANTABLE = frozenset({"Read", "Write", "Edit", "Grep", "Glob"})
    _ALLOW_SESSION = "Allow for this session"
    _ALLOW_COMMAND_SESSION = "Allow this exact command for this session"
    _ALLOW_MCP_SESSION = "Allow this tool for this session"
    _ALLOW_ONCE = "Allow once"

    def _egress_destination(self) -> str:
        """Where this session's traffic goes, named for the approval prompt.

        The pinned endpoint's operator, not just the model slug: "sent to
        DeepSeek V4 Flash" and "sent to Alibaba" are different disclosures,
        and it is the operator that actually receives the bytes.
        """
        if not self._model:
            return ""
        if self._route is not None and self._route.provider_name:
            return f"{self._model} via {self._route.provider_name}"
        return self._model

    def _session_grant_key(self, tool_name: str, arguments: dict, outside_path: str) -> str:
        if outside_path:
            return ""
        if tool_name in self._SESSION_GRANTABLE:
            return tool_name
        if tool_name == "Bash":
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip():
                return ""
            # No shell parsing, prefix rules, interpolation or normalization:
            # a changed byte requires new consent, even in the same cwd.
            identity = json.dumps([os.path.realpath(self._cwd), command])
            return "command:" + hashlib.sha256(identity.encode()).hexdigest()
        if self._estate is not None:
            key_for = getattr(self._estate, "session_grant_key", None)
            return key_for(tool_name) if callable(key_for) else ""
        return ""

    def _has_session_grant(self, tool_name: str, outside_path: str, arguments: dict | None = None) -> bool:
        """True when the user already allowed this tool for the session.

        A grant never covers a call that resolves outside the working
        directory: escalating to a prompt is the whole mitigation for an
        out-of-workspace target, and a grant made about in-project edits is not
        consent to write somewhere else.
        """
        key = self._session_grant_key(tool_name, arguments or {}, outside_path)
        return bool(key and key in self._session_grants)

    def _request_approval(
        self,
        tool_name: str,
        arguments: dict,
        outside_path: str = "",
    ) -> bool:
        """Emit question-asked and block the worker until the user responds."""
        token = f"or:{self._session_id}:{tool_name}:{uuid.uuid4().hex[:8]}"
        event = threading.Event()
        self._pending_approvals[token] = (event, None)

        epoch = self._permission_epoch
        grant_key = self._session_grant_key(tool_name, arguments, outside_path)
        grant_option = self._ALLOW_SESSION
        grant_scope = (
            "Session grants last while this conversation stays open and reset "
            "when its permissions change."
        )
        if tool_name == "Bash":
            grant_option = self._ALLOW_COMMAND_SESSION
            grant_scope += (
                " Only this exact command text in this working directory is covered; "
                "it runs without a filesystem sandbox and its inputs may change."
            )
        elif grant_key.startswith("mcp:"):
            grant_option = self._ALLOW_MCP_SESSION
            grant_scope += (
                " This authorizes this MCP tool with any arguments, including "
                "external effects. A changed server configuration or tool "
                "definition requires approval again."
            )
        else:
            grant_scope += " This tool remains limited to paths inside the working directory."
        options = [self._ALLOW_ONCE]
        if grant_key:
            options.append(grant_option)
        options.append("Deny")
        self._approval_choices[token] = tuple(options)
        detail = _approval_detail(tool_name, arguments)
        if tool_name == "Bash" and isinstance(arguments.get("command"), str):
            # Show short and long commands alike as code, once, with no hidden
            # tail; exact-command consent covers the complete string.
            detail = {"kind": "code", "title": "Command", "text": arguments["command"]}
        elif detail is None and arguments:
            detail = {
                "kind": "code", "title": "Arguments",
                "text": json.dumps(tools._redact_tool_input(arguments), ensure_ascii=False,
                                   indent=2, default=str),
            }
        payload = {
            "presentation": "tool-approval",
            "allowOther": False,
            "requireExplicitChoice": True,
            "caption": f"Working directory: {self._cwd}",
            "grantScope": grant_scope if grant_key else "",
            "approvalButtonLabels": {
                self._ALLOW_COMMAND_SESSION: "Allow exact command",
                self._ALLOW_MCP_SESSION: "Allow tool for session",
                self._ALLOW_SESSION: "Allow tool for session",
            },
            # The same detail surface the Claude path renders (v0.88.0): a
            # unified diff for an Edit, the full content for a Write, the
            # whole command for Bash. Approving a Write on its path
            # alone is approving it blind, and this provider writes as the
            # desktop user.
            "detail": detail,
            "questions": [{
                "header": "Tool approval",
                "question": tools.approval_summary(
                    tool_name,
                    {},
                    agent_label="OpenRouter",
                    outside_path=outside_path,
                    destination=self._egress_destination(),
                ),
                "options": options,
            }],
        }
        GLib.idle_add(self.emit, "question-asked", payload, token)

        # Bounded wait, re-checking for shutdown. stop() only resolves the
        # approvals registered at that instant; a question raised afterwards is
        # dropped by the window (it filters on is_running, already false), so
        # an unbounded wait here would strand the worker forever with _busy
        # stuck true — which also blocks the one other path that could release
        # it, driver_manager's quiet end-input.
        while not event.wait(_APPROVAL_POLL_SECONDS):
            if self._is_cancelled():
                self._pending_approvals.pop(token, None)
                self._approval_choices.pop(token, None)
                self._session_answers.pop(token, None)
                return False
        _, allowed = self._pending_approvals.pop(token, (None, False))
        self._approval_choices.pop(token, None)
        answer = self._session_answers.pop(token, "")
        # Do not revive old consent after a permission change or Stop while
        # the dialog was open. The next model call evaluates the new mode.
        if epoch != self._permission_epoch or self._is_cancelled():
            return False
        if grant_key and grant_key != self._session_grant_key(tool_name, arguments, outside_path):
            return False
        if allowed and grant_key and answer == grant_option:
            self._session_grants.add(grant_key)
        return bool(allowed)

    def _resolve_all_approvals(self, allowed: bool) -> None:
        for token, (event, _) in list(self._pending_approvals.items()):
            self._pending_approvals[token] = (event, allowed)
            event.set()

    # ── helpers ──────────────────────────────────────────────────────────

    def _emit_streaming(self, streaming: StreamingAssistant) -> None:
        GLib.idle_add(self.emit, "assistant-streaming", streaming)

    @staticmethod
    def _notify(
        callback: Callable[[bool, str], None] | None,
        success: bool,
        detail: str = "",
    ) -> None:
        if callback is not None:
            try:
                callback(success, detail)
            except Exception as e:  # noqa: BLE001
                _log.warning("execution callback failed: %s", e)
