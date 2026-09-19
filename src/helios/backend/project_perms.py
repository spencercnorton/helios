"""Permission profiles shared by the Helios UI and provider runtimes.

Contract: unconfigured conversations use the configured global default, or
SAFE_FALLBACK_MODE ("Ask") when none is set or the persisted value is
corrupt/unknown. Bypass (full agentic access) is selectable for every supported
provider as an explicit choice. Corrupt/unknown state, legacy workspace Bypass,
and an unconfirmed persisted global default still resolve to Ask/Read only.

Permissions are independent of execution controls: choosing Bypass does not
change Work budgets, cancellation, native agent policy, or Plan workflow.
HOME as cwd continues to force read-only permissions for every provider.

Helios versions through v0.31 stored per-workspace overrides in
``project-perms.json``. The editor for that policy no longer exists, so the old
store API remains retired. A read-only migration adapter preserves restrictive
choices as visibly labeled safeguards; legacy Bypass is clamped to Ask and is
never inherited as unrestricted access.

The descriptor table is the single UI/runtime source of truth for both Claude
and Codex; `codex_permission_profile` translates each Helios mode to an App
Server approval policy + sandbox without weakening what the user selected.

Scope note: these modes govern the provider's *approval prompting and sandbox*,
plus (via env_scrub) which credentials the child inherits. They do NOT sandbox
the filesystem, HOME, or file-backed secrets — Bypass in particular is full
agentic access. Real confinement is future supervisor/isolation work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from helios.paths import state_dir


# Provider ids, duplicated from model_catalog as plain strings so the policy
# table stays import-free (model_catalog must be able to import this module).
ALL_PROVIDERS = frozenset({"anthropic", "openai", "openrouter"})


@dataclass(frozen=True, slots=True)
class PermissionMode:
    key: str
    label: str
    description: str
    codex_approval_policy: str
    codex_sandbox: str
    # Egress for the Codex sandbox. False makes every network-touching command
    # fail inside the sandbox and escalate to an approval prompt, so this is
    # the second prompt source after the approval policy itself.
    codex_network: bool = False
    # Which providers may select this mode. Unknown providers fail closed.
    providers: frozenset[str] = ALL_PROVIDERS


# Keep this ordering consistent anywhere a compact permission picker is used.
# The OpenAI mapping follows App Server's approval/sandbox contract: Ask keeps
# work project-scoped and prompts when Codex needs broader permissions.
PERMISSION_MODE_DESCRIPTORS: tuple[PermissionMode, ...] = (
    PermissionMode(
        "default",
        "Ask",
        "Work inside the project; ask before broader or unsandboxed access.",
        "on-request",
        "workspace-write",
    ),
    PermissionMode(
        "acceptEdits",
        "Accept edits",
        "Auto-approve trusted edits; still review untrusted actions.",
        # NOT Codex `untrusted`. Measured 2026-08-23: `untrusted` prompts for
        # every command not on Codex's read-only trusted list -- `date` and
        # `wc -l` included -- so a 10-command turn raised 10 approvals while
        # Ask raised 0. That is stricter than Ask, but the picker presents this
        # mode as looser, so the mapping was inverted. `on-request` +
        # workspace-write is what the label already promises: in-workspace
        # edits run unprompted, escalation still asks.
        "on-request",
        "workspace-write",
        codex_network=True,
    ),
    PermissionMode(
        "auto",
        "Auto",
        "Use guarded automation and prompt only when escalation is needed.",
        "on-request",
        "workspace-write",
        codex_network=True,
    ),
    PermissionMode(
        "bypassPermissions",
        "Bypass",
        "Full agentic access, unsandboxed: no approval prompts. Unrelated and "
        "cross-provider credentials are scrubbed from the child, but HOME, "
        "file-backed secrets, and the forwarded SSH agent (ssh/push as you) "
        "are not sandboxed. HOME as cwd stays read-only regardless.",
        "never",
        "danger-full-access",
        codex_network=True,
    ),
    PermissionMode(
        "plan",
        "Plan",
        "Read-only investigation. Claude writes a plan and submits it for "
        "your approval; approving it switches the conversation out of Plan.",
        "never",
        "read-only",
    ),
    PermissionMode(
        "dontAsk",
        "Never ask",
        "Stay workspace-scoped and refuse actions requiring escalation.",
        "never",
        "workspace-write",
    ),
)

PERMISSION_MODES = tuple(mode.key for mode in PERMISSION_MODE_DESCRIPTORS)
MODE_LABELS = {mode.key: mode.label for mode in PERMISSION_MODE_DESCRIPTORS}
MODE_DESCRIPTIONS = {mode.key: mode.description for mode in PERMISSION_MODE_DESCRIPTORS}
_MODE_BY_KEY = {mode.key: mode for mode in PERMISSION_MODE_DESCRIPTORS}

# The starting mode for a conversation with no explicit choice. "default"
# ("Ask") is the safest mode both providers actually implement: Claude prompts
# before each tool, Codex App Server runs on-request / workspace-write.
SAFE_FALLBACK_MODE = "default"

# Autonomy — full agentic access. Selectable as an explicit global default
# (with a warning in the picker) and honored only when confirmed; never the
# fail-safe for corrupt/unknown state.
AUTONOMY_MODE = "bypassPermissions"

# Every executable mode may be selected as the global default. The safety line
# is elsewhere: sanitize_global_default() coerces unknown/corrupt values to the
# safe fallback, resolve_startup_default() requires an explicit confirmation
# before honoring a persisted Bypass, and a provider that cannot select Bypass
# resolves it to Ask.
GLOBAL_DEFAULT_MODES = PERMISSION_MODES


def permission_description(mode: str, *, provider: str) -> str:
    """Describe the selected runtime's actual approval scope in the picker."""
    if provider == "openrouter":
        descriptions = {
            "default": "Read project files freely; ask before edits, commands, or external tools. Approval can cover an exact command or one tool for the open session.",
            "acceptEdits": "Read and edit project files freely. Commands and external tools need approval; explicit session grants avoid repeating it.",
            "auto": "Read and edit project files freely. Commands and external tools need approval; explicit session grants avoid repeating it.",
            "bypassPermissions": (
                "Full agentic access, no approval prompts: reads, edits, commands, and "
                "estate tools run as you, inside and outside the project. File contents "
                "and command output are sent to the selected OpenRouter endpoint. "
                "Spend is bounded only by the Work allowance and tool rounds."
            ),
            "plan": "Read-only investigation inside the project. Commands, edits, and external tools are refused.",
            "dontAsk": "Read project files without prompts; refuse edits, commands, and external tools that require approval.",
        }
        if mode in descriptions:
            return descriptions[mode]
    return MODE_DESCRIPTIONS.get(mode, MODE_DESCRIPTIONS[SAFE_FALLBACK_MODE])


def modes_for_provider(provider: str) -> tuple[str, ...]:
    """Mode keys ``provider`` may select, in canonical picker order."""

    return tuple(
        mode.key
        for mode in PERMISSION_MODE_DESCRIPTORS
        if provider in mode.providers
    )


def provider_allows_mode(provider: str, mode: str) -> bool:
    """Whether ``provider`` may execute ``mode`` at all."""

    descriptor = _MODE_BY_KEY.get(mode)
    return descriptor is not None and provider in descriptor.providers


def effective_provider_mode(provider: str, mode: str) -> str:
    """Narrow ``mode`` to something ``provider`` can actually select.

    Fail-closed: a mode this provider is not allowed to select — however it got
    persisted, staged, or inherited from a global default — becomes Ask rather
    than executing with the wrong provider's semantics.
    """

    safe_mode = sanitize_global_default(mode)
    if provider_allows_mode(provider, safe_mode):
        return safe_mode
    return SAFE_FALLBACK_MODE


@dataclass(frozen=True, slots=True)
class LegacyPermission:
    """Visible compatibility policy from the retired workspace store."""

    mode: str
    original_mode: str
    invalid: bool = False

    @property
    def bypass_retired(self) -> bool:
        return self.original_mode == AUTONOMY_MODE


def sanitize_global_default(mode: str) -> str:
    """Coerce a persisted/selected GLOBAL default to a valid mode.

    Every real mode — including Bypass — is a valid global default, so only an
    unknown/corrupt string falls back to safe. Full access stays an explicit
    choice, while garbage never resolves to it.
    """
    if mode not in PERMISSION_MODES:
        return SAFE_FALLBACK_MODE
    return mode


def resolve_startup_default(mode: str, *, confirmed: bool) -> str:
    """Resolve the persisted global default at startup.

    An explicitly confirmed choice — including Bypass selected in Settings — is
    honored. An unconfirmed or pre-hardening persisted Bypass is clamped to the
    safe fallback until the user reconfirms it, so full access is never silently
    inherited across an upgrade. Non-Bypass modes and invalid strings resolve as
    sanitize_global_default does.
    """
    resolved = sanitize_global_default(mode)
    if resolved == AUTONOMY_MODE and not confirmed:
        return SAFE_FALLBACK_MODE
    return resolved


def more_restrictive_mode(first: str, second: str) -> str:
    """Return the safer of two valid permission modes.

    Used only for upgrade fallback projection; an explicit conversation
    choice remains authoritative after the user makes it.
    """

    safe_first = sanitize_global_default(first)
    safe_second = sanitize_global_default(second)
    return min(
        safe_first,
        safe_second,
        key=lambda mode: _LEGACY_RESTRICTIVENESS[mode],
    )


def canonical_cwd(cwd: str) -> str:
    """Canonical key for a workspace path: resolve symlinks + ``..`` so aliases
    of the same directory collapse to one entry. Empty stays empty; a
    non-existent path is resolved lexically against its existing prefix."""
    if not cwd:
        return cwd
    try:
        return os.path.realpath(cwd)
    except OSError:
        return os.path.normpath(cwd)


PROTECTED_HOME_CWD = canonical_cwd(str(Path.home()))


def is_home_cwd(cwd: str) -> bool:
    """Whether ``cwd`` resolves to the user's HOME directory itself."""

    return bool(cwd) and canonical_cwd(cwd) == PROTECTED_HOME_CWD


def effective_execution_mode(mode: str, cwd: str, *, provider: str) -> str:
    """Clamp a mode to what this provider and cwd actually allow.

    The single chokepoint every driver and the spawn path route through, so
    both clamps are enforced once: a mode the provider may not select narrows
    to Ask, and HOME as cwd is read-only regardless of mode. ``provider`` is
    keyword-only and required — a defaulted provider here would silently grant
    permissions to whichever caller forgot to pass one.
    """

    safe_mode = effective_provider_mode(provider, mode)
    return "plan" if is_home_cwd(cwd) else safe_mode


def execution_mode_restriction_reason(
    mode: str,
    cwd: str,
    *,
    provider: str,
) -> str:
    """Explain why ``mode`` would be narrowed, or return an empty string."""

    if effective_execution_mode(mode, cwd, provider=provider) == mode:
        return ""
    if is_home_cwd(cwd):
        return "HOME is locked to read-only permissions"
    return "This provider does not support the selected permission mode"


_LEGACY_LOCK = Lock()
_LEGACY_CACHE_KEY: tuple[str, int, int] | None = None
_LEGACY_CACHE: dict[str, str] = {}
_LEGACY_INVALID_CWDS: frozenset[str] = frozenset()
_LEGACY_GLOBAL_MALFORMED = False
_LEGACY_RESTRICTIVENESS = {
    "plan": 0,
    "dontAsk": 1,
    "default": 2,
    "acceptEdits": 3,
    "auto": 4,
    "bypassPermissions": 5,
}


def _legacy_path() -> Path:
    return state_dir() / "project-perms.json"


def _load_legacy_permissions(
    path: Path,
) -> tuple[dict[str, str], frozenset[str], bool]:
    """Read the retired workspace map without ever writing or broadening it."""

    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, frozenset(), True
    if not isinstance(raw, dict):
        return {}, frozenset(), True
    data: dict[str, str] = {}
    invalid_cwds: set[str] = set()
    global_malformed = False
    for raw_cwd, raw_mode in raw.items():
        if not isinstance(raw_cwd, str):
            global_malformed = True
            continue
        cwd = canonical_cwd(raw_cwd)
        if not cwd:
            global_malformed = True
            continue
        if not isinstance(raw_mode, str) or raw_mode not in PERMISSION_MODES:
            # The bad value is attributable to one canonical workspace, so
            # fail closed there without locking every unrelated project.
            invalid_cwds.add(cwd)
            data.pop(cwd, None)
            continue
        if cwd in invalid_cwds:
            # An invalid alias for this workspace remains authoritative even
            # if another alias has a syntactically valid value.
            continue
        existing = data.get(cwd)
        data[cwd] = (
            raw_mode
            if existing is None
            else min(
                existing,
                raw_mode,
                key=lambda mode: _LEGACY_RESTRICTIVENESS[mode],
            )
        )
    return data, frozenset(invalid_cwds), global_malformed


def legacy_permission(cwd: str) -> LegacyPermission | None:
    """Return a disclosed upgrade fallback from the retired workspace file.

    Existing non-Bypass choices remain effective until the conversation gets
    its own explicit record, preventing an upgrade from silently widening Plan,
    Never ask, Ask, Accept edits, or Auto. Legacy Bypass is retired to Ask
    because unrestricted access must now be re-confirmed per conversation.
    """

    global _LEGACY_CACHE_KEY, _LEGACY_CACHE
    global _LEGACY_INVALID_CWDS, _LEGACY_GLOBAL_MALFORMED
    key = canonical_cwd(cwd)
    if not key:
        return None
    path = _legacy_path()
    try:
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        cache_key = (str(path), -1, -1)
    with _LEGACY_LOCK:
        if cache_key != _LEGACY_CACHE_KEY:
            if cache_key[1] >= 0:
                (
                    _LEGACY_CACHE,
                    _LEGACY_INVALID_CWDS,
                    _LEGACY_GLOBAL_MALFORMED,
                ) = _load_legacy_permissions(path)
            else:
                _LEGACY_CACHE = {}
                _LEGACY_INVALID_CWDS = frozenset()
                _LEGACY_GLOBAL_MALFORMED = False
            _LEGACY_CACHE_KEY = cache_key
        original = _LEGACY_CACHE.get(key)
        invalid = _LEGACY_GLOBAL_MALFORMED or key in _LEGACY_INVALID_CWDS
    if invalid:
        return LegacyPermission(
            mode="plan",
            original_mode="invalid",
            invalid=True,
        )
    if original is None:
        return None
    return LegacyPermission(
        mode=(SAFE_FALLBACK_MODE if original == AUTONOMY_MODE else original),
        original_mode=original,
    )


def reload_legacy_permissions() -> None:
    """Drop the read-only legacy cache after state-dir changes in tests."""

    global _LEGACY_CACHE_KEY, _LEGACY_CACHE
    global _LEGACY_INVALID_CWDS, _LEGACY_GLOBAL_MALFORMED
    with _LEGACY_LOCK:
        _LEGACY_CACHE_KEY = None
        _LEGACY_CACHE = {}
        _LEGACY_INVALID_CWDS = frozenset()
        _LEGACY_GLOBAL_MALFORMED = False


def codex_permission_profile(mode: str) -> tuple[str, str, str, bool]:
    """Return ``(approvalPolicy, sandbox, approvalsReviewer, network)`` for Codex.

    Fails closed to Ask for any mode Codex may not select, so an unknown or
    wrong-provider string can never resolve to Bypass's unsandboxed pair.
    """

    selected = _MODE_BY_KEY.get(
        effective_provider_mode("openai", mode), _MODE_BY_KEY["default"]
    )
    # ``auto_review`` is implemented by Codex as a reviewer subagent.  P0
    # containment disables *all* implicit delegation, including approval-time
    # delegation that is separate from the model-visible multi-agent tools.
    # Auto keeps its intended guarded workspace-write/on-request semantics; it
    # simply routes the escalation to the user instead of another model.
    return (
        selected.codex_approval_policy,
        selected.codex_sandbox,
        "user",
        selected.codex_network,
    )
