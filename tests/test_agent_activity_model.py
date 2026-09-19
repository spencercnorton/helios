"""GTK-free causal and monotonic contracts for observed provider agents."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from helios.backend.agent_activity import (
    AgentActivityModel,
    AgentActivityScope,
    AgentObservedStatus,
)


def _scope(turn: str = "turn-one", *, provider: str = "openai") -> AgentActivityScope:
    return AgentActivityScope(
        provider=provider,
        work_id="work-one",
        root_turn_id=turn,
        root_actor_id="root-thread",
    )


def test_module_imports_without_gtk() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import helios.backend.agent_activity; "
                "assert not [m for m in sys.modules "
                "if m == 'gi' or m.startswith('gi.') ]"
            ),
        ],
        env={**os.environ, "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_codex_snapshot_is_keyed_to_exact_work_turn_and_actor() -> None:
    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)

    snapshot = model.observe(
        scope,
        {
            "item": {
                "agentsStates": {
                    "child-b": {"status": "completed", "name": "Verifier"},
                    "child-a": {
                        "status": "inProgress",
                        "name": "Builder",
                        "role": "worker",
                        "message": "Editing tests",
                    },
                }
            }
        },
    )

    assert [item.actor_id for item in snapshot.activities] == ["child-b", "child-a"]
    assert all(item.scope == scope for item in snapshot.activities)
    builder = snapshot.activities[1]
    assert builder.status is AgentObservedStatus.RUNNING
    assert builder.name == "Builder"
    assert builder.role == "worker"
    assert builder.detail == "Editing tests"
    assert [item.actor_id for item in snapshot.active] == ["child-a"]
    assert [item.actor_id for item in snapshot.done] == ["child-b"]


def test_terminal_state_is_monotonic_against_late_running_snapshot() -> None:
    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)
    model.observe(scope, {"child": {"status": "running", "name": "Reviewer"}})
    terminal = model.observe(
        scope,
        {"child": {"status": "completed", "message": "Review complete"}},
    )
    late = model.observe(
        scope,
        {
            "child": {
                "status": "inProgress",
                "name": "Reviewer renamed",
                "role": "validator",
                "message": "stale update",
            }
        },
    )

    assert terminal.activities[0].status is AgentObservedStatus.COMPLETED
    assert late.activities[0].status is AgentObservedStatus.COMPLETED
    assert late.activities[0].name == "Reviewer renamed"
    assert late.activities[0].role == "validator"
    assert late.activities[0].detail == "Review complete"


def test_needs_you_can_resume_but_cannot_regress_to_starting() -> None:
    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)
    waiting = model.observe(scope, {"child": {"status": "needsInput"}})
    stale = model.observe(scope, {"child": {"status": "pendingInit"}})
    resumed = model.observe(scope, {"child": {"status": "running"}})

    assert waiting.activities[0].status is AgentObservedStatus.NEEDS_INPUT
    assert stale.activities[0].status is AgentObservedStatus.NEEDS_INPUT
    assert resumed.activities[0].status is AgentObservedStatus.RUNNING


def test_new_root_turn_retires_old_scope_and_stale_callback_is_ignored() -> None:
    model = AgentActivityModel()
    first = _scope("turn-first")
    second = _scope("turn-second")
    model.begin_scope(first)
    model.observe(first, {"old": {"status": "running"}})

    cleared = model.begin_scope(second)
    current = model.observe(second, {"new": {"status": "running"}})
    stale = model.observe(first, {"old": {"status": "completed"}})
    refused_reactivation = model.begin_scope(first)

    assert cleared.activities == ()
    assert [item.actor_id for item in current.activities] == ["new"]
    assert stale == current
    assert refused_reactivation == current


def test_retired_scope_fence_does_not_expire_during_long_visible_session() -> None:
    model = AgentActivityModel()
    first = _scope("turn-0")
    model.begin_scope(first)
    for index in range(1, 65):
        model.begin_scope(_scope(f"turn-{index}"))

    current = model.snapshot()
    refused = model.begin_scope(first)

    assert refused == current
    assert refused.scope == _scope("turn-64")


def test_partial_snapshot_does_not_make_unseen_actor_disappear() -> None:
    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)
    model.observe(
        scope,
        {
            "one": {"status": "running"},
            "two": {"status": "running"},
        },
    )
    snapshot = model.observe(scope, {"one": {"status": "completed"}})

    assert [item.actor_id for item in snapshot.activities] == ["one", "two"]
    assert snapshot.activities[0].status is AgentObservedStatus.COMPLETED
    assert snapshot.activities[1].status is AgentObservedStatus.RUNNING


def test_root_actor_is_not_rendered_as_its_own_descendant() -> None:
    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)
    snapshot = model.observe(
        scope,
        {
            "root-thread": {"status": "running"},
            "child": {"status": "running"},
        },
    )
    assert [item.actor_id for item in snapshot.activities] == ["child"]


def test_missing_or_unrecognized_status_is_observed_not_active() -> None:
    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)
    snapshot = model.observe(
        scope,
        {
            "missing": {"name": "Unclassified"},
            "new-provider-state": {"status": "providerMysteryState"},
        },
    )

    assert snapshot.active == ()
    assert [item.actor_id for item in snapshot.observed] == [
        "missing",
        "new-provider-state",
    ]
    assert all(
        item.status is AgentObservedStatus.UNKNOWN for item in snapshot.observed
    )


def test_only_explicit_input_or_approval_states_need_the_user() -> None:
    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)
    snapshot = model.observe(
        scope,
        {
            "waiting": {"status": "waiting"},
            "blocked": {"status": "blocked"},
            "input": {"status": "awaitingInput"},
            "approval": {"status": "pendingApproval"},
        },
    )

    assert [item.actor_id for item in snapshot.observed] == ["waiting", "blocked"]
    assert [item.actor_id for item in snapshot.needs_you] == ["input", "approval"]

    model = AgentActivityModel()
    model.begin_scope(scope)
    model.observe(scope, {"child": {"status": "running"}})
    ambiguous = model.observe(scope, {"child": {"status": "blocked"}})
    assert ambiguous.active == ()
    assert ambiguous.observed[0].status is AgentObservedStatus.UNKNOWN


def test_claude_activity_projects_now_that_the_lifecycle_is_causal() -> None:
    """Replaces the dormancy assertion.

    The adapter was dormant because tool-use records proved only that delegation
    was *requested*. The driver now keys every state on the `Task`/`Workflow`
    tool_use_id that the child's own records carry back as `parent_tool_use_id`,
    so the projection is causal and this layer may show it.

    The anti-fabrication guarantee did not go away, it moved: it is enforced in
    `ClaudeCliDriver` and pinned by `tests/test_claude_agent_observer.py` — an
    unknown parent id creates no actor, a terminal actor is never resurrected,
    and a requested-but-unobserved child stays `starting`.
    """

    model = AgentActivityModel()
    scope = _scope(provider="anthropic")
    model.begin_scope(scope)

    snapshot = model.observe(
        scope,
        {"tool-use-id": {"status": "working", "name": "Explore"}},
    )

    assert [item.actor_id for item in snapshot.activities] == ["tool-use-id"]
    assert snapshot.activities[0].status is AgentObservedStatus.RUNNING


def test_claude_starting_is_not_promoted_to_running() -> None:
    """`starting` means requested-but-not-yet-observed. Keep the distinction."""

    model = AgentActivityModel()
    scope = _scope(provider="anthropic")
    model.begin_scope(scope)

    snapshot = model.observe(scope, {"tool-use-id": {"status": "starting"}})

    assert snapshot.activities[0].status is AgentObservedStatus.STARTING


def test_invalid_scope_never_projects_unowned_activity() -> None:
    model = AgentActivityModel()
    valid = _scope()
    model.begin_scope(valid)
    model.observe(valid, {"child": {"status": "running"}})

    invalid = AgentActivityScope(
        provider="openai",
        work_id="",
        root_turn_id="turn-two",
        root_actor_id="root-thread",
    )
    snapshot = model.begin_scope(invalid)

    assert snapshot.scope is None
    assert snapshot.activities == ()


# ── observed duration ────────────────────────────────────────────────────


class _FakeClock:
    """Injected so duration assertions are not wall-clock races."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _model_with_clock():
    clock = _FakeClock()
    model = AgentActivityModel(clock=clock)
    scope = _scope()
    model.begin_scope(scope)
    return model, scope, clock


def test_elapsed_runs_from_first_observation_and_freezes_when_terminal() -> None:
    """A finished agent's duration must stop, or the dock reports a completed
    subagent as though it were still burning."""
    model, scope, clock = _model_with_clock()
    model.observe(scope, {"child": {"status": "running"}})

    clock.advance(30)
    activity = model.snapshot().activities[0]
    assert activity.elapsed(clock.now) == 30

    model.observe(scope, {"child": {"status": "completed"}})
    finished_at = clock.now
    clock.advance(600)
    activity = model.snapshot().activities[0]
    assert activity.terminal_at == finished_at
    assert activity.elapsed(clock.now) == 30


def test_the_origin_survives_every_later_observation() -> None:
    """`replace()` rebuilds the record on every update, so the origin is one
    dropped keyword away from silently restarting the clock each event."""
    model, scope, clock = _model_with_clock()
    model.observe(scope, {"child": {"status": "starting"}})
    started = model.snapshot().activities[0].first_observed_at

    for _ in range(5):
        clock.advance(10)
        model.observe(scope, {"child": {"status": "running", "message": f"{clock.now}"}})

    activity = model.snapshot().activities[0]
    assert activity.first_observed_at == started
    assert activity.elapsed(clock.now) == 50


def test_an_actor_first_seen_already_terminal_has_no_duration() -> None:
    """Its whole life happened before Helios looked; a growing number there
    would be invented, which is the one thing this model refuses to do."""
    model, scope, clock = _model_with_clock()
    model.observe(scope, {"child": {"status": "completed"}})

    clock.advance(120)
    assert model.snapshot().activities[0].elapsed(clock.now) == 0


def test_a_hand_built_activity_reports_no_duration_rather_than_a_wrong_one() -> None:
    from helios.backend.agent_activity import AgentActivity

    activity = AgentActivity(
        scope=_scope(),
        actor_id="child",
        status=AgentObservedStatus.RUNNING,
        provider_status="running",
        name="Agent",
        role="subagent",
        detail="",
        ordinal=1,
    )
    assert activity.elapsed(9_999_999.0) == 0.0
