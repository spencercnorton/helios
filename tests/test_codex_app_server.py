"""Pure-Python tests for the persistent Codex App Server transport."""

from __future__ import annotations

import json
import queue
import signal
import subprocess
import threading
import time
from concurrent.futures import Future
from typing import Any

import pytest

from helios.backend.process import codex_app_server as cas


class _QueueReader:
    def __init__(self) -> None:
        self._items: queue.Queue[str | None] = queue.Queue()
        self._closed = False

    def feed(self, value: str | dict[str, Any]) -> None:
        line = value if isinstance(value, str) else json.dumps(value)
        self._items.put(line if line.endswith("\n") else line + "\n")

    def readline(self) -> str:
        item = self._items.get()
        return "" if item is None else item

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._items.put(None)


class _CaptureWriter:
    def __init__(self, on_message=None, on_close=None) -> None:
        self.messages: list[dict[str, Any]] = []
        self.raw_writes: list[str] = []
        self._partial = ""
        self._on_message = on_message
        self._on_close = on_close
        self._lock = threading.Lock()
        self.closed = False

    def write(self, text: str) -> int:
        with self._lock:
            if self.closed:
                raise BrokenPipeError("closed")
            self.raw_writes.append(text)
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                if not line:
                    continue
                message = json.loads(line)
                self.messages.append(message)
                if self._on_message is not None:
                    self._on_message(message)
        return len(text)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        callback = None
        with self._lock:
            if not self.closed:
                self.closed = True
                callback = self._on_close
        if callback is not None:
            callback()


class FakeProcess:
    _next_pid = 91000

    def __init__(self, *, on_message=None, exit_on_stdin_close=True) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.stdout = _QueueReader()
        self.stderr = _QueueReader()
        self._exit_event = threading.Event()
        self._exit_on_stdin_close = exit_on_stdin_close
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.stdin = _CaptureWriter(on_message, self._stdin_closed)

    def _stdin_closed(self) -> None:
        if self._exit_on_stdin_close:
            self.exit(0)

    def feed(self, value: str | dict[str, Any]) -> None:
        self.stdout.feed(value)

    def exit(self, code: int) -> None:
        if self._exit_event.is_set():
            return
        self.returncode = code
        self._exit_event.set()
        self.stdout.close()
        self.stderr.close()

    def wait(self, timeout=None):
        if not self._exit_event.wait(timeout):
            raise subprocess.TimeoutExpired("fake-codex", timeout)
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.exit(-signal.SIGTERM)

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit(-signal.SIGKILL)


class PopenFactory:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.argv = None
        self.kwargs = None
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        self.argv = argv
        self.kwargs = kwargs
        return self.process


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


def _handshaking_process(**kwargs) -> FakeProcess:
    process: FakeProcess

    def on_message(message):
        if message.get("method") == "initialize":
            process.feed({"id": message["id"], "result": {"userAgent": "codex-test"}})

    process = FakeProcess(on_message=on_message, **kwargs)
    return process


def _started_server(process: FakeProcess, **kwargs) -> cas.CodexAppServer:
    server = cas.CodexAppServer(
        "/fake/codex",
        popen_factory=PopenFactory(process),
        startup_timeout=0.5,
        stop_timeout=0.2,
        **kwargs,
    )
    server.start()
    server.perform_handshake(
        {"name": "helios", "title": "Helios", "version": "test"},
        {"optOutNotificationMethods": ["unused/event"]},
    )
    return server


def test_spawn_scrubs_env_and_handshake_order():
    process = _handshaking_process()
    factory = PopenFactory(process)
    server = cas.CodexAppServer(
        "/fake/codex",
        cwd="/tmp/project",
        env={
            "PATH": "/bin",
            "OPENAI_API_KEY": "fixture-key",
            "CODEX_API_KEY": "fixture-exec-key",
            "CODEX_ACCESS_TOKEN": "-".join(("fixture", "access", "token")),
            "APOLLO_SCRATCHPAD_KEY": "must-not-leak",
            "HELIOS_DEBUG": "1",
        },
        popen_factory=factory,
        startup_timeout=0.5,
    )
    server.start()
    result = server.perform_handshake(
        {"name": "helios", "title": "Helios", "version": "0.29"},
        {"requestAttestation": False},
    )

    assert result == {"userAgent": "codex-test"}
    assert server.running and server.initialized
    assert factory.argv == ["/fake/codex", "app-server"]
    assert factory.kwargs["start_new_session"] is True
    assert factory.kwargs["cwd"] == "/tmp/project"
    assert "OPENAI_API_KEY" not in factory.kwargs["env"]
    assert "CODEX_API_KEY" not in factory.kwargs["env"]
    assert "CODEX_ACCESS_TOKEN" not in factory.kwargs["env"]
    assert "APOLLO_SCRATCHPAD_KEY" not in factory.kwargs["env"]
    assert "HELIOS_DEBUG" not in factory.kwargs["env"]
    assert [m["method"] for m in process.stdin.messages] == [
        "initialize",
        "initialized",
    ]
    init = process.stdin.messages[0]
    assert init["params"]["clientInfo"]["name"] == "helios"
    assert init["params"]["capabilities"] == {"requestAttestation": False}
    assert "jsonrpc" not in init
    server.stop()


def test_concurrent_start_spawns_exactly_one_process():
    process = _handshaking_process()
    factory = PopenFactory(process)
    server = cas.CodexAppServer("/fake/codex", popen_factory=factory)
    barrier = threading.Barrier(8)
    failures = []

    def start():
        try:
            barrier.wait()
            server.start()
        except Exception as exc:  # pragma: no cover - asserted empty
            failures.append(exc)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert factory.calls == 1
    server.stop()


def test_responses_callbacks_errors_and_out_of_order_delivery():
    process = _handshaking_process()
    callback_results: list[tuple[Any, BaseException | None]] = []
    callback_event = threading.Event()
    server = _started_server(process)

    def callback(done: Future[Any]) -> None:
        callback_results.append((None if done.exception() else done.result(), done.exception()))
        callback_event.set()

    first = server.request("thread/start", {"cwd": "/tmp"}, callback=callback)
    second = server.request("model/list", {"limit": 10})
    first_wire, second_wire = process.stdin.messages[-2:]
    assert second_wire["id"] > first_wire["id"]

    process.feed({"id": second_wire["id"], "result": {"data": [1, 2]}})
    process.feed({
        "id": first_wire["id"],
        "error": {"code": -32001, "message": "busy", "data": {"retry": True}},
    })
    assert second.result(timeout=0.5) == {"data": [1, 2]}
    with pytest.raises(cas.CodexAppServerRpcError) as raised:
        first.result(timeout=0.5)
    assert raised.value.code == -32001
    assert raised.value.data == {"retry": True}
    assert callback_event.wait(0.5)
    assert isinstance(callback_results[0][1], cas.CodexAppServerRpcError)
    server.stop()


def test_concurrent_requests_have_unique_ids_and_atomic_jsonl_writes():
    process = _handshaking_process()
    server = _started_server(process)
    futures: list[Future[Any]] = []
    futures_lock = threading.Lock()

    def send(index: int) -> None:
        future = server.request("test/echo", {"index": index})
        with futures_lock:
            futures.append(future)

    threads = [threading.Thread(target=send, args=(i,)) for i in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    requests = [m for m in process.stdin.messages if m.get("method") == "test/echo"]
    ids = [m["id"] for m in requests]
    assert len(ids) == 30
    assert len(set(ids)) == 30
    assert all(raw.endswith("\n") and raw.count("\n") == 1 for raw in process.stdin.raw_writes)
    for message in requests:
        process.feed({"id": message["id"], "result": message["params"]})
    assert sorted(f.result(timeout=0.5)["index"] for f in futures) == list(range(30))
    server.stop()


def test_notifications_and_server_requests_support_success_and_error():
    process = _handshaking_process()
    notifications = []
    requests = []

    def on_notification(method, params):
        notifications.append((method, params))

    def on_request(request: cas.ServerRequest):
        requests.append(request)
        if request.method == "item/commandExecution/requestApproval":
            assert request.respond({"decision": "accept"})
            assert not request.respond({"decision": "duplicate"})
        else:
            assert request.respond_error(4001, "declined", {"reason": "test"})

    server = _started_server(
        process,
        on_notification=on_notification,
        on_request=on_request,
    )
    process.feed({"method": "turn/started", "params": {"turn": {"id": "t1"}}})
    process.feed({
        "id": "approval-1",
        "method": "item/commandExecution/requestApproval",
        "params": {"command": "true"},
    })
    process.feed({
        "id": 800,
        "method": "item/tool/requestUserInput",
        "params": {"question": "Continue?"},
    })

    _wait_until(lambda: len(notifications) == 1 and len(requests) == 2)
    assert notifications == [("turn/started", {"turn": {"id": "t1"}})]
    success = next(m for m in process.stdin.messages if m.get("id") == "approval-1")
    error = next(m for m in process.stdin.messages if m.get("id") == 800)
    assert success["result"] == {"decision": "accept"}
    assert error["error"] == {
        "code": 4001,
        "message": "declined",
        "data": {"reason": "test"},
    }
    server.stop()


def test_missing_server_request_handler_returns_method_not_found():
    process = _handshaking_process()
    server = _started_server(process)
    process.feed({"id": 77, "method": "unknown/prompt", "params": {}})
    _wait_until(lambda: any(m.get("id") == 77 for m in process.stdin.messages))
    response = next(m for m in process.stdin.messages if m.get("id") == 77)
    assert response["error"]["code"] == -32601
    server.stop()


def test_server_resolved_request_can_be_abandoned_without_wire_response():
    process = _handshaking_process()
    requests = []
    server = _started_server(process, on_request=requests.append)
    message = {
        "id": "auto-resolved",
        "method": "item/tool/requestUserInput",
        "params": {"threadId": "thread-1", "questions": []},
    }
    process.feed(message)
    _wait_until(lambda: len(requests) == 1)

    assert requests[0].abandon()
    assert not requests[0].abandon()
    assert not any(m.get("id") == "auto-resolved" for m in process.stdin.messages)

    # Reuse after abandonment is not mistaken for a duplicate pending id.
    process.feed(message)
    _wait_until(lambda: len(requests) == 2)
    requests[1].respond({"answers": {}})
    server.stop()


def test_server_resolved_notification_retires_request_before_ui_callback():
    process = _handshaking_process()
    requests = []
    notifications = []
    server = _started_server(
        process,
        on_request=requests.append,
        on_notification=lambda method, params: notifications.append((method, params)),
    )
    message = {
        "id": "resolved-on-wire",
        "method": "item/tool/requestUserInput",
        "params": {"threadId": "thread-1", "questions": []},
    }
    process.feed(message)
    _wait_until(lambda: len(requests) == 1)
    process.feed(
        {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thread-1", "requestId": "resolved-on-wire"},
        }
    )
    _wait_until(lambda: len(notifications) == 1)

    assert not requests[0].respond({"answers": {}})
    assert not any(m.get("id") == "resolved-on-wire" for m in process.stdin.messages)
    process.feed(message)
    _wait_until(lambda: len(requests) == 2)
    assert requests[1].respond({"answers": {}})
    server.stop()


def test_malformed_and_noise_lines_do_not_break_reader():
    process = _handshaking_process()
    server = _started_server(process)
    future = server.request("healthy/request", {})
    request_id = process.stdin.messages[-1]["id"]

    process.feed("this is a Rust startup warning")
    process.feed("{not-json")
    process.feed("[]")
    process.feed({"id": 999})
    process.feed({"id": [], "result": {"invalid": True}})
    process.feed({"id": request_id, "result": {"ok": True}})

    assert future.result(timeout=0.5) == {"ok": True}
    _wait_until(lambda: server.malformed_line_count == 5)
    assert server.running
    server.stop()


def test_request_timeout_removes_pending_and_ignores_late_response():
    process = _handshaking_process()
    server = _started_server(process, request_timeout=0.05)
    future = server.request("never/replies", timeout=0.03)
    request_id = process.stdin.messages[-1]["id"]
    with pytest.raises(cas.CodexAppServerTimeout):
        future.result(timeout=0.5)

    process.feed({"id": request_id, "result": {"late": True}})
    time.sleep(0.02)
    assert isinstance(future.exception(), cas.CodexAppServerTimeout)
    assert server.running
    server.stop()


def test_unexpected_exit_fails_every_pending_request_and_notifies_once():
    process = _handshaking_process()
    exits = []
    exit_event = threading.Event()

    def on_exit(code):
        exits.append(code)
        exit_event.set()

    server = _started_server(process, on_exit=on_exit)
    first = server.request("pending/one", {})
    second = server.request("pending/two", {})
    process.exit(17)

    for future in (first, second):
        with pytest.raises(cas.CodexAppServerExited) as raised:
            future.result(timeout=0.5)
        assert raised.value.returncode == 17
    assert exit_event.wait(0.5)
    assert exits == [17]
    assert not server.running and not server.initialized
    assert server.returncode == 17
    assert server.stop()
    assert exits == [17]


def test_stdout_response_is_drained_before_exit_cleanup():
    process = _handshaking_process()
    server = _started_server(process)
    future = server.request("final/request", {})
    request_id = process.stdin.messages[-1]["id"]

    process.feed({"id": request_id, "result": {"landed": True}})
    process.exit(0)

    assert future.result(timeout=0.5) == {"landed": True}
    _wait_until(lambda: not server.running)
    assert server.returncode == 0
    assert not server.running and not server.initialized
    assert server.stop()


def test_stop_signals_owned_process_group_when_eof_is_ignored(monkeypatch):
    process = _handshaking_process(exit_on_stdin_close=False)
    signals = []

    def killpg(pid, sig):
        assert pid == process.pid
        signals.append(sig)
        if sig == signal.SIGTERM:
            process.exit(-signal.SIGTERM)

    monkeypatch.setattr(cas.os, "killpg", killpg)
    server = _started_server(process)
    assert server.stop(timeout=0.3)
    assert signals == [signal.SIGTERM]
    assert process.stdin.closed
    assert server.returncode == -signal.SIGTERM


def test_stop_skips_blocking_stdin_close_when_writer_lock_is_wedged(monkeypatch):
    process = _handshaking_process(exit_on_stdin_close=False)
    signals = []

    def killpg(_pid, sig):
        signals.append(sig)
        process.exit(-sig)

    monkeypatch.setattr(cas.os, "killpg", killpg)
    server = _started_server(process)
    server._write_lock.acquire()
    started = time.monotonic()
    try:
        assert server.stop(timeout=0.04)
    finally:
        server._write_lock.release()

    assert time.monotonic() - started < 0.25
    assert signals == [signal.SIGTERM]
    assert not process.stdin.closed


def test_handshake_timeout_is_bounded_and_closes_process():
    process = FakeProcess()
    server = cas.CodexAppServer(
        "/fake/codex",
        popen_factory=PopenFactory(process),
        startup_timeout=0.03,
        stop_timeout=0.1,
    )
    started = time.monotonic()
    with pytest.raises(cas.CodexAppServerTimeout):
        server.perform_handshake({"name": "helios", "version": "test"})
    elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert not server.running
    assert process.stdin.closed
