"""Muse (Meta) native sessions through `muse serve` (MSP v1).

One serve host per driver, newline JSON-RPC 2.0 over stdio. The handshake is
`initialize` + `initialized`; `session/start|resume` auto-subscribes the
connection, so no `view/subscribe` is needed for a fresh session. Native
session/turn identities, Work admission and terminal receipts govern every
turn, exactly as for the other native drivers.

Deliberately simple: approvals and user-input prompts reuse Helios's existing
question dialog (the decision travels as `approval/decide` /
`userInput/answer`), and subagent/workflow items feed the same
`agents-updated` snapshot the Claude driver emits, so the dock animations are
shared rather than reimplemented. Reminder children are host housekeeping
and never surface as delegated work.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import subprocess
import threading
import time
from typing import Any

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

from helios.backend import model_catalog
from helios.backend.muse_binary import MuseBinaryNotFound, find_muse_binary
from helios.backend import muse_env
from helios.backend.muse_env import CLIENT_NAME, uuid7
from helios.backend.process.codex_transcript import CodexTranscriptWriter
from helios.backend.process.env_scrub import (
    MUSE_AUTH_ENV,
    NORVI_TRACKER_ENV,
    scrubbed_child_env,
)
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence, ExecutionDispatchEvidence,
    ExecutionTerminalEvidence, MessageDelivery, RequiredPromptContextError,
    UserMessageQueueMixin,
)
from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.project_perms import effective_execution_mode, is_home_cwd
from helios.backend.sensitive_text import scrub_sensitive
from helios.backend.transcript import ToolResult
from helios.log import get_logger

_log = get_logger("muse-driver")

_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# Helios permission mode -> MSP ApprovalMode (selected on the wire).
_APPROVAL_MODES = {
    "default": "promptUnmatched",
    "acceptEdits": "onRequest",
    "auto": "onRequest",
    "bypassPermissions": "allowAll",
    "plan": "denyUnmatched",
    "dontAsk": "denyUnmatched",
}

# Muse tool name -> Helios tool-card vocabulary (Claude names). Unknown
# tools pass through unchanged and render as generic cards.
_TOOL_NAMES = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Edit",
    "bash": "Bash", "search": "Grep", "web_search": "WebSearch",
    "web_fetch": "WebFetch", "subagent_spawn": "Agent",
    "workflow": "Workflow", "write_todos": "TodoWrite",
}


def _normalize_tool(name: str, args: Any) -> tuple[str, dict]:
    parsed = args if isinstance(args, dict) else {}
    display = _TOOL_NAMES.get(name, name)
    if name in ("read_file", "write_file", "edit_file") and "path" in parsed:
        parsed = dict(parsed)
        parsed.setdefault("file_path", parsed.get("path"))
    if name == "edit_file":
        parsed = dict(parsed)
        parsed.setdefault("old_string", parsed.get("find", ""))
        parsed.setdefault("new_string", parsed.get("replace", ""))
    return display, parsed


def _parse_args(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


class MuseDriverSpawnError(RuntimeError):
    """Native Muse session could not be started."""


# The tracker grant every interactive provider gets (Infisical stays Claude-only).
MUSE_INTERACTIVE_ENV = MUSE_AUTH_ENV | NORVI_TRACKER_ENV


def serve_args(model: str, permission_mode: str) -> list[str]:
    """`muse serve` argv; sandbox posture is fixed at spawn, never on the wire."""
    args = ["serve", "--provider", "meta", "--model", model, "--trust-workspace"]
    if permission_mode == "plan":
        args += ["--disable-write", "--disable-shell"]  # honestly read-only
    elif permission_mode == "bypassPermissions":
        # Bypass = full access, like Codex danger-full-access. Also the only
        # working shell on Ubuntu: Muse rejects stock bwrap (no
        # --ro-bind-symlink) and its embedded bwrap is denied a user namespace.
        args.append("--disable-sandbox")
    return args


class MuseServeDriver(UserMessageQueueMixin, GObject.Object):
    display_name = "muse"
    provider = "muse"

    __gsignals__ = {
        "session-started": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
        "assistant-streaming": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-appended": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "result": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "usage-updated": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        "rate-limit-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "budget-exhausted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "question-asked": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "queued-user-sent": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
        "delivery-confirmed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "exited": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "spend-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "agents-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "context-compacted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "capabilities-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "activity-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "plan-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-status-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, cwd: str, model: str = "", permission_mode: str = "default",
                 resume_session_id: str | None = None, effort: str = "",
                 **kwargs: Any) -> None:
        GObject.Object.__init__(self)
        self._init_user_queue()
        self._cwd = cwd
        self._model = model
        self._effort = effort if effort in _EFFORTS else ""
        self._permission_mode = effective_execution_mode(
            permission_mode, cwd, provider=self.provider)
        self._resume_session_id = resume_session_id or ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._session_id = ""
        self._is_busy = False
        self._accepting_input = False
        self._stopped = False
        self._last_activity = time.monotonic()
        self._streaming: StreamingAssistant | None = None
        self._pending_text = ""
        self._prepared_prompt = None
        self._accepted = False
        self._accepted_turn_id = ""
        self._accounting_failed = False
        self._stderr = ""
        self._startup_pending_text: str | None = None
        self._rpc_seq = 0
        self._pending_rpc: dict[int, str] = {}
        self._root_turn_id = ""
        self._turn_usage: dict[str, int] = {}
        self._blocks: dict[str, Block] = {}
        self._tool_outputs: dict[str, str] = {}
        self._agents: dict[str, dict[str, str]] = {}
        self._pending_approvals: dict[str, dict] = {}
        self._pending_inputs: dict[str, dict] = {}
        self._transcript = CodexTranscriptWriter(cwd, provider=model_catalog.PROVIDER_MUSE)
        self.init_tools: list[str] = []
        self.init_mcp_servers: list[dict[str, Any]] = []

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def is_busy(self) -> bool:
        return self._is_busy

    @property
    def is_initializing(self) -> bool:
        return self.is_running and not self._session_id and not self._stopped \
            and not self._accounting_failed

    @property
    def is_accepting_input(self) -> bool:
        return (
            self._accepting_input
            and not self._stopped
            and not self._accounting_failed
            and self.is_running
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def model(self) -> str:
        return self._model

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def live_session_id(self) -> str:
        return self._session_id

    @property
    def last_activity(self) -> float:
        return self._last_activity

    @property
    def effort_key(self) -> str:
        return self._effort

    @property
    def observed_agent_root_turn_id(self) -> str:
        return self._root_turn_id

    def observed_agent_snapshot(self) -> dict:
        return copy.deepcopy(self._agents)

    # ── wire ────────────────────────────────────────────────────────────────

    def _send(self, method: str, params: dict | None = None, *, notify: bool = False) -> bool:
        if not self._proc or not self._proc.stdin:
            return False
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self._rpc_seq += 1
            message["id"] = self._rpc_seq
            self._pending_rpc[self._rpc_seq] = method
        return self._write(message)

    def _respond(self, request_id: Any, result: Any) -> bool:
        return self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _write(self, message: dict) -> bool:
        if not self._proc or not self._proc.stdin:
            return False
        line = (json.dumps(message) + "\n").encode()
        try:
            offset = 0
            while offset < len(line):
                written = self._proc.stdin.write(line[offset:])
                if not isinstance(written, int) or written <= 0:
                    raise OSError("incomplete input write")
                offset += written
            self._proc.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._model or not self._model.startswith("muse-"):
            raise MuseDriverSpawnError("Choose a discovered Muse model before starting a session.")
        if is_home_cwd(self._cwd):
            raise MuseDriverSpawnError("Muse sessions cannot start in your home folder. Choose a project directory.")
        try:
            binary = find_muse_binary()
        except MuseBinaryNotFound as exc:
            raise MuseDriverSpawnError(str(exc)) from exc
        cmd = [str(binary.path), *serve_args(self._model, self._permission_mode)]
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=self._cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=scrubbed_child_env(keep=MUSE_INTERACTIVE_ENV),
                bufsize=0, start_new_session=True)
        except OSError as exc:
            raise MuseDriverSpawnError("Could not start `muse serve`.") from exc
        self._stopped = False
        self._accepting_input = False
        threading.Thread(target=self._read_stderr, daemon=True, name="muse-stderr").start()
        threading.Thread(target=self._read_stdout, daemon=True, name="muse-stdout").start()
        from helios import __version__ as helios_version

        if not self._send("initialize", {
                "clientInfo": {"name": CLIENT_NAME, "version": helios_version},
                "capabilities": {"experimentalApi": True}}):
            raise MuseDriverSpawnError("Could not start `muse serve`.")

    def _begin_session(self) -> None:
        approval = _APPROVAL_MODES.get(self._permission_mode, "promptUnmatched")
        if self._resume_session_id:
            self._send("session/resume", {
                "commandId": uuid7(), "sessionId": self._resume_session_id,
                "excludeItems": True})
        else:
            self._send("session/start", {
                "commandId": uuid7(), "workspaceRoot": self._cwd,
                "modelId": self._model, "approvalMode": approval})

    def _on_session_bound(self, session: Any) -> None:
        if self._stopped or self._accounting_failed or not isinstance(session, dict):
            return
        sid = session.get("sessionId")
        if not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
            self._protocol_failure("Muse did not report a valid native session identity.")
            return
        if self._session_id:
            if sid != self._session_id:
                self._protocol_failure("Muse changed its native session identity. The Work remains blocked.")
            return
        if self._resume_session_id and sid != self._resume_session_id:
            self._protocol_failure("Muse resumed a different session. The requested Work remains blocked.")
            return
        model_id = session.get("modelId")
        if isinstance(model_id, str) and model_id:
            self._model = model_id
        workspace = session.get("workspaceRoot")
        if isinstance(workspace, str) and workspace \
                and os.path.realpath(workspace) != os.path.realpath(self._cwd):
            self._protocol_failure("Muse reported an unexpected project. The session was stopped.")
            return
        try:
            self._transcript.bind_thread(sid)
        except Exception:
            self._hold_accounting_failure("Helios could not save the Muse session identity. The session remains blocked.")
            return
        raw_tools = session.get("tools")
        if isinstance(raw_tools, list):
            self.init_tools = [
                str(t.get("name") if isinstance(t, dict) else t)
                for t in raw_tools
                if (isinstance(t, dict) and t.get("name")) or isinstance(t, str)
            ]
        elif not self.init_tools:
            self.init_tools = list(_TOOL_NAMES.values())
        raw_mcp = session.get("mcpServers") or session.get("mcp_servers")
        if isinstance(raw_mcp, list):
            self.init_mcp_servers = [dict(row) for row in raw_mcp if isinstance(row, dict)]
        else:
            self.init_mcp_servers = [
                {"name": s["name"], "status": "configured"}
                for s in muse_env.list_mcp_servers()
                if isinstance(s, dict) and s.get("name")
            ]
        self._session_id = sid
        self._last_activity = time.monotonic()
        self._accepting_input = True
        pending, self._startup_pending_text = self._startup_pending_text, None
        self._is_busy = False
        self.emit("session-started", sid, self._cwd, self._model)
        self.emit(
            "capabilities-updated",
            {"tools": list(self.init_tools), "mcp_servers": list(self.init_mcp_servers)},
        )
        if pending:
            self.send_user_text(pending, _reuse_execution_attempt=True)

    # ── sending ─────────────────────────────────────────────────────────────

    def send_user_text(
        self,
        text: str,
        *,
        _reuse_execution_attempt: bool = False,
    ) -> MessageDelivery:
        if not self.is_running or self._stopped:
            self.emit("error", "Muse process is not running. Sign in with `muse login` in a terminal.")
            return MessageDelivery("rejected")
        if self._accounting_failed:
            self.emit("error", "Helios could not verify Muse accounting. New Work remains blocked.")
            return MessageDelivery("rejected")
        if self.is_busy and self._startup_pending_text is None:
            self.emit("error", "Muse is still processing the current turn.")
            return MessageDelivery("rejected")
        if not self._session_id:
            if self._startup_pending_text is not None:
                self.emit("error", "Muse is still preparing this session.")
                return MessageDelivery("rejected")
            reason = self._execution_block_reason()
            if reason:
                self.emit("error", reason)
                return MessageDelivery("rejected")
            try:
                self._prepare_prompt_context(text)
            except RequiredPromptContextError as exc:
                self.emit("error", exc.user_message)
                return MessageDelivery("rejected")
            admission_error = self._begin_execution_attempt(
                reuse_existing=_reuse_execution_attempt)
            if admission_error:
                self.emit("error", admission_error)
                return MessageDelivery("rejected")
            self._startup_pending_text = text
            self._is_busy = True
            self._last_activity = time.monotonic()
            return MessageDelivery("pending")
        reason = self._execution_block_reason()
        if reason:
            self.emit("error", reason)
            return MessageDelivery("rejected")
        try:
            prepared = self._prepare_prompt_context(text)
        except RequiredPromptContextError as exc:
            self.emit("error", exc.user_message)
            return MessageDelivery("rejected")
        reason = self._begin_execution_attempt(reuse_existing=_reuse_execution_attempt)
        if reason:
            self.emit("error", reason)
            return MessageDelivery("rejected")
        command_id = uuid7()
        if not self._record_execution_dispatch(ExecutionDispatchEvidence(
                wire_prompt_text=prepared.text, provider_request_key=self.execution_attempt_id,
                native_binding_id=self._confirmed_execution_native_id() or self._session_id)):
            released = self._finish_execution_with_evidence(ExecutionTerminalEvidence(
                evidence_type="local_abort", status="aborted",
                reason_code="local_abort", queue_disposition="restored"))
            if released:
                self.emit("error", "Helios could not persist the Muse dispatch. The message was not sent.")
            else:
                self._hold_accounting_failure("Helios could not close the unsent Muse reservation. This Work remains blocked.")
            return MessageDelivery("rejected")
        self._is_busy = True
        self._accepted = False
        self._accepted_turn_id = ""
        self._root_turn_id = ""
        self._pending_text = text
        self._prepared_prompt = prepared
        self._streaming = StreamingAssistant(model=self._model)
        self._blocks = {}
        self._tool_outputs = {}
        self._agents = {}
        self._turn_usage = {}
        params: dict[str, Any] = {
            "commandId": command_id,
            "sessionId": self._session_id,
            "input": [{"type": "text", "text": prepared.text}],
        }
        if self._effort:
            params["reasoningEffort"] = self._effort
        if not self._send("turn/start", params):
            self.end_input()
            self.emit("error", "Muse delivery is uncertain. This Work remains blocked until provider-backed recovery confirms the outcome.")
            return MessageDelivery("uncertain")
        self._last_activity = time.monotonic()
        # The turn/start ack (turnId) is native acceptance evidence; the
        # provisional bubble is released only then, never on the pipe write.
        return MessageDelivery("accepted")

    def _accept_prompt(self, native_turn_id: str) -> bool:
        if self._accounting_failed:
            return False
        if self._accepted or not self.execution_attempt_id:
            return self._accepted
        if not self._session_id or not self._record_execution_acceptance(
                ExecutionAcceptanceEvidence(
                    accepted_turn_id=native_turn_id,
                    provider_request_key=self.execution_attempt_id,
                    native_binding_id=self._session_id)):
            self._hold_accounting_failure("Helios could not record Muse acceptance. This Work remains blocked.")
            return False
        try:
            self._transcript.note_user_text(self._pending_text)
        except Exception:
            self._hold_accounting_failure("Helios could not save the accepted Muse prompt. This Work remains blocked.")
            return False
        self._accepted = True
        self._accepted_turn_id = native_turn_id
        if self._prepared_prompt is not None:
            self._prepared_prompt.mark_sent()
            self._prepared_prompt = None
        self._pending_text = ""
        self._accept_uncertain_queue_delivery()
        self.emit("delivery-confirmed")
        return True

    def _flush_user_queue(self) -> None:
        """Keep a written queue head provisional until native acceptance."""
        if not self._user_queue or not self.is_accepting_input \
                or self._uncertain_queue_delivery is not None:
            return
        reason = self._execution_block_reason()
        if reason:
            self.emit("error", reason)
            return
        delivery = self._user_queue[0]
        self._queue_dispatch_in_progress = True
        try:
            outcome = self.send_user_text(delivery[1])
        finally:
            self._queue_dispatch_in_progress = False
        if outcome.accepted or outcome.uncertain:
            self._uncertain_queue_delivery = delivery

    # ── inbound dispatch ────────────────────────────────────────────────────

    def _dispatch_message(self, msg: dict[str, Any]) -> bool:
        if not isinstance(msg, dict):
            return False
        self._last_activity = time.monotonic()
        # Server-initiated request (must answer): receipt first, decide later.
        if "method" in msg and "id" in msg:
            self._respond(msg["id"], {})
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if msg["method"] == "approval/request":
                self._on_approval_requested(params)
            elif msg["method"] == "userInput/request":
                self._on_user_input_requested(params)
            return False
        # Result / error for one of our requests.
        if "id" in msg and ("result" in msg or "error" in msg):
            method = self._pending_rpc.pop(msg["id"], "")
            if "error" in msg:
                detail, _ = scrub_sensitive(
                    str((msg.get("error") or {}).get("message") or "request failed"))
                if method in ("initialize", "session/start", "session/resume"):
                    self._protocol_failure(f"Muse refused to start: {detail[:400]}")
                elif method == "turn/start" and self.execution_attempt_id:
                    self.emit("error", f"Muse refused the turn: {detail[:400]}")
                    self._finish_turn("failed", error_text=detail[:400])
                elif method:
                    self.emit("error", detail[:400])
                return False
            result = msg.get("result")
            if method == "initialize":
                self._send("initialized", None, notify=True)
                self._begin_session()
            elif method in ("session/start", "session/resume"):
                session = result.get("session") if isinstance(result, dict) else None
                self._on_session_bound(session)
            elif method == "turn/start" and isinstance(result, dict):
                turn_id = result.get("turnId")
                if isinstance(turn_id, str) and turn_id:
                    self._root_turn_id = turn_id
                    self._accept_prompt(turn_id)
            return False
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "turn/started":
            turn_id = params.get("turnId")
            if isinstance(turn_id, str) and turn_id:
                self._root_turn_id = turn_id
                self._accept_prompt(turn_id)
                self.emit("turn-status-updated",
                          {"turnId": turn_id, "status": "inProgress"})
        elif method in ("item/started", "item/updated", "item/completed"):
            item = params.get("item")
            if isinstance(item, dict):
                self._on_item(item, terminal=method == "item/completed")
        elif method == "item/delta":
            self._on_delta(params)
        elif method == "session/tokenUsage":
            usage = params.get("usage") if isinstance(params.get("usage"), dict) else {}
            for key, value in usage.items():
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self._turn_usage[key] = self._turn_usage.get(key, 0) + value
            cumulative = params.get("cumulative") if isinstance(params.get("cumulative"), dict) else {}
            prompt = cumulative.get("promptTokens")
            output = cumulative.get("outputTokens")
            if isinstance(prompt, int) and isinstance(output, int):
                self.emit("usage-updated", prompt, output)
        elif method == "usage/changed":
            self.emit("rate-limit-updated", params)
        elif method == "session/contextUsage":
            self.emit("activity-updated", {
                "category": "tool-progress",
                "windowTokens": params.get("windowTokens"),
                "usedTokens": params.get("usedTokens"),
                "pressure": params.get("pressure"),
            })
        elif method == "session/todoListChanged":
            todos = params.get("todos") or params.get("items") or params.get("todoList")
            if isinstance(todos, list):
                self.emit("plan-updated", {"source": "turn", "plan": todos})
        elif method == "approval/requested":
            self._on_approval_requested(params)
        elif method == "userInput/requested":
            self._on_user_input_requested(params)
        elif method == "turn/completed":
            terminal = str(params.get("terminal") or "")
            usage = params.get("usage") if isinstance(params.get("usage"), dict) else {}
            for key, value in usage.items():
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self._turn_usage[key] = value
            error = params.get("error")
            detail = ""
            if isinstance(error, dict):
                detail, _ = scrub_sensitive(str(error.get("message") or error))
            self._finish_turn(terminal, error_text=detail[:2000])
        return False

    # ── items → streaming blocks, tool cards, agent dock ────────────────────

    def _block_for(self, item_id: str, block_type: str) -> Block | None:
        if self._streaming is None:
            return None
        block = self._blocks.get(item_id)
        if block is None:
            block = Block(type=block_type)
            self._blocks[item_id] = block
            self._streaming.blocks.append(block)
        return block

    def _on_item(self, item: dict, *, terminal: bool) -> None:
        kind = str(item.get("kind") or "")
        item_id = str(item.get("itemId") or "")
        if not item_id or kind in ("userMessage", "reminderChild"):
            return  # own echo; housekeeping children are not delegated work
        if self._session_id and item.get("turnId") and self._root_turn_id \
                and item.get("turnId") != self._root_turn_id:
            return  # stale-turn item; never paint it into the live turn
        status = str(item.get("status") or "")
        if kind == "agentMessage":
            block = self._block_for(item_id, "text")
            if block is not None:
                if isinstance(item.get("text"), str) and (terminal or item["text"]):
                    block.text = item["text"]
                self.emit("assistant-streaming", self._streaming)
        elif kind == "reasoning":
            block = self._block_for(item_id, "reasoning_summary")
            if block is not None:
                summary = item.get("summary")
                if isinstance(summary, list):
                    block.text = "\n".join(str(part) for part in summary)
                elif isinstance(item.get("text"), str):
                    block.text = item["text"]
                self.emit("assistant-streaming", self._streaming)
        elif kind == "toolCall":
            block = self._block_for(item_id, "tool_use")
            if block is not None:
                raw_name = str(item.get("tool") or "")
                display, parsed = _normalize_tool(raw_name, _parse_args(item.get("args")))
                block.tool_use_name = display
                block.tool_use_id = str(item.get("callId") or item_id)
                block.tool_use_input_json = json.dumps(parsed)
                output = item.get("visibleOutput")
                if isinstance(output, str) and output:
                    self._tool_outputs[block.tool_use_id] = output
                self.emit("assistant-streaming", self._streaming)
                if item.get("statusLine"):
                    self.emit("activity-updated", {
                        "category": "tool-progress", "tool": display,
                        "message": str(item.get("statusLine"))})
        elif kind in ("subagent", "workflow"):
            self._on_agent_item(item, kind=kind, status=status, terminal=terminal)
        elif kind == "compaction":
            if terminal:
                self.emit("context-compacted", {
                    "tokensBefore": item.get("tokensBefore"),
                    "tokensAfter": item.get("tokensAfter")})
        elif kind in ("userShell", "webSearch", "hookRun", "sideChat"):
            text = item.get("visibleOutput") or item.get("fallbackText") or ""
            self.emit("activity-updated", {
                "category": "tool-progress", "tool": kind,
                "message": str(text)[:500]})
        else:  # unknown kind: render generically, never drop silently
            fallback = item.get("fallbackText")
            if isinstance(fallback, str) and fallback:
                self.emit("activity-updated", {
                    "category": "tool-progress", "tool": kind,
                    "message": fallback[:500]})

    def _on_agent_item(self, item: dict, *, kind: str, status: str, terminal: bool) -> None:
        item_id = str(item.get("itemId") or "")
        if terminal or status not in ("", "inProgress"):
            mapped = {"completed": "complete", "failed": "error",
                      "cancelled": "stopped", "rejected": "error",
                      "timedOut": "error"}.get(status, status or "complete")
        elif item_id in self._agents:
            mapped = "working"  # an update after first sight is observed output
        else:
            mapped = "starting"  # delegation proven; nothing observed yet
        message = (item.get("failureReason") or item.get("objective")
                   or item.get("message") or item.get("fallbackText") or "")
        self._agents[item_id] = {
            "status": mapped,
            "name": str(item.get("role") or item.get("agentPath") or kind),
            "role": str(item.get("role") or kind),
            "message": str(message)[:500],
            "tool": kind,
            "task_id": str(item.get("subagentId") or item.get("workflowRunId") or item_id),
        }
        children = item.get("children")
        if kind == "workflow" and isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_id = f"{item_id}:{child.get('childId')}:{child.get('attempt')}"
                child_status = str(child.get("terminal") or child.get("status") or "")
                if child.get("failureKind") and child_status == "completed":
                    child_status = "failed"
                self._agents[child_id] = {
                    "status": {"completed": "complete", "failed": "error",
                               "cancelled": "stopped"}.get(child_status, child_status or "working"),
                    "name": str(child.get("label") or child.get("childId") or "workflow child"),
                    "role": str(child.get("phase") or "workflow"),
                    "message": str(child.get("failureReason") or "")[:500],
                    "tool": "workflow",
                    "task_id": child_id,
                }
        self.emit("agents-updated", copy.deepcopy(self._agents))

    def _on_delta(self, params: dict) -> None:
        item_id = str(params.get("itemId") or "")
        delta = params.get("delta")
        if not item_id or not isinstance(delta, str) or self._streaming is None:
            return
        field = str(params.get("field") or "text")
        block = self._blocks.get(item_id)
        if block is None:
            block = self._block_for(item_id, "reasoning_summary" if field.startswith("summary") else "text")
            if block is None:
                return
        if field in ("text",) or field.startswith("summary"):
            block.text += delta
        elif field in ("args", "input", "tool_use_input_json"):
            block.tool_use_input_json += delta
        elif field in ("output", "visibleOutput"):
            key = block.tool_use_id or item_id
            self._tool_outputs[key] = self._tool_outputs.get(key, "") + delta
            return
        else:
            return
        self.emit("assistant-streaming", self._streaming)

    # ── approvals + user input (existing question dialog) ───────────────────

    def _on_approval_requested(self, params: dict) -> None:
        approval_id = str(params.get("approvalId") or "")
        if not approval_id or approval_id in self._pending_approvals:
            return
        self._pending_approvals[approval_id] = params
        choices = [c for c in (params.get("availableChoices") or []) if isinstance(c, dict)]
        options = [{"label": str(c.get("label") or c.get("choiceId") or ""),
                    "description": str(c.get("rulePreview") or "")} for c in choices]
        tool_name = str(params.get("toolName") or "tool")
        payload = {"questions": [{
            "question": f"Allow Muse to run {tool_name}?",
            "header": "Approval", "options": options, "multiSelect": False}]}
        self.emit("question-asked", payload, approval_id)

    def _on_user_input_requested(self, params: dict) -> None:
        user_input_id = str(params.get("userInputId") or "")
        if not user_input_id or user_input_id in self._pending_inputs:
            return
        self._pending_inputs[user_input_id] = params
        questions = []
        for question in params.get("questions") or []:
            if not isinstance(question, dict):
                continue
            selection = question.get("selection") if isinstance(question.get("selection"), dict) else {}
            questions.append({
                "question": str(question.get("question") or ""),
                "header": str(question.get("header") or ""),
                "options": [{"label": str(o.get("label") or ""),
                             "description": str(o.get("description") or "")}
                            for o in (question.get("options") or []) if isinstance(o, dict)],
                "multiSelect": selection.get("mode") == "multiple",
            })
        self.emit("question-asked", {"questions": questions}, user_input_id)

    def answer_question(self, token: str, answer: Any) -> None:
        """Route a dialog answer back over MSP. Anything unrecognized
        denies/cancels — a refusal is the default outcome, never a hang."""
        approval = self._pending_approvals.pop(token, None)
        if approval is not None:
            label = answer.strip() if isinstance(answer, str) else ""
            chosen = None
            for choice in approval.get("availableChoices") or []:
                if isinstance(choice, dict) and str(choice.get("label") or "") == label:
                    chosen = choice
                    break
            if chosen is None:
                for choice in approval.get("availableChoices") or []:
                    if isinstance(choice, dict) and str(choice.get("decision") or "") in (
                            "denied", "abort", "deniedPolicyAmendment"):
                        chosen = choice
                        break
            if chosen is not None:
                self._send("approval/decide", {
                    "commandId": uuid7(), "sessionId": self._session_id,
                    "approvalId": token, "choiceId": str(chosen.get("choiceId") or ""),
                    "requirementId": approval.get("currentRequirementId")})
            return
        prompt = self._pending_inputs.pop(token, None)
        if prompt is None:
            return
        answers = _translate_answers(prompt, answer)
        if answers is None:
            self._send("userInput/cancel", {
                "commandId": uuid7(), "sessionId": self._session_id,
                "userInputId": token, "reason": "Dismissed in Helios."})
        else:
            self._send("userInput/answer", {
                "commandId": uuid7(), "sessionId": self._session_id,
                "userInputId": token, "answers": answers})

    # ── turn terminal ───────────────────────────────────────────────────────

    def _finish_turn(self, terminal: str, *, error_text: str = "") -> None:
        if not self.execution_attempt_id:
            return
        if not self._accept_prompt(self._accepted_turn_id or self._root_turn_id):
            return
        subtype = {"completed": "success", "cancelled": "aborted"}.get(terminal, "error")
        if self._streaming is not None:
            turn = self._streaming.to_turn()
            for tool_use in turn.tool_uses:
                output = self._tool_outputs.get(tool_use.id)
                if output:
                    turn.tool_results.append(ToolResult(
                        tool_use_id=tool_use.id, content=output,
                        is_error=subtype == "error"))
            if turn.content or turn.tool_uses:
                if not self._record_execution_contribution(turn):
                    self._hold_accounting_failure(
                        "Helios could not save the Muse response. This Work remains blocked.")
                    return
                try:
                    self._transcript.append_assistant(turn, model=self._model)
                except Exception:
                    self._hold_accounting_failure(
                        "Helios could not save the Muse transcript. This Work remains blocked.")
                    return
                self.emit("turn-appended", turn)
        usage = self._turn_usage
        receipt_usage = {}
        for source, target in (("inputTokens", "input_tokens"),
                               ("outputTokens", "output_tokens"),
                               ("reasoningTokens", "reasoning_output_tokens"),
                               ("cacheReadTokens", "cache_read_input_tokens"),
                               ("cachedTokens", "cache_read_input_tokens")):
            value = usage.get(source)
            if isinstance(value, int) and value >= 0 and target not in receipt_usage:
                receipt_usage[target] = value
        receipt_status = {"success": "completed", "aborted": "aborted",
                          "error": "failed"}[subtype]
        if not self._finish_execution_with_evidence(ExecutionTerminalEvidence(
                evidence_type="provider_terminal", status=receipt_status,
                reason_code=f"provider_result.{subtype}",
                provider_status=terminal or subtype,
                request_id=self.execution_attempt_id,
                turn_id=self._accepted_turn_id, native_id=self._session_id,
                usage=receipt_usage, queue_disposition="released")):
            self._hold_accounting_failure(
                "Helios could not finish the Muse execution receipt. This Work remains blocked.")
            return
        self._is_busy = False
        self._streaming = None
        self._blocks = {}
        self._tool_outputs = {}
        self._last_activity = time.monotonic()
        result = {
            "subtype": subtype,
            "usage": receipt_usage,
            "turn_usage": receipt_usage,
        }
        if error_text:
            result["error"] = error_text
            self.emit("error", error_text[:2000])
        self.emit("turn-status-updated",
                  {"turnId": self._root_turn_id, "status": terminal or subtype})
        self.emit("result", result)
        if subtype == "success" and not self._stopped:
            self._flush_user_queue()

    # ── stop / exit / accounting ────────────────────────────────────────────

    def _finish_predispatch_attempt(self) -> bool:
        if self._startup_pending_text is None:
            return True
        self._startup_pending_text = None
        self._is_busy = False
        return self._finish_execution_with_evidence(
            ExecutionTerminalEvidence(
                evidence_type="local_abort", status="aborted",
                reason_code="local_abort", queue_disposition="restored"))

    def _hold_accounting_failure(self, message: str) -> None:
        self._accounting_failed = True
        self._accepting_input = False
        self._finish_predispatch_attempt()
        self.emit("error", message)

    def _protocol_failure(self, message: str) -> None:
        self._finish_predispatch_attempt()
        self.emit("error", message)
        self.stop()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw_line in proc.stdout:
            try:
                message = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            GLib.idle_add(self._dispatch_message, message)
        code = proc.wait()
        GLib.idle_add(self._on_exit, code)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            detail, _ = scrub_sensitive(line.decode("utf-8", errors="replace"))
            self._stderr = (self._stderr + detail)[-4000:]

    def stop(self, *, interrupt: bool = True) -> None:
        self._stopped = True
        self._accepting_input = False
        self._finish_predispatch_attempt()
        if interrupt and self._session_id and self._root_turn_id and self._is_busy:
            self._send("turn/interrupt", {
                "commandId": uuid7(), "sessionId": self._session_id,
                "turnId": self._root_turn_id})
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except OSError:
                pass
            threading.Thread(target=self._reap_stopped, args=(self._proc,), daemon=True).start()

    @staticmethod
    def _reap_stopped(proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def stop_task(self, actor_id: str) -> bool:
        """Stop one observed subagent/workflow actor, when Muse exposes it."""
        agent = self._agents.get(actor_id)
        if agent is None or not self._session_id:
            return False
        if agent.get("tool") == "workflow":
            return self._send("workflow/cancel", {
                "commandId": uuid7(), "sessionId": self._session_id,
                "workflowRunId": agent.get("task_id") or actor_id})
        return self._send("subagent/stop", {
            "commandId": uuid7(), "sessionId": self._session_id,
            "subagentId": agent.get("task_id") or actor_id})

    def end_input(self) -> None:
        self._accepting_input = False
        self._finish_predispatch_attempt()
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def _on_exit(self, code: int) -> bool:
        self._accepting_input = False
        self._finish_predispatch_attempt()
        if self.execution_attempt_id:
            self.emit("error", "Muse exited without a confirmed terminal receipt. This Work remains blocked; its input will not be replayed.")
        else:
            self._is_busy = False
        if code and self._stderr and not self._stopped:
            self.emit("error", self._stderr.strip())
        self.emit("exited", code)
        return False


def _translate_answers(prompt: dict, answer: Any) -> list[dict] | None:
    """Map the Helios dialog result onto MSP `UserInputAnswer` rows.

    Accepts the dialog's `{"0": {"answers": [...]}}` map or a plain label
    string (single-question prompts). Anything else returns None so the
    caller cancels instead of guessing.
    """
    questions = [q for q in (prompt.get("questions") or []) if isinstance(q, dict)]
    if not questions:
        return None
    picked: dict[str, list[str]] = {}
    if isinstance(answer, str) and answer.strip() and len(questions) == 1:
        picked[str(questions[0].get("id") or "")] = [answer.strip()]
    elif isinstance(answer, dict):
        for index, question in enumerate(questions):
            entry = answer.get(str(index))
            labels: list[str] = []
            if isinstance(entry, dict):
                raw = entry.get("answers")
                if isinstance(raw, list):
                    labels = [str(v) for v in raw if str(v).strip()]
            elif isinstance(entry, str) and entry.strip():
                labels = [entry.strip()]
            if labels:
                picked[str(question.get("id") or "")] = labels
    else:
        return None
    rows: list[dict] = []
    for question in questions:
        question_id = str(question.get("id") or "")
        labels = picked.get(question_id)
        if not question_id or not labels:
            return None
        selection = question.get("selection") if isinstance(question.get("selection"), dict) else {}
        if selection.get("mode") == "multiple":
            rows.append({"questionId": question_id, "selectedLabels": labels})
        else:
            rows.append({"questionId": question_id, "selectedLabel": labels[0]})
    return rows
