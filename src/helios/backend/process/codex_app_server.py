"""Thread-safe JSONL transport for a persistent ``codex app-server``.

Codex App Server speaks bidirectional JSON-RPC 2.0 over stdio, except the
``jsonrpc`` field is intentionally omitted.  This module owns only the wire
and process lifecycle; translating Codex thread/turn events into Helios UI
objects belongs in the driver layer.

All callbacks run on transport-owned daemon threads.  UI callers must marshal
work onto their main loop.  The module deliberately has no GTK/GObject
dependency so it can be tested in Helios's slim CI image.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future, InvalidStateError, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias

from helios.backend.process.env_scrub import (
    NORVI_TRACKER_ENV,
    scrubbed_child_env,
)


RpcId: TypeAlias = int | str
ResponseCallback: TypeAlias = Callable[[Future[Any]], None]
NotificationHandler: TypeAlias = Callable[[str, Any], None]
ServerRequestHandler: TypeAlias = Callable[["ServerRequest"], None]
ExitHandler: TypeAlias = Callable[[int | None], None]
StderrHandler: TypeAlias = Callable[[str], None]


class CodexAppServerError(RuntimeError):
    """Base class for transport, lifecycle, and JSON-RPC failures."""


class CodexAppServerStartError(CodexAppServerError):
    """The App Server process could not be started."""


class CodexAppServerStateError(CodexAppServerError):
    """An operation is invalid in the transport's current lifecycle state."""


class CodexAppServerTimeout(CodexAppServerError, TimeoutError):
    """A bounded startup or JSON-RPC request timed out."""


class CodexAppServerExited(CodexAppServerError):
    """The App Server exited while one or more requests were pending."""

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode
        detail = "without a return code" if returncode is None else f"with code {returncode}"
        super().__init__(f"Codex App Server exited {detail}")


class CodexAppServerRpcError(CodexAppServerError):
    """A JSON-RPC error response returned by Codex App Server."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        data: Any = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        prefix = f"Codex App Server error {code}" if code is not None else "Codex App Server error"
        super().__init__(f"{prefix}: {message}")


@dataclass(slots=True)
class _PendingRequest:
    future: Future[Any]
    method: str
    timer: threading.Timer | None = None


@dataclass(frozen=True, slots=True)
class ServerRequest:
    """A request initiated by App Server and awaiting a client response.

    Handlers may answer immediately or retain this object and answer later
    (for example, after a user approves an action).  Exactly one response is
    accepted; the response methods return ``False`` after another responder
    has already won the race.
    """

    id: RpcId
    method: str
    params: Any
    _transport: "CodexAppServer" = field(repr=False, compare=False)

    def respond(self, result: Any = None) -> bool:
        return self._transport.respond_result(self.id, result)

    def respond_error(
        self,
        code: int,
        message: str,
        data: Any = None,
    ) -> bool:
        return self._transport.respond_error(self.id, code, message, data)

    def abandon(self) -> bool:
        """Forget a server request that Codex resolved without a response."""

        return self._transport.abandon_server_request(self.id)


class CodexAppServer:
    """Persistent, bidirectional Codex App Server stdio connection.

    ``start()`` only spawns the process.  ``perform_handshake()`` performs the
    required, bounded ``initialize`` request followed by the ``initialized``
    notification.  Ordinary requests are rejected until that handshake has
    completed, which prevents accidental protocol-order violations.
    """

    def __init__(
        self,
        binary: str | os.PathLike[str] | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        startup_timeout: float = 10.0,
        request_timeout: float = 60.0,
        stop_timeout: float = 2.0,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        on_notification: NotificationHandler | None = None,
        on_request: ServerRequestHandler | None = None,
        on_exit: ExitHandler | None = None,
        on_stderr: StderrHandler | None = None,
    ) -> None:
        self._binary = str(binary) if binary is not None else None
        self._cwd = str(Path(cwd)) if cwd is not None else None
        self._source_env = dict(env) if env is not None else None
        self._startup_timeout = max(0.001, float(startup_timeout))
        self._request_timeout = max(0.001, float(request_timeout))
        self._stop_timeout = max(0.0, float(stop_timeout))
        self._popen_factory = popen_factory

        self._on_notification = on_notification
        self._on_request = on_request
        self._on_exit = on_exit
        self._on_stderr = on_stderr

        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._handshake_lock = threading.Lock()
        self._proc: Any | None = None
        self._started = False
        self._running = False
        self._stopping = False
        self._initialized = False
        self._initialize_result: dict[str, Any] | None = None
        self._returncode: int | None = None
        self._exit_notified = False
        self._next_request_id = 0
        self._pending: dict[RpcId, _PendingRequest] = {}
        self._inbound_pending: set[RpcId] = set()
        self._threads: list[threading.Thread] = []
        self._stdout_done = threading.Event()
        self._stderr_tail: deque[str] = deque(maxlen=100)
        self._malformed_line_count = 0

    # ── observable state ──────────────────────────────────────────────

    @property
    def running(self) -> bool:
        with self._state_lock:
            return self._running and not self._stopping

    @property
    def user_agent(self) -> str:
        """The App Server's own `userAgent` from `initialize`.

        The InitializeResponse carries no capability list to negotiate against
        (0.149.1: codexHome, platformFamily, platformOs, userAgent — that is
        the whole schema), so the version string is the only thing in it worth
        keeping, and it is what names the build in a drift report.
        """

        with self._state_lock:
            result = self._initialize_result or {}
        return str(result.get("userAgent") or "")

    @property
    def initialized(self) -> bool:
        with self._state_lock:
            return self._initialized and self._running and not self._stopping

    @property
    def returncode(self) -> int | None:
        with self._state_lock:
            return self._returncode

    @property
    def malformed_line_count(self) -> int:
        with self._state_lock:
            return self._malformed_line_count

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        with self._state_lock:
            return tuple(self._stderr_tail)

    def set_notification_handler(self, handler: NotificationHandler | None) -> None:
        with self._state_lock:
            self._on_notification = handler

    def set_request_handler(self, handler: ServerRequestHandler | None) -> None:
        with self._state_lock:
            self._on_request = handler

    def set_exit_handler(self, handler: ExitHandler | None) -> None:
        with self._state_lock:
            self._on_exit = handler

    # ── process lifecycle ─────────────────────────────────────────────

    def start(self) -> "CodexAppServer":
        with self._lifecycle_lock:
            return self._start_locked()

    def _start_locked(self) -> "CodexAppServer":
        """Spawn ``codex app-server`` and start the reader threads.

        Startup readiness is established by :meth:`perform_handshake`, whose
        timeout is bounded.  Calling ``start`` twice while running is
        idempotent; an exited transport must be replaced with a new instance.
        """
        with self._state_lock:
            if self._running and not self._stopping:
                return self
            if self._started:
                raise CodexAppServerStateError(
                    "an exited Codex App Server transport cannot be restarted"
                )

        binary = self._binary
        if binary is None:
            try:
                from helios.backend.codex_env import find_codex_binary

                binary = str(find_codex_binary().path)
            except Exception as exc:
                raise CodexAppServerStartError(str(exc)) from exc

        # Helios's native App Server contract uses Codex-managed auth
        # (file/keyring/etc.), so forward NO provider auth. Its tool children
        # inherit this scrubbed env and therefore get no OPENAI_API_KEY /
        # CODEX_API_KEY either. Direct env-only CODEX_ACCESS_TOKEN compatibility
        # is deliberately out of H1. The tracker token is the one exception,
        # and it is not provider auth: it is the estate work record a session
        # reads for context and records its checkpoints against.
        child_env = scrubbed_child_env(self._source_env, keep=NORVI_TRACKER_ENV)
        child_env.setdefault("RUST_LOG", "error")
        try:
            proc = self._popen_factory(
                [binary, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
                env=child_env,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CodexAppServerStartError(
                f"could not start Codex App Server: {exc}"
            ) from exc

        if proc.stdin is None or proc.stdout is None:
            _terminate_process_group(proc, signal.SIGKILL)
            raise CodexAppServerStartError(
                "Codex App Server did not provide stdio pipes"
            )

        with self._state_lock:
            self._binary = binary
            self._proc = proc
            self._started = True
            self._running = True
            self._stopping = False
            self._returncode = None

        self._start_thread("codex-app-server-stdout", self._stdout_loop, proc)
        if proc.stderr is not None:
            self._start_thread("codex-app-server-stderr", self._stderr_loop, proc)
        self._start_thread("codex-app-server-wait", self._wait_loop, proc)
        return self

    def perform_handshake(
        self,
        client_info: Mapping[str, Any],
        capabilities: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Initialize one transport connection and acknowledge readiness.

        A failed or timed-out initialization closes the unusable connection;
        the protocol permits only one ``initialize`` request per connection.
        """
        budget = self._startup_timeout if timeout is None else max(0.001, float(timeout))
        deadline = time.monotonic() + budget
        with self._handshake_lock:
            with self._state_lock:
                if self._initialized and self._initialize_result is not None:
                    return dict(self._initialize_result)
            if not self.running:
                self.start()

            params: dict[str, Any] = {"clientInfo": dict(client_info)}
            if capabilities is not None:
                params["capabilities"] = dict(capabilities)
            remaining = max(0.001, deadline - time.monotonic())
            future = self._request(
                "initialize",
                params,
                timeout=remaining,
                allow_uninitialized=True,
            )
            try:
                result = future.result(timeout=remaining)
                if not isinstance(result, dict):
                    raise CodexAppServerError(
                        "Codex App Server returned an invalid initialize result"
                    )
                self._send_message({"method": "initialized", "params": {}})
            except FutureTimeout as exc:
                self._expire_request_for_future(future, "initialize")
                self.stop()
                raise CodexAppServerTimeout(
                    "Codex App Server initialization timed out"
                ) from exc
            except Exception:
                self.stop()
                raise

            with self._state_lock:
                if not self._running or self._stopping:
                    raise CodexAppServerExited(self._returncode)
                self._initialized = True
                self._initialize_result = dict(result)
            return dict(result)

    def stop(self, timeout: float | None = None) -> bool:
        with self._lifecycle_lock:
            return self._stop_locked(timeout)

    def _stop_locked(self, timeout: float | None = None) -> bool:
        """Close stdin, then terminate/kill the owned process group if needed.

        Returns ``True`` when process exit was observed inside the bounded stop
        window.  Pending client requests fail immediately in either case.
        """
        budget = self._stop_timeout if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + budget
        with self._state_lock:
            proc = self._proc
            if proc is None:
                return True
            self._stopping = True
            self._initialized = False
        self._fail_all_pending(CodexAppServerStateError("Codex App Server is stopping"))

        # A large write can be blocked by a wedged server. Keep shutdown
        # bounded: normally serialize the close behind that write, but signal
        # the child and skip synchronous stream close if the lock stays wedged.
        acquired_write_lock = self._write_lock.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        )
        term_sent = False
        if acquired_write_lock:
            try:
                _close_stream(getattr(proc, "stdin", None))
            finally:
                self._write_lock.release()
        else:
            # TextIOWrapper.close() can itself block on the writer's internal
            # lock. Signal first and leave that stream to the process teardown
            # path instead of defeating this method's time bound.
            _terminate_process_group(proc, signal.SIGTERM)
            term_sent = True

        # Give EOF a brief opportunity to shut the server down cleanly before
        # signalling the entire session/process group.
        grace = min(0.1, budget * 0.2)
        returncode = _wait_for_process(proc, grace)
        if returncode is None and not term_sent:
            _terminate_process_group(proc, signal.SIGTERM)
            term_sent = True
        if returncode is None:
            remaining = max(0.0, deadline - time.monotonic())
            # Reserve a small tail for SIGKILL if TERM is ignored.
            term_wait = max(0.0, remaining - min(0.1, budget * 0.2))
            returncode = _wait_for_process(proc, term_wait)
        if returncode is None:
            _terminate_process_group(proc, signal.SIGKILL)
            returncode = _wait_for_process(proc, max(0.0, deadline - time.monotonic()))

        _close_stream(getattr(proc, "stdout", None))
        _close_stream(getattr(proc, "stderr", None))
        self._handle_exit(proc, returncode)

        # Reader threads are daemons, but bounded joins prevent routine stops
        # from leaving them around after their streams have closed.
        for thread in tuple(self._threads):
            if thread is threading.current_thread():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return returncode is not None

    # ── client -> server messages ─────────────────────────────────────

    def request(
        self,
        method: str,
        params: Any = None,
        *,
        callback: ResponseCallback | None = None,
        timeout: float | None = None,
    ) -> Future[Any]:
        """Send a request and return a future completed by the reader thread.

        ``callback`` follows :meth:`Future.add_done_callback` semantics and may
        inspect ``future.result()`` or ``future.exception()``.
        """
        if not self.initialized:
            raise CodexAppServerStateError(
                "Codex App Server handshake has not completed"
            )
        return self._request(method, params, callback=callback, timeout=timeout)

    def call(self, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
        """Synchronous bounded convenience wrapper around :meth:`request`."""
        budget = self._request_timeout if timeout is None else max(0.001, float(timeout))
        future = self.request(method, params, timeout=budget)
        try:
            return future.result(timeout=budget)
        except FutureTimeout as exc:
            self._expire_request_for_future(future, method)
            raise CodexAppServerTimeout(
                f"Codex App Server request {method!r} timed out"
            ) from exc

    def notify(self, method: str, params: Any = None) -> None:
        """Send a client notification (no response is expected)."""
        if not self.initialized:
            raise CodexAppServerStateError(
                "Codex App Server handshake has not completed"
            )
        self._send_message(_method_message(method, params))

    def respond_result(self, request_id: RpcId, result: Any = None) -> bool:
        """Answer one server-initiated request successfully, exactly once."""
        if not self._claim_inbound_request(request_id):
            return False
        try:
            self._send_message({"id": request_id, "result": result})
        except Exception:
            # A failed write cannot be retried safely on this connection.
            raise
        return True

    def respond_error(
        self,
        request_id: RpcId,
        code: int,
        message: str,
        data: Any = None,
    ) -> bool:
        """Answer one server-initiated request with a JSON-RPC error."""
        if not self._claim_inbound_request(request_id):
            return False
        error: dict[str, Any] = {"code": int(code), "message": str(message)}
        if data is not None:
            error["data"] = data
        self._send_message({"id": request_id, "error": error})
        return True

    def abandon_server_request(self, request_id: RpcId) -> bool:
        """Drop a server-resolved request without writing a late response."""

        return self._claim_inbound_request(request_id)

    def _request(
        self,
        method: str,
        params: Any,
        *,
        callback: ResponseCallback | None = None,
        timeout: float | None = None,
        allow_uninitialized: bool = False,
    ) -> Future[Any]:
        if not isinstance(method, str) or not method:
            raise ValueError("JSON-RPC method must be a non-empty string")
        budget = self._request_timeout if timeout is None else max(0.001, float(timeout))
        future: Future[Any] = Future()
        if callback is not None:
            future.add_done_callback(callback)

        with self._state_lock:
            if not self._running or self._stopping:
                raise CodexAppServerStateError("Codex App Server is not running")
            if not allow_uninitialized and not self._initialized:
                raise CodexAppServerStateError(
                    "Codex App Server handshake has not completed"
                )
            self._next_request_id += 1
            request_id = self._next_request_id
            timer = threading.Timer(budget, self._expire_request, args=(request_id, method))
            timer.daemon = True
            self._pending[request_id] = _PendingRequest(future, method, timer)
            future.add_done_callback(
                lambda done, rid=request_id: self._discard_cancelled(rid, done)
            )
            timer.start()

        try:
            message = _method_message(method, params)
            message["id"] = request_id
            self._send_message(message)
        except Exception as exc:
            self._fail_pending(request_id, exc)
        return future

    def _send_message(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._write_lock:
            with self._state_lock:
                proc = self._proc
                if proc is None or not self._running or self._stopping:
                    raise CodexAppServerStateError("Codex App Server is not running")
                stdin = proc.stdin
            try:
                stdin.write(payload)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise CodexAppServerExited(self.returncode) from exc

    # ── server -> client dispatch ─────────────────────────────────────

    def _stdout_loop(self, proc: Any) -> None:
        stream = proc.stdout
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                self._dispatch_line(line)
        except (OSError, ValueError):
            return
        finally:
            self._stdout_done.set()

    def _stderr_loop(self, proc: Any) -> None:
        stream = proc.stderr
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                clean = line.rstrip("\r\n")
                with self._state_lock:
                    self._stderr_tail.append(clean)
                    handler = self._on_stderr
                if handler is not None:
                    try:
                        handler(clean)
                    except Exception:
                        pass
        except (OSError, ValueError):
            return

    def _wait_loop(self, proc: Any) -> None:
        try:
            returncode = proc.wait()
        except Exception:
            try:
                returncode = proc.poll()
            except Exception:
                returncode = None
        # Reaping may win the race with the stdout reader even though the pipe
        # still contains a final response.  Let the reader drain before failing
        # all pending requests as exited.
        self._stdout_done.wait(timeout=0.25)
        self._handle_exit(proc, returncode)

    def _dispatch_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            self._note_malformed_line()
            return
        if not isinstance(message, dict):
            self._note_malformed_line()
            return

        method = message.get("method")
        if isinstance(method, str) and method:
            if "id" in message and _valid_rpc_id(message.get("id")):
                self._dispatch_server_request(message["id"], method, message.get("params"))
            elif "id" not in message:
                self._dispatch_notification(method, message.get("params"))
            else:
                self._note_malformed_line()
            return

        if (
            "id" in message
            and _valid_rpc_id(message.get("id"))
            and ("result" in message or "error" in message)
        ):
            self._dispatch_response(message)
            return
        self._note_malformed_line()

    def _dispatch_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        with self._state_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return  # late response, unknown id, or timed-out request
        if pending.timer is not None:
            pending.timer.cancel()

        error = message.get("error")
        if error is not None:
            if isinstance(error, dict):
                code = error.get("code")
                code = code if isinstance(code, int) and not isinstance(code, bool) else None
                detail = error.get("message")
                detail = detail if isinstance(detail, str) and detail else "request failed"
                exc = CodexAppServerRpcError(
                    detail,
                    code=code,
                    data=error.get("data"),
                )
            else:
                exc = CodexAppServerRpcError(str(error) or "request failed")
            _safe_set_exception(pending.future, exc)
        else:
            _safe_set_result(pending.future, message.get("result"))

    def _dispatch_notification(self, method: str, params: Any) -> None:
        if method == "serverRequest/resolved" and isinstance(params, dict):
            request_id = params.get("requestId")
            if _valid_rpc_id(request_id):
                # Codex resolved this inbound request through another reviewer.
                # Retire it at the wire boundary before UI callback ordering can
                # race, so the ID is immediately reusable and a late response
                # is rejected locally instead of being written.
                self.abandon_server_request(request_id)
        with self._state_lock:
            handler = self._on_notification
        if handler is None:
            return
        try:
            handler(method, params)
        except Exception:
            # A consumer callback must never kill the single stdout reader.
            pass

    def _dispatch_server_request(self, request_id: RpcId, method: str, params: Any) -> None:
        with self._state_lock:
            duplicate = request_id in self._inbound_pending
            if not duplicate:
                self._inbound_pending.add(request_id)
            handler = self._on_request
        if duplicate:
            self._send_untracked_error(
                request_id,
                -32600,
                "Duplicate server request id",
            )
            return
        if handler is None:
            self.respond_error(request_id, -32601, f"Unsupported server request: {method}")
            return
        request = ServerRequest(request_id, method, params, self)
        try:
            handler(request)
        except Exception as exc:
            request.respond_error(-32603, "Server request handler failed", str(exc))

    # ── pending-state helpers ─────────────────────────────────────────

    def _claim_inbound_request(self, request_id: RpcId) -> bool:
        with self._state_lock:
            if request_id not in self._inbound_pending:
                return False
            self._inbound_pending.remove(request_id)
            return True

    def _send_untracked_error(self, request_id: RpcId, code: int, message: str) -> None:
        try:
            self._send_message({
                "id": request_id,
                "error": {"code": code, "message": message},
            })
        except CodexAppServerError:
            pass

    def _expire_request(self, request_id: RpcId, method: str) -> None:
        with self._state_lock:
            pending = self._pending.pop(request_id, None)
        if pending is not None:
            _safe_set_exception(
                pending.future,
                CodexAppServerTimeout(
                    f"Codex App Server request {method!r} timed out"
                ),
            )

    def _expire_request_for_future(self, future: Future[Any], method: str) -> None:
        with self._state_lock:
            request_id = next(
                (rid for rid, pending in self._pending.items() if pending.future is future),
                None,
            )
        if request_id is not None:
            self._expire_request(request_id, method)

    def _discard_cancelled(self, request_id: RpcId, future: Future[Any]) -> None:
        if not future.cancelled():
            return
        with self._state_lock:
            pending = self._pending.pop(request_id, None)
        if pending is not None and pending.timer is not None:
            pending.timer.cancel()

    def _fail_pending(self, request_id: RpcId, exc: BaseException) -> None:
        with self._state_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        if pending.timer is not None:
            pending.timer.cancel()
        _safe_set_exception(pending.future, exc)

    def _fail_all_pending(self, exc: BaseException) -> None:
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            if item.timer is not None:
                item.timer.cancel()
            _safe_set_exception(item.future, exc)

    def _handle_exit(self, proc: Any, returncode: int | None) -> None:
        with self._state_lock:
            if proc is not self._proc or self._exit_notified:
                return
            self._exit_notified = True
            self._running = False
            self._stopping = False
            self._initialized = False
            self._returncode = returncode
            self._inbound_pending.clear()
            handler = self._on_exit
        self._fail_all_pending(CodexAppServerExited(returncode))
        if handler is not None:
            try:
                handler(returncode)
            except Exception:
                pass

    def _note_malformed_line(self) -> None:
        with self._state_lock:
            self._malformed_line_count += 1

    def _start_thread(self, name: str, target: Callable[[Any], None], proc: Any) -> None:
        thread = threading.Thread(name=name, target=target, args=(proc,), daemon=True)
        self._threads.append(thread)
        thread.start()


def _method_message(method: str, params: Any) -> dict[str, Any]:
    if not isinstance(method, str) or not method:
        raise ValueError("JSON-RPC method must be a non-empty string")
    message: dict[str, Any] = {"method": method}
    if params is not None:
        message["params"] = params
    return message


def _valid_rpc_id(value: Any) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


def _safe_set_result(future: Future[Any], result: Any) -> None:
    try:
        future.set_result(result)
    except InvalidStateError:
        pass


def _safe_set_exception(future: Future[Any], exc: BaseException) -> None:
    try:
        future.set_exception(exc)
    except InvalidStateError:
        pass


def _close_stream(stream: Any | None) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _wait_for_process(proc: Any, timeout: float) -> int | None:
    try:
        return proc.wait(timeout=max(0.0, timeout))
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        try:
            return proc.poll()
        except Exception:
            return None


def _terminate_process_group(proc: Any, sig: signal.Signals) -> None:
    """Signal the isolated process group, falling back to Popen methods."""
    pid = getattr(proc, "pid", None)
    if os.name == "posix" and isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, sig)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        pass
