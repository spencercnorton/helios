from __future__ import annotations

import copy
import io
import json
import socket
import threading
import uuid
from pathlib import Path

import pytest

from helios.backend import router_client
from helios.backend.router_client import (
    PROTOCOL_VERSION,
    RouterClient,
    RouterError,
    RouterProtocolError,
    RouterRejectedError,
)
from helios.backend.router_tools import (
    CODEX_NAMESPACE,
    CONTRACT_VERSION,
    claude_mcp_config,
    codex_dynamic_tools,
    mcp_tools,
    tool_names,
)
from helios.router_mcp import RouterMcpServer, serve


@pytest.fixture
def short_socket_path():
    # Darwin's sockaddr_un path cap is only 104 bytes; pytest's normal tmp
    # hierarchy is intentionally descriptive and can exceed it.
    path = Path("/tmp") / f"helios-router-test-{uuid.uuid4().hex[:12]}.sock"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def test_tool_contracts_are_closed_and_provider_neutral():
    tools = mcp_tools()
    assert {tool["name"] for tool in tools} == tool_names()
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        rendered = json.dumps(tool).lower()
        assert "api_key" not in rendered
        assert "model_id" not in rendered
        assert "endpoint" not in rendered
        assert "dollar" not in rendered

    delegate = next(tool for tool in tools if tool["name"] == "delegate_task")
    schema = delegate["inputSchema"]
    assert schema["properties"]["contract_version"]["const"] == CONTRACT_VERSION
    assert "objective" in schema["required"]
    assert schema["properties"]["context_refs"]["maxItems"] == 64


def test_callers_cannot_mutate_the_canonical_schema():
    first = mcp_tools()
    first[0]["inputSchema"]["properties"]["objective"]["maxLength"] = 1
    second = mcp_tools()
    assert second[0]["inputSchema"]["properties"]["objective"]["maxLength"] == 4000


def test_codex_contract_uses_one_non_reserved_namespace():
    rows = codex_dynamic_tools()
    assert len(rows) == 1
    namespace = rows[0]
    assert namespace["type"] == "namespace"
    assert namespace["name"] == CODEX_NAMESPACE
    assert {tool["name"] for tool in namespace["tools"]} == tool_names()
    assert next(
        tool for tool in namespace["tools"] if tool["name"] == "search_specialty_tools"
    )["deferLoading"] is True


def test_claude_mcp_config_contains_only_launcher_binding_and_socket():
    rendered = claude_mcp_config(
        "claude_binding",
        socket_path="/run/helios-router/router.sock",
        launcher_path="/opt/helios-router-mcp",
    )
    config = json.loads(rendered)
    server = config["mcpServers"]["helios-router"]
    assert server == {
        "type": "stdio",
        "command": "/opt/helios-router-mcp",
        "args": [
            "--binding",
            "claude_binding",
            "--socket",
            "/run/helios-router/router.sock",
        ],
    }
    assert "key" not in rendered.lower()
    assert "token" not in rendered.lower()


def test_mcp_adapter_returns_structured_result_with_binding():
    calls = []

    class FakeClient:
        def call(self, method, params, *, binding):
            calls.append((method, params, binding))
            return {"contract_version": 1, "status": "completed"}

    server = RouterMcpServer(FakeClient(), binding_id="claude-1")
    initialized = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    assert initialized["result"]["capabilities"]["tools"]["listChanged"] is False
    assert (
        server.handle(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }
        )
        is None
    )
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {
                "name": "routing_status",
                "arguments": {"contract_version": 1},
            },
        }
    )
    assert calls == [
        (
            "routing_status",
            {"contract_version": 1},
            {
                "kind": "claude",
                "client_binding": "claude-1",
                "call_id": "call-1",
            },
        )
    ]
    assert response["result"]["structuredContent"]["status"] == "completed"
    assert response["result"]["isError"] is False


def test_mcp_enforces_jsonrpc_lifecycle_and_supported_protocol():
    class FakeClient:
        def call(self, *_args, **_kwargs):
            raise AssertionError("client must not be reached")

    server = RouterMcpServer(FakeClient(), binding_id="claude-1")
    invalid = server.handle({"id": 1, "method": "initialize", "params": {}})
    assert invalid["error"]["code"] == -32600

    premature = server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    )
    assert premature["error"]["code"] == -32002

    initialized = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        }
    )
    assert initialized["result"]["protocolVersion"] == "2025-06-18"

    before_notification = server.handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
    )
    assert before_notification["error"]["code"] == -32002
    server.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    listed = server.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
    )
    assert {row["name"] for row in listed["result"]["tools"]} == tool_names()


def test_mcp_serve_bounds_oversized_lines_and_recovers():
    oversized = (
        b'{"jsonrpc":"2.0","id":1,"method":"'
        + b"x" * (2 * 1024 * 1024)
        + b'"}\n'
    )
    valid = (
        b'{"jsonrpc":"2.0","id":2,"method":"initialize",'
        b'"params":{"protocolVersion":"2025-06-18"}}\n'
    )
    source = io.BytesIO(oversized + valid)
    sink = io.BytesIO()
    server = RouterMcpServer(object(), binding_id="claude-1")

    assert serve(server, input_stream=source, output_stream=sink) == 0

    responses = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32600
    assert responses[1]["id"] == 2
    assert responses[1]["result"]["protocolVersion"] == "2025-06-18"


def _serve_once(path, response_for):
    ready = threading.Event()

    def run():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                raw = b""
                while not raw.endswith(b"\n"):
                    raw += conn.recv(65_536)
                request = json.loads(raw)
                response = response_for(copy.deepcopy(request))
                conn.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread


def test_router_client_round_trip_and_binding(short_socket_path):
    sock = short_socket_path
    seen = {}

    def response(request):
        seen.update(request)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": request["id"],
            "result": {"enabled": False},
        }

    thread = _serve_once(sock, response)
    result = RouterClient(sock).call(
        "status",
        {"contract_version": 1},
        binding={"kind": "codex", "thread_id": "th", "turn_id": "turn"},
    )
    thread.join(2)
    assert result == {"enabled": False}
    assert seen["binding"]["thread_id"] == "th"
    assert seen["method"] == "status"


def test_router_client_surfaces_policy_rejection(short_socket_path):
    sock = short_socket_path

    def response(request):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": request["id"],
            "error": {
                "code": "ROUTING_DISABLED",
                "message": "Smart Routing is disabled",
                "data": {"enabled": False},
            },
        }

    thread = _serve_once(sock, response)
    with pytest.raises(RouterRejectedError) as caught:
        RouterClient(sock).call("delegate_task", {})
    thread.join(2)
    assert caught.value.code == "ROUTING_DISABLED"
    assert caught.value.data == {"enabled": False}


def test_router_client_rejects_mismatched_response_id(short_socket_path):
    sock = short_socket_path

    def response(_request):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "id": "wrong",
            "result": {},
        }

    thread = _serve_once(sock, response)
    with pytest.raises(RouterProtocolError, match="id did not match"):
        RouterClient(sock).status()
    thread.join(2)


class TestDispatchAvailableCache:
    """The advertisement decision is read on the GTK main thread.

    So it has to be a cached bool, not a socket round trip: a wedged broker
    must not be able to stall a session opening by the client timeout.
    """

    @pytest.fixture(autouse=True)
    def _reset(self):
        router_client.note_status(None)
        yield
        router_client.note_status(None)

    def test_defaults_to_unavailable(self):
        assert router_client.dispatch_available() is False

    def test_tracks_automatic_dispatch(self):
        assert router_client.note_status({"automatic_dispatch": True}) is True
        assert router_client.dispatch_available() is True

        assert router_client.note_status({"automatic_dispatch": False}) is False
        assert router_client.dispatch_available() is False

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"automatic_dispatch": "true"},   # a string is not the bit
            {"automatic_dispatch": 1},        # nor is a truthy int
            {"execution_mode": "active"},     # nor is a neighbouring field
            "automatic_dispatch",
        ],
    )
    def test_anything_that_is_not_the_bit_reads_as_unavailable(self, payload):
        """Fail closed. Advertising is a claim the broker has to actually make."""
        router_client.note_status({"automatic_dispatch": True})
        assert router_client.note_status(payload) is False
        assert router_client.dispatch_available() is False

    def test_status_warms_the_cache(self, short_socket_path):
        """Every existing off-thread status read keeps it fresh for free.

        That is what makes the cache cheap enough to be worth having, and it is
        why flipping the preview switch in Settings — which calls status right
        after — takes effect on the next session without any extra plumbing.
        """
        def response(request):
            return {
                "protocol_version": PROTOCOL_VERSION,
                "id": request["id"],
                "result": {
                    "contract_version": CONTRACT_VERSION,
                    "automatic_dispatch": True,
                    "execution_mode": "active",
                },
            }

        thread = _serve_once(short_socket_path, response)
        RouterClient(short_socket_path).status()
        thread.join(timeout=2)
        assert router_client.dispatch_available() is True

    def test_a_failed_status_read_clears_a_cached_true(self, tmp_path):
        """The cache must fail closed on the *direct* status path too.

        Settings reads status by calling `status()` and letting it raise, not
        through `refresh_dispatch_available`. If only the success path wrote to
        the cache, a broker that answered once and then died would keep its last
        "dispatchable" answer forever — and Helios would go on advertising the
        Router MCP and spawning its subprocess against a socket that is gone.
        """
        router_client.note_status({"automatic_dispatch": True})
        assert router_client.dispatch_available() is True

        with pytest.raises(RouterError):
            RouterClient(tmp_path / "absent.sock", timeout=0.05).status()

        assert router_client.dispatch_available() is False

    def test_an_older_result_never_overwrites_a_newer_one(self):
        """Two status reads overlap easily — the startup refresh against a
        settings reload, or that reload against the read issued right after the
        preview toggle — and a socket round trip gives no ordering guarantee.
        An older `False` landing after a newer `True` would leave the cache
        wrong until something else asked, which is precisely the toggle case
        the cache exists to serve."""
        old_seq = router_client.issue_status_seq()
        new_seq = router_client.issue_status_seq()

        # The newer request completes first.
        router_client.note_status({"automatic_dispatch": True}, seq=new_seq)
        assert router_client.dispatch_available() is True

        # The older one lands late and must be dropped, not applied.
        assert router_client.note_status(
            {"automatic_dispatch": False}, seq=old_seq
        ) is True
        assert router_client.dispatch_available() is True

    def test_a_late_failure_cannot_clobber_a_newer_success(self):
        """The clear-on-failure path is ordered too. Failing closed must not
        mean a stale error wins over a fresh answer."""
        old_seq = router_client.issue_status_seq()
        new_seq = router_client.issue_status_seq()

        router_client.note_status({"automatic_dispatch": True}, seq=new_seq)
        router_client.note_status(None, seq=old_seq)

        assert router_client.dispatch_available() is True

    def test_a_newer_result_still_applies(self):
        """The ordering guard must not wedge the cache shut."""
        first = router_client.issue_status_seq()
        router_client.note_status({"automatic_dispatch": True}, seq=first)

        second = router_client.issue_status_seq()
        assert router_client.note_status(None, seq=second) is False
        assert router_client.dispatch_available() is False

    def test_refresh_does_not_reapply_a_result_status_already_rejected(self):
        """`refresh_dispatch_available` must record nothing of its own.

        `status()` already does the ordered write. Recording the returned
        payload a second time would do it *unordered* — and unordered always
        wins — so the wrapper would reapply exactly the stale result the
        sequence number had just rejected, defeating the ordering it sits on
        top of.
        """
        slow = {"automatic_dispatch": False}

        class _SlowClient:
            """A refresh that started first and finishes last."""

            def status(self):
                seq = router_client.issue_status_seq()
                # A newer read lands while this one is still in flight.
                newer = router_client.issue_status_seq()
                router_client.note_status(
                    {"automatic_dispatch": True}, seq=newer
                )
                router_client.note_status(slow, seq=seq)
                return slow

        original = router_client.RouterClient
        router_client.RouterClient = _SlowClient
        try:
            assert router_client.refresh_dispatch_available() is True
        finally:
            router_client.RouterClient = original
        assert router_client.dispatch_available() is True

    def test_the_cache_recovers_once_the_broker_appears(self, tmp_path):
        """The case a one-shot warmup cannot reach.

        Helios can win the race against the broker's socket at boot, or the
        broker can restart while Helios stays open. Both leave the cache false
        with nothing to correct it, and new sessions only read the cache — so
        the Router MCP would stay unadvertised against a broker that is
        dispatching, until somebody happened to open Settings. The recovery
        path is a periodic refresh, so what matters here is that a later
        successful read is believed after an earlier failed one.
        """
        missing = tmp_path / "absent.sock"
        original = router_client.RouterClient
        router_client.RouterClient = lambda: original(missing, timeout=0.05)
        try:
            assert router_client.refresh_dispatch_available() is False
        finally:
            router_client.RouterClient = original

        class _Available:
            def status(self):
                return router_client.note_status(
                    {"automatic_dispatch": True},
                    seq=router_client.issue_status_seq(),
                ) and {"automatic_dispatch": True}

        router_client.RouterClient = _Available
        try:
            assert router_client.refresh_dispatch_available() is True
        finally:
            router_client.RouterClient = original
        assert router_client.dispatch_available() is True

    def test_an_unreachable_broker_is_not_dispatchable(self, tmp_path):
        router_client.note_status({"automatic_dispatch": True})
        missing = tmp_path / "absent.sock"

        with pytest.raises(RouterError):
            RouterClient(missing, timeout=0.05).status()

        # refresh_dispatch_available swallows that failure and fails closed,
        # rather than leaving the last good answer in place. A router that
        # cannot be asked cannot be advertised.
        monkey = router_client.RouterClient
        router_client.RouterClient = lambda: monkey(missing, timeout=0.05)
        try:
            assert router_client.refresh_dispatch_available() is False
        finally:
            router_client.RouterClient = monkey
        assert router_client.dispatch_available() is False
