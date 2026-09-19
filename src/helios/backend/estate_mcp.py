"""Bounded synchronous MCP stdio client for the OpenRouter worker thread.

Wire contract: MCP 2025-11-25 lifecycle, stdio and tools specifications.
The eleven installed estate transports are stdio. HTTP/OAuth, resources,
sampling, elicitation and task-augmented calls are deliberately not advertised.
Tool annotations are not permission grants; the driver must approve calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time

from helios.backend.estate_config import EstateConfigError, ServerConfig, load_selected

PROTOCOLS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_SCHEMA_BYTES = 128 * 1024
# Leave room for the driver's built-ins within a 128-function request.
MAX_TOOLS = 120
MAX_RESULT_CHARS = 60_000


class McpError(RuntimeError):
    """Safe errors: wire messages and stderr are never embedded in exceptions."""


class StdioClient:
    def __init__(self, config: ServerConfig, cwd: str, cancellation=None):
        self.config = config
        self.cancellation = cancellation
        self.changed = False
        self._seq = 0
        self._closed = False
        self._buffer = bytearray()
        self.proc = subprocess.Popen(
            [config.command, *config.args], cwd=cwd, env=config.child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, start_new_session=True,
        )
        os.set_blocking(self.proc.stdin.fileno(), False)
        os.set_blocking(self.proc.stdout.fileno(), False)

    def _check(self, deadline: float) -> None:
        if self.cancellation is not None and self.cancellation.cancelled:
            raise McpError("MCP request cancelled; tool outcome may be unknown")
        if time.monotonic() >= deadline:
            raise McpError("MCP deadline exceeded; tool outcome may be unknown")

    def _write(self, value: dict, deadline: float) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(data) > MAX_MESSAGE_BYTES:
            raise McpError("MCP request exceeds byte limit")
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdin, selectors.EVENT_WRITE)
            while data:
                self._check(deadline)
                if selector.select(0.05):
                    try:
                        data = data[os.write(self.proc.stdin.fileno(), data):]
                    except OSError as error:
                        raise McpError("MCP connection closed") from error

    def _read(self, deadline: float) -> dict:
        self._check(deadline)
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                self._check(deadline)
                if selector.select(0.05):
                    part = os.read(self.proc.stdout.fileno(), 65536)
                    if not part:
                        raise McpError("MCP server closed its output")
                    self._buffer.extend(part)
                    if len(self._buffer) > MAX_MESSAGE_BYTES:
                        raise McpError("MCP response exceeds byte limit")
            raw, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise McpError("MCP server emitted invalid JSON") from error
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise McpError("MCP server emitted an invalid envelope")
        return value

    def _server_message(self, value: dict, deadline: float) -> bool:
        """Handle server control traffic, never authorize tool effects."""
        if "method" not in value:
            return False
        if not isinstance(value["method"], str) or not value["method"]:
            raise McpError("MCP server emitted an invalid method")
        if value["method"] == "notifications/tools/list_changed":
            self.changed = True
        if "id" in value:
            reply = {"jsonrpc": "2.0", "id": value["id"]}
            if value["method"] == "ping":
                reply["result"] = {}
            else:
                reply["error"] = {"code": -32601, "message": "Client capability not supported"}
            self._write(reply, deadline)
        return True

    def _before_tool_call(self, deadline: float) -> None:
        """Consume queued control messages before the first tools/call byte.

        Approval can leave stdout unread for minutes. Opaque partial messages
        cannot prove the approved inventory is still current, so fail closed
        rather than waiting for a frame to finish or sending through it.
        This is a bounded local check, not a server-side schema-version lock.
        """
        deadline = min(deadline, time.monotonic() + 1.0)
        pending_bytes, messages = len(self._buffer), 0
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            while True:
                self._check(deadline)
                if self.changed:
                    raise McpError("MCP inventory changed; tool was not sent. Start a new turn to discover it again.")
                if pending_bytes > MAX_MESSAGE_BYTES:
                    raise McpError("Queued MCP messages exceed byte limit; tool was not sent")
                if b"\n" in self._buffer:
                    if messages >= 1024:
                        raise McpError("Queued MCP notification limit exceeded; tool was not sent")
                    messages += 1
                    value = self._read(deadline)
                    if not self._server_message(value, deadline):
                        raise McpError("Unexpected queued MCP response; tool was not sent")
                elif selector.select(0):
                    part = os.read(self.proc.stdout.fileno(), 65536)
                    if not part:
                        raise McpError("MCP server closed its output; tool was not sent")
                    self._buffer.extend(part)
                    pending_bytes += len(part)
                elif self._buffer:
                    raise McpError("Incomplete queued MCP message; tool was not sent")
                else:
                    return

    def request(self, method: str, params: dict, *, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        if method == "tools/call":
            self._before_tool_call(deadline)
        self._seq += 1
        request_id = self._seq
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, deadline)
        # Bound notification floods as well as bytes and elapsed time.
        for _ in range(1024):
            value = self._read(deadline)
            if self._server_message(value, deadline):
                continue
            if type(value.get("id")) is not int or value["id"] != request_id:
                raise McpError("MCP response identity mismatch")
            if "error" in value:
                raise McpError("MCP server returned a protocol error")
            if not isinstance(value.get("result"), dict):
                raise McpError("MCP server returned an invalid result")
            return value["result"]
        raise McpError("MCP notification limit exceeded")

    def initialize(self, *, timeout: float = 5) -> None:
        result = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                              "clientInfo": {"name": "helios", "version": "1"}}, timeout=timeout)
        if result.get("protocolVersion") not in PROTOCOLS:
            raise McpError("MCP protocol version unsupported")
        capabilities = result.get("capabilities")
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            raise McpError("MCP server does not advertise tools")
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"}, time.monotonic() + timeout)

    def list_tools(self, *, deadline: float) -> list[dict]:
        tools, cursors, cursor = [], set(), None
        size = 0
        for _ in range(10):
            self._check(deadline)
            result = self.request("tools/list", {"cursor": cursor} if cursor else {},
                                  timeout=max(0.01, deadline - time.monotonic()))
            page = result.get("tools")
            if not isinstance(page, list):
                raise McpError("MCP tool list is invalid")
            size += len(json.dumps(page).encode())
            tools.extend(page)
            if len(tools) > MAX_TOOLS or size > MAX_SCHEMA_BYTES:
                raise McpError("MCP tool inventory exceeds budget")
            cursor = result.get("nextCursor")
            if cursor is None:
                self.changed = False
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise McpError("MCP pagination cursor is invalid or repeated")
            cursors.add(cursor)
        raise McpError("MCP pagination limit exceeded")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            # Reap descendants even if the immediate server already exited.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    break
                if sig == signal.SIGTERM:
                    try:
                        self.proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
            self.proc.wait(timeout=0.5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        finally:
            self.proc.stdout.close()


def _function_name(server: str, tool: str) -> str:
    digest = hashlib.sha256(f"{server}\0{tool}".encode()).hexdigest()[:12]
    readable = re.sub(r"[^a-zA-Z0-9_]", "_", f"{server}_{tool}")[:44]
    return f"mcp__{readable}_{digest}"


def _scrub_server_secrets(text: str, config: ServerConfig) -> str:
    for key, value in config.env.items():
        if value and (key.endswith("_KEY") or key == "HA_LLAT"):
            text = text.replace(json.dumps(value)[1:-1], "[REDACTED BY HELIOS]")
            text = text.replace(value, "[REDACTED BY HELIOS]")
    return text


class EstateMcp:
    """One worker-owned inventory, held stable through a provider turn.

    Construct/discover/close outside GTK's main loop. The driver's existing
    approval path must authorize calls; no `readOnlyHint` can grant access.
    """
    def __init__(self, cwd: str, cancellation=None, *, configs=None):
        self.cwd, self.cancellation = cwd, cancellation
        self.statuses: list[dict] = []
        self._clients: dict[str, StdioClient] = {}
        self._tools: dict[str, tuple[str, str]] = {}
        self._configs = configs
        self._inventory: list[dict] | None = None
        self._advertised: set[str] = set()
        self._grant_fingerprints: dict[str, str] = {}

    def discover(self) -> list[dict]:
        """Expose two small capabilities; no server starts until tool search."""
        try:
            if self._configs is None:
                self._configs = load_selected()
        except EstateConfigError as error:
            self.statuses = [{"name": "estate", "status": "failed", "reason": str(error), "checked_at": time.time()}]
            return []
        if not self._configs:
            return []
        names = [config.name for config in self._configs]
        self.statuses = [{"name": name, "status": "configured", "reason": "Connects when tool search is used"} for name in names]
        return [
            {"type": "function", "function": {"name": "EstateSearchTools",
             "description": "Discover estate tools on demand. Returns exact tool names and argument schemas; use EstateCallTool to call one. Servers: " + ", ".join(names),
             "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "server": {"type": "string", "enum": names}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "EstateCallTool",
             "description": "Call an exact tool returned by EstateSearchTools using its argument schema. Calls retain Helios permissions; tools may read or change external systems.",
             "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["name", "arguments"]}}},
        ]

    def _discover_all(self) -> list[dict]:
        self.close()
        self.statuses = []
        schemas, total_bytes = [], 0
        deadline = time.monotonic() + 20
        try:
            configs = self._configs if self._configs is not None else load_selected()
        except EstateConfigError as error:
            self.statuses.append({"name": "estate", "status": "failed", "reason": str(error), "checked_at": time.time()})
            return []
        for config in configs:
            client = None
            try:
                if time.monotonic() >= deadline:
                    raise McpError("Estate discovery deadline exceeded")
                client = StdioClient(config, self.cwd, self.cancellation)
                client.initialize(timeout=min(5, max(0.01, deadline - time.monotonic())))
                native = client.list_tools(deadline=min(deadline, time.monotonic() + 5))
                pending, names = [], set()
                for tool in native:
                    if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                            or not tool["name"] or len(tool["name"]) > 128
                            or tool["name"] in names or not isinstance(tool.get("inputSchema"), dict)
                            or tool["inputSchema"].get("type") != "object"):
                        raise McpError("MCP tool definition is invalid")
                    names.add(tool["name"])
                    execution = tool.get("execution", {})
                    if not isinstance(execution, dict):
                        raise McpError("MCP tool execution metadata is invalid")
                    if execution.get("taskSupport") == "required":
                        continue
                    fn = _function_name(config.name, tool["name"])
                    pending.append({"type": "function", "function": {"name": fn,
                                    "description": f"{config.name}: {str(tool.get('description', ''))[:4000]}",
                                    "parameters": tool["inputSchema"]}})
                # A server can echo its own configured key in metadata as well
                # as results. Neither path may copy that value to a provider.
                pending = json.loads(_scrub_server_secrets(json.dumps(pending), config))
                size = len(json.dumps(pending).encode())
                if total_bytes + size > MAX_SCHEMA_BYTES or len(schemas) + len(pending) > MAX_TOOLS:
                    raise McpError("Estate schema budget exceeded")
                for tool in native:
                    fn = _function_name(config.name, tool["name"])
                    if any(p["function"]["name"] == fn for p in pending):
                        self._tools[fn] = (config.name, tool["name"])
                        # Hash the complete native definition, including
                        # annotations, plus the actual server configuration.
                        # Keep credentials out of grant keys and UI/logs.
                        identity = {"server": config.name, "command": config.command,
                                    "args": config.args, "env": config.env, "tool": tool}
                        self._grant_fingerprints[fn] = hashlib.sha256(
                            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest()
                schemas.extend(pending)
                total_bytes += size
                self._clients[config.name] = client
                self.statuses.append({"name": config.name, "status": "connected", "tool_count": len(pending), "checked_at": time.time()})
            except (McpError, OSError, ValueError, TypeError, RecursionError) as error:
                if client:
                    client.close()
                reason = str(error) if isinstance(error, McpError) else "MCP server failed to start or returned invalid data"
                self.statuses.append({"name": config.name, "status": "failed", "reason": reason, "checked_at": time.time()})
        return schemas

    def owns(self, name: str) -> bool:
        return name in {"EstateSearchTools", "EstateCallTool"} and bool(self._configs)

    def approval_name(self, name: str, arguments: dict) -> str:
        """Use this for per-tool approval/session-grant identity, not the proxy."""
        target = arguments.get("name") if isinstance(arguments, dict) else None
        if name == "EstateCallTool" and isinstance(target, str) and target in self._advertised:
            return target
        return name

    def session_grant_key(self, name: str) -> str:
        """Bind explicit consent to a discovered tool and its current server.

        An annotation never grants permission. An unknown, unadvertised or
        invalidated tool cannot inherit a grant, even if its name is reused.
        The transport still checks queued inventory changes before dispatch.
        """
        if name not in self._advertised or name not in self._tools:
            return ""
        server, _native = self._tools[name]
        client = self._clients.get(server)
        if client is None or client.changed or client._closed:
            return ""
        fingerprint = self._grant_fingerprints.get(name)
        return f"mcp:{name}:{fingerprint}" if fingerprint else ""

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        if not isinstance(arguments, dict):
            return "Estate tool arguments must be an object.", True
        if name == "EstateSearchTools":
            query, server = arguments.get("query"), arguments.get("server")
            if not isinstance(query, str) or not query.strip() or len(query) > 500:
                return "Search requires a query of 1–500 characters.", True
            names = {config.name for config in (self._configs or [])}
            if server is not None and (not isinstance(server, str) or server not in names):
                return "Search server is not in the selected estate configuration.", True
            if self._inventory is None:
                self._inventory = self._discover_all()
            words = set(re.findall(r"[a-z0-9]+", query.lower()))
            ranked = []
            for schema in self._inventory:
                fn = schema["function"]
                origin, native = self._tools[fn["name"]]
                if server is not None and origin != server:
                    continue
                identity = set(re.findall(r"[a-z0-9]+", (origin + " " + native).lower()))
                description = set(re.findall(r"[a-z0-9]+", fn["description"].lower()))
                score = 3 * len(words & identity) + len(words & description)
                if score:
                    ranked.append((score, fn["name"], schema))
            picked, size = [], 0
            for _, _, schema in sorted(ranked, key=lambda x: (-x[0], x[1]))[:6]:
                encoded = json.dumps(schema)
                if size + len(encoded) > 24_000:
                    continue
                picked.append(schema)
                size += len(encoded)
                self._advertised.add(schema["function"]["name"])
            return json.dumps({"tools": picked, "servers": self.statuses,
                               "note": "Use the exact returned name and arguments with EstateCallTool. Narrow the query or server to find other tools."}), False
        if name != "EstateCallTool":
            return "Unknown estate tool capability.", True
        name, arguments = arguments.get("name"), arguments.get("arguments")
        if not isinstance(name, str) or name not in self._advertised:
            return "Search must return this exact tool before it can be called.", True
        return self._invoke(name, arguments)

    def _invoke(self, name: str, arguments: dict) -> tuple[str, bool]:
        if name not in self._tools or not isinstance(arguments, dict):
            return "MCP tool is not in this turn's verified inventory.", True
        server, native = self._tools[name]
        client = self._clients[server]
        if client.changed:
            return "MCP inventory changed; start a new turn to discover it again.", True
        try:
            result = client.request("tools/call", {"name": native, "arguments": arguments}, timeout=120)
            blocks = result.get("content", [])
            if not isinstance(blocks, list) or len(blocks) > 256:
                raise McpError("MCP result content is invalid")
            parts = []
            unsupported = False
            for block in blocks:
                if not isinstance(block, dict):
                    raise McpError("MCP result block is invalid")
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                else:
                    unsupported = True
            if "structuredContent" in result:
                parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
            if unsupported:
                parts.append("[MCP returned non-text content; this provider supports text/structured results only.]")
            text = _scrub_server_secrets("\n".join(parts), client.config)
            if len(text) > MAX_RESULT_CHARS:
                text = text[:MAX_RESULT_CHARS] + "\n[MCP result truncated at 60,000 characters.]"
            return text, bool(result.get("isError", False)) or (unsupported and not any(b.get("type") == "text" for b in blocks))
        except (McpError, OSError, ValueError, RecursionError) as error:
            client.changed = True  # Never retry an unknown tool outcome automatically.
            client.close()
            return (str(error) if isinstance(error, McpError) else "MCP connection failed; tool outcome may be unknown"), True

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
        self._tools.clear()
        self._grant_fingerprints.clear()
        self._inventory = None
        self._advertised.clear()
