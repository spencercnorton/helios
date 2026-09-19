"""Shared connection and routing for Codex App Server.

Helios may have several open Codex sessions, but Codex App Server is designed
to multiplex their threads over one persistent connection.  ``CodexAppServerHub``
owns that connection and routes inbound traffic to lightweight driver clients.
It deliberately has no GTK/GObject dependency; callbacks run on the transport
thread and drivers are responsible for marshalling them onto the UI loop.

Clients are duck typed and may implement any of these methods::

    on_app_notification(method, params)
    on_app_request(request)
    on_app_exit(returncode)

The first acquisition starts and initializes the server.  The final release
stops it, so opening more Helios sessions never creates a process per session.
"""

from __future__ import annotations

import threading
from collections import deque
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from helios import __version__
from helios.backend.process.codex_app_server import (
    CodexAppServer,
    CodexAppServerStateError,
    ServerRequest,
)


_CLIENT_INFO = {"name": "helios", "version": __version__}
_CAPABILITIES = {"experimentalApi": True}


class CodexCredentialUpdateInUseError(CodexAppServerStateError):
    """Raised when credentials cannot change while GPT clients are active."""


class _Transport(Protocol):
    """The subset of :class:`CodexAppServer` used by the hub."""

    initialized: bool

    def perform_handshake(
        self,
        client_info: dict[str, Any],
        capabilities: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]: ...

    def request(
        self,
        method: str,
        params: Any = None,
        *,
        callback: Any = None,
        timeout: float | None = None,
    ) -> Future[Any]: ...

    def call(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any: ...

    def notify(self, method: str, params: Any = None) -> None: ...

    def stop(self, timeout: float | None = None) -> bool: ...


@dataclass(slots=True)
class _BufferedInbound:
    """An inbound message received just before its thread was bound."""

    route_thread_id: str
    kind: str
    payload: Any
    method: str


class CodexAppServerHub:
    """Thread-safe owner and router for one lazy App Server connection.

    ``acquire`` and ``release`` register client objects by identity.  Once a
    ``thread/start`` or ``thread/resume`` response supplies an id, the driver
    calls ``bind_thread``.  Any notifications or server requests that raced
    ahead of that response are then delivered in wire order.
    """

    def __init__(
        self,
        *,
        transport_factory: Any = CodexAppServer,
        handshake_timeout: float = 10.0,
        shutdown_timeout: float = 2.0,
        early_buffer_limit: int = 256,
    ) -> None:
        self._transport_factory = transport_factory
        self._handshake_timeout = max(0.001, float(handshake_timeout))
        self._shutdown_timeout = max(0.0, float(shutdown_timeout))
        self._early_buffer_limit = max(1, int(early_buffer_limit))

        # Creation and bounded teardown are serialized separately from routing
        # so a last-release/first-acquire race cannot briefly own two servers.
        self._lifecycle_lock = threading.Lock()
        self._lock = threading.RLock()
        self._transport: _Transport | None = None
        self._clients: dict[int, Any] = {}
        self._primary_threads: dict[int, str] = {}
        self._thread_clients: dict[str, int] = {}
        self._early: deque[_BufferedInbound] = deque()

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def server_version(self) -> str:
        """The App Server's `userAgent`, or "" before the handshake lands."""

        with self._lock:
            transport = self._transport
        getter = getattr(transport, "user_agent", "") if transport else ""
        return str(getter or "")

    @property
    def connected(self) -> bool:
        with self._lock:
            transport = self._transport
            return transport is not None and bool(transport.initialized)

    def acquire(
        self,
        client: Any,
        binary: str | None = None,
    ) -> "CodexAppServerHub":
        """Register ``client`` and lazily initialize the shared connection.

        The first acquisition's ``binary`` selects the executable for that
        connection.  Later values are intentionally ignored while it remains
        alive because all clients must share the same server.
        """
        if client is None:
            raise ValueError("Codex App Server client cannot be None")
        key = id(client)
        with self._lifecycle_lock:
            with self._lock:
                existing = self._clients.get(key)
                if existing is not None and existing is not client:
                    raise CodexAppServerStateError("client identity collision")

                transport = self._transport
                if transport is None or not transport.initialized:
                    transport = self._new_transport(binary)
                    try:
                        transport.perform_handshake(
                            dict(_CLIENT_INFO),
                            dict(_CAPABILITIES),
                            timeout=self._handshake_timeout,
                        )
                    except Exception:
                        # Detach before stopping so its exit callback cannot
                        # tear down a newer connection.
                        if self._transport is transport:
                            self._transport = None
                        try:
                            transport.stop(timeout=self._shutdown_timeout)
                        except Exception:
                            pass
                        raise

                self._clients[key] = client
        return self

    @contextmanager
    def credential_update(self) -> Iterator[None]:
        """Exclusively rotate Codex credentials without crossing a live server.

        The lease deliberately spans both the credential-store mutation and
        the caller's forced capability discovery.  It shares ``acquire``'s
        lifecycle lock, so a new GPT session cannot initialize against a
        half-updated account.  Existing sessions fail the update closed rather
        than continuing on a process authenticated as the previous account.

        Helios-managed updates use this lease.  An external ``codex login`` in
        another process cannot participate in this in-process lock; callers
        must still close or restart live GPT sessions after such a change.
        """
        self._lifecycle_lock.acquire()
        try:
            with self._lock:
                if self._clients:
                    raise CodexCredentialUpdateInUseError(
                        "Close all active GPT sessions before changing "
                        "OpenAI credentials."
                    )

                # No client owns this transport.  Detach it before stop so an
                # exit callback cannot mutate a future connection, and clear
                # any orphan routing state before the credential mutation.
                transport = self._transport
                self._transport = None
                self._primary_threads.clear()
                self._thread_clients.clear()
                pending = tuple(self._early)
                self._early.clear()

            for item in pending:
                if item.kind == "request":
                    _reject_unroutable(
                        item.payload,
                        "Codex credentials are being updated",
                    )
            if transport is not None:
                self._stop_transport(transport)

            yield
        finally:
            self._lifecycle_lock.release()

    def call(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Make a synchronous bounded request on the shared connection."""
        return self._active_transport().call(method, params, timeout=timeout)

    def request(
        self,
        method: str,
        params: Any = None,
        *,
        callback: Any = None,
        timeout: float | None = None,
    ) -> Future[Any]:
        """Make an asynchronous request on the shared connection."""
        return self._active_transport().request(
            method,
            params,
            callback=callback,
            timeout=timeout,
        )

    def notify(self, method: str, params: Any = None) -> None:
        """Send a notification over the shared connection."""
        self._active_transport().notify(method, params)

    def bind_thread(self, client: Any, thread_id: str) -> None:
        """Bind a primary App Server thread and drain its early messages."""
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id must be a non-empty string")
        key = id(client)
        with self._lock:
            if self._clients.get(key) is not client:
                raise CodexAppServerStateError("client has not acquired the hub")
            owner = self._thread_clients.get(thread_id)
            if owner is not None and owner != key:
                raise CodexAppServerStateError(
                    f"Codex thread {thread_id!r} is already bound"
                )
            previous = self._primary_threads.get(key)
            if previous is not None and previous != thread_id:
                self._thread_clients.pop(previous, None)
            self._primary_threads[key] = thread_id
            self._thread_clients[thread_id] = key
            deliveries = self._drain_early_locked(key)
        self._deliver_many(client, deliveries)

    def release(self, client: Any) -> None:
        """Unregister a client and stop the connection after the final one.

        Thread unsubscription is deliberately best effort.  It is submitted
        before final shutdown but never makes closing a Helios tab wait on an
        unresponsive App Server.
        """
        key = id(client)
        rejected: list[ServerRequest] = []
        with self._lifecycle_lock:
            with self._lock:
                if self._clients.get(key) is not client:
                    return
                owned = {
                    thread_id
                    for thread_id, owner in self._thread_clients.items()
                    if owner == key
                }
                for thread_id in owned:
                    self._thread_clients.pop(thread_id, None)
                self._primary_threads.pop(key, None)
                del self._clients[key]

                retained: deque[_BufferedInbound] = deque()
                for item in self._early:
                    if item.route_thread_id in owned:
                        if item.kind == "request":
                            rejected.append(item.payload)
                    else:
                        retained.append(item)
                self._early = retained

                transport = self._transport
                final = not self._clients
                if final:
                    self._transport = None

            for request in rejected:
                _reject_unroutable(request, "Helios session closed")
            if transport is None:
                return
            for thread_id in sorted(owned):
                try:
                    transport.request(
                        "thread/unsubscribe",
                        {"threadId": thread_id},
                        timeout=min(0.5, max(0.001, self._shutdown_timeout)),
                    )
                except Exception:
                    pass
            if final:
                self._stop_transport(transport)

    def shutdown(self) -> None:
        """Boundedly stop the current shared connection exactly once."""
        with self._lifecycle_lock:
            with self._lock:
                transport = self._transport
                self._transport = None
                clients = tuple(self._clients.values())
                self._clients.clear()
                self._primary_threads.clear()
                self._thread_clients.clear()
                pending = tuple(self._early)
                self._early.clear()
            for item in pending:
                if item.kind == "request":
                    _reject_unroutable(item.payload, "Helios is shutting down")
            if transport is not None:
                self._stop_transport(transport)
            # Retain the local reference until stop has finished.  This makes
            # client finalizers unable to disappear midway through close.
            del clients

    def abort_transport(self, *, returncode: int = -1) -> None:
        """Force-stop the shared transport and invalidate every live client.

        Unlike application-level :meth:`shutdown`, this is a safety breaker:
        clients must be told that their shared execution substrate disappeared
        so none can remain visibly idle while retaining a dead binding.  The
        transport is detached before ``stop`` so its own exit callback cannot
        double-deliver the synthetic failure.
        """

        with self._lifecycle_lock:
            with self._lock:
                transport = self._transport
                self._transport = None
                clients = tuple(self._clients.values())
                self._clients.clear()
                self._primary_threads.clear()
                self._thread_clients.clear()
                pending = tuple(self._early)
                self._early.clear()
            for item in pending:
                if item.kind == "request":
                    _reject_unroutable(
                        item.payload,
                        "Codex App Server was stopped by a safety breaker",
                    )
            if transport is not None:
                self._stop_transport(transport)

        for client in clients:
            _safe_callback(client, "on_app_exit", returncode)

    # ── transport setup and lifecycle ────────────────────────────────

    def _new_transport(self, binary: str | None) -> _Transport:
        transport: _Transport
        transport = self._transport_factory(
            binary,
            on_notification=lambda method, params: self._on_notification(
                transport, method, params
            ),
            on_request=lambda request: self._on_request(transport, request),
            on_exit=lambda code: self._on_exit(transport, code),
        )
        self._transport = transport
        return transport

    def _active_transport(self) -> _Transport:
        with self._lock:
            transport = self._transport
            if transport is None or not transport.initialized:
                raise CodexAppServerStateError(
                    "no initialized Codex App Server connection"
                )
            return transport

    def _stop_transport(self, transport: _Transport) -> None:
        try:
            transport.stop(timeout=self._shutdown_timeout)
        except Exception:
            pass

    def _on_exit(self, transport: _Transport, returncode: int | None) -> None:
        with self._lock:
            if self._transport is not transport:
                return
            self._transport = None
            clients = tuple(self._clients.values())
            # A transport exit invalidates every subscription. Drivers close
            # themselves from the broadcast below; retaining them here would
            # keep dead clients in the refcount and prevent a replacement
            # connection from stopping after its final live client releases.
            self._clients.clear()
            self._primary_threads.clear()
            self._thread_clients.clear()
            self._early.clear()
        for client in clients:
            _safe_callback(client, "on_app_exit", returncode)

    # ── inbound routing ──────────────────────────────────────────────

    def _on_notification(
        self,
        transport: _Transport,
        method: str,
        params: Any,
    ) -> None:
        with self._lock:
            if self._transport is not transport:
                return
            routing = _notification_routing(method, params)
            if routing is None:
                clients = tuple(self._clients.values())
                delivery = ("notification", method, params)
            else:
                route_thread_id, child_thread_id = routing
                key = self._thread_clients.get(route_thread_id)
                if key is None:
                    self._buffer_locked(
                        _BufferedInbound(
                            route_thread_id,
                            "notification",
                            params,
                            method,
                        )
                    )
                    return
                if child_thread_id is not None:
                    owner = self._thread_clients.get(child_thread_id)
                    if owner is None or owner == key:
                        self._thread_clients[child_thread_id] = key
                    else:
                        return
                client = self._clients.get(key)
                clients = (client,) if client is not None else ()
                delivery = ("notification", method, params)
        for client in clients:
            self._deliver(client, delivery)

    def _on_request(self, transport: _Transport, request: ServerRequest) -> None:
        params = request.params
        with self._lock:
            if self._transport is not transport:
                reject = True
                clients: tuple[Any, ...] = ()
            else:
                thread_id = _thread_id(params)
                if thread_id is None:
                    # Connection-level requests must have one responder, not
                    # one dialog per tab.  The oldest acquired client owns it.
                    first = next(iter(self._clients.values()), None)
                    clients = (first,) if first is not None else ()
                    reject = first is None
                else:
                    key = self._thread_clients.get(thread_id)
                    if key is None:
                        self._buffer_locked(
                            _BufferedInbound(
                                thread_id,
                                "request",
                                request,
                                request.method,
                            )
                        )
                        return
                    client = self._clients.get(key)
                    clients = (client,) if client is not None else ()
                    reject = client is None
        if reject:
            _reject_unroutable(request, "No Helios client owns this request")
            return
        for client in clients:
            self._deliver(client, ("request", request.method, request))

    def _buffer_locked(self, item: _BufferedInbound) -> None:
        evicted: _BufferedInbound | None = None
        if len(self._early) >= self._early_buffer_limit:
            evicted = self._early.popleft()
        self._early.append(item)
        if evicted is not None and evicted.kind == "request":
            # ``respond_error`` is one bounded JSONL write.  It is safe here;
            # transport callbacks never acquire the hub lock on that path.
            _reject_unroutable(
                evicted.payload,
                "Thread was not bound before the Helios routing buffer filled",
            )

    def _drain_early_locked(self, key: int) -> list[tuple[str, str, Any]]:
        deliveries: list[tuple[str, str, Any]] = []
        retained: deque[_BufferedInbound] = deque()
        for item in self._early:
            if self._thread_clients.get(item.route_thread_id) != key:
                retained.append(item)
                continue
            if item.kind == "notification":
                routing = _notification_routing(item.method, item.payload)
                if routing is not None and routing[1] is not None:
                    child_id = routing[1]
                    owner = self._thread_clients.get(child_id)
                    if owner is None or owner == key:
                        self._thread_clients[child_id] = key
                    else:
                        continue
            deliveries.append((item.kind, item.method, item.payload))
        self._early = retained
        return deliveries

    @staticmethod
    def _deliver_many(
        client: Any,
        deliveries: list[tuple[str, str, Any]],
    ) -> None:
        for delivery in deliveries:
            CodexAppServerHub._deliver(client, delivery)

    @staticmethod
    def _deliver(client: Any, delivery: tuple[str, str, Any]) -> None:
        kind, method, payload = delivery
        if kind == "request":
            _safe_callback(client, "on_app_request", payload)
        else:
            _safe_callback(client, "on_app_notification", method, payload)


def _notification_routing(method: str, params: Any) -> tuple[str, str | None] | None:
    """Return ``(routing id, optional child id)`` for a scoped event."""
    if method == "thread/started" and isinstance(params, dict):
        thread = params.get("thread")
        if isinstance(thread, dict):
            child_id = _nonempty_string(thread.get("id"))
            parent_id = _nonempty_string(thread.get("parentThreadId"))
            if parent_id is None:
                parent_id = _nonempty_string(params.get("parentThreadId"))
            if child_id is not None:
                return (parent_id or child_id, child_id if parent_id else None)
    thread_id = _thread_id(params)
    if thread_id is None:
        return None
    return (thread_id, None)


def _thread_id(params: Any) -> str | None:
    if not isinstance(params, dict):
        return None
    for key in ("threadId", "conversationId"):
        thread_id = _nonempty_string(params.get(key))
        if thread_id is not None:
            return thread_id
    thread = params.get("thread")
    if isinstance(thread, dict):
        return _nonempty_string(thread.get("id"))
    return None


def _nonempty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_callback(client: Any, name: str, *args: Any) -> None:
    callback = getattr(client, name, None)
    if not callable(callback):
        return
    try:
        callback(*args)
    except Exception:
        # A single tab must never break routing for every other session.
        pass


def _reject_unroutable(request: Any, message: str) -> None:
    try:
        request.respond_error(-32000, message)
    except Exception:
        pass


_shared_hub_lock = threading.Lock()
_shared_hub: CodexAppServerHub | None = None


def get_shared_hub() -> CodexAppServerHub:
    """Return the process-wide shared Codex App Server hub."""
    global _shared_hub
    with _shared_hub_lock:
        if _shared_hub is None:
            _shared_hub = CodexAppServerHub()
        return _shared_hub
