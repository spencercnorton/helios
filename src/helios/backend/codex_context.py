"""Provider-neutral Helios instructions for non-Claude runtimes.

Codex loads AGENTS.md through its native instruction hierarchy. Claude loads
CLAUDE.md through its own hierarchy. Helios must never copy one provider's
role document into another provider's user message: doing that once sent a
GPT session into a runaway loop (2026-08-03).

The compatibility helper now adds only the small, Helios-owned presentation
policy. Codex App Server receives it as typed
``additionalContext(kind=application)`` on every turn. This preserves Codex's
built-in collaboration-mode instructions and never copies policy into the user
message. The exec and OpenRouter fallbacks retain the explicit user-message
wrapper until they gain an equivalent native channel.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger("helios.codex-context")


DEFAULT_MAX_CHARS = 0
# Claude receives the extra prompt as ONE argv element, and Linux caps a single
# argument at 128 KiB (MAX_ARG_STRLEN); past that every spawn fails with E2BIG.
MAX_EXTRA_SYSTEM_PROMPT_BYTES = 64 * 1024
PRESENTATION_POLICY = (
    "Helios presentation policy for this chat: During tool-using work, send at "
    "most one sentence per meaningful milestone; do not use progress updates "
    "to preview or repeat claims that belong in the final response. Keep the "
    "final response concise by default, while retaining material safety "
    "warnings, blockers, unresolved risks, and decisions the user must make."
)


def load_extra_system_prompt(config_home: str | None = None) -> str:
    """Operator-supplied instructions every provider gets, or "" when none.

    Reads ``$XDG_CONFIG_HOME/helios/system-prompt.d/*.md`` (default
    ``~/.config``) in name order and joins the non-empty files with a blank
    line. This is how a site adds its own standing policy (a work tracker, a
    house style) without patching Helios. An unreadable file, or one that
    would take the total past :data:`MAX_EXTRA_SYSTEM_PROMPT_BYTES`, is skipped
    with a warning rather than failing the session.
    """

    base = Path(config_home or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    parts = []
    size = 0
    for path in sorted((base / "helios" / "system-prompt.d").glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            _log.warning("skipping extra system prompt %s: %s", path, e)
            continue
        if not text:
            continue
        added = len(text.encode("utf-8")) + (2 if parts else 0)
        if size + added > MAX_EXTRA_SYSTEM_PROMPT_BYTES:
            _log.warning(
                "skipping extra system prompt %s: total would exceed %d bytes",
                path, MAX_EXTRA_SYSTEM_PROMPT_BYTES,
            )
            continue
        parts.append(text)
        size += added
    return "\n\n".join(parts)


# Read once per process: every provider gets the same constant string (Claude
# via --append-system-prompt, Codex via typed application context, OpenRouter
# in its system prompt), so the cached prompt prefix stays stable.
EXTRA_SYSTEM_PROMPT = load_extra_system_prompt()

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
    + (" " + EXTRA_SYSTEM_PROMPT if EXTRA_SYSTEM_PROMPT else "")
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
