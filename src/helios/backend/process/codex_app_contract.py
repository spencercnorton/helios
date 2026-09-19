"""Pure builders for the Codex App Server v2 contract.

Keeping protocol policy outside the GObject driver makes permission and
interaction behavior directly testable in Helios's slim CI environment.
"""

from __future__ import annotations

import copy
from typing import Any

from helios.backend.codex_context import CODEX_DEVELOPER_INSTRUCTIONS
from helios.backend.project_perms import (
    AUTONOMY_MODE,
    canonical_cwd,
    codex_permission_profile,
    effective_execution_mode,
)
from helios.backend.sensitive_text import scrub_sensitive
from helios.backend.workflow_modes import (
    PLAN_WORKFLOW_MODE,
    canonical_workflow_mode,
)


APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
USER_INPUT_METHOD = "item/tool/requestUserInput"
PERMISSIONS_METHOD = "item/permissions/requestApproval"
MCP_ELICITATION_METHOD = "mcpServer/elicitation/request"
DYNAMIC_TOOL_METHOD = "item/tool/call"

_APPROVAL_LABELS = {
    "Approve once": "accept",
    "Approve for session": "acceptForSession",
    "Decline": "decline",
    "Cancel turn": "cancel",
}


def _thread_config() -> dict[str, Any]:
    """Return Helios's fail-closed Codex thread feature policy."""

    return {
        # Helios opts into and tests the experimental request-input/tool
        # contract itself. Suppress only Codex's generic startup banner;
        # typed runtime, configuration, and Guardian warnings still flow
        # through the provider-notice signal.
        "suppress_unstable_features_warning": True,
        "features": {"default_mode_request_user_input": True},
        # Native task planning is an opt-in Codex tool. Without this flag
        # ordinary GPT turns never emit turn/plan/updated.
        "tools": {"update_plan": {"enabled": True}},
        # Standard Helios Work is single-agent. A later explicit Parallel
        # lease can replace this only with family budgets and supervision.
        "agents": {"enabled": False},
    }


def build_thread_params(
    *,
    cwd: str,
    model: str,
    permission_mode: str,
    workflow_mode: str = "default",
    thread_id: str = "",
) -> dict[str, Any]:
    """Build schema-exact ``thread/start`` or ``thread/resume`` params."""

    workflow_mode = canonical_workflow_mode(workflow_mode)
    if workflow_mode == PLAN_WORKFLOW_MODE:
        permission_mode = "plan"
    permission_mode = effective_execution_mode(permission_mode, cwd, provider="openai")
    approval, sandbox, reviewer, _network = codex_permission_profile(permission_mode)
    if sandbox == "danger-full-access" and permission_mode != AUTONOMY_MODE:
        raise ValueError("Codex danger-full-access requires explicit Bypass permissions")
    params: dict[str, Any] = {
        "cwd": cwd,
        "model": model or None,
        "approvalPolicy": approval,
        "approvalsReviewer": reviewer,
        "sandbox": sandbox,
        "personality": "pragmatic",
        # App Server keeps request_user_input outside default-mode tools unless
        # this tested feature policy is installed on the native thread.
        "config": _thread_config(),
    }
    if thread_id:
        params["threadId"] = thread_id
    else:
        params.update(
            {
                "ephemeral": False,
                "serviceName": "helios",
                "threadSource": "helios",
            }
        )
    return params


def build_turn_start_params(
    *,
    thread_id: str,
    text: str,
    permission_mode: str,
    cwd: str,
    model: str = "",
    effort: str = "",
    workflow_mode: str = "default",
    client_message_id: str = "",
) -> dict[str, Any]:
    workflow_mode = canonical_workflow_mode(workflow_mode)
    if workflow_mode == PLAN_WORKFLOW_MODE:
        permission_mode = "plan"
    permission_mode = effective_execution_mode(permission_mode, cwd, provider="openai")
    approval, sandbox, reviewer, network = codex_permission_profile(permission_mode)
    if sandbox == "danger-full-access" and permission_mode != AUTONOMY_MODE:
        raise ValueError("Codex danger-full-access requires explicit Bypass permissions")
    if sandbox == "danger-full-access":
        sandbox_policy = {"type": "dangerFullAccess"}
    elif sandbox == "read-only":
        sandbox_policy = {"type": "readOnly", "networkAccess": network}
    else:
        root = canonical_cwd(cwd)
        sandbox_policy = {
            "type": "workspaceWrite",
            "writableRoots": [root] if root else [],
            "networkAccess": network,
        }
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": text}],
        # App Server applies these sticky overrides to this turn and later
        # turns. Emitting them every time keeps the current conversation
        # aligned with Helios even after a live toolbar change.
        "approvalPolicy": approval,
        "approvalsReviewer": reviewer,
        "sandboxPolicy": sandbox_policy,
        # Collaboration presets supersede model, effort, and developer
        # instructions. Emit even Default so a sticky Plan preset is cleared.
        # Null developer_instructions deliberately retains Codex's built-in
        # mode instructions; Helios policy travels in typed application
        # context instead of replacing those instructions or contaminating
        # the user's message.
        "collaborationMode": {
            "mode": workflow_mode,
            "settings": {
                "model": model,
                "reasoning_effort": effort or None,
                "developer_instructions": None,
            },
        },
        "additionalContext": {
            "helios-policy": {
                "kind": "application",
                "value": CODEX_DEVELOPER_INSTRUCTIONS,
            }
        },
    }
    if model:
        params["model"] = model
    if effort:
        params["effort"] = effort
    if client_message_id:
        params["clientUserMessageId"] = client_message_id
    return params


def build_turn_steer_params(
    *,
    thread_id: str,
    turn_id: str,
    text: str,
    client_message_id: str = "",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "expectedTurnId": turn_id,
        "input": [{"type": "text", "text": text}],
    }
    if client_message_id:
        params["clientUserMessageId"] = client_message_id
    return params


def build_review_settings_params(
    *,
    thread_id: str,
    model: str = "",
    effort: str = "",
) -> dict[str, Any]:
    """Narrow subsequent native work before ``review/start``.

    Review is model work, but it must never mutate the checkout or turn a
    blocked action into an approval prompt. Ordinary ``turn/start`` sends the
    selected Helios profile again, so this sticky narrowing is restored by the
    next explicit non-review turn.
    """

    params: dict[str, Any] = {
        "threadId": thread_id,
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
    }
    if model:
        params["model"] = model
        # Clear a sticky native Plan preset before entering Review while
        # preserving Codex's built-in Default instructions. Review then owns
        # its own reviewer prompt instead of inheriting a previous workflow.
        params["collaborationMode"] = {
            "mode": "default",
            "settings": {
                "model": model,
                "reasoning_effort": effort or None,
                "developer_instructions": None,
            },
        }
    if effort:
        params["effort"] = effort
    return params


def build_review_start_params(
    *,
    thread_id: str,
    instructions: str = "",
) -> dict[str, Any]:
    """Build one inline native Review request."""

    custom = str(instructions or "").strip()
    target: dict[str, str]
    if custom:
        target = {"type": "custom", "instructions": custom}
    else:
        target = {"type": "uncommittedChanges"}
    return {
        "threadId": thread_id,
        "delivery": "inline",
        "target": target,
    }


def build_thread_fork_params(
    *,
    thread_id: str,
    cwd: str,
    permission_mode: str,
    model: str = "",
    before_turn_id: str = "",
) -> dict[str, Any]:
    """Build a persisted native fork without inherited goal accounting.

    ``deferGoalContinuation`` is deliberately omitted.  On App Server builds
    that support it, setting it true copies the source goal snapshot --
    including consumed tokens and a terminal budget-limited status -- into the
    fork.  A Helios fork owns a new Work and therefore a fresh budget.  The
    user-owned Goal is copied by :class:`WorkCoordinator` and synchronized as
    a new native goal when the forked chat is opened.
    """

    permission_mode = effective_execution_mode(
        permission_mode,
        cwd,
        provider="openai",
    )
    approval, sandbox, reviewer, _network = codex_permission_profile(permission_mode)
    if sandbox == "danger-full-access" and permission_mode != AUTONOMY_MODE:
        raise ValueError("Codex danger-full-access requires explicit Bypass permissions")
    params: dict[str, Any] = {
        "threadId": thread_id,
        "cwd": canonical_cwd(cwd),
        "approvalPolicy": approval,
        "approvalsReviewer": reviewer,
        "sandbox": sandbox,
        "config": _thread_config(),
        "ephemeral": False,
        "threadSource": "helios",
    }
    if model:
        params["model"] = model
    if before_turn_id:
        params["beforeTurnId"] = before_turn_id
    return params


def approval_question(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Convert a command/file approval into the shared question UI shape."""

    command = _display_text(params.get("command"), 1200)
    cwd = _display_text(params.get("cwd"), 500)
    reason = _display_text(params.get("reason"), 1200)
    network = params.get("networkApprovalContext")
    if method == PERMISSIONS_METHOD:
        title = "Expanded permissions"
        reason = reason or "Codex is requesting additional runtime permissions."
        summary = reason
        if cwd:
            summary += f"\n\nWorking directory: {cwd}"
            cwd = ""
        requested = _permission_profile_details(params.get("permissions"))
        if requested:
            summary += "\n\nRequested access:\n" + "\n".join(requested)
        reason = ""
    elif method == "item/fileChange/requestApproval":
        title = "File changes"
        target = _display_text(params.get("grantRoot"), 500)
        summary = f"Allow Codex to change files{f' under {target}' if target else ''}?"
    elif network:
        title = "Network access"
        summary = reason or "Allow this command to access the network?"
    else:
        title = "Run command"
        summary = command or "Allow Codex to run this command?"

    details = []
    if network and command:
        details.append(f"Command: {command}")
    if cwd:
        details.append(f"Working directory: {cwd}")
    if reason and reason != summary:
        details.append(reason)
    if details:
        summary += "\n\n" + "\n".join(details)
    options = [
        {
            "label": "Approve once",
            "description": "Allow only this action.",
        },
        {
            "label": "Approve for session",
            "description": "Allow matching actions for this Codex session.",
        },
        {
            "label": "Decline",
            "description": "Deny this action and let the turn continue.",
        },
    ]
    if method != PERMISSIONS_METHOD:
        options.append(
            {
                "label": "Cancel turn",
                "description": "Deny the action and interrupt the active turn.",
            }
        )
    return {
        "allowOther": False,
        "requireExplicitChoice": True,
        "questions": [
            {
                "header": title,
                "question": summary,
                "multiSelect": False,
                "allowOther": False,
                "options": options,
            }
        ],
    }


def interaction_response(
    method: str,
    answer: object,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the schema-exact response for a supported interaction."""

    if method in APPROVAL_METHODS:
        label = answer if isinstance(answer, str) else ""
        return {"decision": _APPROVAL_LABELS.get(label, "decline")}
    if method == USER_INPUT_METHOD:
        answers = answer if isinstance(answer, dict) else {}
        return {"answers": answers}
    if method == PERMISSIONS_METHOD:
        requested = (params or {}).get("permissions")
        granted = copy.deepcopy(requested) if isinstance(requested, dict) else {}
        if answer not in {"Approve once", "Approve for session"}:
            granted = {}
        return {
            "permissions": granted,
            "scope": "session" if answer == "Approve for session" else "turn",
        }
    raise ValueError(f"unsupported Codex interaction: {method}")


def _display_text(value: object, limit: int) -> str:
    text, _changed = scrub_sensitive(str(value or "").strip())
    return text[:limit]


def _permission_profile_details(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    details: list[str] = []
    network = value.get("network")
    if isinstance(network, dict) and network.get("enabled") is not None:
        details.append(
            "Network: " + ("enabled" if bool(network.get("enabled")) else "disabled")
        )
    file_system = value.get("fileSystem")
    if not isinstance(file_system, dict):
        return details
    for key, label in (("read", "Read"), ("write", "Write")):
        paths = file_system.get(key)
        if not isinstance(paths, list):
            continue
        for path in paths[:20]:
            details.append(f"{label}: {_display_text(path, 500)}")
    entries = file_system.get("entries")
    if isinstance(entries, list):
        for entry in entries[:20]:
            if not isinstance(entry, dict):
                continue
            access = str(entry.get("access") or "access").title()
            path = entry.get("path")
            rendered = _permission_path(path)
            if rendered:
                details.append(f"{access}: {rendered}")
    return details


def _permission_path(value: object) -> str:
    if not isinstance(value, dict):
        return _display_text(value, 500)
    path_type = value.get("type")
    if path_type == "path":
        return _display_text(value.get("path"), 500)
    if path_type == "glob_pattern":
        return f"glob {_display_text(value.get('pattern'), 500)}"
    if path_type == "special":
        special = value.get("value")
        if isinstance(special, dict):
            kind = _display_text(special.get("kind"), 100)
            explicit_path = _display_text(special.get("path"), 500)
            subpath = _display_text(special.get("subpath"), 300)
            if explicit_path:
                return f"{kind}: {explicit_path}"
            return f"{kind}{f'/{subpath}' if subpath else ''}"
    return _display_text(value, 500)


def humanize_window_mins(mins: object) -> str:
    """"300" -> "5-hour usage". Empty string when the CLI sent no duration.

    `RateLimitWindow.windowDurationMins` is the only self-describing field the
    App Server gives a window; `limitId` is an opaque backend id (schema says
    nullable string, not an enum), so keying a label table off it was always
    going to render a raw key the first time OpenAI changed one.
    """

    try:
        total = int(mins)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    for size, one, many in (
        (10080, "Weekly usage", "{n}-week usage"),
        (1440, "Daily usage", "{n}-day usage"),
        (60, "Hourly usage", "{n}-hour usage"),
    ):
        if total % size == 0:
            count = total // size
            return one if count == 1 else many.format(n=count)
    return f"{total}-minute usage"


def rate_limit_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten an App Server rate-limit snapshot for Helios's toolbar."""

    if not isinstance(snapshot, dict):
        return []
    limit_id = str(snapshot.get("limitId") or "codex")
    limit_name = str(snapshot.get("limitName") or "").strip()
    reached = bool(snapshot.get("rateLimitReachedType"))
    rows: list[dict[str, Any]] = []
    for bucket in ("primary", "secondary"):
        window = snapshot.get(bucket)
        if not isinstance(window, dict):
            continue
        span = humanize_window_mins(window.get("windowDurationMins"))
        # limitName is the backend's own human name for the limit; the span
        # distinguishes the two windows under it. Either alone is still better
        # than the opaque id, and "Codex primary window" is the last resort.
        label = " · ".join(p for p in (limit_name, span) if p)
        rows.append(
            {
                "provider": "openai",
                "rateLimitType": f"{limit_id}_{bucket}",
                "label": label or f"Codex {bucket} window",
                "status": "blocked" if reached else "allowed",
                "usedPercent": max(0, min(100, int(window.get("usedPercent") or 0))),
                "windowDurationMins": window.get("windowDurationMins"),
                "resetsAt": window.get("resetsAt"),
            }
        )
    return rows
