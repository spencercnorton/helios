from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from helios.backend.openrouter import chat as or_chat
from helios.backend.openrouter.gateway import CancellationToken
from helios.backend.openrouter.transport import (
    HttpRequest,
    TransportFailure,
    TransportFailureKind,
    UrlLibTransport,
)


def test_transport_does_not_follow_redirect_or_forward_authorization():
    sink_hits: list[str | None] = []

    class Sink(BaseHTTPRequestHandler):
        def do_POST(self):
            sink_hits.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    sink_thread.start()

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(307)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{sink.server_port}/capture",
            )
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    try:
        response = UrlLibTransport().open(
            HttpRequest(
                method="POST",
                url=f"http://127.0.0.1:{redirect.server_port}/redirect",
                headers={"Authorization": "Bearer fixture-secret"},
                body=b"{}",
            ),
            timeout_seconds=2,
        )
        assert response.status == 307
        response.close()
        assert sink_hits == []
    finally:
        redirect.shutdown()
        sink.shutdown()
        redirect.server_close()
        sink.server_close()


def _chat_sse(*payloads: str) -> list[bytes]:
    return ["".join(f"data: {payload}\n\n" for payload in payloads).encode()]


@pytest.mark.parametrize("allow_tools", [True, False])
def test_tool_choice_reaches_the_http_body(allow_tools):
    requests = []

    class Response:
        status = 200

        def iter_bytes(self):
            yield from _chat_sse(
                '{"choices":[{"delta":{"content":"handoff"},"finish_reason":"stop"}]}',
                "[DONE]",
            )

        def close(self):
            pass

    class Transport:
        def open(self, request, **_kwargs):
            requests.append(json.loads(request.body))
            return Response()

    tools = [{"type": "function", "function": {"name": "Read", "parameters": {"type": "object"}}}]
    list(or_chat.stream_chat(
        [{"role": "user", "content": "finish"}], model="test/model", api_key="fixture",
        tools=tools, allow_tool_calls=allow_tools, max_tokens=4096, transport=Transport(),
    ))
    assert requests[0]["tool_choice"] == ("auto" if allow_tools else "none")
    assert requests[0]["tools"] == tools
    assert requests[0]["max_tokens"] == 4096


def test_chat_stream_reports_provider_response_identity_before_content():
    accepted: list[str] = []
    chunks = _chat_sse(
        '{"id":"resp_1","choices":[{"delta":{"content":"ok"},'
        '"finish_reason":"stop"}],"usage":{"prompt_tokens":5,'
        '"completion_tokens":1}}',
        "[DONE]",
    )

    events = list(
        or_chat._consume_stream(
            chunks,
            token=CancellationToken(),
            on_response_accepted=accepted.append,
            deadline=100.0,
            monotonic=lambda: 0.0,
        )
    )

    assert accepted == ["resp_1"]
    assert isinstance(events[0], or_chat.TextDelta)
    assert events[0].text == "ok"
    assert isinstance(events[-1], or_chat.Done)
    assert events[-1].completion.response_id == "resp_1"
    assert events[-1].completion.finish_reason == "stop"


def test_chat_stream_eof_without_finish_receipt_remains_ambiguous():
    accepted: list[str] = []
    chunks = _chat_sse(
        '{"id":"resp_partial","choices":[{"delta":{"content":"partial"},'
        '"finish_reason":null}]}'
    )

    with pytest.raises(or_chat.ChatError) as raised:
        list(
            or_chat._consume_stream(
                chunks,
                token=CancellationToken(),
                on_response_accepted=accepted.append,
                deadline=100.0,
                monotonic=lambda: 0.0,
            )
        )

    assert raised.value.kind is or_chat.ChatErrorKind.PROTOCOL
    assert "without a terminal finish reason" in raised.value.message
    assert accepted == ["resp_partial"]


@pytest.mark.parametrize("progress", [
    pytest.param({"id": "response-started", "choices": []}, id="response-id"),
    pytest.param({"usage": {"prompt_tokens": 10, "completion_tokens": 2}}, id="usage-only"),
    pytest.param({"usage": {"prompt_tokens": 10, "completion_tokens": 0}}, id="input-usage-only"),
    pytest.param({"choices": [{"delta": {"content": "partial"}}]}, id="text"),
    pytest.param({"choices": [{"delta": {"reasoning": "working"}}]}, id="reasoning"),
    pytest.param({"choices": [{"delta": {"reasoning_details": [
        {"type": "reasoning.encrypted", "data": "opaque", "index": 0},
    ]}}]}, id="buffered-reasoning"),
    pytest.param({"choices": [{"delta": {"reasoning_details": [
        {"type": "reasoning.encrypted", "data": "opaque"},
    ]}}]}, id="unindexed-reasoning"),
    pytest.param({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call-1", "function": {"name": "Read", "arguments": '{"path":'}},
    ]}}]}, id="buffered-tool"),
])
@pytest.mark.parametrize("failure", ["wire-error", "same-chunk-error", "connection", "malformed-json", "eof"])
def test_chat_progress_survives_errors_and_prevents_transport_retries(
    monkeypatch, progress, failure,
):
    requests = []
    accepted = []

    class Response:
        status = 200

        def iter_bytes(self):
            if failure == "same-chunk-error":
                yield from _chat_sse(json.dumps({
                    **progress, "error": {"code": 429, "message": "upstream limit"},
                }))
                return
            yield from _chat_sse(json.dumps(progress))
            if failure == "wire-error":
                yield from _chat_sse('{"error":{"code":429,"message":"upstream limit"}}')
            elif failure == "connection":
                raise TransportFailure(TransportFailureKind.CONNECTION)
            elif failure == "malformed-json":
                yield from _chat_sse("{")

        def close(self):
            pass

    class Transport:
        def open(self, request, **_kwargs):
            requests.append(request)
            return Response()

    monkeypatch.setattr(
        or_chat, "_sleep_before_retry",
        lambda *_args, **_kwargs: pytest.fail("provider progress cannot authorize a retry"),
    )
    with pytest.raises(or_chat.ChatError) as raised:
        list(or_chat.stream_chat(
            [{"role": "user", "content": "go"}], model="vendor/model", api_key="fixture-key",
            transport=Transport(), max_attempts=3, on_response_accepted=accepted.append,
        ))

    assert raised.value.response_started is True
    assert len(requests) == 1
    assert accepted == (["response-started"] if "id" in progress else [])


@pytest.mark.parametrize("empty_delta", [
    {}, {"role": "assistant"}, {"content": "", "reasoning": ""},
    {"reasoning_details": [], "tool_calls": []},
])
def test_chat_preprogress_rate_limit_still_retries(monkeypatch, empty_delta):
    requests = []
    retries = []

    class Response:
        status = 200

        def iter_bytes(self):
            if len(requests) == 1:
                yield from _chat_sse(
                    json.dumps({"choices": [{"delta": empty_delta}]}),
                    '{"error":{"code":429,"message":"upstream limit"}}',
                )
            else:
                yield from _chat_sse(
                    '{"choices":[{"delta":{"content":"recovered"},"finish_reason":"stop"}]}',
                    "[DONE]",
                )

        def close(self):
            pass

    class Transport:
        def open(self, request, **_kwargs):
            requests.append(request)
            return Response()

    monkeypatch.setattr(or_chat, "_sleep_before_retry", lambda *args, **kwargs: retries.append(args))
    events = list(or_chat.stream_chat(
        [{"role": "user", "content": "go"}], model="vendor/model", api_key="fixture-key",
        transport=Transport(), max_attempts=3,
    ))
    assert len(requests) == 2
    assert len(retries) == 1
    assert events[0] == or_chat.TextDelta("recovered")
    assert isinstance(events[-1], or_chat.Done)
