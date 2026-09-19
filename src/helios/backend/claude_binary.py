"""Find a working `claude` binary.

Lookup order:
  1. $HELIOS_CLAUDE_BINARY env var (explicit user override)
  2. `claude` on PATH (if executable)
  3. `~/.local/bin/claude` — GUI launches (GNOME app grid / .desktop) get a
     session environment WITHOUT `~/.local/bin` on PATH, so step 2 misses the
     standalone install exactly when Helios is started the normal way. This
     step closes that gap. (Verified 2026-06-09: the live app was silently
     spawning the VSCode extension's bundled binary because of it.)
  4. Newest standalone install under `~/.local/share/claude/versions/`
  5. Bundled binary inside the most recent VSCode extension dir
  6. Bundled binary inside the most recent JetBrains plugin dir
  7. Raise ClaudeBinaryNotFound

Steps 3-4 deliberately outrank the IDE bundles: Helios's goal is to not
depend on an IDE being installed, and the standalone install self-updates.
The IDE bundles remain as fallbacks for machines that only have the
extension. Whatever is resolved is surfaced in Settings → Account so version
skew between installs is visible instead of silent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from helios.backend.process.env_scrub import scrubbed_child_env


class ClaudeBinaryNotFound(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaudeBinary:
    path: Path
    source: str  # "env" | "PATH" | "local-bin" | "standalone" | "vscode-ext" | "jetbrains-plugin"

    def __str__(self) -> str:
        return f"{self.path} (via {self.source})"


def find_claude_binary() -> ClaudeBinary:
    # 1. env override
    env = os.environ.get("HELIOS_CLAUDE_BINARY")
    if env:
        p = Path(env).expanduser()
        if _is_runnable(p):
            return ClaudeBinary(path=p, source="env")

    # 2. PATH
    on_path = shutil.which("claude")
    if on_path:
        p = Path(on_path).resolve()
        if _is_runnable(p):
            return ClaudeBinary(path=p, source="PATH")

    # 3. ~/.local/bin/claude — present but not on PATH in GUI sessions.
    #    `_is_runnable` follows the symlink, so a dangling link is skipped.
    local_bin = Path.home() / ".local" / "bin" / "claude"
    if _is_runnable(local_bin):
        return ClaudeBinary(path=local_bin.resolve(), source="local-bin")

    # 4. Newest standalone install (the native installer's layout:
    #    one executable file per version under versions/).
    standalone = sorted(
        Path.home().glob(".local/share/claude/versions/*"),
        key=lambda p: _version_key(p.name),
        reverse=True,
    )
    for cand in standalone:
        if _is_runnable(cand):
            return ClaudeBinary(path=cand, source="standalone")

    # 5. VSCode extension bundle
    vscode_dirs = sorted(
        Path.home().glob(".vscode*/extensions/anthropic.claude-code-*-linux-x64"),
        key=lambda p: _version_key(p.name),
        reverse=True,
    )
    for d in vscode_dirs:
        cand = d / "resources" / "native-binary" / "claude"
        if _is_runnable(cand):
            return ClaudeBinary(path=cand, source="vscode-ext")

    # 6. JetBrains plugin bundle (best-effort path; structure may vary)
    jb_dirs = sorted(
        Path.home().glob(".local/share/JetBrains/*/claude-code/*linux-x64/resources/native-binary/claude"),
        reverse=True,
    )
    for cand in jb_dirs:
        if _is_runnable(cand):
            return ClaudeBinary(path=cand, source="jetbrains-plugin")

    raise ClaudeBinaryNotFound(
        "Could not find a working `claude` binary. Set $HELIOS_CLAUDE_BINARY "
        "or fix your `claude` install."
    )


# ---------------------------------------------------------------------------
# Capability probes for optional CLI flags.
# ---------------------------------------------------------------------------

# Cache help text per binary fingerprint so all capability probes share one
# bounded subprocess call.
_help_text_cache: dict[tuple[str, int], str] = {}
_version_cache: dict[tuple[str, int], tuple[int, ...]] = {}

# Anthropic documents full budget-family enforcement (preventing new
# subagents and stopping still-running background subagents when the cap is
# reached) for Claude Code 2.1.217 and newer.  A CLI that merely accepts the
# flag is not enough for Helios's interactive safety contract.
_MIN_BUDGET_FAMILY_VERSION = (2, 1, 217)


def supports_effort_flag() -> bool:
    """Return True if the resolved `claude` binary advertises `--effort` in its
    help text.  Cached per (path, mtime) so it re-checks once after a self-
    update but otherwise costs nothing.  Returns False on any error."""
    return _supports_flag("--effort")


def supports_max_budget_usd_flag() -> bool:
    """Whether this Claude CLI supports the print-mode budget breaker."""

    return _supports_flag("--max-budget-usd")


def supports_forward_subagent_text() -> bool:
    """Whether this CLI can forward subagent text into the root stream.

    With `--forward-subagent-text` (2.1.211+) the CLI emits each child's
    complete `assistant`/`user` records tagged with `parent_tool_use_id` —
    full messages, not stream deltas (measured on 2.1.241, 2026-08-25).
    """

    return _supports_flag("--forward-subagent-text")


def supports_budget_family_enforcement() -> bool:
    """Whether Claude can enforce the budget across its process family.

    Interactive Helios sessions can use Claude subagents, so fail closed
    unless both the native budget flag and Anthropic's documented minimum
    version for background-subagent termination are verified.
    """

    return (
        supports_max_budget_usd_flag()
        and (_version := _claude_cli_version()) is not None
        and _version >= _MIN_BUDGET_FAMILY_VERSION
    )


#: Optional CLI flags Helios passes when the binary advertises them, paired
#: with what stops working when it does not. Every entry here is a real
#: silent-degradation path: the flag is dropped from argv, the feature simply
#: stops happening, and nothing anywhere says why.
_OPTIONAL_FLAGS: dict[str, str] = {
    "--effort": "the reasoning-effort slider",
    "--max-budget-usd": "Claude's own spend cap (Helios stops the turn instead)",
    "--forward-subagent-text": "live subagent text in the Agent Dock",
}


def capability_report() -> dict[str, object]:
    """Warm every capability probe once and name what this CLI cannot do.

    Deliberately *not* a capability-negotiation framework. The CLI has no
    machine-readable capability list to negotiate against: the `initialize`
    control response carries no `capabilities` key at all (measured on
    2.1.245), and `system/init.capabilities` lists interrupt/message-lifecycle
    protocol versions Helios does not use (it interrupts with SIGINT). What
    the CLI *does* expose is its `--help` and its version, which is exactly
    what the optional-flag paths already read — so the preflight is the
    existing probes run once, together, with their consequences written down.

    One subprocess: `_supports_flag` caches the whole help text per
    (path, mtime), so the first probe pays for all of them.
    """

    version = _claude_cli_version()
    degraded: list[str] = []
    for flag, consequence in _OPTIONAL_FLAGS.items():
        if not _supports_flag(flag):
            degraded.append(f"{consequence} (no {flag})")
    # A CLI that accepts --max-budget-usd but predates documented
    # background-subagent termination is a *different* degradation from not
    # having the flag, and only one of the two can be true at a time.
    if _supports_flag("--max-budget-usd") and not supports_budget_family_enforcement():
        floor = ".".join(str(n) for n in _MIN_BUDGET_FAMILY_VERSION)
        degraded.append(
            f"budget enforcement across Claude's subagents (needs {floor}+)"
        )
    return {
        "version": ".".join(str(n) for n in version) if version else "",
        "degraded": degraded,
    }


def _claude_cli_version() -> tuple[int, ...] | None:
    try:
        binary = find_claude_binary()
        key = (str(binary.path), binary.path.stat().st_mtime_ns)
    except Exception:
        return None
    if key in _version_cache:
        return _version_cache[key]
    try:
        result = subprocess.run(
            [str(binary.path), "--version"],
            capture_output=True,
            text=True,
            env=scrubbed_child_env(),  # --version needs no credentials
            timeout=4,
        )
        if result.returncode != 0:
            return None
        version = _version_key(result.stdout + "\n" + result.stderr)
        if version == (0,):
            return None
    except Exception:
        # As with the help probe, do not make a transient failure permanent.
        return None
    _version_cache[key] = version
    return version


def _supports_flag(flag: str) -> bool:
    try:
        binary = find_claude_binary()
        key = (str(binary.path), binary.path.stat().st_mtime_ns)
    except Exception:
        return False
    cached = _help_text_cache.get(key)
    if cached is not None:
        return flag in cached
    try:
        result = subprocess.run(
            [str(binary.path), "--help"],
            capture_output=True,
            text=True,
            env=scrubbed_child_env(),  # --help needs no credentials
            # Bounded low: normally warmed off-thread at startup, but if a cold
            # send ever hits it, a slow/hung `claude --help` must not freeze the
            # GTK main loop for long. Timeout means "unverified"; callers that
            # require a safety capability must fail startup closed.
            timeout=4,
        )
        help_text = result.stdout + "\n" + result.stderr
    except Exception:
        # A transient timeout must not poison every later capability check in
        # this process. Callers with a required safety flag fail closed and a
        # later explicit retry gets a fresh probe.
        return False
    _help_text_cache[key] = help_text
    return flag in help_text


def _is_runnable(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def _version_key(name: str) -> tuple:
    # "anthropic.claude-code-2.1.145-linux-x64" or "2.1.145" -> (2, 1, 145)
    import re

    m = re.search(r"(\d+(?:\.\d+){2,})", name)
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))
