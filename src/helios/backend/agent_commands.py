"""Provider-capability model for Helios's agent command palette.

Commands are control-plane actions, not magic prompt strings.  The UI can
render unavailable actions (and why), while invocation is resolved against
the same immutable capability snapshot and rechecked immediately before the
action runs.

Keep this module GTK-free: protocol/command behavior belongs in the backend
test suite that also runs in python-slim CI.
"""

from __future__ import annotations

from dataclasses import dataclass

from helios.backend import model_catalog


COMPACT_CONTEXT_COMMAND = "context.compact"
REVIEW_CHANGES_COMMAND = "review.start"
FORK_CONVERSATION_COMMAND = "thread.fork"
REVERT_CONVERSATION_COMMAND = "thread.revert"


@dataclass(frozen=True, slots=True)
class AgentCommand:
    """One command the selected provider can perform natively."""

    command_id: str
    name: str
    title: str
    description: str
    source: str
    supported: bool = True
    enabled: bool = True
    unavailable_reason: str = ""
    accepts_arguments: bool = False

    @property
    def invocation(self) -> str:
        return f"/{self.name}"

    @property
    def searchable_text(self) -> str:
        return " ".join(
            (
                self.name,
                self.title,
                self.description,
                self.source,
                self.unavailable_reason,
            )
        ).casefold()


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    command: AgentCommand
    arguments: str = ""


def commands_for_provider(
    provider: str,
    *,
    session_ready: bool,
    session_busy: bool,
    manual_compaction: bool,
    native_review: bool = False,
    native_fork: bool = False,
    safe_revert: bool = False,
    blocked_reason: str = "",
) -> tuple[AgentCommand, ...]:
    """Return the command surface for one selected provider/session.

    Unsupported and not-yet-ready actions remain visible but disabled.  This
    makes the palette a capability explanation as well as a launcher, and
    prevents a command from silently becoming ordinary model prose.
    """

    provider = str(provider or "")
    if provider == model_catalog.PROVIDER_OPENAI:
        actor = "GPT"
        compact_source = "Codex App Server · thread/compact/start"
        review_source = "Codex App Server · review/start"
        fork_source = "Codex App Server · thread/fork"
    elif provider == model_catalog.PROVIDER_ANTHROPIC:
        actor = "Claude"
        compact_source = "Claude CLI · /compact"
        review_source = "No verified native Claude review action"
        fork_source = "No verified native Claude fork action"
    elif provider == model_catalog.PROVIDER_OPENROUTER:
        actor = "OpenRouter"
        compact_source = "No native provider action"
        review_source = "No native provider action"
        fork_source = "No native provider action"
    else:
        actor = "assistant"
        compact_source = "No verified provider"
        review_source = "No verified provider"
        fork_source = "No verified provider"

    def unavailable_reason(
        capability: bool,
        capability_name: str,
        *,
        execution: bool,
    ) -> str:
        if not session_ready:
            return f"Start or resume a {actor} conversation first."
        if not capability:
            return f"{actor} does not advertise native {capability_name}."
        if execution and blocked_reason:
            return str(blocked_reason)
        if session_busy:
            return "Wait for the current provider operation to finish."
        return ""

    compact_reason = unavailable_reason(
        manual_compaction,
        "manual compaction",
        execution=True,
    )
    review_reason = unavailable_reason(
        native_review,
        "Review",
        execution=True,
    )
    fork_reason = unavailable_reason(
        native_fork,
        "conversation Fork",
        execution=False,
    )
    revert_reason = unavailable_reason(
        safe_revert,
        "safe turn-level Revert",
        execution=False,
    )
    if session_ready and not safe_revert:
        revert_reason = (
            "Safe turn-level restore needs a durable native/local turn boundary. "
            "Use Rewind for files or /fork to preserve conversation history."
        )

    return (
        AgentCommand(
            command_id=REVIEW_CHANGES_COMMAND,
            name="review",
            title="Review changes",
            description=(
                "Run the provider's native read-only code review. Add arguments "
                "to supply custom review instructions."
            ),
            source=review_source,
            supported=session_ready and native_review,
            enabled=not review_reason,
            unavailable_reason=review_reason,
            accepts_arguments=True,
        ),
        AgentCommand(
            command_id=FORK_CONVERSATION_COMMAND,
            name="fork",
            title="Fork conversation",
            description=(
                "Create a persisted branch at the current conversation head and "
                "continue it as a separate Work."
            ),
            source=fork_source,
            supported=session_ready and native_fork,
            enabled=not fork_reason,
            unavailable_reason=fork_reason,
        ),
        AgentCommand(
            command_id=REVERT_CONVERSATION_COMMAND,
            name="revert",
            title="Revert conversation",
            description=(
                "Restore conversation context at a durable turn boundary without "
                "silently changing workspace files."
            ),
            source="Helios safety boundary · no deprecated rollback fallback",
            supported=session_ready and safe_revert,
            enabled=not revert_reason,
            unavailable_reason=revert_reason,
        ),
        AgentCommand(
            command_id=COMPACT_CONTEXT_COMMAND,
            name="compact",
            title="Compact context",
            description=(
                "Replace older provider history with the provider-owned "
                "summary while preserving this Work's contract and task plan."
                + (
                    " Add text to tell Claude what the summary should keep."
                    if provider == model_catalog.PROVIDER_ANTHROPIC
                    else ""
                )
            ),
            source=compact_source,
            supported=session_ready and manual_compaction,
            enabled=not compact_reason,
            unavailable_reason=compact_reason,
            # Claude's /compact takes free-text focus instructions; Codex's
            # thread/compact/start takes nothing.
            accepts_arguments=provider == model_catalog.PROVIDER_ANTHROPIC,
        ),
    )


def resolve_command(
    text: str,
    commands: tuple[AgentCommand, ...] | list[AgentCommand],
) -> CommandInvocation | None:
    """Resolve a leading slash invocation only when its name is registered.

    An absolute path such as ``/home/<user>/project`` therefore remains
    ordinary prose.  Arguments are returned separately so a no-argument
    control method can reject them instead of leaking the whole string into
    the model as a pretend command.
    """

    head = str(text or "").strip()
    if not head.startswith("/"):
        return None
    body = head[1:]
    if not body:
        return None
    parts = body.split(maxsplit=1)
    name = parts[0].casefold()
    arguments = parts[1].strip() if len(parts) == 2 else ""
    for command in commands:
        if command.name.casefold() == name:
            return CommandInvocation(command=command, arguments=arguments)
    return None
