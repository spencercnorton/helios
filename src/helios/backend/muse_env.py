"""Introspect the local Muse environment for Helios's settings UI.

Muse has no `auth status` command. The honest probe is the wire itself:
a short `muse serve` handshake answers `account/read` (experimental surface),
`model/list`, and `usage/read` without a model call. `META_API_KEY` always
overrides the stored account login, so probes run with the same scrubbed
environment as the driver and detection agrees with launch.

Credentials stay owned by the CLI (OS keychain / its own store). Helios never
reads or copies the secret: sign-in runs `muse login` in a terminal, and a
pasted API key is piped to `muse auth set --api-key-stdin` only.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from helios.backend.muse_binary import MuseBinaryNotFound, find_muse_binary
from helios.backend.process.env_scrub import MUSE_AUTH_ENV, scrubbed_child_env
from helios.backend.sensitive_text import scrub_sensitive

CLIENT_NAME = "helios"
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def uuid7() -> str:
    """A UUIDv7 string. MSP command ids must be UUIDv7; `uuid.uuid7` only
    exists on newer Pythons, so older ones get the same layout by hand."""
    factory = getattr(uuid, "uuid7", None)
    if factory is not None:
        return str(factory())
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    raw = bytearray(os.urandom(16))
    raw[0:6] = timestamp_ms.to_bytes(6, "big")
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


class MuseServeError(RuntimeError):
    """A `muse serve` probe could not complete."""


def _serve_argv(binary: Path) -> list[str]:
    # Probes must never write session logs or run tools.
    return [
        str(binary), "serve", "--provider", "meta",
        "--no-session-log", "--disable-shell", "--disable-write",
    ]


def serve_query(
    requests: list[tuple[str, dict[str, Any]]],
    *,
    binary: Path | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Run queries against a throwaway `muse serve` host.

    Returns {method: result}. Raises MuseServeError on spawn failure,
    timeout, or a JSON-RPC error for any requested method.
    """
    resolved = binary or find_muse_binary().path
    try:
        proc = subprocess.Popen(
            _serve_argv(resolved),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=scrubbed_child_env(keep=MUSE_AUTH_ENV),
            bufsize=0,
            start_new_session=True,
        )
    except OSError as exc:
        raise MuseServeError("Could not start `muse serve`.") from exc
    deadline = time.monotonic() + timeout
    sequence = 0
    pending: dict[int, str] = {}
    results: dict[str, Any] = {}

    def send(method: str, params: dict[str, Any] | None, *, notify: bool = False) -> None:
        nonlocal sequence
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            sequence += 1
            message["id"] = sequence
            pending[sequence] = method
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()

    def await_result(method: str) -> Any:
        assert proc.stdout is not None
        while method not in results:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MuseServeError(f"`muse serve` timed out waiting for {method}.")
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                raise MuseServeError(f"`muse serve` timed out waiting for {method}.")
            line = proc.stdout.readline()
            if not line:
                raise MuseServeError("`muse serve` exited during the probe.")
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(message, dict) or "id" not in message:
                continue  # notification; probes only need results
            name = pending.pop(message["id"], None)
            if name is None:
                continue
            if "error" in message:
                detail = str((message["error"] or {}).get("message") or "request failed")
                raise MuseServeError(f"`muse serve` refused {name}: {detail[:300]}")
            results[name] = message.get("result")
        return results[method]

    try:
        from helios import __version__ as helios_version

        send("initialize", {
            "clientInfo": {"name": CLIENT_NAME, "version": helios_version},
            # account/read lives on the experimental surface; the stable
            # fingerprint is still what the host reports and serves.
            "capabilities": {"experimentalApi": True},
        })
        await_result("initialize")
        send("initialized", None, notify=True)
        for method, params in requests:
            send(method, params)
            await_result(method)
        return results
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass


# ── Account + catalog ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MuseModel:
    id: str
    label: str
    context_limit: int = 0
    output_limit: int = 0
    reasoning_efforts: tuple[tuple[str, str], ...] = ()
    default_effort: str = ""
    is_default: bool = False


@dataclass(slots=True)
class MuseAuthStatus:
    """One `muse serve` account/catalog probe. Never carries a secret."""

    logged_in: bool
    state: str = ""  # loggedOut | envKey | apiKey | accountLogin
    label: str = ""  # account label/email or "META_API_KEY"
    models: tuple[MuseModel, ...] = ()
    catalog_status: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _parse_models(result: Any) -> tuple[MuseModel, ...]:
    if not isinstance(result, dict):
        return ()
    models: list[MuseModel] = []
    for row in result.get("models") or []:
        if not isinstance(row, dict) or not row.get("modelId"):
            continue
        variants = row.get("variants")
        efforts: tuple[tuple[str, str], ...] = ()
        if isinstance(variants, list):
            efforts = tuple(
                (str(v), str(v)) for v in variants if str(v) in _EFFORTS
            )
        default_effort = row.get("defaultReasoningEffort")
        models.append(MuseModel(
            id=str(row["modelId"]),
            label=str(row.get("displayLabel") or row["modelId"]),
            context_limit=row.get("contextLimit") if isinstance(row.get("contextLimit"), int) else 0,
            output_limit=row.get("outputLimit") if isinstance(row.get("outputLimit"), int) else 0,
            reasoning_efforts=efforts,
            default_effort=default_effort if isinstance(default_effort, str) else "",
            is_default=bool(row.get("isDefault")),
        ))
    return tuple(models)


_cache: MuseAuthStatus | None = None


def cached_auth_status() -> MuseAuthStatus | None:
    return _cache


def fetch_auth_status(*, force: bool = False, timeout: float = 15.0) -> MuseAuthStatus:
    """Probe account state and the model catalog. Never raises."""
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        binary = find_muse_binary().path
    except MuseBinaryNotFound as exc:
        status = MuseAuthStatus(False, error=str(exc), catalog_status="no-binary")
        _cache = status
        return status
    try:
        results = serve_query(
            [("account/read", {}), ("model/list", {}), ("usage/read", {})],
            binary=binary,
            timeout=timeout,
        )
    except MuseServeError as exc:
        detail, _ = scrub_sensitive(str(exc))
        status = MuseAuthStatus(False, error=detail[:400], catalog_status="probe-failed")
        _cache = status
        return status
    account = results.get("account/read") if isinstance(results.get("account/read"), dict) else {}
    state = str(account.get("state") or "")
    models = _parse_models(results.get("model/list"))
    usage_result = results.get("usage/read")
    usage = usage_result.get("usage") if isinstance(usage_result, dict) else None
    status = MuseAuthStatus(
        logged_in=state in {"accountLogin", "apiKey", "envKey"},
        state=state,
        label=str(account.get("label") or ""),
        models=models,
        catalog_status="muse-serve" if models else "empty-catalog",
        usage=usage if isinstance(usage, dict) else {},
    )
    _cache = status
    return status


def is_subscription_billing(*, refresh: bool = False) -> bool:
    """True only for a proven Meta-account login. Fails closed to False."""
    status = fetch_auth_status(force=refresh)
    return status.ok and status.state == "accountLogin"


# ── Sign-in / sign-out / API key (the CLI owns every secret) ─────────────────


def _muse_path() -> Path | None:
    try:
        return find_muse_binary().path
    except MuseBinaryNotFound:
        return None


def launch_login() -> tuple[bool, str]:
    """Open `muse login` in a terminal (browser code approval needs a TTY)."""
    from helios.backend.claude_env import _launch_in_terminal

    path = _muse_path()
    if path is None:
        return False, "No `muse` binary found."
    inner = f"{shlex.quote(str(path))} login; echo; read -rp 'Press Enter to close…'"
    return _launch_in_terminal(inner, "sign-in")


def launch_logout() -> tuple[bool, str]:
    """Open `muse logout` in a terminal."""
    from helios.backend.claude_env import _launch_in_terminal

    path = _muse_path()
    if path is None:
        return False, "No `muse` binary found."
    inner = f"{shlex.quote(str(path))} logout; echo; read -rp 'Press Enter to close…'"
    return _launch_in_terminal(inner, "sign-out")


def set_api_key(key: str) -> tuple[bool, str]:
    """Persist a Meta API key through the CLI's own stdin-only lane.

    The key is never logged, never placed on argv, and never stored by
    Helios. Returns (ok, message) with any CLI output scrubbed.
    """
    global _cache
    path = _muse_path()
    if path is None:
        return False, "No `muse` binary found."
    if not key or not key.strip():
        return False, "Enter an API key first."
    try:
        proc = subprocess.run(
            [str(path), "auth", "set", "--provider", "meta", "--api-key-stdin"],
            input=key.strip(),
            capture_output=True,
            text=True,
            timeout=15.0,
            env=scrubbed_child_env(keep=MUSE_AUTH_ENV),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not save the API key ({type(exc).__name__})."
    if proc.returncode != 0:
        detail, _ = scrub_sensitive(((proc.stderr or proc.stdout) or "").strip())
        return False, (detail[:300] or "The Muse CLI refused the API key.")
    _cache = None
    return True, "API key saved by the Muse CLI."



def list_mcp_servers() -> list[dict[str, Any]]:
    """Read configured MCP servers from ~/.config/muse/settings.json."""
    config_path = Path.home() / ".config" / "muse" / "settings.json"
    if not config_path.is_file():
        return []
    try:
        raw = config_path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        data = json.loads(raw)
        servers = data.get("mcpServers") or {}
        if not isinstance(servers, dict):
            return []
        result = []
        for name, cfg in servers.items():
            if isinstance(cfg, dict):
                result.append({
                    "name": str(name),
                    "command": str(cfg.get("command") or cfg.get("url") or "stdio"),
                    "args": list(cfg.get("args") or []),
                })
        return result
    except Exception:
        return []
