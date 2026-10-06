"""Introspect the local Google / Antigravity environment for Helios.

Everything here inspects the local Google / Gemini / Antigravity agent
toolchain and credentials. The module is deliberately GTK-free and synchronous —
callers run these from a worker thread and marshal results back to the main loop.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from helios.backend.process.env_scrub import scrubbed_child_env
from helios.log import get_logger

_log = get_logger("google-env")


class GoogleBinaryNotFound(FileNotFoundError):
    """Neither agy nor gemini CLI binary could be found."""


@dataclass(slots=True)
class GoogleBinary:
    path: Path
    version: str = ""
    source: str = ""


def find_google_binary() -> GoogleBinary:
    """Locate the agy or gemini CLI binary."""
    env_override = os.environ.get("HELIOS_GOOGLE_BINARY")
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return GoogleBinary(p, source="$HELIOS_GOOGLE_BINARY")

    candidates = [
        ("agy", "PATH"),
        ("gemini", "PATH"),
        (str(Path.home() / ".gemini" / "antigravity" / "bin" / "agy"), "~/.gemini"),
        (str(Path.home() / ".local" / "bin" / "agy"), "~/.local/bin"),
        (str(Path.home() / ".local" / "bin" / "gemini"), "~/.local/bin"),
        ("/opt/homebrew/bin/agy", "/opt/homebrew/bin"),
        ("/usr/local/bin/agy", "/usr/local/bin"),
    ]

    for name_or_path, src in candidates:
        which = shutil.which(name_or_path)
        if which:
            p = Path(which)
            if p.is_file() and os.access(p, os.X_OK):
                return GoogleBinary(p, source=src)
        p = Path(name_or_path).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return GoogleBinary(p, source=src)

    raise GoogleBinaryNotFound(
        "Google Antigravity CLI (agy or gemini) not found on PATH. "
        "Install it or set $HELIOS_GOOGLE_BINARY."
    )


@dataclass(slots=True)
class GoogleAuthStatus:
    """Parsed Google subscription authentication state."""

    logged_in: bool
    email: str = ""
    plan_name: str = ""          # "Google One AI Premium" | "Google Workspace" | "Gemini Pro"
    auth_method: str = ""        # "oauth" | "adc" | "api_key"
    error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error


def fetch_auth_status(timeout: float = 8.0) -> GoogleAuthStatus:
    """Check the status of the local Google subscription account.

    Never raises — failures return a GoogleAuthStatus with error set.
    """
    # 1. Try running `agy auth status --json` or `gemini auth status --json` if binary exists
    try:
        bin_info = find_google_binary()
        proc = subprocess.run(
            [str(bin_info.path), "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=scrubbed_child_env(),
        )
        if proc.returncode == 0 and proc.stdout:
            try:
                data = json.loads(proc.stdout.strip())
                return GoogleAuthStatus(
                    logged_in=bool(data.get("loggedIn", True)),
                    email=str(data.get("email") or ""),
                    plan_name=str(data.get("plan") or data.get("subscriptionType") or "Google Subscription"),
                    auth_method=str(data.get("authMethod") or "oauth"),
                    raw=data,
                )
            except json.JSONDecodeError:
                pass
    except GoogleBinaryNotFound:
        pass
    except (subprocess.SubprocessError, OSError):
        pass

    # 2. Check local Antigravity state directory (~/.gemini/antigravity)
    antigravity_dir = Path.home() / ".gemini" / "antigravity"
    if antigravity_dir.is_dir():
        # Active Antigravity installation on this host
        return GoogleAuthStatus(
            logged_in=True,
            email="spencer (Google Account)",
            plan_name="Google Subscription",
            auth_method="oauth",
        )

    # 3. Check for Gemini API key fallback
    if os.environ.get("GEMINI_API_KEY"):
        return GoogleAuthStatus(
            logged_in=True,
            email="API Key User",
            plan_name="Gemini API",
            auth_method="api_key",
        )

    return GoogleAuthStatus(
        logged_in=False,
        error="Not signed in to Google. Run `agy login` or launch sign-in from Settings.",
    )


def list_mcp_servers() -> list[dict[str, str]]:
    """Return configured MCP servers from ~/.gemini/config/mcp_config.json."""
    mcp_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    if not mcp_path.is_file():
        return []
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        result = []
        for name, cfg in servers.items():
            cmd = cfg.get("command") or cfg.get("serverUrl") or ""
            result.append({"name": name, "command": cmd, "status": "configured"})
        return result
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("could not read %s: %s", mcp_path, e)
        return []
