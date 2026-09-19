"""Explicit estate MCP selection; never import project-supplied processes/secrets.

Both Codex's native stdio proxy and Helios's OpenRouter client resolve the same
user-selected definitions. Credentials remain in their existing local source.

The selection file (``estate-mcp.json`` in the state dir) is the reviewed
allowlist: it names each selected server and pins the environment variable
names that server may receive. Both were shown to the operator by the setup
preview; a later edit to the source definition that adds an environment name
is refused rather than silently forwarded.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from helios.paths import state_dir

SELECTION_VERSION = 2
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
BASE_ENV = {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SSH_AUTH_SOCK"}


class EstateConfigError(ValueError):
    """Messages contain field names/reasons only, never configuration values."""


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)

    def child_env(self) -> dict[str, str]:
        return {**{k: v for k, v in os.environ.items() if k in BASE_ENV}, **self.env}


def read_json(path: Path, limit: int = 4 * 1024 * 1024) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise EstateConfigError("Configuration exceeds the byte limit")
        data = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as error:
        raise EstateConfigError("Configuration is unreadable or invalid") from error
    if not isinstance(data, dict):
        raise EstateConfigError("Configuration must be an object")
    return data


def server_config(name: str, definition: object, allowed_env: frozenset[str]) -> ServerConfig:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise EstateConfigError("Server name is not a plain lowercase identifier")
    if not isinstance(definition, dict) or definition.get("type", "stdio") != "stdio":
        raise EstateConfigError("Only installed stdio servers are supported")
    if set(definition) - {"type", "command", "args", "env"}:
        raise EstateConfigError("Server has unsupported configuration fields")
    command, args, env = definition.get("command"), definition.get("args", []), definition.get("env", {})
    if not isinstance(command, str) or not Path(command).is_absolute() or "\0" in command:
        raise EstateConfigError("Server command must be an absolute executable path")
    if not isinstance(args, list) or len(args) > 32 or any(
        not isinstance(arg, str) or "\0" in arg or len(arg) > 8192 for arg in args
    ):
        raise EstateConfigError("Invalid server arguments")
    if not isinstance(env, dict) or set(env) - set(allowed_env):
        raise EstateConfigError("Server contains environment names outside its reviewed allowlist")
    if any(not isinstance(v, str) or "\0" in v for v in env.values()):
        raise EstateConfigError("Invalid server environment values")
    # No shell expansion, ${VAR} interpolation or ambient secret inheritance.
    return ServerConfig(name, command, tuple(args), dict(env))


def pinned_env_names(definition: object) -> list[str]:
    """The environment names a definition carries now; setup pins them for review."""
    env = definition.get("env", {}) if isinstance(definition, dict) else {}
    if not isinstance(env, dict) or any(not isinstance(k, str) or not ENV_NAME_RE.fullmatch(k) for k in env):
        raise EstateConfigError("Server environment names must be plain uppercase identifiers")
    return sorted(env)


def load_selected(manifest: Path | None = None) -> list[ServerConfig]:
    path = manifest or state_dir() / "estate-mcp.json"
    if not path.exists():
        return []
    selection = read_json(path, 16 * 1024)
    servers = selection.get("servers")
    source = selection.get("source")
    if (selection.get("version") != SELECTION_VERSION or not isinstance(servers, dict) or not servers
            or not all(isinstance(n, str) and NAME_RE.fullmatch(n) and isinstance(allowed, list)
                       and all(isinstance(e, str) and ENV_NAME_RE.fullmatch(e) for e in allowed)
                       for n, allowed in servers.items())
            or not isinstance(source, str) or not Path(source).is_absolute()):
        raise EstateConfigError("Invalid estate selection; rerun reviewed setup")
    definitions = read_json(Path(source)).get("mcpServers", {})
    if not isinstance(definitions, dict):
        raise EstateConfigError("Source has no MCP server object")
    return [server_config(name, definitions.get(name), frozenset(allowed))
            for name, allowed in servers.items()]
