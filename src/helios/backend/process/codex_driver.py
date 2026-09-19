"""Drive the OpenAI Codex CLI (`codex exec --json`) behind the same signal
surface as ClaudeCliDriver, so MainWindow can treat both backends alike.

Lifecycle difference that shapes everything here: claude is one persistent
stream-json process per session; codex is one process per TURN, with
continuity via `codex exec resume <thread_id>`. So this driver spawns on
every send, holds the thread id between turns, and consumes ~zero resources
while idle. Consequences:

  * `start()` validates the binary + login but spawns nothing.
  * `exited` is NOT emitted when a per-turn process ends — that's normal.
    It's emitted when the driver is wound down (end_input / teardown stop),
    which is what the window's registry cleanup actually means by "exited".
  * A user Stop kills the in-flight process but KEEPS the driver (and its
    thread id) so the conversation can continue — parity with claude, where
    post-stop continuity comes from --resume of the on-disk session.

Codex turns are mirrored into Claude-style project JSONLs so GPT chats appear
in the sidebar and resume through the same Helios flow as Claude chats.
"""

from __future__ import annotations

import shutil
import signal
import time
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject  # noqa: E402

from helios.backend import codex_env, model_catalog
from helios.backend.process import codex_events
from helios.backend.process.env_scrub import CODEX_EXEC_ENV, scrub_helios_env
from helios.backend.process.cli_driver import (
    DriverSpawnError,
    _is_alive,
    _safe_kill,
    _signal_group,
)
from helios.backend.process.codex_transcript import CodexTranscriptWriter
from helios.backend.process.message_queue import (
    MessageDelivery,
    RequiredPromptContextError,
    UserMessageQueueMixin,
)
from helios.backend.project_perms import (
    PERMISSION_MODES,
    effective_execution_mode,
    execution_mode_restriction_reason,
)
from helios.log import get_logger

_log = get_logger("codex-driver")

# Bypass is deliberately absent. This compatibility transport remains testable
# for explicit callers, but CodexAppServerDriver never selects it as a fallback
# and no OpenAI execution path may become unsandboxed.
NONINTERACTIVE_PERMISSION_MODES = frozenset({"dontAsk", "plan"})
_ExecutionCallback = Callable[[bool, str], None]


class CodexCliDriver(UserMessageQueueMixin, GObject.Object):
    """One live OpenAI-backed chat session (process-per-turn)."""

    __gsignals__ = {
        "session-started": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
        "assistant-streaming": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "turn-appended": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "result": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "usage-updated": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        "rate-limit-updated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "question-asked": (GObject.SignalFlags.RUN_FIRST, None, (object, str)),
        "queued-user-sent": (GObject.SignalFlags.RUN_FIRST, None, (int, str)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "exited": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
    }

    display_name = "codex"
    provider = model_catalog.PROVIDER_OPENAI

    _STOP_TERM_AFTER_MS = 800
    _STOP_KILL_AFTER_MS = 1600
    _MAX_STREAM_READ_ERRORS = 20

    def __init__(
        self,
        *,
        cwd: str,
        model: str,
        # Safe-by-default: a caller that omits the mode gets "Ask", not full
        # bypass (SAFE_FALLBACK_MODE). MainWindow always passes an explicit
        # per-cwd mode; this default only guards stray/future constructions.
        permission_mode: str = "default",
        resume_session_id: str = "",
        max_thinking_tokens: int | None = None,  # accepted for ctor parity
        effort: str = "",
    ) -> None:
        super().__init__()
        self._init_user_queue()
        self._cwd = cwd
        self._model = model
        self._permission_mode = effective_execution_mode(permission_mode, cwd, provider="openai")
        self._effort = effort
        self._acc = codex_events.CodexTurnAccumulator(model=model)
        self._acc.thread_id = resume_session_id
        # A resume id is a request, not proof. The first thread.started event
        # must still cross MainWindow's exact-identity boundary before user
        # text is mirrored or presented as accepted.
        self._pending_identity_user_text: str | None = None

        # Mirror turns into a claude-format transcript so this GPT chat becomes
        # a first-class, resumable sidebar session (see codex_transcript).
        self._mirror = CodexTranscriptWriter(cwd, thread_id=resume_session_id)

        self._binary = ""
        self._proc: Gio.Subprocess | None = None
        self._own_group = False
        self._stdout: Gio.DataInputStream | None = None
        self._stderr: Gio.DataInputStream | None = None
        self._busy = False
        self._closed = False
        self._close_after_turn = False
        self._stop_requested = False
        self._pending_mirror_turn = None
        self._stdout_err_count = 0
        self._stderr_err_count = 0
        self.last_activity: float = time.monotonic()

        # Parity attributes the window reads off any driver.
        self._init_tools: list = []
        self._init_mcp_servers: list = []

        # Pin async callbacks (PyGObject weak-ref footgun — see cli_driver).
        self._cb_stdout = self._on_stdout_line
        self._cb_stderr = self._on_stderr_line
        self._cb_exit = self._on_exit

    # ── parity surface ───────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._acc.thread_id

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
    def is_running(self) -> bool:
        return not self._closed

    @property
    def is_busy(self) -> bool:
        return self._busy

    @property
    def is_accepting_input(self) -> bool:
        return not self._closed

    @property
    def permission_mode(self) -> str:
        return self._permission_mode

    @property
    def effort_key(self) -> str:
        return self._effort

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

    def _execution_change_ready(
        self,
        callback: _ExecutionCallback | None,
    ) -> bool:
        # No busy gate. For both Codex transports these are plain fields read
        # when the NEXT turn is built — `build_turn_start_params` re-emits the
        # sticky overrides every turn, and the exec fallback rebuilds argv per
        # turn. So accepting a change while a turn runs *is* "apply at the
        # earliest opportunity"; it cannot disturb the turn in flight.
        if self._closed or getattr(self, "_native_starting", False):
            self._notify_execution_callback(
                callback,
                False,
                "the Codex session is not accepting setting changes",
            )
            return False
        return True

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
            provider="openai",
        )
        if restriction:
            self._notify_execution_callback(
                callback,
                False,
                restriction,
            )
            return False
        # `codex exec` has no interactive approval channel.  Never accept a
        # toolbar state that this fallback transport cannot honour.
        if mode not in NONINTERACTIVE_PERMISSION_MODES:
            self._notify_execution_callback(
                callback,
                False,
                "codex exec cannot handle interactive approval modes",
            )
            return False
        if not self._execution_change_ready(callback):
            return False
        self._permission_mode = mode
        self._notify_execution_callback(callback, True)
        return True

    def set_effort(
        self,
        key: str,
        callback: _ExecutionCallback | None = None,
    ) -> bool:
        # Reasoning efforts are model-catalog data, so accept future non-empty
        # keys rather than freezing today's low/medium/high set in the driver.
        if not isinstance(key, str) or not key.strip():
            self._notify_execution_callback(callback, False, "unknown effort level")
            return False
        if not self._execution_change_ready(callback):
            return False
        self._effort = key
        self._notify_execution_callback(callback, True)
        return True

    def answer_question(self, tool_use_id: str, answer: str | None) -> None:
        """Codex has no AskUserQuestion channel — nothing to answer."""

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Validate the backend; the first real spawn happens on send.

        Raises DriverSpawnError with a user-actionable message when codex
        is missing or logged out, mirroring claude's binary-not-found path."""
        try:
            self._binary = str(codex_env.find_codex_binary().path)
        except codex_env.CodexBinaryNotFound as e:
            raise DriverSpawnError(str(e)) from e
        auth = codex_env.fetch_auth_status()
        if not auth.logged_in:
            raise DriverSpawnError(
                "Codex isn't signed in — add your OpenAI API key in "
                "Settings → Providers."
            )

    def send_user_text(self, text: str) -> MessageDelivery:
        if self._closed:
            self.emit("error", "Cannot send: codex session was closed")
            return MessageDelivery("rejected")
        if self._busy:
            self.emit("error", "Codex is still working — wait for the turn to finish.")
            return MessageDelivery("rejected")

        try:
            prepared = self._prepare_prompt_context(text)
        except RequiredPromptContextError as exc:
            self.emit("error", exc.user_message)
            return MessageDelivery("rejected")

        argv = codex_events.build_exec_argv(
            self._binary or "codex",
            model=self._model,
            permission_mode=self._permission_mode,
            resume_thread_id=self._acc.thread_id,
            effort=self._effort,
        )
        setsid_path = shutil.which("setsid")
        if setsid_path:
            argv = [setsid_path, *argv]
            self._own_group = True

        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDIN_PIPE
            | Gio.SubprocessFlags.STDOUT_PIPE
            | Gio.SubprocessFlags.STDERR_PIPE
        )
        launcher.set_cwd(self._cwd)
        # Keep Rust log noise out of the stderr surface we show users.
        launcher.setenv("RUST_LOG", "error", True)
        # This is the `codex exec` fallback path — the only Codex command that
        # honors an env key (CODEX_API_KEY, exec-only). Keep Helios-internal
        # vars, unrelated credentials, and Anthropic auth out; forward at most
        # CODEX_API_KEY.
        scrub_helios_env(launcher, keep=CODEX_EXEC_ENV)

        _log.debug("codex spawn argv=%s cwd=%s", argv, self._cwd)
        try:
            self._proc = launcher.spawnv(argv)
        except GLib.Error as e:
            self.emit("error", f"Failed to spawn codex: {e.message}")
            return MessageDelivery("rejected")

        # Prompt via stdin, then EOF — codex reads to end before starting.
        stdin = self._proc.get_stdin_pipe()
        try:
            prompt = prepared.text
            stdin.write_all(prompt.encode("utf-8"), None)
            stdin.close(None)
        except GLib.Error as e:
            self.emit("error", f"codex stdin write failed: {e.message}")
            self._reap_process()
            return MessageDelivery("rejected")
        prepared.mark_sent()

        # Persist only after codex accepted the prompt on stdin. On resumed
        # chats the transcript path is already known, so writing before spawn
        # success would create a false user turn if codex failed to start.
        if self._acc._announced:
            self._mirror.note_user_text(text)
        else:
            self._pending_identity_user_text = text
        self._stdout = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        self._stderr = Gio.DataInputStream.new(self._proc.get_stderr_pipe())
        self._read_next_stdout_line()
        self._read_next_stderr_line()
        # Busy BEFORE the accepted return: v0.85.0 inserted the return above
        # these three lines and made them unreachable, so the driver looked
        # idle for the whole of an active `codex exec` and a second send could
        # overwrite self._proc.
        self._busy = True
        self._stop_requested = False
        self.last_activity = time.monotonic()
        self._proc.wait_async(None, self._cb_exit, None)
        return MessageDelivery("accepted")

    def end_input(self) -> None:
        """Wind-down (idle-reap path). Idle codex drivers hold no process, so
        this just closes the registry handle; mid-turn it defers until the
        turn lands."""
        if self._closed:
            return
        if self._busy:
            self._close_after_turn = True
            return
        self._finalize(0)

    def stop(self, *, interrupt: bool = True) -> None:
        """interrupt=True → user Stop: abort the in-flight turn, keep the
        session usable. interrupt=False → teardown: kill and close."""
        if self._closed:
            return
        if not interrupt:
            self._close_after_turn = False
            if self._proc is None:
                self._finalize(0)
                return
            self._closed = True  # finalized in _on_exit (emits exited there)
            self._kill_staged(signal.SIGTERM)
            return
        if self._proc is None or not self._busy:
            return
        self._stop_requested = True
        self._kill_staged(signal.SIGINT)

    def _kill_staged(self, first_sig: signal.Signals) -> None:
        pid = self._pid()
        if pid is None:
            return
        own_group = self._own_group
        _signal_group(pid, first_sig, own_group)

        def _stage_term() -> bool:
            if _is_alive(pid):
                _signal_group(pid, signal.SIGTERM, own_group)
            return False

        def _stage_kill() -> bool:
            if _is_alive(pid):
                _signal_group(pid, signal.SIGKILL, own_group)
                _safe_kill(pid, signal.SIGKILL)
            return False

        GLib.timeout_add(self._STOP_TERM_AFTER_MS, _stage_term)
        GLib.timeout_add(self._STOP_KILL_AFTER_MS, _stage_kill)

    def _pid(self) -> int | None:
        if self._proc is None:
            return None
        try:
            ident = self._proc.get_identifier()
            return int(ident) if ident is not None else None
        except (GLib.Error, ValueError, TypeError):
            return None

    def _finalize(self, code: int) -> None:
        if self._closed and self._proc is None:
            return
        self._closed = True
        self._busy = False
        self._proc = None
        self.emit("exited", code)

    # ── async readers ────────────────────────────────────────────────────

    def _read_next_stdout_line(self) -> None:
        if self._stdout is None:
            return
        self._stdout.read_line_async(GLib.PRIORITY_DEFAULT, None, self._cb_stdout, None)

    def _on_stdout_line(self, src: Gio.DataInputStream, res, _user_data) -> None:
        if self._stdout is None:
            return
        try:
            line, _len = src.read_line_finish_utf8(res)
        except GLib.Error as e:
            self._stdout_err_count += 1
            if self._stdout_err_count > self._MAX_STREAM_READ_ERRORS:
                self.emit("error", f"codex stdout read failed: {e.message}")
                return
            _log.warning("codex stdout read error (skipping line): %s", e.message)
            self._read_next_stdout_line()
            return
        self._stdout_err_count = 0
        if line is None:
            return  # EOF — wait_async handles the rest
        if line.strip():
            self.last_activity = time.monotonic()
            for action in self._acc.feed_line(line):
                self._dispatch(action)
        self._read_next_stdout_line()

    def _read_next_stderr_line(self) -> None:
        if self._stderr is None:
            return
        self._stderr.read_line_async(GLib.PRIORITY_DEFAULT_IDLE, None, self._cb_stderr, None)

    def _on_stderr_line(self, src: Gio.DataInputStream, res, _user_data) -> None:
        if self._stderr is None:
            return
        try:
            line, _len = src.read_line_finish_utf8(res)
        except GLib.Error as e:
            self._stderr_err_count += 1
            if self._stderr_err_count > self._MAX_STREAM_READ_ERRORS:
                self.emit("error", f"codex stderr read failed: {e.message}")
                return
            _log.warning("codex stderr read error (skipping line): %s", e.message)
            self._read_next_stderr_line()
            return
        self._stderr_err_count = 0
        if line is None:
            return
        s = line.strip()
        # codex's stderr is tracing-formatted; ERROR lines that aren't the
        # connection retries (already surfaced via JSONL) are worth showing.
        if s and " ERROR " in s and "Reconnecting" not in s and "websocket" not in s:
            self.emit("error", f"codex: {s[-200:]}")
        self._read_next_stderr_line()

    def _dispatch(self, action) -> None:
        kind, payload = action.kind, action.payload
        if (
            kind != codex_events.ACT_SESSION_STARTED
            and getattr(self, "_helios_identity_rejected", False)
        ):
            return
        if kind == codex_events.ACT_SESSION_STARTED:
            self.emit("session-started", payload, self._cwd, self._model)
            if self._closed or getattr(self, "_helios_identity_rejected", False):
                self._pending_identity_user_text = None
                return
            self._mirror.bind_thread(payload)
            pending_user = self._pending_identity_user_text
            self._pending_identity_user_text = None
            if pending_user:
                self._mirror.note_user_text(pending_user)
            self._after_session_started(payload)
        elif kind == codex_events.ACT_STREAMING:
            self.emit("assistant-streaming", payload)
        elif kind == codex_events.ACT_TURN:
            self._pending_mirror_turn = payload
            self.emit("turn-appended", payload)
        elif kind == codex_events.ACT_RESULT:
            self._busy = False
            if self._pending_mirror_turn is not None:
                self._mirror.append_assistant(
                    self._pending_mirror_turn,
                    model=self._model,
                    result=payload,
                )
                self._pending_mirror_turn = None
            self.emit("result", payload)
            usage = payload.get("usage") or {}
            used = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
            )
            window = model_catalog.context_window_for(self._model)
            if window > 0 and used > 0:
                self.emit("usage-updated", used, window)
            # Real turn completion (the synthetic post-Stop "aborted" result
            # goes through _on_exit, not here) — send the next queued message.
            # The respawn is race-safe: _on_exit ignores the previous turn's
            # process once self._proc points at the new one.
            self._flush_user_queue()
        elif kind == codex_events.ACT_ERROR:
            if not self._stop_requested:
                self.emit("error", payload)

    def _after_session_started(self, _session_id: str) -> None:
        """Subclass hook after synchronous identity handlers accept a thread."""

    def _on_exit(self, proc: Gio.Subprocess, _res, _data) -> None:
        if proc.get_if_exited():
            code = proc.get_exit_status()
        elif proc.get_if_signaled():
            code = 128 + proc.get_term_sig()
        else:
            code = -1
        _log.debug("codex turn process exited code=%s", code)
        if proc is not self._proc:
            # A previous turn's process finishing after the next turn already
            # spawned (its result landed first; exec processes exit shortly
            # after). Touching driver state here would null the NEW turn's
            # readers and misreport a mid-turn death — verified live.
            return
        self._reap_process()
        self._pending_identity_user_text = None

        turn_was_aborted = self._stop_requested
        turn_died_silently = self._busy and not turn_was_aborted
        self._busy = False
        self._stop_requested = False

        if self._closed:
            # Teardown stop() already marked us closed; emit the final exited.
            self._closed = False  # let _finalize run once
            self._finalize(code)
            return

        if turn_was_aborted:
            # Synthetic result so the window clears its busy UI; the session
            # (thread id) stays usable for the next send.
            self.emit("result", {
                "type": "result", "subtype": "aborted", "provider": "openai",
                "total_cost_usd": 0.0, "duration_ms": 0, "usage": {},
                "modelUsage": {},
            })
        elif turn_died_silently:
            # Process ended mid-turn without turn.completed/failed JSONL.
            self.emit("error", f"codex exited mid-turn (code {code})")

        if self._close_after_turn:
            self._finalize(code if code else 0)

    def _reap_process(self) -> None:
        self._proc = None
        self._stdout = None
        self._stderr = None
