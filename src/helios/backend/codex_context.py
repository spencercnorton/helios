"""Provider-neutral Helios instructions for non-Claude runtimes.

Codex loads AGENTS.md through its native instruction hierarchy. Claude loads
CLAUDE.md through its own hierarchy. Helios must never copy one provider's
role document into another provider's user message: doing that caused the
2026-08-03 GPT runaway.

The compatibility helper now adds only the small, Helios-owned presentation
policy. Codex App Server receives it as typed
``additionalContext(kind=application)`` on every turn. This preserves Codex's
built-in collaboration-mode instructions and never copies policy into the user
message. The exec and OpenRouter fallbacks retain the explicit user-message
wrapper until they gain an equivalent native channel.
"""

from __future__ import annotations

import os


DEFAULT_MAX_CHARS = 0
PRESENTATION_POLICY = (
    "Helios presentation policy for this chat: During tool-using work, send at "
    "most one sentence per meaningful milestone; do not use progress updates "
    "to preview or repeat claims that belong in the final response. Keep the "
    "final response concise by default, while retaining material safety "
    "warnings, blockers, unresolved risks, and decisions the user must make."
)
# Norvi Tracker is the estate's durable work record, and a session should reach
# it the way it reaches GitLab (Spencer, 2026-08-22). This is Helios-owned
# policy, not a copied provider role document, so every provider gets the same
# string: Claude via --append-system-prompt, Codex via typed application
# context, and OpenRouter in its system prompt.
#
# It names the `norvi-work` CLI rather than the HTTP API on purpose. That CLI
# (from the tracker gateway repository) is the estate's only sanctioned OpenProject
# client; its write path reaches `PATCH /work_packages/{id}` and
# `POST .../activities` and nothing else, so a session cannot create or delete
# work packages even though the forwarded token would allow it.
#
# The binding is the objective, which already ships to every provider in the
# goal envelope (`session_goals.wrap_user_prompt`) — a ticket reference the user typed
# there is the whole mechanism, which is why no binding UI exists.
# The instance is named by NORVI_TRACKER_URL in the session environment; the
# public build carries no estate hostname.
TRACKER_URL = os.environ.get("NORVI_TRACKER_URL", "").strip()
TRACKER_POLICY = (
    "Norvi Tracker (OpenProject"
    + (f" at {TRACKER_URL}" if TRACKER_URL else "")
    + ") is the durable "
    "record of work across this estate — current work, history, blockers, and "
    "what a project is for. Read it with the already-authenticated `norvi-work` "
    "CLI: `norvi-work inbox` for open work and next actions, `norvi-work "
    "context OP#<id>` for one package's objective, accepted state, blockers and "
    "evidence. Consult it before assuming a task is new. When this chat's "
    "objective names an OP#<id>, that work package is this work's record: read "
    "it before starting, and record meaningful checkpoints, blockers and "
    "findings against it with `norvi-work checkpoint`. Do not open work "
    "packages for routine chat, and never write to the tracker by any other "
    "route."
)
EXECUTION_PLAN_POLICY = (
    "For multi-step research, implementation, debugging, or operational work, "
    "create a concrete native execution plan before the first write or other "
    "consequential action and keep its statuses current. Use 2–8 outcome-based "
    "tasks; keep at most one root task in progress unless the accepted Work "
    "explicitly calls for parallel work; mark a task complete only after "
    "evidence; and never silently drop an unfinished task. A simple one-step "
    "answer needs no decorative plan. The execution plan does not replace the "
    "user's objective, definition of done, or acceptance checklist."
)
ASK_OR_CONTINUE_POLICY = (
    "Continue reversible, in-scope local work without asking and state material "
    "assumptions. Stop and ask the smallest focused question only when the "
    "answer would materially change acceptance criteria or architecture, "
    "authorize an external, destructive, or irreversible effect, or provide "
    "missing authority or credentials. Do not ask merely for confirmation when "
    "the request is already clear. When a question blocks one task, keep safe "
    "independent work moving and tie the question to that blocked plan task."
)

CODEX_DEVELOPER_INSTRUCTIONS = (
    "Follow the user request and the repository's native AGENTS.md instructions. "
    "Do not infer provider roles, tandem review, delegation, or additional scope "
    "unless the accepted Work context or user request explicitly requires it. "
    + PRESENTATION_POLICY
    + " "
    + EXECUTION_PLAN_POLICY
    + " "
    + ASK_OR_CONTINUE_POLICY
    + " "
    + TRACKER_POLICY
)


def shared_context_for_cwd(cwd: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Return no copied provider context.

    Kept as a compatibility seam while the typed P1 context compiler is built.
    Native provider instruction sources own durable repository guidance.
    """

    del cwd, max_chars
    return ""


def inject_shared_context(
    cwd: str,
    user_text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Wrap a fallback prompt with only Helios-owned neutral policy."""

    del cwd, max_chars
    return f"{PRESENTATION_POLICY}\n\nUser request:\n{user_text}"
