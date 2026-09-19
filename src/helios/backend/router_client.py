"""Small JSONL client for the local Helios Router broker.

The broker owns the OpenRouter credential.  Native model processes and the
MCP shim receive only this credential-free Unix-socket capability.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1
DEFAULT_SOCKET_PATH = Path("/run/helios-router/router.sock")
SOCKET_ENV = "HELIOS_ROUTER_SOCKET"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: Last known answer to "can the broker dispatch right now?".
#:
#: Callers that decide whether to *advertise* the router to a model need this
#: on the GTK main thread, where a socket round trip is not acceptable — a
#: wedged broker would stall every new session by the client timeout. So the
#: read is a cached bool and the fetch is somebody else's job.
#:
#: It starts False, and that is the honest default rather than a pessimistic
#: one: dispatch additionally requires the preview bit, which only
#: ``set_enabled`` can turn on and which the broker refuses to any caller
#: carrying a native binding. An unwarmed cache therefore cannot be hiding a
#: dispatchable broker that the user reached without Helios noticing.
_dispatch_available = False
_dispatch_lock = threading.Lock()

#: Ordering for concurrent status reads. Two overlap easily — the startup
#: refresh against a settings-dialog reload, or the dialog's own reload against
#: the read it issues right after the preview toggle — and nothing about a
#: socket round trip guarantees they finish in the order they started. Without
#: this, an older `False` landing after a newer `True` leaves the cache wrong
#: until something else asks, which is exactly the toggle case the cache exists
#: to serve. A sequence number is enough: writes are ordered, the network call
#: still happens outside the lock, and no caller has to be serialized.
_dispatch_seq = 0
_dispatch_written_seq = 0


class RouterError(RuntimeError):
    """Base error for broker transport or policy failures."""


class RouterUnavailableError(RouterError):
    """The local broker socket is absent or unreachable."""


class RouterProtocolError(RouterError):
    """The broker returned an invalid or mismatched response."""


class RouterRejectedError(RouterError):
    """The broker understood the command but rejected it."""

    def __init__(self, code: str, message: str, *, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def router_socket_path() -> Path:
    override = os.environ.get(SOCKET_ENV, "").strip()
    return Path(override) if override else DEFAULT_SOCKET_PATH


def dispatch_available() -> bool:
    """Can the broker dispatch? Cached, non-blocking, safe on the UI thread."""
    with _dispatch_lock:
        return _dispatch_available


def issue_status_seq() -> int:
    """Claim an ordering slot for a status read that is about to start."""
    global _dispatch_seq
    with _dispatch_lock:
        _dispatch_seq += 1
        return _dispatch_seq


def note_status(status: object, *, seq: int | None = None) -> bool:
    """Record dispatchability from a status payload someone already fetched.

    Every ``RouterClient.status()`` feeds this, so the settings dialog's
    existing off-thread status reads keep the cache warm for free — including
    immediately after the preview switch is flipped, which is the only way
    dispatch can be turned on at all.

    ``seq`` is the slot claimed by ``issue_status_seq`` before the request
    started. A result that is older than one already applied is dropped rather
    than written, so two overlapping reads cannot leave the cache holding the
    slower one's answer. Passing no ``seq`` means "unordered, always wins",
    which is what a direct reset wants.

    Returns the cache's value after the call, which is not always this
    payload's — a dropped write returns the newer answer that stood.
    """
    value = bool(
        isinstance(status, dict) and status.get("automatic_dispatch") is True
    )
    global _dispatch_available, _dispatch_written_seq
    with _dispatch_lock:
        if seq is not None:
            if seq < _dispatch_written_seq:
                return _dispatch_available
            _dispatch_written_seq = seq
        _dispatch_available = value
        return value


def refresh_dispatch_available() -> bool:
    """Fetch status and update the cache. Blocking — never on the UI thread.

    A convenience for callers that want the bit and not the payload. It records
    nothing itself: ``status()`` already performs the ordered update on both the
    success and the failure path, and writing the payload a second time here
    would do it *unordered* — which always wins, and would therefore reapply
    exactly the stale result the sequence number had just rejected. A router
    that cannot be asked cannot be advertised, and ``status()`` has already said
    so by the time the exception arrives here.
    """
    try:
        RouterClient().status()
    except Exception:  # noqa: BLE001 — status() already failed closed for us
        pass
    return dispatch_available()


class RouterClient:
    """One-request-per-connection client with strict framing and bounds."""

    def __init__(
        self,
        socket_path: str | os.PathLike[str] | None = None,
        *,
        timeout: float = 65.0,
    ) -> None:
        self.socket_path = Path(socket_path) if socket_path else router_socket_path()
        self.timeout = max(0.05, float(timeout))

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        binding: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        request_id = f"req_{uuid.uuid4().hex}"
        request: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if binding:
            request["binding"] = dict(binding)
        payload = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        budget = self.timeout if timeout is None else max(0.05, float(timeout))

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(budget)
                sock.connect(str(self.socket_path))
                sock.sendall(payload)
                raw = _read_line(sock)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
            raise RouterUnavailableError(
                f"Helios Router is unavailable at {self.socket_path}"
            ) from exc

        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RouterProtocolError("Helios Router returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise RouterProtocolError("Helios Router response id did not match")
        if response.get("protocol_version") != PROTOCOL_VERSION:
            raise RouterProtocolError("Helios Router protocol version did not match")
        error = response.get("error")
        if isinstance(error, dict):
            raise RouterRejectedError(
                str(error.get("code") or "ROUTER_REJECTED"),
                str(error.get("message") or "Helios Router rejected the command"),
                data=error.get("data"),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RouterProtocolError("Helios Router returned no result object")
        return result

    def status(self) -> dict[str, Any]:
        """Read broker status. Also refreshes the ``dispatch_available`` cache.

        The side effect is deliberate and is what keeps the cache cheap: every
        caller that already asks the broker how it is doing pays for the answer
        anyway, and all of them are off the UI thread.

        **A failure clears the cache before re-raising.** Updating only on
        success would make the cache fail-*open*: a broker that answered once
        and then died would keep its last "dispatchable" answer indefinitely,
        because the settings dialog's status reads raise rather than return, so
        nothing would ever write False. Putting it here rather than in each
        caller means there is one chokepoint and no consumer can opt out of it
        by forgetting to wrap the call.
        """
        seq = issue_status_seq()
        try:
            result = self.call("status", {"contract_version": 1}, timeout=6.0)
        except Exception:
            note_status(None, seq=seq)
            raise
        note_status(result, seq=seq)
        return result

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        return self.call(
            "set_enabled",
            {"contract_version": 1, "enabled": bool(enabled)},
            timeout=8.0,
        )


def _read_line(sock: socket.socket) -> str:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = sock.recv(min(65_536, _MAX_RESPONSE_BYTES + 1 - size))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
            raise RouterProtocolError("Helios Router response exceeded the size limit")
    raw = b"".join(chunks)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise RouterProtocolError("Helios Router response exceeded the size limit")
    if not raw:
        raise RouterProtocolError("Helios Router closed without a response")
    return raw.decode("utf-8")
