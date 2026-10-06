"""Gemini / Google Antigravity CLI process driver for Helios.

Owns one running `agy` (or `gemini`) subprocess executing in streaming JSON mode,
translating stdin/stdout frames to GObject signals on the GLib main loop.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any
from collections.abc import Callable

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject

from helios.backend.google_env import GoogleBinaryNotFound, find_google_binary
from helios.backend.process.env_scrub import scrubbed_child_env
from helios.backend.process.message_queue import (
    PreparedPrompt,
    RequiredPromptContextError,
    UserMessageQueueMixin,
)
from helios.backend.transcript import Turn
from helios.log import get_logger

_log = get_logger("gemini-driver")


class GeminiDriverSpawnError(RuntimeError):
    """Subprocess could not be spawned."""


class GeminiCliDriver(UserMessageQueueMixin, GObject.Object):
    """Owns one running Google Antigravity / Gemini CLI session."""

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
    }

    def __init__(
        self,
        cwd: str,
        model: str = "",
        permission_mode: str = "default",
        resume_session_id: str | None = None,
        effort: str = "",
        **kwargs: Any,
    ) -> None:
        GObject.Object.__init__(self)
        self._init_user_queue()
        self._cwd = cwd
        self._model = model or "gemini-2.5-pro"
        self._permission_mode = permission_mode
        self._resume_session_id = resume_session_id
        self._effort = effort

        self._proc: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._session_id = resume_session_id or ""
        self._is_busy = False
        self._accepting_input = False
        self._stopped = False
        self._last_active = time.monotonic()

        self._prompt_context_provider: Callable[..., Any] | None = None
        self._execution_guard: Callable[[Any], bool] | None = None
        self._attempt_start: Callable[[Any], Any] | None = None
        self._attempt_finish: Callable[[Any, str, str, str], None] | None = None
        self._attempt_dispatch: Callable[[Any, str, dict], None] | None = None
        self._attempt_accept: Callable[[Any, str, dict], None] | None = None
        self._attempt_stop: Callable[[Any, str, dict], None] | None = None
        self._finish_with_evidence: Callable[[Any, str, dict], None] | None = None
        self._record_contribution: Callable[[Any, Any], None] | None = None

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
        return self._accepting_input and not self._is_busy

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def live_session_id(self) -> str:
        return self._session_id

    @property
    def last_active(self) -> float:
        return self._last_active

    def set_prompt_context_provider(self, provider: Callable[..., Any]) -> None:
        self._prompt_context_provider = provider

    def set_execution_guard(self, guard: Callable[[Any], bool]) -> None:
        self._execution_guard = guard

    def set_execution_attempt_controller(
        self,
        start_attempt: Callable[[Any], Any],
        finish_attempt: Callable[[Any, str, str, str], None],
        *,
        record_dispatch: Callable[[Any, str, dict], None] | None = None,
        record_acceptance: Callable[[Any, str, dict], None] | None = None,
        record_stop: Callable[[Any, str, dict], None] | None = None,
        finish_with_evidence: Callable[[Any, str, dict], None] | None = None,
        record_contribution: Callable[[Any, Any], None] | None = None,
    ) -> None:
        self._attempt_start = start_attempt
        self._attempt_finish = finish_attempt
        self._attempt_dispatch = record_dispatch
        self._attempt_accept = record_acceptance
        self._attempt_stop = record_stop
        self._finish_with_evidence = finish_with_evidence
        self._record_contribution = record_contribution

    def start(self) -> None:
        """Spawn the agy subprocess or start the communication stream."""
        try:
            binary_info = find_google_binary()
        except GoogleBinaryNotFound as exc:
            raise GeminiDriverSpawnError(str(exc)) from exc

        cmd = [
            str(binary_info.path),
            "--print",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--model",
            self._model,
        ]
        if self._resume_session_id:
            cmd.extend(["--resume", self._resume_session_id])

        env = scrubbed_child_env()
        # Ensure PATH includes standard locations for tools
        env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=self._cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                bufsize=0,
            )
        except OSError as exc:
            raise GeminiDriverSpawnError(f"Failed to spawn {cmd[0]}: {exc}") from exc

        self._accepting_input = True
        self._stopped = False
        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            daemon=True,
            name=f"gemini-reader-{id(self)}",
        )
        self._reader_thread.start()

        # Emit simulated session-started if resuming or generated
        sid = self._session_id or f"gemini-{int(time.time())}"
        self._session_id = sid
        GLib.idle_add(self._emit_session_started, sid, self._cwd, self._model)

    def _emit_session_started(self, sid: str, cwd: str, model: str) -> bool:
        self.emit("session-started", sid, cwd, model)
        return False

    def send_user_text(self, text: str) -> bool:
        """Send user input to the driver."""
        if not self._proc or not self._proc.stdin or self._stopped:
            self.emit("error", "Gemini process is not running.")
            return False

        if self._prompt_context_provider:
            try:
                _context = self._prompt_context_provider(self)
            except Exception as exc:
                self.emit("error", f"Context resolution error: {exc}")
                return False

        self._is_busy = True
        self._last_active = time.monotonic()

        turn_payload = {
            "type": "user",
            "text": text,
            "timestamp": int(time.time()),
        }
        raw_line = json.dumps(turn_payload) + "\n"

        try:
            self._proc.stdin.write(raw_line.encode("utf-8"))
            self._proc.stdin.flush()
        except OSError as exc:
            self._is_busy = False
            self.emit("error", f"Write failed: {exc}")
            return False

        # Emit turn-appended for user turn
        turn = UserTurn(text=text, timestamp=time.time())
        self.emit("turn-appended", turn)
        self.emit("delivery-confirmed")
        return True

    def stop(self) -> None:
        """Interrupt current turn or terminate process."""
        self._stopped = True
        self._is_busy = False
        if self._proc:
            try:
                self._proc.terminate()
            except OSError:
                pass

    def end_input(self) -> None:
        """Close stdin gracefully."""
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.close()
            except OSError:
                pass

    def answer_question(self, token: str, answers: Any) -> None:
        """Reply to an interactive prompt or tool question."""
        if not self._proc or not self._proc.stdin or self._stopped:
            return
        payload = {
            "type": "question_response",
            "token": token,
            "response": answers,
        }
        try:
            line = json.dumps(payload) + "\n"
            self._proc.stdin.write(line.encode("utf-8"))
            self._proc.stdin.flush()
        except OSError as exc:
            _log.warning("could not write question response: %s", exc)

    def _read_stdout(self) -> None:
        """Read and parse stream-json frames from the child process."""
        proc = self._proc
        if not proc or not proc.stdout:
            return

        for raw_line in proc.stdout:
            if self._stopped:
                break
            line_str = raw_line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            self._dispatch_message(msg)

        exit_code = proc.wait() if proc else 0
        GLib.idle_add(self._on_exit, exit_code)

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "delta":
            text = msg.get("text", "")
            if text:
                GLib.idle_add(self.emit, "assistant-streaming", text)
        elif mtype == "result":
            self._is_busy = False
            self._last_active = time.monotonic()
            GLib.idle_add(self.emit, "result", msg)
            GLib.idle_add(self._flush_user_queue)
        elif mtype == "turn":
            text = msg.get("text", "")
            turn = Turn(role="assistant")
            if text:
                turn.add("text", text)
            GLib.idle_add(self.emit, "turn-appended", turn)
        elif mtype == "usage":
            used = int(msg.get("tokens_used", 0))
            window = int(msg.get("context_window", 2_000_000))
            GLib.idle_add(self.emit, "usage-updated", used, window)
        elif mtype == "question":
            token = str(msg.get("token") or msg.get("tool_use_id") or "")
            qdata = msg.get("question") or msg
            GLib.idle_add(self.emit, "question-asked", qdata, token)

    def _on_exit(self, code: int) -> bool:
        self._is_busy = False
        self._accepting_input = False
        self.emit("exited", code)
        return False
