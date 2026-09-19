"""Strict, bounded Server-Sent Events framing with chunk-safe UTF-8 decoding."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Iterable, Iterator


class SSEProtocolError(Exception):
    """An intentionally content-free SSE parse failure."""


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str | None = None
    event_id: str | None = None


def iter_sse_events(
    chunks: Iterable[bytes],
    *,
    max_event_bytes: int,
    max_stream_bytes: int,
) -> Iterator[SSEEvent]:
    """Decode SSE across arbitrary byte boundaries.

    Supports LF, CRLF, CR, comments, multi-line ``data`` fields, partial UTF-8,
    and a final event without a blank-line terminator. Unknown fields are
    ignored per the SSE specification. Invalid UTF-8 or over-limit input fails
    closed without including provider content in the exception.
    """

    if max_event_bytes < 1 or max_stream_bytes < max_event_bytes:
        raise ValueError("invalid SSE byte limits")

    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    line: list[str] = []
    data_lines: list[str] = []
    event_name: str | None = None
    event_id: str | None = None
    pending_cr = False
    stream_bytes = 0
    event_bytes = 0

    def dispatch_line(value: str) -> SSEEvent | None:
        nonlocal data_lines, event_name, event_id, event_bytes
        if value == "":
            if not data_lines:
                event_name = None
                event_id = None
                event_bytes = 0
                return None
            event = SSEEvent(
                data="\n".join(data_lines),
                event=event_name,
                event_id=event_id,
            )
            data_lines = []
            event_name = None
            event_id = None
            event_bytes = 0
            return event
        if value.startswith(":"):
            return None
        field, separator, raw_value = value.partition(":")
        if separator and raw_value.startswith(" "):
            raw_value = raw_value[1:]
        if field == "data":
            event_bytes += len(raw_value.encode("utf-8"))
            if event_bytes > max_event_bytes:
                raise SSEProtocolError("OpenRouter SSE event exceeded the limit")
            data_lines.append(raw_value)
        elif field == "event":
            event_name = raw_value
        elif field == "id" and "\x00" not in raw_value:
            event_id = raw_value
        return None

    def process_text(text: str) -> Iterator[SSEEvent]:
        nonlocal pending_cr
        for character in text:
            if pending_cr:
                pending_cr = False
                if character == "\n":
                    continue
            if character == "\r":
                event = dispatch_line("".join(line))
                line.clear()
                pending_cr = True
                if event is not None:
                    yield event
            elif character == "\n":
                event = dispatch_line("".join(line))
                line.clear()
                if event is not None:
                    yield event
            else:
                line.append(character)
                if len(line) > max_event_bytes:
                    raise SSEProtocolError("OpenRouter SSE line exceeded the limit")

    try:
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise SSEProtocolError("OpenRouter SSE transport returned non-bytes")
            stream_bytes += len(chunk)
            if stream_bytes > max_stream_bytes:
                raise SSEProtocolError("OpenRouter SSE stream exceeded the limit")
            yield from process_text(decoder.decode(chunk, final=False))
        yield from process_text(decoder.decode(b"", final=True))
    except UnicodeDecodeError as exc:
        raise SSEProtocolError("OpenRouter SSE was not valid UTF-8") from exc

    if line:
        event = dispatch_line("".join(line))
        line.clear()
        if event is not None:
            yield event
    event = dispatch_line("")
    if event is not None:
        yield event

