"""MCP stdio server that lets any provider's agent drive the Helios browser pane.

Launched by the agent CLI with a scrubbed environment. Each ``tools/call`` is
forwarded verbatim to the Helios window over the session bus named by
``--bus``; the window owns every decision (what loads, what needs approval).
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from helios import APP_ID, __version__
from helios.backend.browser_tools import (
    INSTRUCTIONS,
    INTERFACE,
    MCP_SERVER_NAME,
    OBJECT_PATH,
    TOOL_NAMES,
    TOOLS,
)
from helios.router_mcp import _error, _success, serve

_PROTOCOL = "2025-06-18"
# Long enough for a slow page load or for the person to answer an approval.
_CALL_TIMEOUT_MS = 180_000

Caller = Callable[[str, dict], dict]


class BrowserMcpServer:
    def __init__(self, call: Caller, *, cwd: str, agent: str) -> None:
        self._call = call
        self._cwd = cwd
        self._agent = agent

    def handle(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid JSON-RPC request")
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if "id" not in message:
            return None  # notifications need no answer
        if method == "initialize":
            return _success(request_id, {
                "protocolVersion": _PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": MCP_SERVER_NAME, "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _success(request_id, {})
        if method == "tools/list":
            return _success(request_id, {"tools": TOOLS})
        if method != "tools/call":
            return _error(request_id, -32601, f"Unsupported method: {method}")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOL_NAMES or not isinstance(arguments, dict):
            return _error(request_id, -32602, "Unknown browser tool")
        try:
            result = self._call(name, {**arguments, "_cwd": self._cwd, "_agent": self._agent})
        except Exception as exc:  # the bus, not the page, failed
            result = {"ok": False, "error": f"Helios is not reachable: {exc}"}
        return _success(request_id, _content(result))


def _content(result: dict) -> dict:
    if not result.get("ok"):
        return {"content": [{"type": "text", "text": str(result.get("error") or "Failed")}], "isError": True}
    png = result.pop("png_base64", None)
    result.pop("ok", None)
    content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
    if png:
        content.append({"type": "image", "data": png, "mimeType": "image/png"})
    return {"content": content, "isError": False}


def dbus_caller(address: str) -> Caller:
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    connection = Gio.DBusConnection.new_for_address_sync(
        address,
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None,
        None,
    )

    def call(tool: str, arguments: dict) -> dict:
        reply = connection.call_sync(
            APP_ID, OBJECT_PATH, INTERFACE, "Call",
            GLib.Variant("(ss)", (tool, json.dumps(arguments))),
            GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, _CALL_TIMEOUT_MS, None,
        )
        return json.loads(reply.unpack()[0])

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helios browser pane MCP server")
    parser.add_argument("--bus", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--agent", default="Agent")
    args = parser.parse_args(argv)
    return serve(BrowserMcpServer(dbus_caller(args.bus), cwd=args.cwd, agent=args.agent))


if __name__ == "__main__":
    raise SystemExit(main())
