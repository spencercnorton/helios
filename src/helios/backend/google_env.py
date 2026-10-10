"""Discover Antigravity subscription models without enabling API billing.

The Gemini CLI is a different transport and is not interchangeable with agy.
These bounded synchronous probes belong on a background worker, never GTK's
main loop. Catalog availability does not by itself prove account sign-in.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from helios.backend.process.env_scrub import scrubbed_child_env
from helios.log import get_logger

_log = get_logger("google-env")


class GoogleBinaryNotFound(FileNotFoundError):
    """The Antigravity CLI binary could not be found."""


class GoogleSubscriptionRequired(ValueError):
    """A configured API provider would bypass the subscription route."""


@dataclass(slots=True)
class GoogleBinary:
    path: Path
    version: str = ""
    source: str = ""


def find_google_binary() -> GoogleBinary:
    """Locate Antigravity CLI; never substitute the Gemini CLI."""
    env_override = os.environ.get("HELIOS_GOOGLE_BINARY")
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return GoogleBinary(p, source="$HELIOS_GOOGLE_BINARY")
        raise GoogleBinaryNotFound("$HELIOS_GOOGLE_BINARY is not an executable file.")

    candidates = [
        ("agy", "PATH"),
        (str(Path.home() / ".gemini" / "antigravity" / "bin" / "agy"), "~/.gemini"),
        (str(Path.home() / ".local" / "bin" / "agy"), "~/.local/bin"),
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
        "Antigravity CLI (agy) not found on PATH. "
        "Install it or set $HELIOS_GOOGLE_BINARY."
    )


@dataclass(frozen=True, slots=True)
class GoogleModel:
    id: str
    label: str


@dataclass(slots=True)
class GoogleAuthStatus:
    """Observed CLI catalog and account status, without credential contents."""

    logged_in: bool
    email: str = ""
    plan_name: str = ""
    auth_method: str = ""
    binary_path: str = ""
    models: tuple[GoogleModel, ...] = ()
    catalog_status: str = "unavailable"
    auth_warning: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error


def subscription_configuration_error() -> str:
    """Reject settings that explicitly select an API/custom provider."""
    path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    if not path.exists():
        return ""
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "Cannot verify Antigravity settings. Check ~/.gemini/antigravity-cli/settings.json."
    if not isinstance(settings, dict):
        return "Antigravity settings must be a JSON object."
    if "useG1Credits" in settings and settings["useG1Credits"] is not False:
        return (
            "Antigravity AI-credit overages are enabled or invalid. "
            "Turn off AI-credit fallback before using Helios."
        )
    provider = settings.get("modelProvider", "")
    if not isinstance(provider, str) or provider not in {"", "antigravity"}:
        return (
            "Antigravity is configured for an API/custom provider. "
            "Choose the Antigravity Google-account provider before using Helios."
        )
    return ""


def google_subscription_env() -> dict[str, str]:
    """Return a scrubbed account-only environment, refusing API settings."""
    error = subscription_configuration_error()
    if error:
        raise GoogleSubscriptionRequired(error)
    env = scrubbed_child_env()
    for name in tuple(env):
        upper = name.upper()
        if upper.startswith((
            "GEMINI_", "GOOGLE_", "ANTHROPIC_", "OPENAI_", "ANTIGRAVITY_",
            "CLAUDE_", "CODEX_", "HELIOS_", "CLOUDSDK_", "AGY_",
        )) or upper == "GCLOUD_PROJECT":
            del env[name]
    return env


_MODEL_ID = re.compile(r"gemini-[a-zA-Z0-9][a-zA-Z0-9._-]*\Z")


def parse_model_catalog(output: str) -> tuple[GoogleModel, ...]:
    """Read the actual agy models tab-separated id/label format."""
    result: list[GoogleModel] = []
    seen: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        mid, label = (part.strip() for part in parts)
        if not _MODEL_ID.fullmatch(mid) or not label or mid in seen:
            continue
        seen.add(mid)
        result.append(GoogleModel(mid, label))
    return tuple(result)


def account_quota_verified(output: str) -> bool:
    """Validate the native /usage envelope without retaining account data."""
    try:
        result = json.loads(output)
    except (ValueError, TypeError):
        return False
    if not isinstance(result, dict) or result.get("status") != "SUCCESS":
        return False
    command = result.get("command")
    if not isinstance(command, dict) or command.get("name") != "usage":
        return False
    data = command.get("data")
    if not isinstance(data, dict):
        return False
    groups = data.get("groups")
    return isinstance(groups, list) and any(
        isinstance(group, dict)
        and isinstance(group.get("name"), str)
        and group["name"]
        and isinstance(group.get("buckets"), list)
        and group["buckets"]
        and all(isinstance(bucket, dict) for bucket in group["buckets"])
        for group in groups
    )


_CACHE_SECONDS = 60.0
_CACHE_LOCK = threading.Lock()
_AUTH_CACHE: tuple[tuple, float, GoogleAuthStatus] | None = None


def _stat_key(path: Path) -> tuple:
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(path), None, None


def _cache_key(binary: GoogleBinary) -> tuple:
    return (
        _stat_key(binary.path),
        _stat_key(Path.home() / ".gemini" / "antigravity-cli" / "settings.json"),
        str(Path.home()),
    )


def cached_auth_status() -> GoogleAuthStatus | None:
    """Return a fresh cached observation without waiting for a worker probe."""
    if subscription_configuration_error():
        return None
    try:
        key = _cache_key(find_google_binary())
    except GoogleBinaryNotFound:
        return None
    if not _CACHE_LOCK.acquire(blocking=False):
        return None
    try:
        if _AUTH_CACHE and _AUTH_CACHE[0] == key:
            if time.monotonic() - _AUTH_CACHE[1] < _CACHE_SECONDS:
                return replace(_AUTH_CACHE[2], raw=dict(_AUTH_CACHE[2].raw))
        return None
    finally:
        _CACHE_LOCK.release()


def fetch_auth_status(timeout: float = 8.0, *, force: bool = False) -> GoogleAuthStatus:
    """Discover live models and account quotas; failures never imply sign-in."""
    global _AUTH_CACHE
    try:
        env = google_subscription_env()
        binary = find_google_binary()
    except (GoogleBinaryNotFound, GoogleSubscriptionRequired) as exc:
        return GoogleAuthStatus(logged_in=False, error=str(exc))
    key = _cache_key(binary)
    with _CACHE_LOCK:
        if not force and _AUTH_CACHE and _AUTH_CACHE[0] == key:
            if time.monotonic() - _AUTH_CACHE[1] < _CACHE_SECONDS:
                return replace(_AUTH_CACHE[2], raw=dict(_AUTH_CACHE[2].raw))
        status = _probe_account(binary, env, timeout)
        _AUTH_CACHE = key, time.monotonic(), status
        return replace(status, raw=dict(status.raw))


def _probe_account(binary: GoogleBinary, env: dict[str, str], timeout: float) -> GoogleAuthStatus:
    status = GoogleAuthStatus(
        logged_in=False,
        binary_path=str(binary.path),
        auth_method="oauth-unverified",
    )
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.run(
            [str(binary.path), "models"], capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except (subprocess.SubprocessError, OSError):
        status.error = "Antigravity model discovery failed. Check `agy models` in a terminal."
        return status
    if proc.returncode:
        status.error = "Antigravity could not list models. Open `agy` to check account sign-in."
        return status
    status.models = parse_model_catalog(proc.stdout)
    if not status.models:
        status.error = "Antigravity returned no Google Gemini models."
        return status
    status.catalog_status = "agy-models"
    status.auth_warning = "Account sign-in is not verified. Open `agy` to check sign-in."
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return status
    try:
        usage = subprocess.run(
            [str(binary.path), "--print", "/usage", "--output-format", "json"],
            capture_output=True, text=True, timeout=remaining, env=env,
        )
    except (subprocess.SubprocessError, OSError):
        return status
    if usage.returncode == 0 and account_quota_verified(usage.stdout):
        status.logged_in = True
        status.auth_method = "oauth-quota"
        status.plan_name = "Google account (tier not reported)"
        status.auth_warning = ""
    return status


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
