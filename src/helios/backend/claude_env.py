"""Introspect the local Claude Code environment for Helios's settings UI.

Everything here shells out to the same `claude` binary the driver uses
(located via :func:`find_claude_binary`) and parses its output. The module
is deliberately GTK-free and synchronous — callers run these from a worker
thread and marshal results back to the main loop. Three concerns:

  * **Account** — `claude auth status --json` (who's signed in, plan, method)
    plus terminal launchers for `claude auth login` / `logout`.
  * **MCP servers** — `claude mcp list` (name, transport target, health). This
    is the live, zero-API source for "our tools" (code intelligence, ollama, …).
  * **Built-in tools** — the canonical list lives in the `system/init` event,
    which only arrives once a real session starts. The driver hands us that
    array and we cache it (:func:`save_init_snapshot`); until then we fall
    back to the standard Claude Code tool set.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from helios.backend.claude_binary import ClaudeBinaryNotFound, find_claude_binary
from helios.backend.process.env_scrub import CLAUDE_AUTH_ENV, scrubbed_child_env


# ── Account ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class AuthStatus:
    """Parsed `claude auth status --json`."""

    logged_in: bool
    email: str = ""
    auth_method: str = ""        # "claude.ai" | "console" | ...
    api_provider: str = ""       # "firstParty" | "bedrock" | ...
    org_name: str = ""
    subscription_type: str = ""  # "max" | "pro" | ...
    error: str = ""              # non-empty when the query itself failed
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error


def fetch_auth_status(timeout: float = 10.0) -> AuthStatus:
    """Run `claude auth status --json` and parse it.

    Never raises — failures (no binary, timeout, non-JSON) come back as an
    AuthStatus with `error` set so the UI can show a clear message.
    """
    path = _claude_path()
    if path is None:
        return AuthStatus(False, error="No `claude` binary found.")
    try:
        proc = subprocess.run(
            [str(path), "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            # Same scoped env as the driver launch so detection and launch
            # agree (an env-only ANTHROPIC_AUTH_TOKEN survives both, or neither).
            env=scrubbed_child_env(keep=CLAUDE_AUTH_ENV),
        )
    except subprocess.TimeoutExpired:
        return AuthStatus(False, error="`claude auth status` timed out.")
    except OSError as e:
        return AuthStatus(False, error=str(e))

    stdout = (proc.stdout or "").strip()
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        # Logged-out / error builds sometimes print plain text instead of JSON.
        msg = (proc.stderr or stdout or "Could not read auth status.").strip()
        return AuthStatus(False, error=msg[:200])

    return AuthStatus(
        logged_in=bool(data.get("loggedIn")),
        email=data.get("email") or "",
        auth_method=data.get("authMethod") or "",
        api_provider=data.get("apiProvider") or "",
        org_name=data.get("orgName") or "",
        subscription_type=data.get("subscriptionType") or "",
        raw=data,
    )


# Auth methods that bill by SUBSCRIPTION, not per token. On these accounts the
# CLI's reported `cost_micro_usd` is a notional API-equivalent figure, not money
# charged, so a dollar cap measures nothing real — see is_subscription_billing.
_SUBSCRIPTION_AUTH_METHODS = frozenset({"claude.ai", "claudeai"})

_billing_cache: dict[str, bool] = {}


def is_subscription_billing(*, refresh: bool = False) -> bool:
    """Whether this account is billed by subscription rather than per token.

    Helios must not apply a dollar breaker to a subscription: the CLI's
    `cost_micro_usd` is an API-equivalent estimate there, and the 2026-08-03
    incident evidence explicitly recorded that its token totals were "not a
    billable-equivalent" (5.36B of 5.5B tokens were cached input). The real
    ceiling on these accounts is the 5-hour/weekly rate limit, which the driver
    already receives as `rate_limit_info`.

    An explicit ``ANTHROPIC_API_KEY`` wins: that is real per-token billing even
    when a subscription also exists. Fails CLOSED to False (= keep the dollar
    cap) when auth cannot be read, so an unreadable status never silently
    removes a real spend control.
    """

    if os.environ.get("ANTHROPIC_API_KEY"):
        return False
    if not refresh and "subscription" in _billing_cache:
        return _billing_cache["subscription"]
    status = fetch_auth_status()
    if not status.ok or not status.logged_in:
        return False
    subscription = (
        status.auth_method in _SUBSCRIPTION_AUTH_METHODS
        and bool(status.subscription_type)
    )
    _billing_cache["subscription"] = subscription
    return subscription


# Login method → CLI flag for `claude auth login`.
LOGIN_METHODS: dict[str, str] = {
    "claudeai": "--claudeai",   # Claude subscription (default)
    "console": "--console",     # Anthropic Console (API billing)
    "sso": "--sso",             # force SSO flow
}


def launch_login(method: str = "claudeai", email: str = "") -> tuple[bool, str]:
    """Open `claude auth login` in a terminal so the user can complete the
    interactive (browser/device-code) OAuth flow.

    Returns (ok, message). `claude auth login` needs a TTY and pops a browser,
    so we run it inside a terminal emulator rather than capturing it.
    """
    path = _claude_path()
    if path is None:
        return False, "No `claude` binary found."
    flag = LOGIN_METHODS.get(method, "--claudeai")
    parts = [shlex.quote(str(path)), "auth", "login", flag]
    if email:
        parts += ["--email", shlex.quote(email)]
    inner = " ".join(parts) + "; echo; read -rp 'Press Enter to close…'"
    return _launch_in_terminal(inner, "sign-in")


def launch_logout() -> tuple[bool, str]:
    """Open `claude auth logout` in a terminal."""
    path = _claude_path()
    if path is None:
        return False, "No `claude` binary found."
    inner = f"{shlex.quote(str(path))} auth logout; echo; read -rp 'Press Enter to close…'"
    return _launch_in_terminal(inner, "sign-out")


# ── MCP servers ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class McpServer:
    name: str
    target: str          # stdio command or http(s) URL
    status_text: str     # verbatim from `claude mcp list`
    health: str          # "connected" | "needs_auth" | "failed" | "unknown"

    @property
    def transport(self) -> str:
        return "http" if self.target.startswith(("http://", "https://")) else "stdio"


# `claude mcp list` line: "<name>: <target> - <status>" (status starts with a
# ✓ / ✗ / ! glyph). The leading "Checking MCP server health…" line and blanks
# have no ": " and are skipped.
_MCP_LINE = re.compile(r"^(?P<name>.+?):\s+(?P<rest>.+)$")


def fetch_mcp_servers(timeout: float = 30.0) -> tuple[list[McpServer], str]:
    """Run `claude mcp list` and parse it.

    Returns (servers, error). `error` is non-empty only when the command
    itself couldn't run; an empty server list with no error means "none
    configured". The command runs live MCP health checks, so it can take a
    few seconds — callers should run it off the main thread.
    """
    path = _claude_path()
    if path is None:
        return [], "No `claude` binary found."
    try:
        proc = subprocess.run(
            [str(path), "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=timeout,
            # `mcp list` runs health checks that can spawn stdio MCP servers;
            # scrub so they don't inherit the workstation's whole credential set.
            env=scrubbed_child_env(keep=CLAUDE_AUTH_ENV),
        )
    except subprocess.TimeoutExpired:
        return [], "`claude mcp list` timed out (MCP health check slow)."
    except OSError as e:
        return [], str(e)

    servers: list[McpServer] = []
    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line or ": " not in line:
            continue
        m = _MCP_LINE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        rest = m.group("rest").strip()
        # Split the trailing " - <status>" off the right; targets (URLs, file
        # paths) don't contain " - ", statuses always do.
        if " - " in rest:
            target, status_text = rest.rsplit(" - ", 1)
        else:
            target, status_text = rest, ""
        servers.append(
            McpServer(
                name=name,
                target=target.strip(),
                status_text=status_text.strip(),
                health=_classify_health(status_text),
            )
        )
    return servers, ""


def _classify_health(status_text: str) -> str:
    s = status_text.lower()
    if "✓" in status_text or ("connect" in s and "fail" not in s and "disconnect" not in s):
        return "connected"
    if "auth" in s or "!" in status_text:
        return "needs_auth"
    if "✗" in status_text or "fail" in s or "error" in s or "disconnect" in s:
        return "failed"
    return "unknown"


# ── Built-in tools (init snapshot + fallback) ───────────────────────────────


# Standard Claude Code tool set — the cold-start view shown before any session
# has run. Once a real session starts, the driver's `system/init` event gives
# us the authoritative list and we cache it (save_init_snapshot), superseding
# this. Kept deliberately short and labelled "standard set" in the UI.
STANDARD_TOOLS: tuple[str, ...] = (
    "Task", "Bash", "BashOutput", "KillShell", "Glob", "Grep", "Read",
    "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "TodoWrite",
    # `Skill`, not `SlashCommand`: the CLI retired that tool. Measured against
    # 2.1.238's own `system/init` tools array on 2026-08-21 — SlashCommand is
    # absent, Skill is present, and skills are how the model reaches a `/name`
    # now. This list is only the cold-start placeholder; a live session
    # supersedes it via save_init_snapshot.
    "Skill", "ExitPlanMode", "AskUserQuestion",
)


def _data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "helios"


_SNAPSHOT_PATH = _data_dir() / "tool_snapshot.json"


def save_init_snapshot(tools: list, mcp_servers: list) -> None:
    """Persist the `tools` + `mcp_servers` arrays from a session's init event.

    Best-effort and atomic; never raises into the caller (driver callback).
    """
    try:
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "tools": [_tool_name(t) for t in (tools or [])],
            "mcp_servers": mcp_servers or [],
            "saved_at": time.time(),
        }
        tmp = _SNAPSHOT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(_SNAPSHOT_PATH)
    except OSError:
        pass


def load_init_snapshot() -> dict | None:
    try:
        return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def builtin_tools() -> tuple[list[str], str]:
    """Return (tool_names, source) where source is "session" (captured from a
    real init event) or "standard" (the static fallback)."""
    snap = load_init_snapshot()
    if snap:
        names = [n for n in (snap.get("tools") or []) if n]
        if names:
            return sorted(names), "session"
    return list(STANDARD_TOOLS), "standard"


def _tool_name(t) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        return t.get("name") or ""
    return str(t)


# ── Shared helpers ──────────────────────────────────────────────────────────


def _claude_path() -> Path | None:
    try:
        return find_claude_binary().path
    except ClaudeBinaryNotFound:
        return None


# Terminal emulators we know how to drive, in preference order. gnome-terminal
# and its kin take `-- <argv>`; everything else honours `-e <argv>`.
_DASHDASH_TERMS = {"gnome-terminal", "kgx", "tilix"}
_TERMINAL_CANDIDATES = (
    "x-terminal-emulator", "gnome-terminal", "kgx", "konsole",
    "tilix", "kitty", "alacritty", "foot", "xterm",
)


def available_terminal() -> str | None:
    """Absolute path of the first usable terminal emulator, or None."""
    env_term = os.environ.get("TERMINAL")
    candidates = ([env_term] if env_term else []) + list(_TERMINAL_CANDIDATES)
    for name in candidates:
        if name and shutil.which(name):
            return shutil.which(name)
    return None


def _terminal_argv(term_path: str, inner_cmd: str) -> list[str]:
    name = Path(term_path).name
    sep = "--" if name in _DASHDASH_TERMS else "-e"
    return [term_path, sep, "bash", "-lc", inner_cmd]


def _launch_in_terminal(inner_cmd: str, what: str) -> tuple[bool, str]:
    term = available_terminal()
    if term is None:
        return False, f"No terminal emulator found to run the {what} flow."
    try:
        # Detach so the terminal outlives this turn and isn't killed with Helios.
        # User-initiated login shell, but still scoped: the auth flow needs
        # Anthropic auth, not the workstation's whole credential set.
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            _terminal_argv(term, inner_cmd),
            start_new_session=True,
            env=scrubbed_child_env(keep=CLAUDE_AUTH_ENV),
        )
    except OSError as e:
        return False, f"Could not open a terminal: {e}"
    return True, f"Opened {what} in a terminal — follow the prompts there."
