"""Capability-driven agent commands stay out of model prompt text."""

from helios.backend import model_catalog
from helios.backend.agent_commands import (
    COMPACT_CONTEXT_COMMAND,
    FORK_CONVERSATION_COMMAND,
    REVIEW_CHANGES_COMMAND,
    REVERT_CONVERSATION_COMMAND,
    commands_for_provider,
    resolve_command,
)


def _compact(provider: str, **overrides):
    state = {
        "session_ready": True,
        "session_busy": False,
        "manual_compaction": True,
    }
    state.update(overrides)
    commands = commands_for_provider(provider, **state)
    return next(
        command
        for command in commands
        if command.command_id == COMPACT_CONTEXT_COMMAND
    )


def _native_commands(provider: str, **overrides):
    state = {
        "session_ready": True,
        "session_busy": False,
        "manual_compaction": True,
        "native_review": provider == model_catalog.PROVIDER_OPENAI,
        "native_fork": provider == model_catalog.PROVIDER_OPENAI,
    }
    state.update(overrides)
    return commands_for_provider(provider, **state)


def test_codex_compaction_names_the_native_method() -> None:
    command = _compact(model_catalog.PROVIDER_OPENAI)

    assert command.command_id == COMPACT_CONTEXT_COMMAND
    assert command.invocation == "/compact"
    assert command.supported
    assert command.enabled
    assert "thread/compact/start" in command.source


def test_command_availability_explains_session_capability_and_busy_state() -> None:
    disconnected = _compact(
        model_catalog.PROVIDER_OPENAI,
        session_ready=False,
    )
    unsupported = _compact(
        model_catalog.PROVIDER_OPENROUTER,
        manual_compaction=False,
    )
    busy = _compact(
        model_catalog.PROVIDER_ANTHROPIC,
        session_busy=True,
    )
    blocked = _compact(
        model_catalog.PROVIDER_OPENAI,
        blocked_reason="This Work is paused.",
    )

    assert not disconnected.supported and not disconnected.enabled
    assert "Start or resume" in disconnected.unavailable_reason
    assert not unsupported.supported and not unsupported.enabled
    assert "does not advertise" in unsupported.unavailable_reason
    assert busy.supported and not busy.enabled
    assert "current provider operation" in busy.unavailable_reason
    assert blocked.supported and not blocked.enabled
    assert blocked.unavailable_reason == "This Work is paused."


def test_only_a_registered_slash_name_resolves_as_a_control_action() -> None:
    commands = _native_commands(model_catalog.PROVIDER_OPENAI)

    invocation = resolve_command("  /COMPACT  ", commands)
    with_arguments = resolve_command("/compact focus on tests", commands)

    assert invocation is not None
    assert invocation.command.command_id == COMPACT_CONTEXT_COMMAND
    assert invocation.arguments == ""
    assert with_arguments is not None
    assert with_arguments.arguments == "focus on tests"
    assert resolve_command("/home/alice/helios has a bug", commands) is None
    assert resolve_command("/not-a-command", commands) is None
    assert resolve_command("please /compact", commands) is None


def test_gpt_review_and_fork_are_native_while_revert_fails_closed() -> None:
    commands = _native_commands(model_catalog.PROVIDER_OPENAI)
    by_id = {command.command_id: command for command in commands}

    review = by_id[REVIEW_CHANGES_COMMAND]
    fork = by_id[FORK_CONVERSATION_COMMAND]
    revert = by_id[REVERT_CONVERSATION_COMMAND]

    assert review.enabled and review.supported and review.accepts_arguments
    assert review.invocation == "/review"
    assert "review/start" in review.source
    assert fork.enabled and fork.supported
    assert fork.invocation == "/fork"
    assert "thread/fork" in fork.source
    assert not revert.enabled and not revert.supported
    assert "durable native/local turn boundary" in revert.unavailable_reason
    assert "deprecated rollback" in revert.source

    invocation = resolve_command("/review focus on concurrency", commands)
    assert invocation is not None
    assert invocation.command.command_id == REVIEW_CHANGES_COMMAND
    assert invocation.arguments == "focus on concurrency"


def test_review_obeys_work_block_but_fork_remains_a_renewal_path() -> None:
    commands = _native_commands(
        model_catalog.PROVIDER_OPENAI,
        blocked_reason="This Work reached its execution budget.",
    )
    by_id = {command.command_id: command for command in commands}

    assert not by_id[REVIEW_CHANGES_COMMAND].enabled
    assert "execution budget" in by_id[REVIEW_CHANGES_COMMAND].unavailable_reason
    assert by_id[FORK_CONVERSATION_COMMAND].enabled


def test_non_gpt_providers_explain_unverified_review_and_fork() -> None:
    commands = _native_commands(
        model_catalog.PROVIDER_ANTHROPIC,
        native_review=False,
        native_fork=False,
    )
    by_id = {command.command_id: command for command in commands}

    assert not by_id[REVIEW_CHANGES_COMMAND].enabled
    assert "does not advertise native Review" in by_id[
        REVIEW_CHANGES_COMMAND
    ].unavailable_reason
    assert not by_id[FORK_CONVERSATION_COMMAND].enabled


def test_compact_takes_a_focus_only_for_claude() -> None:
    """Claude's /compact accepts free-text focus; Codex's method takes nothing."""
    claude = commands_for_provider(
        model_catalog.PROVIDER_ANTHROPIC,
        session_ready=True,
        session_busy=False,
        manual_compaction=True,
    )
    codex = commands_for_provider(
        model_catalog.PROVIDER_OPENAI,
        session_ready=True,
        session_busy=False,
        manual_compaction=True,
    )
    compact_claude = next(c for c in claude if c.name == "compact")
    compact_codex = next(c for c in codex if c.name == "compact")
    assert compact_claude.accepts_arguments is True
    assert compact_codex.accepts_arguments is False
    invocation = resolve_command("/compact keep the migration plan", claude)
    assert invocation is not None
    assert invocation.arguments == "keep the migration plan"
