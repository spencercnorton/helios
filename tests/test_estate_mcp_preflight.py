"""Real peer traffic queued while a discovered tool waits for user approval."""
from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

import pytest

from helios.backend.estate_config import ServerConfig
from helios.backend.estate_mcp import EstateMcp, MAX_MESSAGE_BYTES, McpError


@pytest.fixture
def waiting_peer(tmp_path):
    script = tmp_path / "waiting_peer.py"
    script.write_text('''
import json, os, sys, threading, time
from pathlib import Path
mode, root = sys.argv[1], Path(sys.argv[2])
trace = root / "received.jsonl"
changed = {"jsonrpc":"2.0", "method":"notifications/tools/list_changed"}
def encoded(message):
    return (json.dumps(message) + "\\n").encode()
def pending():
    while not (root/"approval-pending").exists():
        time.sleep(0.002)
    if mode == "eof":
        os.close(sys.stdout.fileno())
        (root/"notification-queued").touch()
        return
    value = {
        "changed": encoded(changed),
        "partial": b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"',
        "ping": encoded({"jsonrpc":"2.0","id":"control","method":"ping"}),
        "ping-changed": encoded({"jsonrpc":"2.0","id":"control","method":"ping"}) + encoded(changed),
        "sampling": encoded({"jsonrpc":"2.0","id":"control","method":"sampling/createMessage"}),
        "response": encoded({"jsonrpc":"2.0","id":999,"result":{}}),
        "malformed": b'not valid JSON\\n',
        "invalid-method": encoded({"jsonrpc":"2.0","method":None}),
    }.get(mode, b'')
    os.write(sys.stdout.fileno(), value)
    (root/"notification-queued").touch()
for line in sys.stdin:
    message = json.loads(line)
    with trace.open("a") as output:
        output.write(json.dumps(message) + "\\n")
    method = message.get("method")
    if method is None or "id" not in message:
        continue
    result = {}
    if method == "initialize":
        result = {"protocolVersion":"2025-11-25", "capabilities":{"tools":{"listChanged":True}}}
    elif method == "tools/list":
        result = {"tools":[{"name":"write_artifact","inputSchema":{"type":"object"}}]}
    elif method == "tools/call":
        (root/"tool-effect").touch()
        result = {"content":[{"type":"text","text":"ok"}]}
    wire = encoded({"jsonrpc":"2.0","id":message["id"],"result":result})
    if method == "tools/list" and mode == "buffered":
        wire += encoded(changed)
    os.write(sys.stdout.fileno(), wire)
    if method == "tools/list" and mode not in {"buffered", "quiet"}:
        threading.Thread(target=pending, daemon=True).start()
''')
    runtimes = []

    def create(mode, cancellation=None):
        config = ServerConfig("dispatch", sys.executable, (str(script), mode, str(tmp_path)))
        runtime = EstateMcp(str(tmp_path), configs=[config], cancellation=cancellation)
        runtimes.append(runtime)
        runtime.discover()
        found, error = runtime.call("EstateSearchTools", {"query": "write_artifact"})
        assert not error
        name = json.loads(found)["tools"][0]["function"]["name"]
        return runtime, name, runtime._clients["dispatch"]

    yield create
    for runtime in runtimes:
        runtime.close()


def queue_during_approval(tmp_path, client):
    (tmp_path / "approval-pending").touch()
    deadline = time.monotonic() + 3
    while not (tmp_path / "notification-queued").exists() and time.monotonic() < deadline:
        assert client.proc.poll() is None
        time.sleep(0.005)
    assert (tmp_path / "notification-queued").exists(), "peer did not queue its control message"


def received(tmp_path):
    return [json.loads(line) for line in (tmp_path / "received.jsonl").read_text().splitlines()]


def assert_no_call(tmp_path):
    assert not (tmp_path / "tool-effect").exists()
    assert "tools/call" not in [row.get("method") for row in received(tmp_path)]


def test_changed_notification_queued_during_approval_prevents_any_tool_call(waiting_peer, tmp_path):
    runtime, name, client = waiting_peer("changed")
    queue_during_approval(tmp_path, client)
    assert not client.changed  # No reader has consumed the queued notification.
    result, error = runtime.call("EstateCallTool", {"name": name, "arguments": {}})
    assert error and "inventory changed" in result and "not sent" in result
    assert client.changed and client.proc.poll() is not None
    assert_no_call(tmp_path)


def test_changed_notification_buffered_behind_list_response_prevents_call(waiting_peer, tmp_path):
    runtime, name, client = waiting_peer("buffered")
    assert b"notifications/tools/list_changed" in client._buffer
    assert not client.changed
    result, error = runtime.call("EstateCallTool", {"name": name, "arguments": {}})
    assert error and "inventory changed" in result
    assert_no_call(tmp_path)


@pytest.mark.parametrize("mode, reason", [("partial", "Incomplete"), ("response", "Unexpected"),
                                         ("malformed", "invalid JSON"), ("invalid-method", "invalid method"),
                                         ("eof", "closed its output")])
def test_ambiguous_queued_traffic_fails_closed_before_side_effect(waiting_peer, tmp_path, mode, reason):
    runtime, name, client = waiting_peer(mode)
    queue_during_approval(tmp_path, client)
    result, error = runtime.call("EstateCallTool", {"name": name, "arguments": {}})
    assert error and reason in result
    assert_no_call(tmp_path)


def test_ping_followed_by_changed_notice_allows_only_control_reply(waiting_peer, tmp_path):
    runtime, name, client = waiting_peer("ping-changed")
    queue_during_approval(tmp_path, client)
    result, error = runtime.call("EstateCallTool", {"name": name, "arguments": {}})
    assert error and "inventory changed" in result
    assert any(row.get("id") == "control" and row.get("result") == {} for row in received(tmp_path))
    assert_no_call(tmp_path)


@pytest.mark.parametrize("mode", ["ping", "sampling"])
def test_control_request_receives_protocol_reply_before_tool_request(waiting_peer, tmp_path, mode):
    runtime, name, client = waiting_peer(mode)
    queue_during_approval(tmp_path, client)
    assert runtime.call("EstateCallTool", {"name": name, "arguments": {}}) == ("ok", False)
    messages = received(tmp_path)
    reply_index = next(i for i, row in enumerate(messages) if row.get("id") == "control")
    call_index = next(i for i, row in enumerate(messages) if row.get("method") == "tools/call")
    assert reply_index < call_index
    if mode == "ping":
        assert messages[reply_index]["result"] == {}
    else:
        assert messages[reply_index]["error"]["code"] == -32601


@pytest.mark.parametrize("kind", ["oversized", "flood"])
def test_queued_input_has_byte_and_message_bounds(waiting_peer, tmp_path, kind):
    runtime, name, client = waiting_peer("quiet")
    client._buffer = bytearray(b" " * (MAX_MESSAGE_BYTES + 1) if kind == "oversized" else
                              b'{"jsonrpc":"2.0","method":"notifications/message"}\n' * 1025)
    result, error = runtime.call("EstateCallTool", {"name": name, "arguments": {}})
    assert error and "limit" in result
    assert_no_call(tmp_path)


def test_expired_original_deadline_prevents_preflight_and_call(waiting_peer, tmp_path):
    _runtime, _name, client = waiting_peer("quiet")
    with pytest.raises(McpError, match="deadline"):
        client.request("tools/call", {"name": "write_artifact", "arguments": {}}, timeout=0)
    assert_no_call(tmp_path)


def test_cancellation_after_discovery_prevents_call(waiting_peer, tmp_path):
    token = SimpleNamespace(cancelled=False)
    runtime, name, _client = waiting_peer("quiet", cancellation=token)
    token.cancelled = True
    result, error = runtime.call("EstateCallTool", {"name": name, "arguments": {}})
    assert error and "cancelled" in result
    assert_no_call(tmp_path)
