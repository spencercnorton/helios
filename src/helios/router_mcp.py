"""Credential-free MCP stdio adapter for Claude Code.

This process is launched with a scrubbed environment and talks to the
privileged Helios Router over a Unix socket.  It never loads or receives the
OpenRouter API key.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from typing import Any

from helios import __version__
from helios.backend.router_client import (
    RouterClient,
    RouterError,
    RouterRejectedError,
)
from helios.backend.router_tools import (
    CONTRACT_VERSION,
    MCP_SERVER_NAME,
    mcp_tools,
    tool_names,
)


_SUPPORTED_PROTOCOL = "2025-06-18"
_MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _result_payload(result: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    rendered = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": rendered}],
        "structuredContent": result,
        "isError": is_error,
    }


class RouterMcpServer:
    def __init__(self, client: RouterClient, *, binding_id: str) -> None:
        self._client = client
        self._binding_id = binding_id
        self._initialize_complete = False
        self._client_ready = False

    def handle(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Invalid JSON-RPC request")
        has_id = "id" in message
        request_id = message.get("id")
        if has_id and (
            isinstance(request_id, bool)
            or not isinstance(request_id, (int, str))
        ):
            return _error(None, -32600, "Invalid JSON-RPC id")
        method = message.get("method")
        if not isinstance(method, str):
            return _error(request_id, -32600, "Missing JSON-RPC method")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}

        if method == "initialize":
            if not has_id:
                return None
            if self._initialize_complete:
                return _error(request_id, -32600, "Already initialized")
            requested = params.get("protocolVersion")
            protocol = (
                requested
                if requested == _SUPPORTED_PROTOCOL
                else _SUPPORTED_PROTOCOL
            )
            self._initialize_complete = True
            return _success(
                request_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": MCP_SERVER_NAME,
                        "version": __version__,
                    },
                    "instructions": (
                        "Use delegate_task regularly for bounded, low-risk, "
                        "independently checkable subproblems. Keep ambiguous "
                        "intent, secrets, destructive or production authority, "
                        "security decisions, acceptance, and the final answer "
                        "on the primary Claude model. A refusal means retain the "
                        "task; do not bypass Helios by calling raw model APIs."
                    ),
                },
            )
        if not has_id:
            if method == "notifications/initialized" and self._initialize_complete:
                self._client_ready = True
            return None
        if not self._initialize_complete or not self._client_ready:
            return _error(request_id, -32002, "MCP initialization is incomplete")
        if method == "ping":
            return _success(request_id, {})
        if method == "tools/list":
            return _success(request_id, {"tools": mcp_tools()})
        if method != "tools/call":
            return _error(request_id, -32601, f"Unsupported method: {method}")

        name = params.get("name")
        arguments = params.get("arguments")
        if name not in tool_names() or not isinstance(arguments, dict):
            return _error(request_id, -32602, "Invalid Helios tool call")
        binding = {
            "kind": "claude",
            "client_binding": self._binding_id,
            "call_id": str(request_id),
        }
        try:
            result = self._client.call(name, arguments, binding=binding)
        except RouterRejectedError as exc:
            result = {
                "contract_version": CONTRACT_VERSION,
                "accepted": False,
                "status": "rejected",
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "data": exc.data,
                },
            }
            return _success(request_id, _result_payload(result, is_error=False))
        except RouterError as exc:
            result = {
                "contract_version": CONTRACT_VERSION,
                "accepted": False,
                "status": "unavailable",
                "error": {
                    "code": "ROUTER_UNAVAILABLE",
                    "message": str(exc),
                },
            }
            return _success(request_id, _result_payload(result, is_error=True))
        except Exception:
            result = {
                "contract_version": CONTRACT_VERSION,
                "accepted": False,
                "status": "failed",
                "error": {
                    "code": "ROUTER_INTERNAL_ERROR",
                    "message": "Helios Router failed unexpectedly",
                },
            }
            return _success(request_id, _result_payload(result, is_error=True))
        return _success(request_id, _result_payload(result, is_error=False))


def _success(request_id: object, result: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(
    request_id: object,
    code: int,
    message: str,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def serve(
    server: RouterMcpServer,
    *,
    input_stream: io.BufferedIOBase | None = None,
    output_stream: io.BufferedIOBase | None = None,
) -> int:
    source = input_stream or sys.stdin.buffer
    sink = output_stream or sys.stdout.buffer
    while True:
        raw = source.readline(_MAX_REQUEST_BYTES + 1)
        if not raw:
            break
        if len(raw) > _MAX_REQUEST_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = source.readline(_MAX_REQUEST_BYTES + 1)
            response = _error(None, -32600, "Request exceeded size limit")
        else:
            try:
                message = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = _error(None, -32700, "Parse error")
            else:
                response = server.handle(message)
        if response is not None:
            encoded = json.dumps(
                response,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            sink.write(encoded + b"\n")
            sink.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helios Router MCP adapter")
    parser.add_argument("--binding", required=True)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args(argv)
    return serve(
        RouterMcpServer(
            RouterClient(args.socket),
            binding_id=args.binding,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
