from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from helios import __version__
from helios.backend.process import codex_app_hub as hub_module
from helios.backend.process.codex_app_server import CodexAppServerStateError


class FakeTransport:
    def __init__(
        self,
        binary,
        *,
        on_notification,
        on_request,
        on_exit,
        handshake_delay=0.0,
    ):
        self.binary = binary
        self.on_notification = on_notification
        self.on_request = on_request
        self.on_exit = on_exit
        self.handshake_delay = handshake_delay
        self.initialized = False
        self.handshakes = []
        self.calls = []
        self.requests = []
        self.notifications = []
        self.stop_calls = []

    def perform_handshake(
        self,
        client_info,
        capabilities,
        *,
        timeout=None,
    ):
        if self.handshake_delay:
            time.sleep(self.handshake_delay)
        self.handshakes.append((client_info, capabilities, timeout))
        self.initialized = True
        return {"userAgent": "codex-test"}

    def call(self, method, params=None, *, timeout=None):
        self.calls.append((method, params, timeout))
        return {"method": method, "params": params}

    def request(
        self,
        method,
        params=None,
        *,
        callback=None,
        timeout=None,
    ):
        self.requests.append((method, params, timeout))
        future = Future()
        future.set_result({"method": method, "params": params})
        if callback is not None:
            future.add_done_callback(callback)
        return future

    def notify(self, method, params=None):
        self.notifications.append((method, params))

    def stop(self, timeout=None):
        self.stop_calls.append(timeout)
        self.initialized = False
        return True

    def emit_notification(self, method, params):
        self.on_notification(method, params)

    def emit_request(self, request):
        self.on_request(request)

    def emit_exit(self, code):
        self.initialized = False
        self.on_exit(code)


class FakeFactory:
    def __init__(self, handshake_delay=0.0):
        self.handshake_delay = handshake_delay
        self.transports = []
        self._lock = threading.Lock()

    def __call__(self, binary, **callbacks):
        transport = FakeTransport(
            binary,
            handshake_delay=self.handshake_delay,
            **callbacks,
        )
        with self._lock:
            self.transports.append(transport)
        return transport


class Client:
    def __init__(self, *, fail_notifications=False):
        self.notifications = []
        self.requests = []
        self.exits = []
        self.fail_notifications = fail_notifications

    def on_app_notification(self, method, params):
        self.notifications.append((method, params))
        if self.fail_notifications:
            raise RuntimeError("test callback failure")

    def on_app_request(self, request):
        self.requests.append(request)

    def on_app_exit(self, code):
        self.exits.append(code)


class FakeServerRequest:
    def __init__(self, method, params):
        self.method = method
        self.params = params
        self.errors = []

    def respond_error(self, code, message, data=None):
        self.errors.append((code, message, data))
        return True


def _hub(factory=None, **kwargs):
    factory = factory or FakeFactory()
    return (
        hub_module.CodexAppServerHub(
            transport_factory=factory,
            handshake_timeout=0.5,
            shutdown_timeout=0.2,
            **kwargs,
        ),
        factory,
    )


def test_acquire_initializes_one_connection_with_exact_capabilities():
    hub, factory = _hub()
    first = Client()
    second = Client()

    assert hub.acquire(first, binary="/fake/codex") is hub
    assert hub.acquire(second, binary="/ignored/other-codex") is hub
    assert hub.client_count == 2
    assert hub.connected
    assert len(factory.transports) == 1
    transport = factory.transports[0]
    assert transport.binary == "/fake/codex"
    assert transport.handshakes == [
        (
            {"name": "helios", "version": __version__},
            {"experimentalApi": True},
            0.5,
        )
    ]

    assert hub.call("model/list", {"limit": 10}, timeout=0.1) == {
        "method": "model/list",
        "params": {"limit": 10},
    }
    future = hub.request("account/rateLimits/read", {}, timeout=0.2)
    assert future.result()["method"] == "account/rateLimits/read"
    hub.notify("test/clientNotification", {"ok": True})
    assert transport.notifications == [("test/clientNotification", {"ok": True})]
    hub.shutdown()


def test_concurrent_acquire_still_starts_only_one_transport():
    factory = FakeFactory(handshake_delay=0.03)
    hub, _ = _hub(factory)
    clients = [Client() for _ in range(12)]
    barrier = threading.Barrier(len(clients))
    failures = []

    def acquire(client):
        try:
            barrier.wait()
            hub.acquire(client, binary="/fake/codex")
        except Exception as exc:  # pragma: no cover - asserted empty below
            failures.append(exc)

    threads = [threading.Thread(target=acquire, args=(client,)) for client in clients]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(factory.transports) == 1
    assert hub.client_count == len(clients)
    hub.shutdown()


def test_thread_routing_buffers_until_binding_and_broadcasts_globals():
    hub, factory = _hub()
    first = Client(fail_notifications=True)
    second = Client()
    hub.acquire(first)
    hub.acquire(second)
    transport = factory.transports[0]

    early_first = {"threadId": "thread-1", "turn": {"id": "turn-1"}}
    early_second = {"threadId": "thread-2", "turn": {"id": "turn-2"}}
    transport.emit_notification("turn/started", early_first)
    transport.emit_notification("turn/started", early_second)
    assert first.notifications == []
    assert second.notifications == []

    hub.bind_thread(first, "thread-1")
    hub.bind_thread(second, "thread-2")
    assert first.notifications == [("turn/started", early_first)]
    assert second.notifications == [("turn/started", early_second)]

    direct = {"threadId": "thread-2", "delta": "hello"}
    transport.emit_notification("item/agentMessage/delta", direct)
    assert second.notifications[-1] == ("item/agentMessage/delta", direct)
    assert len(first.notifications) == 1

    global_update = {"rateLimits": {"primary": {"usedPercent": 4}}}
    transport.emit_notification("account/rateLimits/updated", global_update)
    assert first.notifications[-1] == (
        "account/rateLimits/updated",
        global_update,
    )
    assert second.notifications[-1] == (
        "account/rateLimits/updated",
        global_update,
    )
    hub.shutdown()


def test_child_thread_started_routes_via_parent_and_remembers_child():
    hub, factory = _hub()
    parent_client = Client()
    other_client = Client()
    hub.acquire(parent_client)
    hub.acquire(other_client)
    hub.bind_thread(parent_client, "parent")
    hub.bind_thread(other_client, "other")
    transport = factory.transports[0]

    child_started = {
        "thread": {
            "id": "child",
            "parentThreadId": "parent",
            "status": {"type": "idle"},
        }
    }
    transport.emit_notification("thread/started", child_started)
    transport.emit_notification(
        "turn/started",
        {"threadId": "child", "turn": {"id": "child-turn"}},
    )

    assert [method for method, _ in parent_client.notifications] == [
        "thread/started",
        "turn/started",
    ]
    assert other_client.notifications == []
    hub.shutdown()


def test_child_and_its_events_buffer_in_wire_order_before_parent_binding():
    hub, factory = _hub()
    client = Client()
    hub.acquire(client)
    transport = factory.transports[0]

    transport.emit_notification(
        "thread/started",
        {"thread": {"id": "child", "parentThreadId": "parent"}},
    )
    transport.emit_notification(
        "item/started",
        {"threadId": "child", "item": {"id": "work"}},
    )
    hub.bind_thread(client, "parent")

    assert [method for method, _ in client.notifications] == [
        "thread/started",
        "item/started",
    ]
    hub.shutdown()


def test_server_requests_route_by_thread_and_buffer_is_bounded():
    hub, factory = _hub(early_buffer_limit=2)
    client = Client()
    hub.acquire(client)
    transport = factory.transports[0]

    evicted = FakeServerRequest("item/tool/requestUserInput", {"threadId": "old"})
    pending = FakeServerRequest(
        "item/commandExecution/requestApproval",
        {"threadId": "owned"},
    )
    newest = FakeServerRequest("item/fileChange/requestApproval", {"threadId": "new"})
    transport.emit_request(evicted)
    transport.emit_request(pending)
    transport.emit_request(newest)

    assert len(evicted.errors) == 1
    assert evicted.errors[0][0] == -32000
    assert client.requests == []
    hub.bind_thread(client, "owned")
    assert client.requests == [pending]
    assert pending.errors == []
    hub.shutdown()
    assert len(newest.errors) == 1


def test_connection_level_request_has_exactly_one_client_responder():
    hub, factory = _hub()
    first = Client()
    second = Client()
    hub.acquire(first)
    hub.acquire(second)
    request = FakeServerRequest("attestation/generate", {})

    factory.transports[0].emit_request(request)

    assert first.requests == [request]
    assert second.requests == []
    hub.shutdown()


def test_release_unsubscribes_all_owned_threads_and_final_release_stops_once():
    hub, factory = _hub()
    first = Client()
    second = Client()
    hub.acquire(first)
    hub.acquire(second)
    hub.bind_thread(first, "parent")
    transport = factory.transports[0]
    transport.emit_notification(
        "thread/started",
        {"thread": {"id": "child", "parentThreadId": "parent"}},
    )

    hub.release(first)
    assert hub.client_count == 1
    assert transport.stop_calls == []
    unsubscribed = {
        request[1]["threadId"]
        for request in transport.requests
        if request[0] == "thread/unsubscribe"
    }
    assert unsubscribed == {"parent", "child"}

    hub.release(second)
    hub.release(second)
    hub.shutdown()
    assert transport.stop_calls == [0.2]
    assert hub.client_count == 0


def test_release_and_shutdown_race_stops_transport_only_once():
    hub, factory = _hub()
    client = Client()
    hub.acquire(client)
    transport = factory.transports[0]
    barrier = threading.Barrier(2)

    def release():
        barrier.wait()
        hub.release(client)

    def shutdown():
        barrier.wait()
        hub.shutdown()

    threads = [threading.Thread(target=release), threading.Thread(target=shutdown)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert transport.stop_calls == [0.2]


def test_safety_abort_stops_transport_and_invalidates_every_client():
    hub, factory = _hub()
    first = Client()
    second = Client()
    hub.acquire(first)
    hub.acquire(second)
    hub.bind_thread(first, "first-thread")
    hub.bind_thread(second, "second-thread")
    transport = factory.transports[0]

    hub.abort_transport(returncode=-9)
    hub.abort_transport(returncode=-9)

    assert transport.stop_calls == [0.2]
    assert first.exits == [-9]
    assert second.exits == [-9]
    assert hub.client_count == 0
    assert not hub.connected
    with pytest.raises(CodexAppServerStateError):
        hub.call("thread/read", {"threadId": "first-thread"})
    hub.shutdown()


def test_new_acquire_waits_for_previous_transport_to_finish_stopping():
    hub, factory = _hub()
    first = Client()
    second = Client()
    hub.acquire(first)
    transport = factory.transports[0]
    stop_entered = threading.Event()
    allow_stop = threading.Event()
    original_stop = transport.stop

    def slow_stop(timeout=None):
        stop_entered.set()
        assert allow_stop.wait(0.5)
        return original_stop(timeout)

    transport.stop = slow_stop
    release_thread = threading.Thread(target=hub.release, args=(first,))
    release_thread.start()
    assert stop_entered.wait(0.5)

    acquire_thread = threading.Thread(target=hub.acquire, args=(second,))
    acquire_thread.start()
    time.sleep(0.02)
    assert len(factory.transports) == 1
    assert acquire_thread.is_alive()

    allow_stop.set()
    release_thread.join()
    acquire_thread.join()
    assert len(factory.transports) == 2
    hub.shutdown()


def test_credential_update_refuses_active_clients_without_mutating_transport():
    hub, factory = _hub()
    client = Client()
    hub.acquire(client)
    transport = factory.transports[0]

    with pytest.raises(
        hub_module.CodexCredentialUpdateInUseError,
        match="Close all active GPT sessions",
    ):
        with hub.credential_update():
            pytest.fail("blocked credential update body must not run")

    assert hub.client_count == 1
    assert hub.connected
    assert transport.stop_calls == []
    hub.shutdown()


def test_acquire_cannot_cross_in_flight_credential_update():
    hub, factory = _hub()

    class ObservableLock:
        def __init__(self):
            self._inner = threading.Lock()
            self.waiter_blocked = threading.Event()

        def acquire(self):
            if self._inner.locked():
                self.waiter_blocked.set()
            return self._inner.acquire()

        def release(self):
            self._inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *_args):
            self.release()
            return False

    lifecycle_lock = ObservableLock()
    hub._lifecycle_lock = lifecycle_lock
    update_entered = threading.Event()
    allow_update = threading.Event()
    update_done = threading.Event()
    client = Client()

    def update():
        with hub.credential_update():
            update_entered.set()
            assert allow_update.wait(0.5)
        update_done.set()

    def acquire():
        assert update_entered.wait(0.5)
        hub.acquire(client)

    update_thread = threading.Thread(target=update)
    acquire_thread = threading.Thread(target=acquire)
    update_thread.start()
    assert update_entered.wait(0.5)
    acquire_thread.start()
    assert lifecycle_lock.waiter_blocked.wait(0.5)
    assert acquire_thread.is_alive()
    assert factory.transports == []

    allow_update.set()
    update_thread.join(timeout=0.5)
    acquire_thread.join(timeout=0.5)
    assert update_done.is_set()
    assert not update_thread.is_alive()
    assert not acquire_thread.is_alive()
    assert len(factory.transports) == 1
    hub.shutdown()


def test_successful_credential_update_discards_idle_authenticated_transport():
    account = {"name": "old"}

    class AccountFactory(FakeFactory):
        def __call__(self, binary, **callbacks):
            transport = super().__call__(binary, **callbacks)
            transport.account = account["name"]
            return transport

    factory = AccountFactory()
    hub, _ = _hub(factory)

    # Exercise the defensive idle-transport branch directly.  Normal final
    # release already stops the transport, but credential rotation must also
    # be safe if an unowned initialized connection survives another path.
    with hub._lifecycle_lock:
        with hub._lock:
            old = hub._new_transport("/old/codex")
            old.perform_handshake({}, {}, timeout=0.1)

    with hub.credential_update():
        account["name"] = "new"

    assert old.stop_calls == [0.2]
    client = Client()
    hub.acquire(client, binary="/new/codex")
    assert len(factory.transports) == 2
    assert factory.transports[1] is not old
    assert factory.transports[1].account == "new"
    hub.shutdown()


def test_unexpected_exit_broadcasts_once_and_allows_fresh_connection():
    hub, factory = _hub()
    first = Client()
    second = Client()
    hub.acquire(first)
    hub.acquire(second)
    transport = factory.transports[0]
    hub.bind_thread(first, "lost-thread")

    transport.emit_exit(17)
    transport.emit_exit(17)

    assert first.exits == [17]
    assert second.exits == [17]
    assert not hub.connected
    assert hub.client_count == 0
    with pytest.raises(CodexAppServerStateError):
        hub.call("thread/read", {"threadId": "lost-thread"})

    hub.acquire(first, binary="/replacement/codex")
    assert len(factory.transports) == 2
    assert factory.transports[1].binary == "/replacement/codex"
    replacement = factory.transports[1]
    hub.release(first)
    assert replacement.stop_calls == [0.2]
    hub.shutdown()


def test_bind_rejects_unacquired_client_and_cross_client_collision():
    hub, _ = _hub()
    first = Client()
    second = Client()
    with pytest.raises(CodexAppServerStateError):
        hub.bind_thread(first, "thread")
    hub.acquire(first)
    hub.acquire(second)
    hub.bind_thread(first, "thread")
    with pytest.raises(CodexAppServerStateError):
        hub.bind_thread(second, "thread")
    with pytest.raises(ValueError):
        hub.bind_thread(first, "")
    hub.shutdown()


def test_get_shared_hub_returns_process_singleton(monkeypatch):
    monkeypatch.setattr(hub_module, "_shared_hub", None)
    first = hub_module.get_shared_hub()
    second = hub_module.get_shared_hub()
    assert first is second
    first.shutdown()
