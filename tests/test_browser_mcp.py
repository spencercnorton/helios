"""The browser pane's MCP server and how each provider is handed it."""

from __future__ import annotations

import json

import pytest

from helios.backend import browser_tools
from helios.browser_mcp import BrowserMcpServer


@pytest.fixture
def published():
    browser_tools.publish("unix:path=/run/user/1000/bus")
    yield
    browser_tools.publish("")


def _rpc(server, method, params=None, rid=1):
    return server.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})


def test_tools_are_listed_and_calls_forward_with_session_context():
    calls = []

    def call(tool, args):
        calls.append((tool, args))
        return {"ok": True, "tab": "1", "title": "Report", "url": "file:///r.html",
                "status": "loaded", "png_base64": "iVBORw0KGgo="}

    server = BrowserMcpServer(call, token="tok-1")
    init = _rpc(server, "initialize")["result"]
    assert init["serverInfo"]["name"] == "helios_browser" and "browser_open" in init["instructions"]
    names = {tool["name"] for tool in _rpc(server, "tools/list")["result"]["tools"]}
    assert names == browser_tools.TOOL_NAMES

    result = _rpc(server, "tools/call", {"name": "browser_screenshot", "arguments": {"tab": "1"}})["result"]
    assert calls == [("browser_screenshot", {"tab": "1", "_token": "tok-1"})]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["title"] == "Report"
    assert result["content"][1] == {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}


def test_refusals_and_an_unreachable_helios_are_tool_errors_not_crashes():
    def unreachable(_tool, _args):
        raise RuntimeError("The name dev.norvi.Helios was not provided")

    server = BrowserMcpServer(unreachable, token="t")
    result = _rpc(server, "tools/call", {"name": "browser_tabs", "arguments": {}})["result"]
    assert result["isError"] is True and "not reachable" in result["content"][0]["text"]

    server = BrowserMcpServer(lambda *_: {"ok": False, "error": "The person declined access to example.com."},
                              token="t")
    result = _rpc(server, "tools/call", {"name": "browser_read", "arguments": {}})["result"]
    assert result == {"content": [{"type": "text", "text": "The person declined access to example.com."}],
                      "isError": True}
    assert "error" in _rpc(server, "tools/call", {"name": "rm_rf", "arguments": {}})
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_nothing_is_injected_until_the_window_publishes_the_pane(published):
    entry = browser_tools.server_entry("/work/proj", "Muse")
    assert entry["command"].endswith("helios-browser-mcp")
    assert entry["args"][:3] == ["--bus", "unix:path=/run/user/1000/bus", "--token"]
    # The folder travels as a Helios-issued token, never as a claim.
    assert browser_tools.session_for(entry["args"][3]) == ("/work/proj", "Muse")
    assert browser_tools.session_for("forged") is None
    assert browser_tools.session_for(["not", "hashable"]) is None
    browser_tools.publish("")
    assert browser_tools.server_entry("/work/proj", "Muse") is None


def test_codex_threads_carry_the_browser_server(published):
    from helios.backend.process.codex_app_contract import build_thread_params

    params = build_thread_params(cwd="/work/proj", model="gpt-6.1-sol", permission_mode="default")
    server = params["config"]["mcp_servers"]["helios_browser"]
    assert browser_tools.session_for(server["args"][-1]) == ("/work/proj", "GPT")
    browser_tools.publish("")
    params = build_thread_params(cwd="/work/proj", model="gpt-6.1-sol", permission_mode="default")
    assert "mcp_servers" not in params["config"]
