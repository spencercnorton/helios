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
_CODEX_HOOK_LABELS = {
    "preToolUse": "Before tool use", "permissionRequest": "Permission check",
    "postToolUse": "After tool use", "preCompact": "Before compaction",
    "postCompact": "After compaction", "sessionStart": "Session start",
    "sessionEnd": "Session end", "userPromptSubmit": "User message",
    "subagentStart": "Agent start", "subagentStop": "Agent finish",
    "stop": "Turn finish", "interrupt": "Interrupt",
}


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


def summarize_codex_hook(payload: dict) -> HookNotice | None:
    """Project only actionable App Server hook output, never context entries."""
    run = payload.get("run")
    if not isinstance(run, dict):
        return None
    status = run.get("status")
    entries = run.get("entries")
    visible = [
        entry for entry in entries
        if isinstance(entry, dict) and entry.get("kind") in {"warning", "error", "stop"}
        and isinstance(entry.get("text"), str)
    ] if isinstance(entries, list) else []
    if status not in {"failed", "blocked", "stopped"} and not visible:
        return None
    event = _CODEX_HOOK_LABELS.get(str(run.get("eventName") or ""), "Hook")
    # The event enum is useful identity; sourcePath and context/feedback output
    # are application internals and do not belong in a transcript notice.
    verdict = status if status in {"failed", "blocked", "stopped"} else "warning"
    title = f"Codex hook · {event} · {verdict}"
    detail = "\n".join(entry["text"] for entry in visible)
    if not detail and isinstance(run.get("statusMessage"), str):
        detail = run["statusMessage"]
    severity = "error" if status == "failed" or any(e["kind"] == "error" for e in visible) else "warning"
    return HookNotice(severity, title, _clean(detail)[:1000])
