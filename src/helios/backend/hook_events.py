"""GTK-free summarization of Claude CLI hook lifecycle events.

`ClaudeCliDriver` emits one `hook-event` payload per hook record it sees on
the wire (subtype `hook_started`, `hook_progress`, or `hook_response`). Most
of that is pure progress noise — `summarize_hook` reduces it to the rare row
worth a transcript entry: a hook that blocked/asked, or one that failed
outright.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from helios.backend.sensitive_text import scrub_sensitive

_BLOCKING = {"block", "deny"}
_ASKING = {"ask"}


@dataclass
class HookNotice:
    severity: str  # "info" | "warning" | "error"
    title: str
    detail: str


def summarize_hook(payload: dict) -> HookNotice | None:
    """Reduce one hook CLI record to a transcript notice, or None to drop it.

    hook_started/hook_progress are pure progress and never surfaced. A
    hook_response that exited 0 with outcome "success" and no block/deny/ask
    decision is equally uninteresting. A decision earns a warning (the hook
    did its job, on purpose); a nonzero exit or non-success outcome earns an
    error.
    """
    if payload.get("subtype") != "hook_response":
        return None
    hook_event = payload.get("hook_event") or "?"
    hook_name = payload.get("hook_name") or "?"
    stdout = payload.get("stdout") or ""

    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    hook_specific = data.get("hookSpecificOutput")
    hook_specific = hook_specific if isinstance(hook_specific, dict) else {}
    decision = hook_specific.get("permissionDecision") or data.get("decision")
    reason = (
        hook_specific.get("permissionDecisionReason") or data.get("reason") or ""
    )
    if decision in _BLOCKING:
        return HookNotice(
            "warning", f"Hook {hook_event} · {hook_name} blocked", _clean(reason)
        )
    if decision in _ASKING:
        return HookNotice(
            "warning", f"Hook {hook_event} · {hook_name} asked", _clean(reason)
        )

    outcome = payload.get("outcome")
    exit_code = payload.get("exit_code", 0)
    if outcome == "success" and exit_code == 0:
        return None
    tail = (payload.get("stderr") or stdout)[-400:]
    return HookNotice(
        "error",
        f"Hook {hook_event} · {hook_name} failed (exit {exit_code})",
        _clean(tail),
    )


def _clean(text: object) -> str:
    """Hook output becomes a durable transcript row, and a hook is an
    arbitrary external program that may print a token on failure. Same
    scrubber every other provider-derived string goes through."""
    cleaned, _found = scrub_sensitive(text or "")
    return cleaned
