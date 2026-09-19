"""Read-only presentation of driver reports, without discovery or context reads.

The time this snapshot was copied is not the time a provider checked a server.
Native instruction loaders do not expose effective file receipts to Helios.
"""
from __future__ import annotations

import math
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone


def _text(value: object, limit: int = 4096) -> str:
    return value[:limit].strip() if isinstance(value, str) else ""


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**12 else None


def timestamp(value: object) -> str:
    """Format a reported Unix timestamp; never replace unknown time with now."""
    if type(value) not in (int, float):
        return "Not reported"
    try:
        if not math.isfinite(value) or value <= 0:
            return "Not reported"
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, ValueError, OSError):
        return "Not reported"


@dataclass(frozen=True)
class ServerReport:
    name: str
    status: str
    checked_at: str
    tool_count: int | None


@dataclass(frozen=True)
class InstructionReceipt:
    path: str
    resolved_path: str
    sha256: str
    byte_count: int
    loaded_at: str
    modified_at: str
    precedence: int


@dataclass(frozen=True)
class CapabilitySnapshot:
    attached: bool
    provider: str
    model: str
    execution_host: str
    cwd: str
    cwd_reported: bool
    copied_at: str
    tools: tuple[str, ...]
    tools_reported: bool
    servers: tuple[ServerReport, ...]
    instructions: tuple[InstructionReceipt, ...]
    instruction_note: str


def build_snapshot(driver: object | None, provider: str = "", cwd: str = "", *,
                   now: float | None = None, host: str | None = None) -> CapabilitySnapshot:
    """Copy current driver fields; never call a provider, filesystem, or process.

    All Helios drivers execute locally; hostname identifies the app/tool host,
    not a cloud inference server. Optional clock/host inputs make tests pure.
    The selected workspace is shown as a fallback only when the driver has no
    cwd field. Whitelisting deliberately excludes raw config, env and errors.
    """
    attached = driver is not None
    actual_provider = _text(getattr(driver, "provider", ""), 100) or _text(provider, 100)
    driver_cwd = _text(getattr(driver, "cwd", "")) or _text(getattr(driver, "_cwd", ""))
    raw_tools = getattr(driver, "init_tools", None)
    tools_reported = isinstance(raw_tools, (list, tuple))
    names = []
    for tool in raw_tools if tools_reported else ():
        name = _text(tool, 512) if isinstance(tool, str) else ""
        if isinstance(tool, dict):
            name = _text(tool.get("name"), 512)
        if name:
            names.append(name)
    servers = []
    raw_servers = getattr(driver, "init_mcp_servers", None)
    statuses = {"configured", "connected", "ready", "failed", "disabled", "pending",
                "starting", "stopped", "disconnected", "needs-auth", "unsupported"}
    for row in raw_servers if isinstance(raw_servers, (list, tuple)) else ():
        if not isinstance(row, dict) or not _text(row.get("name"), 512):
            continue
        status = row.get("status")
        status = status if isinstance(status, str) and status in statuses else "Not reported"
        tool_count = _count(row.get("tool_count"))
        # Native Codex publishes tool definitions, which prove inventory only.
        if tool_count is None and isinstance(row.get("tools"), (dict, list)):
            tool_count = len(row["tools"])
        servers.append(ServerReport(_text(row["name"], 512), status,
                                    timestamp(row.get("checked_at")), tool_count))

    receipts = []
    if actual_provider == "openrouter":
        raw_sources = getattr(driver, "init_instruction_sources", None)
        for row in raw_sources if isinstance(raw_sources, (list, tuple)) else ():
            if not isinstance(row, dict) or row.get("provider") != "openrouter":
                continue
            digest, path = _text(row.get("sha256"), 100), _text(row.get("path"))
            size, precedence = _count(row.get("bytes")), _count(row.get("precedence"))
            if not path or not re.fullmatch(r"[0-9a-f]{64}", digest) or size is None or precedence is None:
                continue
            # Nanoseconds need a larger bound than counts; accept only actual
            # finite timestamp integers, without converting arbitrary strings.
            if type(row.get("mtime_ns")) is int and 0 < row["mtime_ns"] < 10**21:
                mtime = row["mtime_ns"] / 1_000_000_000
            else:
                mtime = None
            receipts.append(InstructionReceipt(path, _text(row.get("resolved_path")), digest,
                                                size, timestamp(row.get("loaded_at")),
                                                timestamp(mtime), precedence))
        note = ("File receipts from the last prepared OpenRouter turn. Files are not re-read here; "
                "current disk freshness is unknown. More specific instructions follow broader ones."
                if receipts else
                "No OpenRouter instruction receipts reported. Effective file context has not been verified here.")
    elif actual_provider in {"openai", "codex"}:
        note = "Codex loads AGENTS.md natively. Helios has no effective file receipts from that loader."
    elif actual_provider in {"anthropic", "claude"}:
        note = "Claude loads its own instructions and memory. Helios has no effective file receipts from that loader."
    else:
        note = "Effective instruction sources are not reported for this provider."
    return CapabilitySnapshot(attached, actual_provider or "Not selected",
                              _text(getattr(driver, "model", ""), 512) or "Not reported",
                              _text(host if host is not None else socket.gethostname(), 512),
                              driver_cwd or _text(cwd) or "Not reported", bool(driver_cwd),
                              timestamp(time.time() if now is None else now),
                              tuple(sorted(set(names))), tools_reported, tuple(servers),
                              tuple(sorted(receipts, key=lambda row: row.precedence)), note)
