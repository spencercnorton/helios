"""Minimal injectable HTTP transport for the GTK-free OpenRouter gateway."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Mapping, Protocol


class TransportFailureKind(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"


class TransportFailure(Exception):
    """A content-free transport error safe to surface and retry."""

    def __init__(self, kind: TransportFailureKind):
        self.kind = kind
        super().__init__(f"OpenRouter transport {kind.value}")


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes


class HttpResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def iter_bytes(self) -> Iterable[bytes]: ...

    def close(self) -> None: ...


class HttpTransport(Protocol):
    def open(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse: ...


class OversizedResponse(Exception):
    """The peer sent more than the caller agreed to hold in memory."""


def read_capped(response: HttpResponse, limit: int) -> bytes:
    """Read at most ``limit`` bytes, stopping as soon as the cap is passed.

    Buffering the whole body and *then* checking its length is not a limit —
    by the time the check runs the allocation has already happened, so an
    unbounded or hostile upstream is bounded by nothing. Stop pulling chunks
    the moment the total exceeds the cap and raise, so the caller decides
    what an oversized response means for it.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise OversizedResponse(f"response exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class _UrlLibResponse:
    def __init__(self, response):
        self._response = response
        self.status = int(response.status)
        self.headers = {str(key).lower(): str(value) for key, value in response.headers.items()}

    def iter_bytes(self) -> Iterable[bytes]:
        while True:
            try:
                chunk = self._response.read(8192)
            except (socket.timeout, TimeoutError) as exc:
                raise TransportFailure(TransportFailureKind.TIMEOUT) from exc
            except OSError as exc:
                raise TransportFailure(TransportFailureKind.CONNECTION) from exc
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._response.close()


class _BufferedResponse:
    def __init__(self, status: int, headers: Mapping[str, str], body: bytes):
        self.status = status
        self.headers = {str(key).lower(): str(value) for key, value in headers.items()}
        self._body = body

    def iter_bytes(self) -> Iterable[bytes]:
        if self._body:
            yield self._body

    def close(self) -> None:
        return None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Treat redirects as responses so bearer headers never change origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrlLibTransport:
    """stdlib transport; no OpenRouter SDK or GTK/PyGObject dependency."""

    _MAX_ERROR_BODY = 64 * 1024

    def __init__(self, *, opener=None) -> None:
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler())

    def open(self, request: HttpRequest, *, timeout_seconds: float) -> HttpResponse:
        wire_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            response = self._opener.open(  # noqa: S310 - URL is a fixed gateway URL
                wire_request,
                timeout=timeout_seconds,
            )
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read(self._MAX_ERROR_BODY + 1)[: self._MAX_ERROR_BODY]
            except (OSError, socket.timeout, TimeoutError):
                # Status and headers remain authoritative when an error peer
                # resets before its optional body is fully readable.
                pass
            finally:
                exc.close()
            return _BufferedResponse(exc.code, dict(exc.headers.items()), body)
        except (socket.timeout, TimeoutError) as exc:
            raise TransportFailure(TransportFailureKind.TIMEOUT) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                kind = TransportFailureKind.TIMEOUT
            else:
                kind = TransportFailureKind.CONNECTION
            raise TransportFailure(kind) from exc
        except OSError as exc:
            raise TransportFailure(TransportFailureKind.CONNECTION) from exc
        return _UrlLibResponse(response)
