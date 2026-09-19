"""GTK-free routing decisions for selected and next chat sessions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(slots=True)
class ChatTarget:
    """Where the composer's next send will go."""

    project: Any
    # Empty = fresh chat in the project's cwd. Non-empty = resume an
    # existing on-disk session by id.
    resume_id: str = ""
    # Provider-neutral logical task.  Claude session and Codex thread ids are
    # participant bindings beneath this identity, never the identity itself.
    work_id: str = ""
    # Provider that owns resume_id. Empty means unresolved and must never be
    # guessed for routing or execution-setting persistence.
    resume_provider: str = ""


@dataclass(slots=True)
class SessionRoute:
    """Routing decision for a selected historical session."""

    target: ChatTarget
    live_driver: Any | None
    follow_transcript: bool


def route_session_selection(
    session: Any,
    live_driver: Any | None,
    *,
    live_matches_provider: bool,
    work_id: str = "",
    resume_provider: str = "",
) -> SessionRoute:
    """Decide how the UI should route a selected session.

    A matching, input-accepting live driver should be rebound to the visible
    UI. Otherwise the selected transcript is staged for resume and file-follow.
    """

    if (
        live_driver is not None
        and getattr(live_driver, "is_accepting_input", False)
        and live_matches_provider
    ):
        # Bind the live driver AND stage its session id for resume. While the
        # driver keeps accepting input, _ensure_driver short-circuits to it and
        # resume_id is never read; but if that background process is later
        # stopped/reaped, the next send must resume this same conversation
        # rather than start a fresh, context-free one. (Read-only pool rows
        # never reach this branch — they have no live driver.)
        return SessionRoute(
            target=ChatTarget(
                project=session.project,
                resume_id=session.session_id,
                resume_provider=resume_provider,
                work_id=work_id,
            ),
            live_driver=live_driver,
            follow_transcript=False,
        )
    return SessionRoute(
        target=ChatTarget(
            project=session.project,
            resume_id=session.session_id,
            resume_provider=resume_provider,
            work_id=work_id,
        ),
        live_driver=None,
        follow_transcript=True,
    )


def fresh_chat_target(project: Any, *, work_id: str = "") -> ChatTarget:
    return ChatTarget(project=project, work_id=work_id)


def stage_work(target: ChatTarget | None, work_id: str) -> ChatTarget | None:
    """Attach a logical Work without changing its provider-native binding."""

    if target is None or not work_id or target.work_id == work_id:
        return target
    return replace(target, work_id=work_id)


def switch_work_participant(
    target: ChatTarget | None,
    resume_id: str = "",
    resume_provider: str = "",
) -> ChatTarget | None:
    """Keep the Work while staging another provider's native participant."""

    if target is None or (
        target.resume_id == resume_id
        and target.resume_provider == resume_provider
    ):
        return target
    return replace(
        target,
        resume_id=resume_id,
        resume_provider=(resume_provider if resume_id else ""),
    )


def stage_resume(
    target: ChatTarget | None,
    session_id: str,
    provider: str = "",
) -> ChatTarget | None:
    """Point `target` at `session_id` so the next spawn resumes it.

    Called whenever a driver reports its live session id. As long as this
    target is still the staged one, we keep its resume_id synced to the live
    session — so if the process later dies (Stop button, idle-reap, crash, app
    restart) the next send runs `claude --resume <id>` and continues the same
    conversation instead of silently starting a fresh, context-free session.

    Safe to leave populated while the driver is alive: `_ensure_driver` only
    consults resume_id when there is NO input-accepting live driver, which is
    exactly when we want to resume. `claude --resume` keeps the same session id
    and replays full history, so the live id is always the right one to stage.
    Read-only pool targets are never resumable, so they're left untouched.
    """
    if (
        target is None
        or not session_id
        or getattr(target.project, "read_only", False)
        or (
            target.resume_id == session_id
            and target.resume_provider == provider
        )
    ):
        return target
    return replace(target, resume_id=session_id, resume_provider=provider)


def clear_resume(target: ChatTarget | None) -> ChatTarget | None:
    if target is None or not target.resume_id:
        return target
    return replace(target, resume_id="", resume_provider="")


def clear_deleted_resume(
    target: ChatTarget | None, deleted_ids: set[str]
) -> ChatTarget | None:
    if target is None or target.resume_id not in deleted_ids:
        return target
    return clear_resume(target)
