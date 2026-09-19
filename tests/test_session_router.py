from __future__ import annotations

from types import SimpleNamespace

from helios.backend.session_router import (
    ChatTarget,
    clear_deleted_resume,
    clear_resume,
    fresh_chat_target,
    route_session_selection,
    stage_resume,
    stage_work,
    switch_work_participant,
)


def _session(session_id: str = "sid-1", project=None):
    return SimpleNamespace(
        session_id=session_id,
        project=project or SimpleNamespace(cwd="/repo", read_only=False),
    )


def _driver(*, accepting: bool = True):
    return SimpleNamespace(is_accepting_input=accepting)


def test_route_selection_binds_matching_live_driver():
    session = _session()
    live = _driver()

    route = route_session_selection(session, live, live_matches_provider=True)

    # Bound to the live driver, but resume_id is still staged so that stopping
    # this background session later doesn't lose its context on the next send.
    assert route.target == ChatTarget(project=session.project, resume_id="sid-1")
    assert route.live_driver is live
    assert route.follow_transcript is False


def test_route_selection_stages_resume_without_usable_live_driver():
    session = _session()

    for live, matches in ((None, False), (_driver(accepting=False), True), (_driver(), False)):
        route = route_session_selection(session, live, live_matches_provider=matches)
        assert route.target == ChatTarget(project=session.project, resume_id="sid-1")
        assert route.live_driver is None
        assert route.follow_transcript is True


def test_route_selection_carries_provider_neutral_work_id():
    session = _session()

    route = route_session_selection(
        session,
        None,
        live_matches_provider=False,
        work_id="wrk-1",
    )

    assert route.target.work_id == "wrk-1"
    assert route.target.resume_id == session.session_id


def test_route_selection_preserves_resolved_resume_provider():
    session = _session()

    route = route_session_selection(
        session,
        None,
        live_matches_provider=False,
        resume_provider="openai",
    )

    assert route.target.resume_provider == "openai"


def test_fresh_chat_target_has_no_resume_id():
    project = SimpleNamespace(cwd="/repo")

    assert fresh_chat_target(project) == ChatTarget(project=project)


def test_clear_resume_returns_new_target_when_needed():
    project = SimpleNamespace(cwd="/repo")
    target = ChatTarget(project=project, resume_id="sid-1")

    cleared = clear_resume(target)

    assert cleared == ChatTarget(project=project)
    assert target.resume_id == "sid-1"
    assert clear_resume(cleared) is cleared
    assert clear_resume(None) is None


def test_clear_deleted_resume_only_clears_matching_session():
    project = SimpleNamespace(cwd="/repo")
    target = ChatTarget(project=project, resume_id="sid-1")

    assert clear_deleted_resume(target, {"sid-2"}) is target
    assert clear_deleted_resume(target, {"sid-1"}) == ChatTarget(project=project)


def test_stage_resume_populates_fresh_target_with_live_id():
    # A fresh chat (no resume_id) that just reported its live session id must
    # become resumable, so a later respawn (Stop, reap, crash) continues it.
    project = SimpleNamespace(cwd="/repo", read_only=False)
    target = ChatTarget(project=project)

    staged = stage_resume(target, "sid-live")

    assert staged == ChatTarget(project=project, resume_id="sid-live")
    assert target.resume_id == ""  # original untouched (frozen-ish semantics)


def test_stage_resume_updates_to_new_live_id():
    # claude --resume keeps the same id, but stage whatever the driver reports.
    project = SimpleNamespace(cwd="/repo", read_only=False)
    target = ChatTarget(project=project, resume_id="sid-old")

    assert stage_resume(target, "sid-new") == ChatTarget(
        project=project, resume_id="sid-new"
    )


def test_stage_resume_noops_when_id_unchanged():
    project = SimpleNamespace(cwd="/repo", read_only=False)
    target = ChatTarget(project=project, resume_id="sid-1")

    assert stage_resume(target, "sid-1") is target


def test_stage_resume_ignores_empty_id_and_none_target():
    project = SimpleNamespace(cwd="/repo", read_only=False)
    target = ChatTarget(project=project, resume_id="sid-1")

    assert stage_resume(target, "") is target
    assert stage_resume(None, "sid-1") is None


def test_stage_resume_leaves_read_only_pool_targets_alone():
    # Remote/pool sessions can't be resumed locally — never stage them.
    project = SimpleNamespace(cwd="/repo", read_only=True)
    target = ChatTarget(project=project)

    assert stage_resume(target, "sid-live") is target


def test_stage_work_preserves_native_resume_binding():
    project = SimpleNamespace(cwd="/repo", read_only=False)
    target = ChatTarget(project=project, resume_id="claude-1")

    staged = stage_work(target, "wrk-1")

    assert staged == ChatTarget(
        project=project,
        resume_id="claude-1",
        work_id="wrk-1",
    )
    assert stage_work(staged, "wrk-1") is staged
    assert stage_work(None, "wrk-1") is None


def test_provider_switch_preserves_work_and_swaps_only_native_binding():
    project = SimpleNamespace(cwd="/repo", read_only=False)
    target = ChatTarget(
        project=project,
        resume_id="claude-1",
        work_id="wrk-1",
    )

    switched = switch_work_participant(target, "codex-1", "openai")

    assert switched == ChatTarget(
        project=project,
        resume_id="codex-1",
        work_id="wrk-1",
        resume_provider="openai",
    )
    assert switch_work_participant(switched, "codex-1", "openai") is switched
    assert switch_work_participant(None, "codex-1") is None
