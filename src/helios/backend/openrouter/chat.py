"""OpenRouter chat-completions client — streaming, tools, usage, cancellation.

This is the desktop chat driver's HTTP core: an OpenAI-compatible
``POST /chat/completions`` with SSE streaming, streamed ``tool_calls``
reassembly, and cost capture. It is deliberately separate from the
broker-bound gateway (``gateway.py``): the gateway enforces frozen read-only
delegation contracts, while this client serves full multi-turn chat with
model-invoked tools. Both share ``transport.py`` (no-redirect bearer pinning)
and ``sse.py`` verbatim.

Fail-safe properties carried over from the gateway:

* the bearer header is never forwarded (transport refuses redirects);
* retries happen only before the first content byte — a partial answer is
  never silently restarted;
* every failure is typed (``ChatError.kind``) so the driver can decide what
  to surface;
* cancellation closes the socket promptly via ``CancellationToken``.

GTK-free; the driver runs it on a worker thread.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from helios.backend.openrouter.gateway import CHAT_COMPLETIONS_URL, CancellationToken
from helios.backend.openrouter.sse import SSEProtocolError, iter_sse_events
from helios.backend.openrouter.transport import (
    HttpRequest,
    HttpTransport,
    TransportFailure,
    TransportFailureKind,
    UrlLibTransport,
)

__all__ = [
    "PROVIDER_ROUTING",
    "ChatCompleted",
    "ChatError",
    "ChatErrorKind",
    "ChatUsage",
    "Done",
    "ReasoningDelta",
    "TextDelta",
    "ToolCallDelta",
    "ToolCallRequest",
    "reasoning_for_effort",
    "stream_chat",
]

# Routing policy sent with every chat request. A chat session ships file
# contents and shell output off-box, so the defaults are not acceptable here:
#
# * ``data_collection: "deny"`` excludes endpoints that may train on or publish
#   the prompt. OpenRouter's default is "allow".
# * ``require_parameters: True`` excludes endpoints that would silently ignore
#   ``tools``. Without it such an endpoint accepts the request and answers in
#   prose, which the agent loop reads as "the model declined to call the tool"
#   rather than as a routing error.
# * ``allow_fallbacks: False`` stops a mid-request swap to a different
#   provider, which would change quantization and tool-schema tolerance with no
#   signal. (It does not rescue a stream that already emitted tokens anyway.)
#
# The ``only``/``order`` pin is supplied per request by the caller from
# ``routes.route_for`` — see ``_build_body``. Without a pin OpenRouter
# load-balances across the *filtered* pool weighted by inverse price squared,
# re-choosing on every request, which changes context length and quantization
# mid-session and discards the upstream prompt cache each time it moves. A
# request with no resolvable route still goes out unpinned rather than failing.
#
# Zero-retention is deliberately *not* forced per-request: the account-level ZDR
# setting composes additively over every request, including these, and it is the
# right place to set it (a per-request ``zdr: True`` cannot relax it, and
# forcing it here would 503 every model with no ZDR endpoint).
PROVIDER_ROUTING: dict = {
    "data_collection": "deny",
    "require_parameters": True,
    "allow_fallbacks": False,
}

# Helios effort keys mapped onto OpenRouter's unified ``reasoning`` parameter.
# OpenRouter exposes only low/medium/high, so the two keys above "high" map to
# "high" rather than being silently dropped — and "off" disables reasoning
# explicitly instead of leaving the provider default in place.
_REASONING_FOR_EFFORT: dict[str, dict] = {
    "off": {"enabled": False},
    "low": {"effort": "low"},
    "medium": {"effort": "medium"},
    "high": {"effort": "high"},
    "xhigh": {"effort": "high"},
    "ultracode": {"effort": "high"},
}


def reasoning_for_effort(effort_key: str) -> dict | None:
    """Wire ``reasoning`` object for a Helios effort key, or None to omit it."""
    return _REASONING_FOR_EFFORT.get(effort_key)


# One SSE event carries a whole streamed JSON chunk; tool-call arguments for a
# large Write can legitimately be megabytes. Bounds stay mandatory (sse.py).
_MAX_EVENT_BYTES = 16 * 1024 * 1024
_MAX_STREAM_BYTES = 256 * 1024 * 1024
_MAX_ERROR_MESSAGE = 300

_DEFAULT_OPEN_TIMEOUT = 30.0
_DEFAULT_READ_TIMEOUT = 120.0  # per socket op; OR keepalive comments reset it
_DEFAULT_WALL_SECONDS = 1800.0
_DEFAULT_MAX_ATTEMPTS = 3


class ChatErrorKind(StrEnum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    PAYMENT_REQUIRED = "payment-required"
    RATE_LIMIT = "rate-limit"
    PROVIDER_UNAVAILABLE = "provider-unavailable"
    MODEL_UNAVAILABLE = "model-unavailable"
    CONTEXT_LENGTH = "context-length"
    NO_ELIGIBLE_ENDPOINT = "no-eligible-endpoint"
    HTTP = "http"
    PROTOCOL = "protocol"


class ChatError(Exception):
    """Typed chat failure. ``message`` is user-presentable (capped)."""

    def __init__(
        self,
        kind: ChatErrorKind,
        message: str = "",
        *,
        status: int | None = None,
        retryable: bool = False,
        response_started: bool = False,
    ) -> None:
        super().__init__(message or kind.value)
        self.kind = kind
        self.message = (message or "")[:_MAX_ERROR_MESSAGE]
        self.status = status
        self.retryable = retryable
        # Includes provider output buffered until Done (reasoning details and
        # tool-call fragments), which may have no display delta or response id.
        # Such a failure cannot prove rejection or authorize a replay.
        self.response_started = response_started


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """One completed model tool call (streamed fragments reassembled)."""

    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class ChatUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None
    # Distinguish a legitimate zero-token receipt from the fail-open historical
    # default used when no valid usage object arrived in the stream.
    reported: bool = False


@dataclass(frozen=True, slots=True)
class ChatCompleted:
    finish_reason: str
    usage: ChatUsage
    response_id: str = ""
    #: Reassembled ``reasoning_details`` for this completion, in the order the
    #: model produced them. Must be replayed verbatim on the assistant message
    #: when the turn continues through tool results — OpenRouter documents that
    #: "the entire sequence of consecutive reasoning blocks must match the
    #: outputs generated by the model during the original request", because
    #: "when tool results are returned, the model will continue building that
    #: existing response".
    reasoning_details: tuple[dict, ...] = ()


# ── stream events ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    call: ToolCallRequest


@dataclass(frozen=True, slots=True)
class Done:
    completion: ChatCompleted


# ── public entry point ─────────────────────────────────────────────────────


def stream_chat(
    messages: Sequence[Mapping],
    *,
    model: str,
    api_key: str,
    tools: Sequence[Mapping] = (),
    allow_tool_calls: bool = True,
    provider_only: str = "",
    cache_prefix: bool = False,
    reasoning: Mapping | None = None,
    max_tokens: int = 0,
    cancellation: CancellationToken | None = None,
    on_response_accepted: Callable[[str], None] | None = None,
    transport: HttpTransport | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    open_timeout: float = _DEFAULT_OPEN_TIMEOUT,
    read_timeout: float = _DEFAULT_READ_TIMEOUT,
    wall_seconds: float = _DEFAULT_WALL_SECONDS,
    monotonic=time.monotonic,
) -> Iterator[TextDelta | ReasoningDelta | ToolCallDelta | Done]:
    """Yield stream events for one completion. Raises ChatError.

    Retries (bounded by ``max_attempts``) apply only to failures before the
    first content byte; a partial stream fails immediately.
    """
    token = cancellation or CancellationToken()
    client = transport or UrlLibTransport()
    deadline = monotonic() + wall_seconds
    attempt = 0
    last_error: ChatError | None = None
    while attempt < max_attempts:
        attempt += 1
        started = False
        try:
            for event in _stream_once(
                messages,
                model=model,
                api_key=api_key,
                tools=tools,
                allow_tool_calls=allow_tool_calls,
                provider_only=provider_only,
                cache_prefix=cache_prefix,
                reasoning=reasoning,
                max_tokens=max_tokens,
                token=token,
                on_response_accepted=on_response_accepted,
                transport=client,
                open_timeout=open_timeout,
                read_timeout=read_timeout,
                deadline=deadline,
                monotonic=monotonic,
            ):
                started = True
                yield event
            return
        except ChatError as e:
            if started or e.response_started or not e.retryable or attempt >= max_attempts:
                raise
            last_error = e
            _sleep_before_retry(
                attempt=attempt,
                token=token,
                deadline=deadline,
                monotonic=monotonic,
                status=e.status,
            )
    assert last_error is not None  # loop only exits via raise/return
    raise last_error


# ── internals ──────────────────────────────────────────────────────────────


def _build_body(
    messages: Sequence[Mapping],
    *,
    model: str,
    tools: Sequence[Mapping],
    allow_tool_calls: bool = True,
    provider_only: str = "",
    cache_prefix: bool = False,
    reasoning: Mapping | None = None,
    max_tokens: int = 0,
) -> bytes:
    provider = dict(PROVIDER_ROUTING)
    if provider_only:
        # Pin for the life of the session. Without this OpenRouter re-picks an
        # endpoint per request weighted by inverse price squared, which changes
        # context length and quantization mid-conversation and discards the
        # upstream prompt cache each time it moves.
        provider["only"] = [provider_only]
        provider["order"] = [provider_only]
    body: dict = {
        "model": model,
        "messages": _with_cache_control(messages) if cache_prefix else list(messages),
        "stream": True,
        "stream_options": {"include_usage": True},
        "usage": {"include": True},  # OpenRouter cost extension
        "provider": provider,
    }
    if tools:
        body["tools"] = list(tools)
        body["tool_choice"] = "auto" if allow_tool_calls else "none"
    if reasoning:
        body["reasoning"] = dict(reasoning)
    if max_tokens > 0:
        # The caller reserved this much of the window for the reply, so cap the
        # reply at what was reserved. Without it the endpoint's own ceiling
        # applies — 393,216 tokens on the endpoint measured 2026-09-03 — and a
        # runaway completion is both unbudgeted and larger than the space held
        # for it.
        body["max_tokens"] = int(max_tokens)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _with_cache_control(messages: Sequence[Mapping]) -> list[dict]:
    """Mark the system prefix cacheable for providers that need it explicitly.

    Only the leading system message is marked. It is the one span guaranteed
    byte-stable across every turn of a session; the conversation below it grows,
    and a breakpoint that moves invalidates more than it saves.

    Applied only when the resolved endpoint's vendor requires explicit
    breakpoints (Anthropic, Google, Qwen). Providers that cache implicitly need
    no markup, and sending a content-parts array to an endpoint that only
    accepts a plain string is a 400 — so this is opt-in per route, never blanket.
    """
    out = [dict(m) for m in messages]
    for message in out:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content:
            break
        message["content"] = [{
            "type": "text",
            "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
        break
    return out


def _stream_once(
    messages: Sequence[Mapping],
    *,
    model: str,
    api_key: str,
    tools: Sequence[Mapping],
    allow_tool_calls: bool = True,
    provider_only: str,
    cache_prefix: bool,
    reasoning: Mapping | None,
    max_tokens: int,
    token: CancellationToken,
    on_response_accepted: Callable[[str], None] | None,
    transport: HttpTransport,
    open_timeout: float,
    read_timeout: float,
    deadline: float,
    monotonic,
) -> Iterator[TextDelta | ReasoningDelta | ToolCallDelta | Done]:
    request = HttpRequest(
        method="POST",
        url=CHAT_COMPLETIONS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Title": "Helios",
        },
        body=_build_body(messages, model=model, tools=tools,
                         allow_tool_calls=allow_tool_calls,
                         provider_only=provider_only, cache_prefix=cache_prefix,
                         reasoning=reasoning, max_tokens=max_tokens),
    )
    try:
        response = transport.open(request, timeout_seconds=open_timeout)
    except TransportFailure as e:
        raise _from_transport_failure(e) from e
    remove_callback = token.add_callback(lambda: _safe_close(response))
    try:
        if response.status != 200:
            raise _error_from_response(response)
        yield from _consume_stream(
            response.iter_bytes(),
            token=token,
            on_response_accepted=on_response_accepted,
            deadline=deadline,
            monotonic=monotonic,
        )
    finally:
        if callable(remove_callback):
            remove_callback()
        _safe_close(response)


def _consume_stream(
    chunks,
    *,
    token: CancellationToken,
    on_response_accepted: Callable[[str], None] | None = None,
    deadline: float,
    monotonic,
) -> Iterator[TextDelta | ReasoningDelta | ToolCallDelta | Done]:
    tool_buffers: dict[int, dict] = {}
    reasoning_buffers: dict[object, dict] = {}
    unindexed_reasoning: list[int] = []
    finish_reason = ""
    response_id = ""
    acceptance_reported = False
    response_started = False
    usage = ChatUsage()
    try:
        events = iter_sse_events(
            chunks,
            max_event_bytes=_MAX_EVENT_BYTES,
            max_stream_bytes=_MAX_STREAM_BYTES,
        )
        for event in events:
            if token.cancelled:
                raise ChatError(ChatErrorKind.CANCELLED, "cancelled")
            if monotonic() >= deadline:
                raise ChatError(ChatErrorKind.TIMEOUT, "deadline exceeded")
            if event.data == "[DONE]":
                break
            try:
                chunk = json.loads(event.data)
            except json.JSONDecodeError as e:
                raise ChatError(ChatErrorKind.PROTOCOL, "unparseable stream chunk") from e
            if not isinstance(chunk, dict):
                continue
            chunk_response_id = chunk.get("id")
            if isinstance(chunk_response_id, str) and chunk_response_id:
                if response_id and response_id != chunk_response_id:
                    raise ChatError(
                        ChatErrorKind.PROTOCOL,
                        "response identity changed during stream",
                    )
                response_id = chunk_response_id
                response_started = True
                if not acceptance_reported:
                    if on_response_accepted is not None:
                        on_response_accepted(response_id)
                    acceptance_reported = True
            chunk_usage = _parse_usage(chunk.get("usage"))
            if chunk_usage is not None:
                usage = chunk_usage
                # A valid token receipt also proves processing, even if the
                # provider omitted output deltas and a response identity.
                response_started = True
            choices = chunk.get("choices")
            if not isinstance(choices, list):
                choices = []
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        response_started = True
                        yield TextDelta(text)
                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        response_started = True
                        yield ReasoningDelta(reasoning)
                    details = delta.get("reasoning_details")
                    if isinstance(details, list):
                        for raw_detail in details:
                            _accumulate_reasoning_detail(
                                reasoning_buffers, raw_detail, unindexed_reasoning
                            )
                        response_started = response_started or bool(reasoning_buffers)
                    tool_calls = delta.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for raw_call in tool_calls:
                            _accumulate_tool_call(tool_buffers, raw_call)
                        response_started = response_started or any(
                            buffered["id"] or buffered["name"] or buffered["args"]
                            for buffered in tool_buffers.values()
                        )
                reason = choice.get("finish_reason")
                if isinstance(reason, str) and reason:
                    finish_reason = reason
                    response_started = True
            # Some error chunks also carry the last output delta. Preserve
            # that progress before classifying their failure as a rejection.
            wire_error = chunk.get("error")
            if isinstance(wire_error, dict):
                raise _error_from_wire(wire_error)
    except ChatError as e:
        e.response_started = e.response_started or response_started
        raise
    except TransportFailure as e:
        error = _from_transport_failure(e)
        error.response_started = response_started
        raise error from e
    except SSEProtocolError as e:
        raise ChatError(
            ChatErrorKind.PROTOCOL, "malformed event stream",
            response_started=response_started,
        ) from e
    if not finish_reason:
        # EOF or [DONE] without a provider finish reason is not a terminal
        # receipt. The request may have executed even though the tail of the
        # stream was lost, so callers must retain its execution slot rather
        # than reporting a failed/completed turn and replaying the prompt.
        raise ChatError(
            ChatErrorKind.PROTOCOL,
            "stream ended without a terminal finish reason",
            response_started=response_started,
        )
    for index in sorted(tool_buffers):
        buffered = tool_buffers[index]
        arguments = "".join(buffered["args"])
        if not buffered["id"] and not buffered["name"] and not arguments:
            continue
        yield ToolCallDelta(ToolCallRequest(
            id=buffered["id"] or f"call_{index}",
            name=buffered["name"],
            arguments_json=arguments,
        ))
    yield Done(
        ChatCompleted(
            finish_reason=finish_reason,
            usage=usage,
            response_id=response_id,
            # Insertion order, which is arrival order, which is the order the
            # provider emitted them in. Sorting would invent an ordering for
            # the unindexed case.
            reasoning_details=tuple(reasoning_buffers.values()),
        )
    )


def _accumulate_tool_call(buffers: dict[int, dict], raw_call: object) -> None:
    if not isinstance(raw_call, dict):
        return
    index = raw_call.get("index")
    if type(index) is not int or index < 0 or index > 64:
        return
    buffered = buffers.setdefault(index, {"id": "", "name": "", "args": []})
    call_id = raw_call.get("id")
    if isinstance(call_id, str) and call_id:
        buffered["id"] = call_id
    function = raw_call.get("function")
    if not isinstance(function, dict):
        return
    name = function.get("name")
    if isinstance(name, str) and name:
        buffered["name"] += name
    arguments = function.get("arguments")
    if isinstance(arguments, str) and arguments:
        buffered["args"].append(arguments)


#: Fields that arrive fragmented across chunks and must be concatenated. Every
#: OTHER field is carried through verbatim from the fragment that first
#: supplied it — see ``_accumulate_reasoning_detail``.
_REASONING_TEXT_FIELDS = ("text", "summary", "data")

#: Same bound as the tool-call buffer: a hostile or broken stream must not be
#: able to allocate an unbounded number of buffers from index values alone.
_MAX_REASONING_INDEX = 64


def _accumulate_reasoning_detail(
    buffers: dict[object, dict], raw: object, unindexed: list[int]
) -> None:
    """Merge one streamed ``reasoning_details`` fragment into its slot.

    Every field is preserved, including ``index`` and anything this build has
    never seen: the block has to go back to the provider matching what the
    model produced, and a whitelist cannot be right about a field it does not
    know exists. Measured 2026-09-03 against the live API, a non-streamed
    response returns ``index`` on every block and ``id``/``data`` on OpenAI's
    encrypted ones, so dropping unlisted fields made the replay demonstrably
    not the "verbatim" the code claimed.

    Only the text-bearing fields are concatenated across fragments; metadata
    is taken from the first fragment at a slot and never overwritten, so a
    later empty ``format`` cannot erase a real one.

    A fragment with no usable ``index`` gets its own slot rather than being
    merged into slot 0 — two blocks that cannot be shown to belong together
    must not be joined.
    """
    if not isinstance(raw, dict):
        return
    index = raw.get("index")
    if type(index) is int and 0 <= index <= _MAX_REASONING_INDEX:
        slot: object = ("i", index)
    elif index is None:
        unindexed.append(len(unindexed))
        slot = ("u", unindexed[-1])
    else:
        # An out-of-range or non-integer index is a stream we do not
        # understand; buffering it would let a peer allocate without bound.
        return
    buffered = buffers.get(slot)
    if buffered is None:
        buffers[slot] = dict(raw)
        return
    for field, value in raw.items():
        if field in _REASONING_TEXT_FIELDS and isinstance(value, str) and value:
            existing = buffered.get(field)
            buffered[field] = (existing if isinstance(existing, str) else "") + value
        elif field not in buffered or buffered.get(field) in (None, ""):
            buffered[field] = value


def _parse_usage(raw: object) -> ChatUsage | None:
    if not isinstance(raw, dict):
        return None

    prompt_tokens = raw.get("prompt_tokens")
    completion_tokens = raw.get("completion_tokens")
    if (
        type(prompt_tokens) is not int
        or prompt_tokens <= 0
        or type(completion_tokens) is not int
        or completion_tokens < 0
    ):
        # A partially present receipt cannot drive a hard lifetime breaker.
        # Prompt tokens cannot be zero for Helios's non-empty system/history
        # request, so a shaped zero receipt is unverified too.
        return None

    def _optional_int(value: object) -> int:
        return value if type(value) is int and value >= 0 else 0

    details = raw.get("prompt_tokens_details")
    cached = (
        _optional_int(details.get("cached_tokens"))
        if isinstance(details, dict)
        else 0
    )
    cost = raw.get("cost")
    if type(cost) not in (int, float) or cost < 0:
        cost = None
    return ChatUsage(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cached_tokens=cached,
        cost_usd=float(cost) if cost is not None else None,
        reported=True,
    )


# ── error mapping ──────────────────────────────────────────────────────────


def _from_transport_failure(exc: TransportFailure) -> ChatError:
    if exc.kind is TransportFailureKind.TIMEOUT:
        return ChatError(ChatErrorKind.TIMEOUT, "connection timed out", retryable=True)
    return ChatError(ChatErrorKind.CONNECTION, "could not reach OpenRouter", retryable=True)


def _error_from_response(response) -> ChatError:
    body = b""
    try:
        body = b"".join(response.iter_bytes())
    except TransportFailure:
        pass
    message, code = _parse_error_payload(body)
    return _classify(response.status, code, message)


def _error_from_wire(wire_error: Mapping) -> ChatError:
    message = wire_error.get("message")
    code = wire_error.get("code")
    if type(code) is str and code.isdigit():
        code = int(code)
    return _classify(
        code if type(code) is int else None,
        code,
        message if isinstance(message, str) else "",
    )


def _parse_error_payload(body: bytes) -> tuple[str, int | None]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", None
    if not isinstance(payload, dict):
        return "", None
    error = payload.get("error")
    if not isinstance(error, dict):
        return "", None
    message = error.get("message")
    code = error.get("code")
    if type(code) is str and code.isdigit():
        code = int(code)
    return (
        message if isinstance(message, str) else "",
        code if type(code) is int else None,
    )


def _classify(status: int | None, code: object, message: str) -> ChatError:
    lowered = message.lower()
    # Distinct from MODEL_UNAVAILABLE: the model exists, but no endpoint
    # satisfies the provider policy we sent — typically a pin to a provider
    # excluded by data_collection="deny". The caller can recover by re-routing,
    # so it must not look like "this model is gone".
    if "no endpoints found" in lowered or "data policy" in lowered:
        return ChatError(ChatErrorKind.NO_ELIGIBLE_ENDPOINT, message, status=status)
    if status == 401 or status == 403:
        return ChatError(ChatErrorKind.AUTHENTICATION, message, status=status)
    if status == 402:
        return ChatError(ChatErrorKind.PAYMENT_REQUIRED, message, status=status)
    if status == 404:
        return ChatError(ChatErrorKind.MODEL_UNAVAILABLE, message, status=status)
    if "context" in lowered and ("length" in lowered or "window" in lowered or "token" in lowered):
        return ChatError(ChatErrorKind.CONTEXT_LENGTH, message, status=status)
    if status == 429:
        return ChatError(ChatErrorKind.RATE_LIMIT, message, status=status, retryable=True)
    if status == 408:
        return ChatError(ChatErrorKind.TIMEOUT, message, status=status, retryable=True)
    if status in {500, 502, 503, 504, 529}:
        return ChatError(ChatErrorKind.PROVIDER_UNAVAILABLE, message, status=status, retryable=True)
    return ChatError(ChatErrorKind.HTTP, message or f"HTTP {status}", status=status)


def _sleep_before_retry(
    *,
    attempt: int,
    token: CancellationToken,
    deadline: float,
    monotonic,
    status: int | None,
) -> None:
    del status  # Retry-After already consumed by the transport layer timing
    base = min(2.0, 0.25 * (2 ** (attempt - 1)))
    remaining = deadline - monotonic()
    if remaining <= base:
        raise ChatError(ChatErrorKind.TIMEOUT, "retry budget exhausted")
    if token.wait(base):
        raise ChatError(ChatErrorKind.CANCELLED, "cancelled")


def _safe_close(response) -> None:
    try:
        response.close()
    except Exception:  # noqa: BLE001 — close must never mask the real error
        pass
