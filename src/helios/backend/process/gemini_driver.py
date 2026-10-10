"""Google subscription sessions through Antigravity's native NDJSON protocol.

Gemini CLI has a different protocol and is deliberately not a fallback. Native
conversation identities, Work admission and terminal receipts govern every turn.
Headless Antigravity cannot relay approval prompts; escalation is refused.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from typing import Any

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

from helios.backend import model_catalog
from helios.backend.google_env import (
    GoogleBinaryNotFound, find_google_binary, google_subscription_env,
)
from helios.backend.process.codex_transcript import CodexTranscriptWriter
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence, ExecutionDispatchEvidence,
    ExecutionTerminalEvidence, MessageDelivery, RequiredPromptContextError,
    UserMessageQueueMixin,
)
from helios.backend.process.streaming import Block, StreamingAssistant
from helios.backend.project_perms import effective_execution_mode, is_home_cwd
from helios.backend.transcript import Turn
from helios.log import get_logger
from helios.backend.sensitive_text import scrub_sensitive

_log = get_logger("gemini-driver")


class GeminiDriverSpawnError(RuntimeError):
    """Native subscription session could not be started."""


class GeminiCliDriver(UserMessageQueueMixin, GObject.Object):
    display_name = "gemini"
    provider = "google"

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
        self._effort = effort
        m = re.search(r"-(low|medium|high|xhigh|max)\Z", self._model)
        if m:
            effort_suffix = m.group(1)
            self._model = self._model[:m.start()]
            if not self._effort:
                self._effort = effort_suffix
        self._permission_mode = effective_execution_mode(permission_mode, cwd, provider=self.provider)
        self._resume_session_id = resume_session_id or ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._session_id = ""
        self._is_busy = False
        self._accepting_input = False
        self._stopped = False
        self._last_active = time.monotonic()
        self._streaming: StreamingAssistant | None = None
        self._pending_text = ""
        self._prepared_prompt = None
        self._accepted = False
        self._accounting_failed = False
        self._stderr = ""
        self._cumulative_usage: dict[str, int] = {}
        self._completed_turns = 0
        self._seen_result_turns = 0
        self._last_step_index = -1
        self._dispatch_step_floor = -1
        self._startup_pending_text: str | None = None
        self._transcript = CodexTranscriptWriter(cwd, provider=model_catalog.PROVIDER_GOOGLE)
        self.init_tools: list[str] = []
        self.init_mcp_servers: dict[str, Any] = {}

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def is_busy(self) -> bool:
        return self._is_busy

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
    def live_session_id(self) -> str:
        return self._session_id

    @property
    def last_active(self) -> float:
        return self._last_active

    @property
    def effort_key(self) -> str:
        return self._effort

    def start(self) -> None:
        if not self._model or not self._model.startswith("gemini-"):
            raise GeminiDriverSpawnError("Choose a discovered Google model before starting a session.")
        if is_home_cwd(self._cwd):
            raise GeminiDriverSpawnError("Google sessions cannot start in your home folder. Choose a project directory.")
        try:
            binary = find_google_binary()
            env = google_subscription_env()
        except (GoogleBinaryNotFound, ValueError) as exc:
            raise GeminiDriverSpawnError(str(exc)) from exc
        effort = self._effort or "high"
        cmd = [
            str(binary.path),
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--model", self._model,
            "--effort", effort,
        ]
        if self._resume_session_id:
            cmd.extend(["--conversation", self._resume_session_id])
        if self._permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        elif self._permission_mode == "plan":
            cmd.extend(["--mode", "plan", "--sandbox"])
        elif self._permission_mode == "acceptEdits":
            cmd.extend(["--mode", "accept-edits", "--sandbox"])
        else:
            cmd.append("--sandbox")
        try:
            self._proc = subprocess.Popen(cmd, cwd=self._cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                bufsize=0, start_new_session=True)
        except OSError as exc:
            raise GeminiDriverSpawnError("Could not start the Antigravity CLI.") from exc
        self._stopped = False
        self._accepting_input = False
        threading.Thread(target=self._read_stderr, daemon=True, name="google-stderr").start()
        threading.Thread(target=self._read_stdout, daemon=True, name="google-stdout").start()

    def send_user_text(
        self,
        text: str,
        *,
        _reuse_execution_attempt: bool = False,
    ) -> MessageDelivery:
        if not self.is_running or not self._proc or not self._proc.stdin or self._stopped:
            self.emit("error", "Google process is not running. Sign in with `agy` in a terminal.")
            return MessageDelivery("rejected")
        if self._accounting_failed:
            self.emit("error", "Helios could not verify Google accounting. New Work remains blocked.")
            return MessageDelivery("rejected")
        if self.is_busy and self._startup_pending_text is None:
            self.emit("error", "Google is still processing the current turn.")
            return MessageDelivery("rejected")
        if not self._session_id:
            if self._startup_pending_text is not None:
                self.emit("error", "Google is still preparing this session.")
                return MessageDelivery("rejected")
            reason = self._execution_block_reason()
            if reason:
                self.emit("error", reason)
                return MessageDelivery("rejected")
            try:
                # Recheck routing before each turn, including queued turns.
                google_subscription_env()
                self._prepare_prompt_context(text)
            except RequiredPromptContextError as exc:
                self.emit("error", exc.user_message)
                return MessageDelivery("rejected")
            except ValueError as exc:
                self.emit("error", str(exc))
                return MessageDelivery("rejected")
            admission_error = self._begin_execution_attempt(
                reuse_existing=_reuse_execution_attempt
            )
            if admission_error:
                self.emit("error", admission_error)
                return MessageDelivery("rejected")
            self._startup_pending_text = text
            self._is_busy = True
            self._last_active = time.monotonic()
            return MessageDelivery("pending")
        reason = self._execution_block_reason()
        if reason:
            self.emit("error", reason)
            return MessageDelivery("rejected")
        try:
            # Recheck routing before each turn, including queued turns.
            google_subscription_env()
            prepared = self._prepare_prompt_context(text)
        except RequiredPromptContextError as exc:
            self.emit("error", exc.user_message)
            return MessageDelivery("rejected")
        except ValueError as exc:
            self.emit("error", str(exc))
            return MessageDelivery("rejected")
        reason = self._begin_execution_attempt(
            reuse_existing=_reuse_execution_attempt
        )
        if reason:
            self.emit("error", reason)
            return MessageDelivery("rejected")
        attempt_id = self.execution_attempt_id
        if not self._record_execution_dispatch(ExecutionDispatchEvidence(
                wire_prompt_text=prepared.text, provider_request_key=attempt_id,
                native_binding_id=self._confirmed_execution_native_id())):
            released = self._finish_execution_with_evidence(ExecutionTerminalEvidence(
                evidence_type="local_abort", status="aborted", reason_code="local_abort", queue_disposition="restored"))
            if released:
                self.emit("error", "Helios could not persist the Google dispatch. The message was not sent.")
            else:
                self._hold_accounting_failure("Helios could not close the unsent Google reservation. This Work remains blocked.")
            return MessageDelivery("rejected")
        self._is_busy = True
        self._accepted = False
        self._pending_text = text
        self._prepared_prompt = prepared
        self._dispatch_step_floor = self._last_step_index
        self._streaming = StreamingAssistant(model=self._model)
        line = (json.dumps({"event": "user", "message": {"content": prepared.text}}) + "\n").encode()
        try:
            # Unbuffered pipes may short-write; every byte must be confirmed.
            offset = 0
            while offset < len(line):
                written = self._proc.stdin.write(line[offset:])
                if not isinstance(written, int) or written <= 0:
                    raise OSError("incomplete input write")
                offset += written
            self._proc.stdin.flush()
        except (OSError, ValueError):
            self.end_input()
            self.emit("error", "Google delivery is uncertain. This Work remains blocked until provider-backed recovery confirms the outcome.")
            return MessageDelivery("uncertain")
        self._last_active = time.monotonic()
        # A pipe write is a local handoff. Native user_input/result events
        # provide acceptance; no synthetic conversation id is emitted.
        return MessageDelivery("accepted")

    def _accept_prompt(self) -> bool:
        if self._accounting_failed:
            return False
        if self._accepted or not self.execution_attempt_id:
            return self._accepted
        if not self._session_id or not self._record_execution_acceptance(ExecutionAcceptanceEvidence(
                provider_request_key=self.execution_attempt_id, native_binding_id=self._session_id)):
            self._hold_accounting_failure("Helios could not record Google acceptance. This Work remains blocked.")
            return False
        try:
            self._transcript.note_user_text(self._pending_text)
        except Exception:
            self._hold_accounting_failure("Helios could not save the accepted Google prompt. This Work remains blocked.")
            return False
        self._accepted = True
        if self._prepared_prompt is not None:
            self._prepared_prompt.mark_sent()
            self._prepared_prompt = None
        self._pending_text = ""
        self._accept_uncertain_queue_delivery()
        self.emit("delivery-confirmed")
        return True

    def _dispatch_message(self, msg: dict[str, Any]) -> bool:
        if not isinstance(msg, dict):
            return False
        event = msg.get("event")
        if event == "init":
            if self._stopped or self._accounting_failed:
                return False
            info = msg.get("init")
            sid = msg.get("conversation_id")
            if not isinstance(info, dict) or not isinstance(sid, str) or not sid or "/" in sid or "\\" in sid:
                self._protocol_failure("Google did not report a valid native conversation identity.")
                return False
            if self._session_id:
                if sid != self._session_id:
                    self._protocol_failure("Google changed its native conversation identity. The Work remains blocked.")
                return False
            if self._resume_session_id and sid != self._resume_session_id:
                self._protocol_failure("Google resumed a different conversation. The requested Work remains blocked.")
                return False
            expected_perm = "always-proceed" if self._permission_mode == "bypassPermissions" else "request-review"
            if (
                info.get("permission_mode") != expected_perm
                or info.get("model") != self._model
                or os.path.realpath(str(info.get("cwd", ""))) != os.path.realpath(self._cwd)
            ):
                self._protocol_failure("Google reported an unexpected model, project, or approval policy. The session was stopped.")
                return False
            self.init_tools = list(info.get("tools") or [])
            try:
                self._transcript.bind_thread(sid)
            except Exception:
                self._hold_accounting_failure("Helios could not save the Google session identity. The session remains blocked.")
                return False
            self._session_id = sid
            self._accepting_input = True
            pending, self._startup_pending_text = self._startup_pending_text, None
            self._is_busy = False
            self.emit("session-started", sid, self._cwd, self._model)
            if pending:
                self.send_user_text(pending, _reuse_execution_attempt=True)
        elif event == "step_update":
            step = msg.get("step_update")
            if not isinstance(step, dict) or not self._is_busy or self._stopped:
                return False
            if step.get("conversation_id") != self._session_id:
                self._protocol_failure("Google returned activity for a different conversation.")
                return False
            index = step.get("step_index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index < self._last_step_index:
                return False
            kind = step.get("step_type")
            if kind == "user_input" and (
                    index <= self._dispatch_step_floor or step.get("state") != "DONE"):
                # Receipt evidence must belong to this dispatch. Equal-index
                # response deltas remain valid within their own active step.
                return False
            self._last_step_index = index
            if kind == "user_input":
                self._accept_prompt()
            elif kind == "agent_response":
                delta = step.get("text_delta")
                if isinstance(delta, str) and delta and self._streaming is not None:
                    if not self._streaming.blocks:
                        self._streaming.blocks.append(Block(type="text"))
                    self._streaming.blocks[0].text += delta
                    self.emit("assistant-streaming", self._streaming)
            elif kind == "tool":
                self.emit("activity-updated", {"tool": str(step.get("tool_name") or ""), "state": str(step.get("state") or "")})
        elif event == "result":
            result = msg.get("result")
            if not isinstance(result, dict) or not self.execution_attempt_id:
                return False
            if result.get("conversation_id") != self._session_id:
                self._protocol_failure("Google returned a result for a different conversation.")
                return False
            status = str(result.get("status") or "")
            count = result.get("num_turns")
            if not isinstance(count, int) or isinstance(count, bool) or count <= self._seen_result_turns:
                # A delayed prior result cannot complete the next queued turn.
                return False
            if self._completed_turns and count != self._completed_turns + 1:
                self._protocol_failure("Google returned an uncorrelated turn result. This Work remains blocked.")
                return False
            if status not in {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID"}:
                self._protocol_failure("Google did not confirm a terminal result. This Work remains blocked.")
                return False
            # Latch provider evidence before local persistence. A duplicate
            # receipt must not append the same contribution again if saving
            # or releasing the first receipt failed. The durable lane stays
            # held until its accounting is repaired separately.
            self._seen_result_turns = count
            if not self._accept_prompt():
                return False
            text = result.get("response")
            if isinstance(text, str) and text:
                turn = Turn(role="assistant")
                turn.add("text", text)
                if not self._record_execution_contribution(turn):
                    self._hold_accounting_failure("Helios could not save the Google response. This Work remains blocked.")
                    return False
                try:
                    self._transcript.append_assistant(turn, model=self._model)
                except Exception:
                    self._hold_accounting_failure("Helios could not save the Google transcript. This Work remains blocked.")
                    return False
                self.emit("turn-appended", turn)
            turn_usage = {}
            usage = result.get("usage")
            if isinstance(usage, dict):
                for key, value in usage.items():
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        if not self._resume_session_id or self._completed_turns:
                            turn_usage[key] = max(0, value - self._cumulative_usage.get(key, 0))
                        self._cumulative_usage[key] = value
            normalized = {**result, "turn_usage": turn_usage, "subtype": "success" if status == "SUCCESS" else "aborted" if status in {"CANCELED", "INTERRUPTED"} else "error"}
            if not self._finish_execution_from_result(normalized):
                self._hold_accounting_failure("Helios could not finish the Google execution receipt. This Work remains blocked.")
                return False
            self._is_busy = False
            self._completed_turns = count
            self._streaming = None
            self._last_active = time.monotonic()
            if status != "SUCCESS":
                detail, _ = scrub_sensitive(str(result.get("error") or status))
                self.emit("error", detail[:2000])
            self.emit("result", normalized)
            if status == "SUCCESS" and not self._stopped:
                self._flush_user_queue()
        return False

    def _finish_predispatch_attempt(self) -> bool:
        if self._startup_pending_text is None:
            return True
        self._startup_pending_text = None
        self._is_busy = False
        return self._finish_execution_with_evidence(
            ExecutionTerminalEvidence(
                evidence_type="local_abort",
                status="aborted",
                reason_code="local_abort",
                queue_disposition="restored",
            )
        )

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
        if self._proc is not None and self._proc.poll() is None:
            try:
                # Stop the whole local process group; do not infer remote
                # cancellation from a signal or a local process exit.
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

    def end_input(self) -> None:
        self._accepting_input = False
        self._finish_predispatch_attempt()
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def answer_question(self, token: str, answers: Any) -> None:
        self.emit("error", "Antigravity headless cannot accept interactive approvals. Configure scoped permissions in the CLI or use an interactive session.")

    def _on_exit(self, code: int) -> bool:
        self._accepting_input = False
        self._finish_predispatch_attempt()
        if self.execution_attempt_id:
            self.emit("error", "Google exited without a confirmed terminal receipt. This Work remains blocked; its input will not be replayed.")
        else:
            self._is_busy = False
        if code and self._stderr:
            self.emit("error", self._stderr.strip())
        self.emit("exited", code)
        return False
