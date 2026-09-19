from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from helios.backend.work_store import (
    EXECUTION_ATTEMPT_AUDIT_EVENT_TYPES,
    SCHEMA_VERSION,
    _PROVIDER_CONCURRENCY_LIMITS,
    ExecutionAdmissionError,
    StaleParticipantError,
    WorkStore,
    WorkStoreError,
)


@pytest.fixture
def store(tmp_path):
    value = WorkStore(tmp_path / "state" / "work" / "work.db")
    try:
        yield value
    finally:
        value.close()


def _downgrade_execution_attempts_to_v4(store: WorkStore) -> None:
    store._conn.executescript(
        """
        DROP INDEX execution_attempts_work_started;
        DROP INDEX execution_attempts_one_running_per_work;
        DROP INDEX IF EXISTS execution_attempts_one_running_non_claude;
        DROP TRIGGER execution_attempts_identity_insert;
        DROP TRIGGER execution_attempts_identity_update;
        DROP TRIGGER participants_execution_identity_update;
        ALTER TABLE execution_attempts RENAME TO execution_attempts_v5;
        CREATE TABLE execution_attempts (
            attempt_id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES works(work_id),
            participant_id TEXT NOT NULL REFERENCES participants(participant_id),
            participant_generation INTEGER NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            terminal_reason TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        INSERT INTO execution_attempts (
            attempt_id, work_id, participant_id, participant_generation,
            provider, status, terminal_reason, metadata_json, started_at,
            finished_at, updated_at
        )
        SELECT
            attempt_id, work_id, participant_id, participant_generation,
            provider, status, terminal_reason, metadata_json, started_at,
            finished_at, updated_at
        FROM execution_attempts_v5;
        DROP TABLE execution_attempts_v5;
        CREATE UNIQUE INDEX execution_attempts_single_running
            ON execution_attempts ((1)) WHERE status = 'running';
        CREATE INDEX execution_attempts_work_started
            ON execution_attempts(work_id, started_at, attempt_id);
        ALTER TABLE events DROP COLUMN control_plane;
        PRAGMA user_version = 4;
        """
    )


def _downgrade_execution_admission_to_v5(store: WorkStore) -> None:
    store._conn.executescript(
        """
        DROP INDEX execution_attempts_one_running_per_work;
        DROP INDEX IF EXISTS execution_attempts_one_running_non_claude;
        DROP TRIGGER execution_attempts_identity_insert;
        DROP TRIGGER execution_attempts_identity_update;
        DROP TRIGGER participants_execution_identity_update;
        CREATE UNIQUE INDEX execution_attempts_single_running
            ON execution_attempts ((1)) WHERE status = 'running';
        PRAGMA user_version = 5;
        """
    )


def test_create_update_and_list_work_emit_ordered_events(store):
    work = store.create_work(
        objective="Ship Helios",
        cwd="/repo",
        lead_provider="anthropic",
    )
    updated = store.update_work(work.work_id, status="paused", mode="deliberate")

    assert updated.status == "paused"
    assert updated.mode == "deliberate"
    assert store.get_work(work.work_id) == updated
    assert store.list_works(status="paused") == [updated]
    events = store.list_events(work.work_id)
    assert [event.seq for event in events] == [1, 2]
    assert [event.event_type for event in events] == ["work.created", "work.updated"]


def test_new_and_native_works_default_to_single_provider(store):
    fresh = store.create_work(cwd="/repo")
    native = store.ensure_work_for_native("openai", "thread-1", cwd="/repo")

    assert fresh.mode == "single"
    assert native.mode == "single"


def test_execution_plan_revisions_are_durable_idempotent_and_keep_task_ids(store):
    work = store.create_work(objective="Ship durable plans", cwd="/repo")
    participant = store.bind_participant(
        work.work_id,
        "openai",
        native_thread_id="thread-plan",
    )

    first = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-one",
        explanation="Starting the implementation",
        steps=[
            {"step": "Inspect current plan flow", "status": "completed"},
            {
                "step": "Implement durable execution plan storage",
                "status": "inProgress",
            },
        ],
    )
    duplicate = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-one",
        explanation="Starting the implementation",
        steps=[
            {"step": "Inspect current plan flow", "status": "completed"},
            {
                "step": "Implement durable execution plan storage",
                "status": "inProgress",
            },
        ],
    )

    assert duplicate == first
    assert first.revision == 1
    assert first.completed_count == 1
    assert first.total_count == 2
    assert len(store.list_execution_plan_revisions(work.work_id)) == 1

    revised = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-one",
        explanation="Storage is ready; wiring the UI",
        steps=[
            {"step": "Inspect current plan flow", "status": "completed"},
            {
                "step": "Implement durable execution-plan storage and migration",
                "status": "completed",
            },
            {"step": "Wire the persistent task counter", "status": "inProgress"},
        ],
    )

    assert revised.revision == 2
    assert revised.steps[0].task_id == first.steps[0].task_id
    assert revised.steps[1].task_id == first.steps[1].task_id
    assert revised.steps[2].task_id not in {step.task_id for step in first.steps}
    assert store.get_execution_plan(work.work_id) == revised
    assert [plan.revision for plan in store.list_execution_plan_revisions(
        work.work_id, plan_id=first.plan_id
    )] == [1, 2]


def test_new_execution_plan_turn_cannot_be_overwritten_by_superseded_turn(store):
    work = store.create_work(objective="Keep turn order", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")

    old = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-old",
        steps=[{"step": "Old task", "status": "inProgress"}],
    )
    current = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-current",
        steps=[{"step": "Current task", "status": "inProgress"}],
    )

    assert current.plan_id != old.plan_id
    with pytest.raises(StaleParticipantError, match="superseded"):
        store.record_execution_plan(
            work.work_id,
            participant.participant_id,
            provider="openai",
            expected_participant_generation=participant.generation,
            native_turn_id="turn-old",
            steps=[{"step": "Late old task", "status": "completed"}],
        )
    assert store.get_execution_plan(work.work_id) == current


def test_execution_plan_revision_marks_removed_unfinished_task_dropped(store):
    work = store.create_work(objective="Keep revision history visible", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    first = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-revise",
        steps=[
            {"step": "Keep this task", "status": "inProgress"},
            {"step": "Do not erase this task", "status": "pending"},
        ],
    )

    revised = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-revise",
        explanation="Scope changed after inspection",
        steps=[{"step": "Keep this task", "status": "completed"}],
    )

    dropped = next(step for step in revised.steps if step.status == "dropped")
    assert dropped.task_id == first.steps[1].task_id
    assert dropped.text == "Do not erase this task"
    assert dropped.blocked_reason == "Scope changed after inspection"
    assert revised.completed_count == 1
    assert revised.total_count == 1
    assert store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-revise",
        explanation="Scope changed after inspection",
        steps=[{"step": "Keep this task", "status": "completed"}],
    ) == revised
    assert len(store.list_execution_plan_revisions(work.work_id)) == 2

    reintroduced = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-revise",
        steps=[
            {"step": "Keep this task", "status": "completed"},
            {"step": "Do not erase this task", "status": "inProgress"},
        ],
    )
    restored = next(
        step for step in reintroduced.steps if step.text == "Do not erase this task"
    )
    assert restored.task_id == first.steps[1].task_id
    assert restored.status == "inProgress"
    assert restored.blocked_reason == ""
    assert len(store.list_execution_plan_revisions(work.work_id)) == 3


def test_execution_plan_must_match_latest_accepted_native_turn(store):
    work = store.create_work(objective="Fence native callbacks", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    attempt = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        attempt.attempt_id,
        wire_prompt_text="Build it",
        provider_request_key="request-one",
    )
    store.record_execution_acceptance(
        attempt.attempt_id,
        accepted_turn_id="turn-current",
        provider_request_key="request-one",
    )

    with pytest.raises(StaleParticipantError, match="latest accepted turn"):
        store.record_execution_plan(
            work.work_id,
            participant.participant_id,
            provider="openai",
            expected_participant_generation=participant.generation,
            native_turn_id="turn-stale",
            steps=[{"step": "Late task", "status": "inProgress"}],
        )
    current = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-current",
        steps=[{"step": "Current task", "status": "inProgress"}],
    )
    assert store.get_execution_plan(work.work_id) == current


def test_execution_plan_interruption_is_a_revision_not_completion(store):
    work = store.create_work(objective="Interrupt safely", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    first = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-stop",
        steps=[
            {"step": "Already verified", "status": "completed"},
            {"step": "Still editing", "status": "inProgress"},
            {"step": "Run tests", "status": "pending"},
        ],
    )

    interrupted = store.interrupt_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-stop",
        reason="Stopped by the user",
    )

    assert interrupted is not None
    assert interrupted.revision == first.revision + 1
    assert interrupted.status == "interrupted"
    assert [step.status for step in interrupted.steps] == [
        "completed",
        "interrupted",
        "pending",
    ]
    assert interrupted.completed_count == 1
    assert interrupted.steps[1].blocked_reason == "Stopped by the user"
    assert store.interrupt_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-stop",
    ) == interrupted
    assert len(store.list_execution_plan_revisions(work.work_id)) == 2
    with pytest.raises(StaleParticipantError, match="terminally interrupted"):
        store.record_execution_plan(
            work.work_id,
            participant.participant_id,
            provider="openai",
            expected_participant_generation=participant.generation,
            native_turn_id="turn-stop",
            steps=[
                {"step": "Already verified", "status": "completed"},
                {"step": "Still editing", "status": "completed"},
                {"step": "Run tests", "status": "completed"},
            ],
        )
    assert store.get_execution_plan(work.work_id) == interrupted


def test_execution_plan_rejects_stale_participant_generation(store):
    work = store.create_work(objective="Fence stale plans", cwd="/repo")
    participant = store.bind_participant(
        work.work_id,
        "openai",
        native_thread_id="thread-old",
    )
    rebound = store.update_participant(
        participant.participant_id,
        native_thread_id="thread-new",
        expected_generation=participant.generation,
    )

    with pytest.raises(StaleParticipantError, match="generation changed"):
        store.record_execution_plan(
            work.work_id,
            participant.participant_id,
            provider="openai",
            expected_participant_generation=participant.generation,
            native_turn_id="turn-stale",
            steps=[{"step": "Should not land", "status": "inProgress"}],
        )
    assert rebound.generation == participant.generation + 1
    assert store.get_execution_plan(work.work_id) is None


def test_execution_plan_revision_rows_are_immutable(store):
    work = store.create_work(objective="Audit plans", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    plan = store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-audit",
        steps=[{"step": "Preserve me", "status": "pending"}],
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._conn.execute(
            "UPDATE execution_plan_revisions SET explanation = 'rewrite' "
            "WHERE plan_id = ? AND revision = ?",
            (plan.plan_id, plan.revision),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._conn.execute(
            "DELETE FROM execution_plan_revisions "
            "WHERE plan_id = ? AND revision = ?",
            (plan.plan_id, plan.revision),
        )


def test_schema_v9_migrates_to_durable_execution_plans(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    work = current.create_work(objective="Preserve this Work", cwd="/repo")
    current._conn.executescript(
        """
        DROP TRIGGER IF EXISTS participants_plan_identity_update;
        DROP TABLE execution_plan_revisions;
        DROP TABLE execution_plans;
        PRAGMA user_version = 9;
        """
    )
    current.close()

    migrated = WorkStore(path)
    try:
        tables = {
            row["name"]
            for row in migrated._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == (
            SCHEMA_VERSION
        )
        assert {"execution_plans", "execution_plan_revisions"} <= tables
        assert migrated.get_work(work.work_id).objective == "Preserve this Work"
    finally:
        migrated.close()


def test_execution_plan_survives_store_reopen(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    first_store = WorkStore(path)
    work = first_store.create_work(objective="Resume the plan", cwd="/repo")
    participant = first_store.bind_participant(work.work_id, "openai")
    plan = first_store.record_execution_plan(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        native_turn_id="turn-persist",
        explanation="Persistent state",
        steps=[
            {"step": "Inspect", "status": "completed"},
            {"step": "Resume", "status": "inProgress"},
        ],
    )
    first_store.close()

    reopened = WorkStore(path)
    try:
        assert reopened.get_execution_plan(work.work_id) == plan
        assert reopened.list_execution_plan_revisions(work.work_id) == [plan]
    finally:
        reopened.close()


def test_contract_epoch_boundary_tracks_only_incomplete_to_accepted_transitions(store):
    work = store.create_work(cwd="/repo")
    speculative = store.append_event(
        work.work_id,
        "participant.contribution",
        {"text": "speculative"},
    )

    accepted = store.update_work(
        work.work_id,
        objective="Bounded objective",
        definition_of_done="Focused tests pass",
        mode="tandem",
    )
    assert accepted.contract_epoch == 1
    assert accepted.contract_start_seq == speculative.seq + 1

    edited = store.update_work(work.work_id, objective="Refined objective")
    assert edited.contract_epoch == accepted.contract_epoch
    assert edited.contract_start_seq == accepted.contract_start_seq

    store.update_work(work.work_id, objective="")
    store.append_event(
        work.work_id,
        "participant.contribution",
        {"text": "new speculative scope"},
    )
    reaccepted = store.update_work(work.work_id, objective="Second objective")
    assert reaccepted.contract_epoch == 2
    assert reaccepted.contract_start_seq == store.latest_event_seq(work.work_id)


def test_budget_exhaustion_is_atomic_idempotent_and_terminal(store):
    work = store.create_work(objective="Bounded work", cwd="/repo")

    limited = store.mark_budget_exhausted(
        work.work_id,
        provider="openai",
        details={"tokens_used": 42, "provider": "untrusted"},
    )

    assert limited.status == "budgetLimited"
    events = store.list_events(work.work_id)
    assert [event.event_type for event in events[-2:]] == [
        "work.updated",
        "budget.exhausted",
    ]
    assert events[-1].provider == "openai"
    assert events[-1].payload == {"provider": "openai", "tokens_used": 42}

    # Repeated budget notifications and stale status-only writes are no-ops.
    assert store.mark_budget_exhausted(work.work_id, provider="anthropic") == limited
    for stale_status in ("active", "paused", "complete"):
        assert store.update_work(work.work_id, status=stale_status) == limited
    assert store.list_events(work.work_id) == events

    # Descriptive goal edits may still land, but cannot renew execution or
    # smuggle a false status transition into the immutable ledger.
    edited = store.update_work(
        work.work_id,
        objective="Stale objective",
        status="complete",
    )
    assert edited.objective == "Stale objective"
    assert edited.status == "budgetLimited"
    assert store.list_events(work.work_id)[-1].payload == {
        "objective": "Stale objective"
    }

    renewed = store.create_work(objective="Explicit renewal", cwd="/repo")
    assert renewed.work_id != work.work_id
    assert renewed.status == "active"


def test_budget_transition_rolls_back_status_when_audit_append_fails(
    store, monkeypatch
):
    work = store.create_work(objective="Bounded work", cwd="/repo")
    before = store.list_events(work.work_id)
    original_append = store._append_event_conn

    def fail_append(conn, work_id, event_type, payload, **kwargs):
        if event_type == "budget.exhausted":
            raise RuntimeError("simulated audit failure")
        return original_append(conn, work_id, event_type, payload, **kwargs)

    monkeypatch.setattr(store, "_append_event_conn", fail_append)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        store.mark_budget_exhausted(work.work_id, provider="openai")

    assert store.get_work(work.work_id).status == "active"
    assert store.list_events(work.work_id) == before


def test_execution_attempt_lifecycle_is_durable_ordered_and_idempotent(store):
    work = store.create_work(objective="Bounded turn", cwd="/repo")
    participant = store.bind_participant(work.work_id, "anthropic")

    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
        metadata={"model": "claude-sonnet"},
        attempt_id="attempt_stable",
    )

    assert running.status == "running"
    assert store.active_execution_attempt() == running
    assert (
        store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="anthropic",
            expected_participant_generation=participant.generation,
            attempt_id="attempt_stable",
        )
        == running
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="exact lifecycle wire prompt",
        provider_request_key="request-lifecycle",
    )
    store.record_execution_acceptance(
        running.attempt_id,
        accepted_turn_id="turn-lifecycle",
        provider_request_key="request-lifecycle",
    )

    completed = store.finish_execution_attempt(
        running.attempt_id,
        status="completed",
        terminal_reason="provider_result",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": "request-lifecycle",
            "turn_id": "turn-lifecycle",
        },
    )
    assert completed.status == "completed"
    assert completed.finished_at
    assert store.active_execution_attempt() is None
    assert (
        store.finish_execution_attempt(
            running.attempt_id,
            status="completed",
            terminal_reason="provider_result",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "completed",
                "request_id": "request-lifecycle",
                "turn_id": "turn-lifecycle",
            },
        )
        == completed
    )
    with pytest.raises(WorkStoreError, match="terminal retry conflicts"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="failed",
            terminal_reason="late_duplicate",
        )
    with pytest.raises(WorkStoreError, match="terminal retry conflicts"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="completed",
            terminal_reason="different_receipt",
        )
    events = store.list_events(work.work_id)
    assert [event.event_type for event in events[-4:]] == [
        "execution.attempt.started",
        "execution.attempt.dispatch-prepared",
        "execution.attempt.accepted",
        "execution.attempt.finished",
    ]


def test_execution_recovery_correlation_is_durable_scrubbed_and_set_once(store):
    work = store.create_work(objective="Recover one turn", cwd="/repo")
    participant = store.bind_participant(
        work.work_id,
        "openai",
        native_thread_id="thread-stable",
    )
    raw_prompt = "UNIQUE RAW PROMPT 4db874af do not persist"
    with pytest.raises(ValueError, match="sensitive field"):
        store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="openai",
            expected_participant_generation=participant.generation,
            metadata={"model": "gpt-test", "password": "hunter2"},
            attempt_id="attempt_sensitive_metadata",
        )
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        metadata={"model": "gpt-test"},
        attempt_id="attempt_recovery",
    )

    dispatched = store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text=raw_prompt,
        provider_request_key="request-stable",
    )
    assert dispatched.prompt_digest == (
        "sha256:" + hashlib.sha256(raw_prompt.encode()).hexdigest()
    )
    assert dispatched.native_binding_id == "thread-stable"
    assert dispatched.provider_request_key == "request-stable"

    accepted = store.record_execution_acceptance(
        running.attempt_id,
        accepted_turn_id="turn-stable",
        provider_request_key="request-stable",
    )
    assert accepted.accepted_turn_id == "turn-stable"
    with pytest.raises(WorkStoreError, match="accepted turn id conflicts"):
        store.record_execution_acceptance(
            running.attempt_id,
            accepted_turn_id="turn-different",
            provider_request_key="request-stable",
        )
    with pytest.raises(WorkStoreError, match="prompt digest conflicts"):
        store.record_execution_dispatch(
            running.attempt_id,
            wire_prompt_text="different prompt",
            provider_request_key="request-stable",
        )

    acknowledgement = {
        "acknowledged": True,
        "cancellation_confirmed": False,
        "request_id": "request-stable",
        "turn_id": "turn-stable",
    }
    with pytest.raises(ValueError, match="sensitive field"):
        store.record_execution_stop(
            running.attempt_id,
            acknowledgement={"authorization": "Bearer stop-secret"},
        )
    stopped = store.record_execution_stop(
        running.attempt_id,
        acknowledgement=acknowledgement,
        queue_disposition="held",
    )
    assert stopped.stop_acknowledgement["acknowledged"] is True
    assert stopped.stop_acknowledgement["cancellation_confirmed"] is False
    assert stopped.queue_disposition == "held"

    completed = store.finish_execution_attempt(
        running.attempt_id,
        status="completed",
        terminal_reason="provider_terminal_receipt",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "done",
            "request_id": "request-stable",
            "turn_id": "turn-stable",
        },
        usage={"input_tokens": 12, "output_tokens": 3},
        cost_micro_usd=456,
        stop_acknowledgement=acknowledgement,
        queue_disposition="restored",
    )
    assert completed.terminal_receipt == {
        "evidence_type": "provider_terminal",
        "provider_status": "done",
        "request_id": "request-stable",
        "turn_id": "turn-stable",
    }
    assert completed.usage == {"input_tokens": 12, "output_tokens": 3}
    assert completed.cost_micro_usd == 456
    assert (
        store.finish_execution_attempt(
            running.attempt_id,
            status="completed",
            terminal_reason="provider_terminal_receipt",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "done",
                "request_id": "request-stable",
                "turn_id": "turn-stable",
            },
            usage={"input_tokens": 12, "output_tokens": 3},
            cost_micro_usd=456,
            stop_acknowledgement=acknowledgement,
            queue_disposition="restored",
        )
        == completed
    )
    with pytest.raises(WorkStoreError, match="terminal retry conflicts"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="completed",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "different",
            },
        )

    persisted = store.path.read_bytes()
    assert raw_prompt.encode() not in persisted
    assert b"hunter2" not in persisted
    assert b"stop-secret" not in persisted
    event_types = [event.event_type for event in store.list_events(work.work_id)]
    assert "execution.attempt.dispatch-prepared" in event_types
    assert "execution.attempt.accepted" in event_types
    assert "execution.attempt.stop-recorded" in event_types


def test_terminal_and_stop_evidence_must_match_durable_correlation(store):
    work = store.create_work(objective="Correlate provider evidence", cwd="/repo")
    participant = store.bind_participant(
        work.work_id,
        "openai",
        native_thread_id="thread-a",
    )
    accepted = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        accepted.attempt_id,
        wire_prompt_text="accepted request",
        provider_request_key="request-a",
    )
    store.record_execution_acceptance(
        accepted.attempt_id,
        accepted_turn_id="turn-a",
        provider_request_key="request-a",
    )

    with pytest.raises(WorkStoreError, match="durable provider request"):
        store.finish_execution_attempt(
            accepted.attempt_id,
            status="completed",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "completed",
                "request_id": "request-other",
                "turn_id": "turn-a",
            },
        )
    with pytest.raises(WorkStoreError, match="durable accepted turn"):
        store.finish_execution_attempt(
            accepted.attempt_id,
            status="completed",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "completed",
                "request_id": "request-a",
                "turn_id": "turn-other",
            },
        )
    with pytest.raises(WorkStoreError, match="durable native identity"):
        store.record_execution_stop(
            accepted.attempt_id,
            acknowledgement={
                "acknowledged": False,
                "request_id": "request-a",
                "turn_id": "turn-a",
                "native_id": "thread-other",
            },
        )

    completed = store.finish_execution_attempt(
        accepted.attempt_id,
        status="completed",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": "request-a",
            "turn_id": "turn-a",
            "response_id": "response-final",
        },
    )
    assert completed.terminal_receipt["response_id"] == "response-final"

    rejected = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        rejected.attempt_id,
        wire_prompt_text="rejected request",
        provider_request_key="request-b",
    )
    with pytest.raises(WorkStoreError, match="durable provider request"):
        store.finish_execution_attempt(
            rejected.attempt_id,
            status="failed",
            terminal_receipt={
                "evidence_type": "verified_rejection",
                "provider_status": "rejected",
                "request_id": "request-other",
            },
        )
    store.finish_execution_attempt(
        rejected.attempt_id,
        status="failed",
        terminal_receipt={
            "evidence_type": "verified_rejection",
            "provider_status": "rejected",
            "request_id": "request-b",
        },
    )

    native_only = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        native_only.attempt_id,
        wire_prompt_text="native-correlated request",
        provider_request_key="request-c",
    )
    with pytest.raises(WorkStoreError, match="durable native identity"):
        store.finish_execution_attempt(
            native_only.attempt_id,
            status="completed",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "completed",
                "request_id": "request-c",
                "native_id": "thread-other",
            },
        )
    store.finish_execution_attempt(
        native_only.attempt_id,
        status="completed",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": "request-c",
            "native_id": "thread-a",
        },
    )


def test_confirmed_cancellation_releases_a_dispatched_attempt(store):
    """The exact release ClaudeCliDriver._on_exit sends after an asked-for stop.

    Without it the attempt stays 'running' forever: no driver survives to
    produce a provider receipt, so the Work is refused until Helios restarts.
    """

    work = store.create_work(objective="Release on stop", cwd="/repo")
    participant = store.bind_participant(work.work_id, "anthropic")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="a turn the user stops",
        provider_request_key=running.attempt_id,
    )

    terminal = store.finish_execution_attempt(
        running.attempt_id,
        status="aborted",
        terminal_reason="claude.stopped_by_helios",
        stop_acknowledgement={
            "acknowledged": True,
            "cancellation_confirmed": True,
            "request_id": running.attempt_id,
        },
        queue_disposition="restored",
    )

    assert terminal.status == "aborted"
    assert terminal.terminal_receipt == {}
    assert terminal.usage == {}
    # The lane is free: a fresh turn on the same Work is admitted again.
    store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
    )


def test_stop_acknowledgement_monotonically_merges_partial_updates(store):
    work = store.create_work(objective="Monotonic stop evidence", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="request to cancel",
        provider_request_key="request-stop",
    )
    provisional = store.record_execution_stop(
        running.attempt_id,
        acknowledgement={
            "acknowledged": False,
            "cancellation_confirmed": False,
            "request_id": "request-stop",
        },
        queue_disposition="held",
    )
    assert provisional.stop_acknowledgement["acknowledged"] is False

    confirmed = store.record_execution_stop(
        running.attempt_id,
        acknowledgement={
            "acknowledged": True,
            "cancellation_confirmed": True,
            "provider_status": "cancelled",
        },
        queue_disposition="restored",
    )
    assert confirmed.stop_acknowledgement == {
        "acknowledged": True,
        "cancellation_confirmed": True,
        "provider_status": "cancelled",
        "request_id": "request-stop",
    }
    with pytest.raises(WorkStoreError, match="acknowledgement regressed"):
        store.record_execution_stop(
            running.attempt_id,
            acknowledgement={"acknowledged": False},
        )
    terminal = store.finish_execution_attempt(
        running.attempt_id,
        status="aborted",
        terminal_reason="cancellation_ack",
    )
    assert terminal.stop_acknowledgement["cancellation_confirmed"] is True


def test_dispatch_refuses_an_attempt_after_participant_generation_changes(store):
    work = store.create_work(objective="Reject stale dispatch", cwd="/repo")
    participant = store.bind_participant(
        work.work_id,
        "openai",
        native_thread_id="thread-a",
    )
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    rebound = store.update_participant(
        participant.participant_id,
        native_thread_id="thread-b",
        expected_generation=participant.generation,
    )
    assert rebound.generation == participant.generation + 1

    with pytest.raises(StaleParticipantError, match="generation changed"):
        store.record_execution_dispatch(
            running.attempt_id,
            wire_prompt_text="must not reach the retired thread",
            provider_request_key="request-stale",
        )
    unchanged = store.get_execution_attempt(running.attempt_id)
    assert unchanged.prompt_digest == ""
    assert unchanged.provider_request_key == ""
    store.finish_execution_attempt(
        running.attempt_id,
        status="aborted",
        terminal_reason="local_abort",
    )


def test_execution_recovery_receipts_reject_raw_prompt_containers(store):
    work = store.create_work(objective="No raw prompts", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openrouter")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openrouter",
        expected_participant_generation=participant.generation,
    )

    with pytest.raises(ValueError, match="raw prompt/content"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="completed",
            terminal_receipt={"prompt": "must not persist"},
        )
    assert store.active_execution_attempt().attempt_id == running.attempt_id


def test_generic_event_api_rejects_the_reserved_execution_attempt_namespace(store):
    work = store.create_work(objective="Reserved audit namespace", cwd="/repo")
    before = store.list_events(work.work_id)

    for event_type in (
        *EXECUTION_ATTEMPT_AUDIT_EVENT_TYPES,
        "execution.attempt.user-message",
    ):
        with pytest.raises(WorkStoreError, match="reserved control-plane"):
            store.append_event(
                work.work_id,
                event_type,
                {"text": "must not enter the ledger"},
            )

    assert store.list_events(work.work_id) == before


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("prompt_text", "raw prompt sentinel"),
        ("result", "raw provider result"),
        ("last_message", "raw last message"),
        ("authorization", "Bearer secret-value"),
        ("headers", {"Authorization": "Bearer secret-value"}),
        ("request_headers", {"X-Api-Key": "secret-value"}),
    ),
)
def test_execution_metadata_rejects_content_credentials_and_headers(
    store,
    field,
    value,
):
    work = store.create_work(objective="Typed metadata", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")

    with pytest.raises(ValueError, match="raw prompt/content|sensitive field"):
        store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="openai",
            expected_participant_generation=participant.generation,
            metadata={field: value},
        )

    assert b"secret-value" not in store.path.read_bytes()


def test_recovery_receipts_reject_nested_content_and_sensitive_containers(store):
    work = store.create_work(objective="Flat typed receipts", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openrouter")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openrouter",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="typed receipt request",
        provider_request_key=running.attempt_id,
    )

    for receipt in (
        {"request_id": {"prompt_text": "nested raw sentinel"}},
        {"request_id": ["list raw sentinel"]},
        {"headers": {"Authorization": "Bearer nested-secret-value"}},
        {"cookie": "session=nested-secret-value"},
        {"authorization": "Basic nested-secret-value"},
    ):
        with pytest.raises((TypeError, ValueError)):
            store.finish_execution_attempt(
                running.attempt_id,
                status="completed",
                terminal_receipt=receipt,
            )
    with pytest.raises((TypeError, ValueError)):
        store.record_execution_stop(
            running.attempt_id,
            acknowledgement={"request_id": {"last_message": "nested raw sentinel"}},
        )

    persisted = store.path.read_bytes()
    assert b"nested raw sentinel" not in persisted
    assert b"nested-secret-value" not in persisted
    assert store.active_execution_attempt().attempt_id == running.attempt_id


def test_same_attempt_retry_requires_identical_metadata(store):
    work = store.create_work(objective="Idempotent admission", cwd="/repo")
    participant = store.bind_participant(work.work_id, "anthropic")
    original = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
        metadata={"model": "claude-sonnet", "effort": "high"},
        attempt_id="attempt_metadata_identity",
    )

    assert (
        store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="anthropic",
            expected_participant_generation=participant.generation,
            metadata={"model": "claude-sonnet", "effort": "high"},
            attempt_id=original.attempt_id,
        )
        == original
    )
    assert (
        store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="anthropic",
            expected_participant_generation=participant.generation,
            attempt_id=original.attempt_id,
        )
        == original
    )
    with pytest.raises(WorkStoreError, match="metadata conflicts"):
        store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="anthropic",
            expected_participant_generation=participant.generation,
            metadata={"model": "claude-opus", "effort": "high"},
            attempt_id=original.attempt_id,
        )


def test_dispatch_digest_is_the_exact_wire_text_and_native_identity_is_storage_owned(
    store,
):
    work = store.create_work(objective="Exact dispatch identity", cwd="/repo")
    participant = store.bind_participant(
        work.work_id,
        "openai",
        native_thread_id="thread-original",
    )
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    wire_text = "  exact wire text\nwith unicode: λ  "

    with pytest.raises(ValueError, match="provider request key"):
        store.record_execution_dispatch(
            running.attempt_id,
            wire_prompt_text=wire_text,
            provider_request_key="",
        )
    assert store.get_execution_attempt(running.attempt_id).prompt_digest == ""

    dispatched = store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text=wire_text,
        provider_request_key=running.attempt_id,
    )
    assert dispatched.prompt_digest == (
        "sha256:" + hashlib.sha256(wire_text.encode("utf-8")).hexdigest()
    )
    assert dispatched.prompt_digest != (
        "sha256:" + hashlib.sha256(wire_text.strip().encode("utf-8")).hexdigest()
    )
    assert dispatched.native_binding_id == "thread-original"

    store.update_participant(
        participant.participant_id,
        native_thread_id="thread-rebound",
        expected_generation=participant.generation,
    )
    assert (
        store.get_execution_attempt(running.attempt_id).native_binding_id
        == "thread-original"
    )


def test_dispatch_fills_a_late_native_identity_only_for_the_admitted_generation(store):
    work = store.create_work(objective="Late native binding", cwd="/repo")
    placeholder = store.bind_participant(work.work_id, "anthropic")
    running = store.start_execution_attempt(
        work.work_id,
        placeholder.participant_id,
        provider="anthropic",
        expected_participant_generation=placeholder.generation,
        attempt_id="attempt_late_native",
    )
    assert running.native_binding_id == ""

    attached = store.bind_participant(
        work.work_id,
        "anthropic",
        native_session_id="session-late",
    )
    assert attached.generation == placeholder.generation
    assert (
        store.start_execution_attempt(
            work.work_id,
            placeholder.participant_id,
            provider="anthropic",
            expected_participant_generation=placeholder.generation,
            attempt_id=running.attempt_id,
        )
        == running
    )
    dispatched = store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="wire prompt after the native session appeared",
        provider_request_key=running.attempt_id,
    )
    assert dispatched.native_binding_id == "session-late"


def test_finish_fills_a_native_identity_attached_after_dispatch(store):
    work = store.create_work(objective="Late terminal binding", cwd="/repo")
    placeholder = store.bind_participant(work.work_id, "anthropic")
    running = store.start_execution_attempt(
        work.work_id,
        placeholder.participant_id,
        provider="anthropic",
        expected_participant_generation=placeholder.generation,
    )
    dispatched = store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="wire prompt before the native session appears",
        provider_request_key="request-late-terminal",
    )
    assert dispatched.native_binding_id == ""
    attached = store.bind_participant(
        work.work_id,
        "anthropic",
        native_session_id="session-terminal",
    )
    assert attached.generation == placeholder.generation

    with pytest.raises(WorkStoreError, match="durable native identity"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="completed",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "completed",
                "request_id": "request-late-terminal",
                "native_id": "session-other",
            },
        )
    terminal = store.finish_execution_attempt(
        running.attempt_id,
        status="completed",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": "request-late-terminal",
            "native_id": "session-terminal",
        },
    )
    assert terminal.native_binding_id == "session-terminal"


def test_release_state_machine_requires_typed_provider_evidence(store):
    work = store.create_work(objective="Evidence-gated release", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    local = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )

    with pytest.raises(WorkStoreError, match="pre-dispatch"):
        store.finish_execution_attempt(local.attempt_id, status="completed")
    assert store.active_execution_attempt().attempt_id == local.attempt_id
    store.finish_execution_attempt(
        local.attempt_id,
        status="aborted",
        terminal_reason="local_abort",
    )

    dispatched = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        dispatched.attempt_id,
        wire_prompt_text="request that may have crossed the boundary",
        provider_request_key=dispatched.attempt_id,
    )
    with pytest.raises(WorkStoreError, match="remains uncertain"):
        store.finish_execution_attempt(
            dispatched.attempt_id,
            status="failed",
            terminal_reason="transport_error",
        )
    with pytest.raises(WorkStoreError, match="authoritative provider status"):
        store.finish_execution_attempt(
            dispatched.attempt_id,
            status="failed",
            terminal_reason="rpc_rejected",
            terminal_receipt={"evidence_type": "verified_rejection"},
        )
    with pytest.raises(WorkStoreError, match="typed evidence kind"):
        store.finish_execution_attempt(
            dispatched.attempt_id,
            status="failed",
            terminal_reason="rpc_rejected",
            terminal_receipt={
                "provider_status": "rpc_rejected",
                "request_id": dispatched.attempt_id,
            },
        )
    assert store.active_execution_attempt().attempt_id == dispatched.attempt_id

    rejected = store.finish_execution_attempt(
        dispatched.attempt_id,
        status="failed",
        terminal_reason="rpc_rejected",
        terminal_receipt={
            "evidence_type": "verified_rejection",
            "provider_status": "rpc_rejected",
            "request_id": dispatched.attempt_id,
        },
    )
    assert rejected.status == "failed"
    assert store.active_execution_attempt() is None


def test_acceptance_requires_dispatch_and_late_new_acceptance_conflicts(store):
    work = store.create_work(objective="Causal acceptance", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )

    with pytest.raises(WorkStoreError, match="no durable dispatch"):
        store.record_execution_acceptance(
            running.attempt_id,
            accepted_turn_id="turn-before-dispatch",
            provider_request_key="request-causal",
        )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="causally ordered wire prompt",
        provider_request_key="request-causal",
    )
    accepted = store.record_execution_acceptance(
        running.attempt_id,
        accepted_turn_id="turn-accepted",
        provider_request_key="request-causal",
    )
    terminal = store.finish_execution_attempt(
        running.attempt_id,
        status="completed",
        terminal_reason="provider_terminal",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": "request-causal",
            "turn_id": "turn-accepted",
        },
    )
    assert (
        store.record_execution_acceptance(
            running.attempt_id,
            accepted_turn_id="turn-accepted",
            provider_request_key="request-causal",
        )
        == terminal
    )
    with pytest.raises(WorkStoreError, match="accepted turn id conflicts"):
        store.record_execution_acceptance(
            running.attempt_id,
            accepted_turn_id="turn-late-different",
            provider_request_key="request-causal",
        )
    assert accepted.accepted_turn_id == "turn-accepted"

    second = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        second.attempt_id,
        wire_prompt_text="definitively rejected wire prompt",
        provider_request_key=second.attempt_id,
    )
    store.finish_execution_attempt(
        second.attempt_id,
        status="failed",
        terminal_reason="rpc_rejected",
        terminal_receipt={
            "evidence_type": "verified_rejection",
            "provider_status": "rpc_rejected",
            "request_id": second.attempt_id,
        },
    )
    with pytest.raises(WorkStoreError, match="already terminal"):
        store.record_execution_acceptance(
            second.attempt_id,
            accepted_turn_id="turn-impossible-late",
            provider_request_key=second.attempt_id,
        )


def test_acceptance_and_verified_rejection_race_never_releases_an_accepted_turn(
    tmp_path,
):
    path = tmp_path / "state" / "work" / "work.db"
    setup = WorkStore(path)
    first = second = None
    try:
        work = setup.create_work(objective="Acceptance race", cwd="/repo")
        participant = setup.bind_participant(work.work_id, "openai")
        first = WorkStore(path)
        second = WorkStore(path)
        for index in range(12):
            running = setup.start_execution_attempt(
                work.work_id,
                participant.participant_id,
                provider="openai",
                expected_participant_generation=participant.generation,
                attempt_id=f"attempt_acceptance_race_{index}",
            )
            setup.record_execution_dispatch(
                running.attempt_id,
                wire_prompt_text=f"race wire prompt {index}",
                provider_request_key=running.attempt_id,
            )
            barrier = threading.Barrier(2)

            def accept():
                barrier.wait()
                try:
                    first.record_execution_acceptance(
                        running.attempt_id,
                        accepted_turn_id=f"turn-race-{index}",
                        provider_request_key=running.attempt_id,
                    )
                except WorkStoreError:
                    return "accept-conflict"
                return "accepted"

            def reject():
                barrier.wait()
                try:
                    second.finish_execution_attempt(
                        running.attempt_id,
                        status="failed",
                        terminal_reason="rpc_rejected",
                        terminal_receipt={
                            "evidence_type": "verified_rejection",
                            "provider_status": "rpc_rejected",
                            "request_id": running.attempt_id,
                        },
                    )
                except WorkStoreError:
                    return "reject-conflict"
                return "rejected"

            with ThreadPoolExecutor(max_workers=2) as pool:
                accepted_future = pool.submit(accept)
                rejected_future = pool.submit(reject)
                outcomes = {accepted_future.result(), rejected_future.result()}

            durable = setup.get_execution_attempt(running.attempt_id)
            if durable.status == "running":
                assert durable.accepted_turn_id == f"turn-race-{index}"
                assert outcomes == {"accepted", "reject-conflict"}
                setup.finish_execution_attempt(
                    running.attempt_id,
                    status="completed",
                    terminal_reason="provider_terminal",
                    terminal_receipt={
                        "evidence_type": "provider_terminal",
                        "provider_status": "completed",
                        "request_id": running.attempt_id,
                        "turn_id": f"turn-race-{index}",
                    },
                )
            else:
                assert durable.status == "failed"
                assert durable.accepted_turn_id == ""
                assert outcomes == {"rejected", "accept-conflict"}
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        setup.close()


def test_exact_dispatch_retry_never_reauthorizes_a_terminal_attempt(store):
    work = store.create_work(objective="Reject late dispatch", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    wire_text = "one authorized request"
    request_key = "request-terminal-dispatch"
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text=wire_text,
        provider_request_key=request_key,
    )
    terminal = store.finish_execution_attempt(
        running.attempt_id,
        status="failed",
        terminal_reason="verified_rejection",
        terminal_receipt={
            "evidence_type": "verified_rejection",
            "provider_status": "rejected",
            "request_id": request_key,
        },
    )
    assert terminal.finished_at

    with pytest.raises(WorkStoreError, match="already terminal"):
        store.record_execution_dispatch(
            running.attempt_id,
            wire_prompt_text=wire_text,
            provider_request_key=request_key,
        )


def test_finish_and_exact_dispatch_race_never_returns_terminal_authorization(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    setup = WorkStore(path)
    dispatch_store = finish_store = None
    try:
        work = setup.create_work(objective="Serialize finish and dispatch", cwd="/repo")
        participant = setup.bind_participant(work.work_id, "openai")
        dispatch_store = WorkStore(path)
        finish_store = WorkStore(path)
        for index in range(20):
            running = setup.start_execution_attempt(
                work.work_id,
                participant.participant_id,
                provider="openai",
                expected_participant_generation=participant.generation,
                attempt_id=f"attempt_dispatch_finish_race_{index}",
            )
            wire_text = f"race request {index}"
            request_key = f"request-dispatch-finish-{index}"
            setup.record_execution_dispatch(
                running.attempt_id,
                wire_prompt_text=wire_text,
                provider_request_key=request_key,
            )
            barrier = threading.Barrier(2)

            def redispatch():
                barrier.wait()
                try:
                    observed = dispatch_store.record_execution_dispatch(
                        running.attempt_id,
                        wire_prompt_text=wire_text,
                        provider_request_key=request_key,
                    )
                except WorkStoreError:
                    return "terminal-rejected"
                return f"authorized-{observed.status}"

            def finish():
                barrier.wait()
                return finish_store.finish_execution_attempt(
                    running.attempt_id,
                    status="failed",
                    terminal_reason="verified_rejection",
                    terminal_receipt={
                        "evidence_type": "verified_rejection",
                        "provider_status": "rejected",
                        "request_id": request_key,
                    },
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                dispatch_future = pool.submit(redispatch)
                finish_future = pool.submit(finish)
                dispatch_outcome = dispatch_future.result()
                terminal = finish_future.result()

            assert dispatch_outcome in {"authorized-running", "terminal-rejected"}
            assert terminal.status == "failed"
            assert setup.get_execution_attempt(running.attempt_id).status == "failed"
    finally:
        if dispatch_store is not None:
            dispatch_store.close()
        if finish_store is not None:
            finish_store.close()
        setup.close()


def test_verified_rejection_conflicts_with_acceptance_and_cancel_ack_releases(store):
    work = store.create_work(objective="Typed terminal evidence", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    accepted = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        accepted.attempt_id,
        wire_prompt_text="accepted provider request",
        provider_request_key=accepted.attempt_id,
    )
    store.record_execution_acceptance(
        accepted.attempt_id,
        accepted_turn_id="turn-authoritative",
        provider_request_key=accepted.attempt_id,
    )
    with pytest.raises(
        WorkStoreError, match="conflicts with durable provider acceptance"
    ):
        store.finish_execution_attempt(
            accepted.attempt_id,
            status="failed",
            terminal_reason="rpc_rejected",
            terminal_receipt={
                "evidence_type": "verified_rejection",
                "provider_status": "rpc_rejected",
                "request_id": accepted.attempt_id,
            },
        )
    store.finish_execution_attempt(
        accepted.attempt_id,
        status="completed",
        terminal_reason="provider_terminal",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": accepted.attempt_id,
            "turn_id": "turn-authoritative",
        },
    )

    cancelled = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        cancelled.attempt_id,
        wire_prompt_text="request cancelled after possible dispatch",
        provider_request_key=cancelled.attempt_id,
    )
    store.record_execution_stop(
        cancelled.attempt_id,
        acknowledgement={
            "acknowledged": True,
            "cancellation_confirmed": True,
            "provider_status": "cancelled",
            "request_id": cancelled.attempt_id,
        },
        queue_disposition="held",
    )
    terminal = store.finish_execution_attempt(
        cancelled.attempt_id,
        status="aborted",
        terminal_reason="cancellation_ack",
        queue_disposition="restored",
    )
    assert terminal.status == "aborted"
    assert terminal.queue_disposition == "restored"


def test_queue_disposition_is_an_enum_with_monotonic_transitions(store):
    work = store.create_work(objective="Queue state", cwd="/repo")
    participant = store.bind_participant(work.work_id, "anthropic")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
    )

    with pytest.raises(ValueError, match="invalid queue disposition"):
        store.record_execution_stop(
            running.attempt_id,
            queue_disposition="maybe-later",
        )
    held = store.record_execution_stop(
        running.attempt_id,
        queue_disposition="held",
    )
    assert held.queue_disposition == "held"
    queue_event = store.list_events(work.work_id)[-1]
    assert queue_event.event_type == "execution.attempt.stop-recorded"
    assert queue_event.payload == {
        "attempt_id": running.attempt_id,
        "queue_disposition": "held",
    }
    restored = store.record_execution_stop(
        running.attempt_id,
        queue_disposition="restored",
    )
    assert restored.queue_disposition == "restored"
    with pytest.raises(WorkStoreError, match="queue disposition conflicts"):
        store.record_execution_stop(
            running.attempt_id,
            queue_disposition="released",
        )
    store.finish_execution_attempt(
        running.attempt_id,
        status="aborted",
        terminal_reason="local_abort",
        queue_disposition="restored",
    )


@pytest.mark.parametrize(
    "terminal_kwargs",
    (
        {"terminal_receipt": {}},
        {"usage": {}},
        {"cost_micro_usd": 0},
        {
            "stop_acknowledgement": {
                "acknowledged": False,
                "request_id": "request-impossible",
            }
        },
    ),
)
def test_predispatch_local_abort_rejects_impossible_provider_evidence(
    store,
    terminal_kwargs,
):
    work = store.create_work(objective="Local abort evidence", cwd="/repo")
    participant = store.bind_participant(work.work_id, "anthropic")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
    )

    with pytest.raises(WorkStoreError, match="pre-dispatch|cannot precede"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="aborted",
            terminal_reason="local_abort",
            **terminal_kwargs,
        )
    assert store.active_execution_attempt().attempt_id == running.attempt_id
    store.finish_execution_attempt(
        running.attempt_id,
        status="aborted",
        terminal_reason="local_abort",
        queue_disposition="discarded",
    )


def test_terminal_release_requires_held_queue_to_reach_a_final_disposition(store):
    work = store.create_work(objective="Resolve held queue", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="request with held retry",
        provider_request_key="request-held",
    )
    store.record_execution_stop(running.attempt_id, queue_disposition="held")

    with pytest.raises(WorkStoreError, match="final queue disposition"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="failed",
            terminal_receipt={
                "evidence_type": "verified_rejection",
                "provider_status": "rejected",
                "request_id": "request-held",
            },
        )
    terminal = store.finish_execution_attempt(
        running.attempt_id,
        status="failed",
        terminal_receipt={
            "evidence_type": "verified_rejection",
            "provider_status": "rejected",
            "request_id": "request-held",
        },
        queue_disposition="restored",
    )
    assert terminal.queue_disposition == "restored"


def test_recovery_mapping_rejects_normalized_duplicate_keys(store):
    work = store.create_work(objective="Unambiguous receipts", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")

    with pytest.raises(ValueError, match="duplicate normalized fields"):
        store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="openai",
            expected_participant_generation=participant.generation,
            metadata={"model": "gpt-a", "MODEL": "gpt-b"},
        )


def test_invalid_queue_disposition_error_does_not_echo_the_value(store):
    work = store.create_work(objective="Opaque queue errors", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openai")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    invalid_value = "private_queue_label"

    with pytest.raises(ValueError) as denied:
        store.record_execution_stop(
            running.attempt_id,
            queue_disposition=invalid_value,
        )
    assert invalid_value not in str(denied.value)


@pytest.mark.parametrize("nonfinite", (float("nan"), float("inf"), float("-inf")))
def test_usage_receipt_rejects_nonfinite_numbers(store, nonfinite):
    work = store.create_work(objective="Finite accounting", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openrouter")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openrouter",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="finite usage required",
        provider_request_key=running.attempt_id,
    )

    with pytest.raises(TypeError, match="must be an integer"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="completed",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "completed",
            },
            usage={"input_tokens": nonfinite},
        )
    assert store.active_execution_attempt().attempt_id == running.attempt_id


def test_terminal_reason_is_a_machine_code_not_provider_content(store):
    work = store.create_work(objective="Metadata-only reasons", cwd="/repo")
    participant = store.bind_participant(work.work_id, "anthropic")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
    )

    with pytest.raises(ValueError, match="invalid terminal reason"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="aborted",
            terminal_reason="raw provider result or user message",
        )
    assert store.active_execution_attempt().attempt_id == running.attempt_id


def test_execution_attempt_gate_is_scoped_to_one_turn_per_work(store):
    work = store.create_work(objective="One turn", cwd="/repo")
    claude = store.bind_participant(work.work_id, "anthropic")
    gpt = store.bind_participant(work.work_id, "openai")
    active = store.start_execution_attempt(
        work.work_id,
        claude.participant_id,
        provider="anthropic",
        expected_participant_generation=claude.generation,
        attempt_id="attempt_owner",
    )

    with pytest.raises(ExecutionAdmissionError, match="already executing") as denied:
        store.start_execution_attempt(
            work.work_id,
            gpt.participant_id,
            provider="openai",
            expected_participant_generation=gpt.generation,
        )
    message = str(denied.value)
    assert "provider=anthropic" in message
    assert f"Work={work.work_id}" in message
    assert "attempt=attempt_owner" in message

    store.finish_execution_attempt(
        active.attempt_id,
        status="aborted",
        terminal_reason="local_abort",
    )
    admitted = store.start_execution_attempt(
        work.work_id,
        gpt.participant_id,
        provider="openai",
        expected_participant_generation=gpt.generation,
    )
    assert admitted.provider == "openai"


def test_budget_attempt_release_and_work_breaker_commit_atomically(store):
    work = store.create_work(objective="Bounded turn", cwd="/repo")
    participant = store.bind_participant(work.work_id, "openrouter")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openrouter",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="bounded OpenRouter request",
        provider_request_key=running.attempt_id,
    )

    terminal = store.finish_execution_attempt(
        running.attempt_id,
        status="budgetLimited",
        terminal_reason="token_cap",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "budget_limited",
            "request_id": running.attempt_id,
        },
    )

    assert terminal.status == "budgetLimited"
    assert store.active_execution_attempt() is None
    assert store.get_work(work.work_id).status == "budgetLimited"
    with pytest.raises(ExecutionAdmissionError, match="execution budget"):
        store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="openrouter",
            expected_participant_generation=participant.generation,
        )

    # The provider's later synchronous/idle signal may enrich the dedicated
    # breaker event without being responsible for the terminal state itself.
    store.mark_budget_exhausted(
        work.work_id,
        provider="openrouter",
        details={"tokens_used": 200_001},
    )
    budget_events = [
        event
        for event in store.list_events(work.work_id)
        if event.event_type == "budget.exhausted"
    ]
    assert [event.payload for event in budget_events] == [
        {"provider": "openrouter", "tokens_used": 200_001}
    ]


@pytest.mark.parametrize(
    ("provider", "expected_admitted"),
    # A canonical provider's bounded lane now fits both racers. An
    # unrecognized id still admits exactly one, so a typo or a future provider
    # cannot mint concurrency merely by being unfamiliar.
    (("anthropic", 2), ("openai", 2), ("openrouter", 2), ("future-api", 1)),
)
def test_execution_attempt_provider_lane_race_is_transaction_safe(
    tmp_path,
    provider,
    expected_admitted,
):
    path = tmp_path / "state" / "work" / "work.db"
    setup = WorkStore(path)
    first = second = None
    try:
        work_a = setup.create_work(objective="A", cwd="/repo/a")
        work_b = setup.create_work(objective="B", cwd="/repo/b")
        part_a = setup.bind_participant(work_a.work_id, provider)
        part_b = setup.bind_participant(work_b.work_id, provider)
        first = WorkStore(path)
        second = WorkStore(path)
        barrier = threading.Barrier(2)

        def compete(args):
            candidate_store, work, participant = args
            barrier.wait()
            try:
                attempt = candidate_store.start_execution_attempt(
                    work.work_id,
                    participant.participant_id,
                    provider=provider,
                    expected_participant_generation=participant.generation,
                )
            except ExecutionAdmissionError:
                return "denied"
            return attempt.attempt_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    compete,
                    (
                        (first, work_a, part_a),
                        (second, work_b, part_b),
                    ),
                )
            )
        assert sum(result != "denied" for result in results) == expected_admitted
        assert len(setup.list_active_execution_attempts()) == expected_admitted
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        setup.close()


def test_shared_unknown_provider_lane_race_admits_exactly_one_connection(tmp_path):
    """Two *unrecognized* provider ids contend for one fail-closed slot.

    Each canonical provider owns its own bounded lane, so the containment that
    still has to hold is for ids Helios does not know: a typo or a future
    provider must not gain concurrency by being unfamiliar. They share one
    slot, and the race for it resolves across connections.
    """

    path = tmp_path / "state" / "work" / "work.db"
    setup = WorkStore(path)
    first = second = None
    try:
        gpt_work = setup.create_work(objective="Typo", cwd="/repo/typo")
        router_work = setup.create_work(objective="Future", cwd="/repo/future")
        gpt = setup.bind_participant(gpt_work.work_id, "openai-preview")
        router = setup.bind_participant(router_work.work_id, "future-api")
        first = WorkStore(path)
        second = WorkStore(path)
        barrier = threading.Barrier(2)

        def compete(args):
            candidate_store, work, participant, provider = args
            barrier.wait()
            try:
                return candidate_store.start_execution_attempt(
                    work.work_id,
                    participant.participant_id,
                    provider=provider,
                    expected_participant_generation=participant.generation,
                ).attempt_id
            except ExecutionAdmissionError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    compete,
                    (
                        (first, gpt_work, gpt, "openai-preview"),
                        (second, router_work, router, "future-api"),
                    ),
                )
            )
        assert sum(result.startswith("attempt_") for result in results) == 1
        denial = next(result for result in results if not result.startswith("attempt_"))
        assert denial.startswith("An unrecognized provider is already executing (")
        assert len(setup.list_active_execution_attempts()) == 1
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        setup.close()


def test_execution_attempt_same_work_race_admits_exactly_one_connection(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    setup = WorkStore(path)
    first = second = None
    try:
        work = setup.create_work(objective="One bounded Work", cwd="/repo")
        claude = setup.bind_participant(work.work_id, "anthropic")
        gpt = setup.bind_participant(work.work_id, "openai")
        first = WorkStore(path)
        second = WorkStore(path)
        barrier = threading.Barrier(2)

        def compete(args):
            candidate_store, participant, provider = args
            barrier.wait()
            try:
                return candidate_store.start_execution_attempt(
                    work.work_id,
                    participant.participant_id,
                    provider=provider,
                    expected_participant_generation=participant.generation,
                ).attempt_id
            except ExecutionAdmissionError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    compete,
                    (
                        (first, claude, "anthropic"),
                        (second, gpt, "openai"),
                    ),
                )
            )
        assert sum(result.startswith("attempt_") for result in results) == 1
        denial = next(result for result in results if not result.startswith("attempt_"))
        assert denial.startswith("This Work is already executing (")
        assert "Wait for it to finish or stop it first." in denial
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        setup.close()


def test_provider_matrix_allows_independent_lanes_and_reports_exact_conflicts(store):
    def bind(provider, suffix):
        work = store.create_work(objective=suffix, cwd=f"/repo/{suffix}")
        participant = store.bind_participant(work.work_id, provider)
        return work, participant

    def admit(work, participant, provider):
        return store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider=provider,
            expected_participant_generation=participant.generation,
        )

    def denial(work, participant, provider):
        with pytest.raises(ExecutionAdmissionError) as conflict:
            admit(work, participant, provider)
        return str(conflict.value)

    gpt_limit = _PROVIDER_CONCURRENCY_LIMITS["openai"]
    router_limit = _PROVIDER_CONCURRENCY_LIMITS["openrouter"]

    # Claude is uncapped: per-Work admission is its only gate.
    for suffix in ("claude-a", "claude-b", "claude-c"):
        work, participant = bind("anthropic", suffix)
        assert admit(work, participant, "anthropic").status == "running"

    # GPT fills its own lane to the ceiling, then the next one is refused.
    gpt_admitted = []
    for index in range(gpt_limit):
        work, participant = bind("openai", f"gpt-{index}")
        gpt_admitted.append(admit(work, participant, "openai"))
    gpt_overflow, gpt_overflow_part = bind("openai", "gpt-overflow")
    gpt_denial = denial(gpt_overflow, gpt_overflow_part, "openai")
    assert gpt_denial.startswith(
        f"openai already has {gpt_limit} running Works (provider=openai, "
    )
    assert f"allows {gpt_limit} concurrent openai attempts" in gpt_denial
    # The named owner is the oldest, so a caller waiting on the lane is told
    # about the attempt most likely to free up first.
    assert gpt_admitted[0].attempt_id in gpt_denial

    # A full GPT lane does not contain OpenRouter — that was the v0.57.1 bug.
    router_works = []
    for index in range(router_limit):
        work, participant = bind("openrouter", f"router-{index}")
        assert admit(work, participant, "openrouter").status == "running"
        router_works.append(work)
    router_overflow, router_overflow_part = bind("openrouter", "router-overflow")
    router_denial = denial(router_overflow, router_overflow_part, "openrouter")
    assert router_denial.startswith(
        f"openrouter already has {router_limit} running Works "
        "(provider=openrouter, "
    )

    # Every unrecognized id shares ONE slot, whatever it is spelled.
    unknown_work, unknown_part = bind("openai-preview", "unknown-a")
    assert admit(unknown_work, unknown_part, "openai-preview").status == "running"
    for provider, suffix in (("openai-preview", "unknown-b"), ("FutureAPI", "unknown-c")):
        work, participant = bind(provider, suffix)
        assert denial(work, participant, provider).startswith(
            "An unrecognized provider is already executing (provider=openai-preview, "
        )

    # Retiring one GPT attempt frees exactly one GPT slot and nothing else.
    store.finish_execution_attempt(
        gpt_admitted[0].attempt_id,
        status="aborted",
        terminal_reason="local_abort",
    )
    assert admit(gpt_overflow, gpt_overflow_part, "openai").status == "running"
    assert denial(router_overflow, router_overflow_part, "openrouter").startswith(
        f"openrouter already has {router_limit} running Works "
        "(provider=openrouter, "
    )

    assert len(store.list_active_execution_attempts()) == 3 + gpt_limit + router_limit + 1
    with pytest.raises(WorkStoreError, match="list_active_execution_attempts"):
        store.active_execution_attempt()


def test_schema_constraints_enforce_one_running_attempt_per_work(store):
    """The per-Work unique index is the surviving declarative constraint.

    A partial unique index cannot express "at most N", so v7's per-provider
    ceilings are enforced in the admission transaction rather than by the
    schema. What the schema still guarantees on its own — against raw SQL, a
    foreign process, or a Helios bug — is that one Work never has two running
    attempts, whatever provider each claims to be.
    """

    def bind(provider, suffix):
        work = store.create_work(objective=suffix, cwd=f"/repo/{suffix}")
        return work, store.bind_participant(work.work_id, provider)

    def insert_raw(attempt_id, work, participant, provider):
        store._conn.execute(
            """
            INSERT INTO execution_attempts (
                attempt_id, work_id, participant_id,
                participant_generation, provider, status, metadata_json,
                started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'running', '{}', ?, ?)
            """,
            (
                attempt_id,
                work.work_id,
                participant.participant_id,
                participant.generation,
                provider,
                "2026-08-05T00:00:00Z",
                "2026-08-05T00:00:00Z",
            ),
        )

    claude_a, claude_part_a = bind("anthropic", "raw-claude-a")
    claude_b, claude_part_b = bind("anthropic", "raw-claude-b")
    insert_raw("attempt_raw_claude_a", claude_a, claude_part_a, "anthropic")
    insert_raw("attempt_raw_claude_b", claude_b, claude_part_b, "anthropic")
    with pytest.raises(sqlite3.IntegrityError):
        insert_raw(
            "attempt_raw_same_work",
            claude_a,
            claude_part_a,
            "anthropic",
        )

    # Unrelated Works are free at the schema layer regardless of provider
    # spelling; their ceiling belongs to the admission path, not an index.
    for provider, suffix in (
        ("openai", "gpt"),
        ("OPENAI", "gpt-case-variant"),
        ("openrouter", "router"),
        ("FutureAPI", "future"),
        ("Anthropic", "claude-case-variant"),
    ):
        candidate_work, candidate_part = bind(provider, f"raw-{suffix}")
        insert_raw(f"attempt_raw_{suffix}", candidate_work, candidate_part, provider)
        # ...but a second running attempt inside that same Work is refused by
        # the index, so no provider spelling can double-dispatch one Work.
        with pytest.raises(sqlite3.IntegrityError):
            insert_raw(
                f"attempt_raw_{suffix}_dup",
                candidate_work,
                candidate_part,
                provider,
            )


def test_attempt_identity_triggers_reject_raw_insert_and_identity_updates(store):
    work = store.create_work(objective="Identity owner", cwd="/repo/owner")
    other = store.create_work(objective="Other Work", cwd="/repo/other")
    participant = store.bind_participant(work.work_id, "openai")

    with pytest.raises(
        sqlite3.IntegrityError,
        match="execution attempt identity does not match participant",
    ):
        store._conn.execute(
            """
            INSERT INTO execution_attempts (
                attempt_id, work_id, participant_id,
                participant_generation, provider, status, metadata_json,
                started_at, updated_at
            ) VALUES (
                'attempt_raw_mislabeled', ?, ?, ?, 'anthropic',
                'running', '{}', ?, ?
            )
            """,
            (
                work.work_id,
                participant.participant_id,
                participant.generation,
                "2026-08-05T00:00:00Z",
                "2026-08-05T00:00:00Z",
            ),
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="execution attempt identity does not match participant",
    ):
        store._conn.execute(
            """
            INSERT INTO execution_attempts (
                attempt_id, work_id, participant_id,
                participant_generation, provider, status, metadata_json,
                started_at, updated_at
            ) VALUES (
                'attempt_raw_wrong_work', ?, ?, ?, 'openai',
                'running', '{}', ?, ?
            )
            """,
            (
                other.work_id,
                participant.participant_id,
                participant.generation,
                "2026-08-05T00:00:00Z",
                "2026-08-05T00:00:00Z",
            ),
        )

    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="execution attempt identity is immutable",
    ):
        store._conn.execute(
            "UPDATE execution_attempts SET provider = 'anthropic' "
            "WHERE attempt_id = ?",
            (running.attempt_id,),
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="participant execution identity is immutable",
    ):
        store._conn.execute(
            "UPDATE participants SET provider = 'anthropic' "
            "WHERE participant_id = ?",
            (participant.participant_id,),
        )
def test_unresolved_attempts_survive_reopen_without_collapsing_provider_lanes(
    tmp_path,
):
    path = tmp_path / "state" / "work" / "work.db"
    first = WorkStore(path)
    claude_a = first.create_work(objective="Claude A", cwd="/repo/claude-a")
    gpt_a = first.create_work(objective="GPT A", cwd="/repo/gpt-a")
    claude_part_a = first.bind_participant(claude_a.work_id, "anthropic")
    gpt_part_a = first.bind_participant(gpt_a.work_id, "openai")
    claude_running = first.start_execution_attempt(
        claude_a.work_id,
        claude_part_a.participant_id,
        provider="anthropic",
        expected_participant_generation=claude_part_a.generation,
    )
    gpt_running = first.start_execution_attempt(
        gpt_a.work_id,
        gpt_part_a.participant_id,
        provider="openai",
        expected_participant_generation=gpt_part_a.generation,
    )
    first.close()

    reopened = WorkStore(path)
    try:
        running_ids = {
            attempt.attempt_id
            for attempt in reopened.list_active_execution_attempts()
        }
        assert running_ids == {claude_running.attempt_id, gpt_running.attempt_id}

        claude_b = reopened.create_work(
            objective="Claude B", cwd="/repo/claude-b"
        )
        claude_part_b = reopened.bind_participant(claude_b.work_id, "anthropic")
        assert reopened.start_execution_attempt(
            claude_b.work_id,
            claude_part_b.participant_id,
            provider="anthropic",
            expected_participant_generation=claude_part_b.generation,
        ).status == "running"

        # The reopened store counts the surviving GPT attempt against the GPT
        # lane, so the lane is neither collapsed nor silently reset by restart.
        gpt_b = reopened.create_work(objective="GPT B", cwd="/repo/gpt-b")
        gpt_part_b = reopened.bind_participant(gpt_b.work_id, "openai")
        assert reopened.start_execution_attempt(
            gpt_b.work_id,
            gpt_part_b.participant_id,
            provider="openai",
            expected_participant_generation=gpt_part_b.generation,
        ).status == "running"

        for index in range(_PROVIDER_CONCURRENCY_LIMITS["openai"] - 2):
            filler = reopened.create_work(objective=f"GPT {index}", cwd=f"/repo/f{index}")
            filler_part = reopened.bind_participant(filler.work_id, "openai")
            reopened.start_execution_attempt(
                filler.work_id,
                filler_part.participant_id,
                provider="openai",
                expected_participant_generation=filler_part.generation,
            )
        gpt_overflow = reopened.create_work(objective="GPT full", cwd="/repo/gpt-full")
        gpt_overflow_part = reopened.bind_participant(gpt_overflow.work_id, "openai")
        with pytest.raises(ExecutionAdmissionError, match="openai already has"):
            reopened.start_execution_attempt(
                gpt_overflow.work_id,
                gpt_overflow_part.participant_id,
                provider="openai",
                expected_participant_generation=gpt_overflow_part.generation,
            )

        # A full GPT lane leaves the OpenRouter lane untouched.
        router = reopened.create_work(objective="Router", cwd="/repo/router")
        router_part = reopened.bind_participant(router.work_id, "openrouter")
        assert reopened.start_execution_attempt(
            router.work_id,
            router_part.participant_id,
            provider="openrouter",
            expected_participant_generation=router_part.generation,
        ).status == "running"
        # Retiring the attempt that survived the restart frees its GPT slot.
        reopened.finish_execution_attempt(
            gpt_running.attempt_id,
            status="aborted",
            terminal_reason="local_abort",
        )
        assert reopened.start_execution_attempt(
            gpt_overflow.work_id,
            gpt_overflow_part.participant_id,
            provider="openai",
            expected_participant_generation=gpt_overflow_part.generation,
        ).status == "running"
    finally:
        reopened.close()


def test_execution_finish_rolls_back_when_audit_append_fails(store, monkeypatch):
    work = store.create_work(objective="Bounded turn", cwd="/repo")
    participant = store.bind_participant(work.work_id, "anthropic")
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="wire prompt before terminal audit failure",
        provider_request_key=running.attempt_id,
    )
    original_append = store._append_event_conn

    def fail_append(conn, work_id, event_type, payload, **kwargs):
        if event_type == "execution.attempt.finished":
            raise RuntimeError("simulated audit failure")
        return original_append(conn, work_id, event_type, payload, **kwargs)

    monkeypatch.setattr(store, "_append_event_conn", fail_append)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        store.finish_execution_attempt(
            running.attempt_id,
            status="completed",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "completed",
                "request_id": running.attempt_id,
            },
        )

    assert store.active_execution_attempt().attempt_id == running.attempt_id
    assert store.get_execution_attempt(running.attempt_id).status == "running"


def test_admitted_attempt_can_finish_after_participant_rebind(store):
    work = store.create_work(objective="Bounded turn", cwd="/repo")
    participant = store.bind_participant(
        work.work_id,
        "openai",
        native_thread_id="thread-old",
    )
    running = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        running.attempt_id,
        wire_prompt_text="request bound to the old native thread",
        provider_request_key=running.attempt_id,
    )
    rebound = store.update_participant(
        participant.participant_id,
        native_thread_id="thread-new",
        expected_generation=participant.generation,
    )
    assert rebound.generation == participant.generation + 1

    completed = store.finish_execution_attempt(
        running.attempt_id,
        status="completed",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": running.attempt_id,
            "native_id": "thread-old",
        },
    )

    assert completed.status == "completed"
    assert store.active_execution_attempt() is None


def test_ensure_work_is_idempotent_for_explicit_id(store):
    first = store.ensure_work("wrk_stable", objective="First")
    second = store.ensure_work("wrk_stable", objective="Ignored")

    assert second == first
    assert len(store.list_works()) == 1


def test_claude_and_openai_bind_as_siblings_under_one_work(store):
    work = store.create_work()
    claude = store.bind_participant(
        work.work_id,
        "anthropic",
        native_session_id="claude-1",
        role="lead",
    )
    codex = store.bind_participant(
        work.work_id,
        "openai",
        native_thread_id="codex-1",
    )

    assert claude.work_id == codex.work_id == work.work_id
    assert store.work_id_for_native_session("anthropic", "claude-1") == work.work_id
    assert store.work_id_for_native_session("openai", "codex-1") == work.work_id
    assert {p.provider for p in store.list_participants(work.work_id)} == {
        "anthropic",
        "openai",
    }


def test_same_native_binding_preserves_cursor_but_rebind_resets_generation(store):
    work = store.create_work()
    participant = store.bind_participant(
        work.work_id, "openai", native_thread_id="thread-1"
    )
    participant = store.update_participant(
        participant.participant_id,
        last_event_seq=store.list_events(work.work_id)[-1].seq,
    )
    event = store.append_event(
        work.work_id,
        "participant.contribution",
        {"text": "answer"},
        emitting_participant_id=participant.participant_id,
        expected_participant_generation=participant.generation,
    )
    same = store.bind_participant(
        work.work_id, "openai", native_thread_id="thread-1"
    )
    assert same.generation == participant.generation
    assert same.last_event_seq == event.seq

    replacement = store.bind_participant(
        work.work_id, "openai", native_thread_id="thread-2"
    )
    assert replacement.generation == participant.generation + 1
    assert replacement.last_event_seq == 0
    assert store.work_id_for_native_session("openai", "thread-1") == work.work_id
    assert store.work_id_for_native_session("openai", "thread-2") == work.work_id


def test_first_native_id_on_placeholder_does_not_invalidate_delivered_cursor(store):
    work = store.create_work()
    placeholder = store.bind_participant(work.work_id, "anthropic")
    placeholder = store.update_participant(
        placeholder.participant_id,
        last_event_seq=store.list_events(work.work_id)[-1].seq,
    )
    own = store.append_event(
        work.work_id,
        "user.message",
        {"text": "hello"},
        emitting_participant_id=placeholder.participant_id,
        expected_participant_generation=placeholder.generation,
    )

    attached = store.bind_participant(
        work.work_id, "anthropic", native_session_id="session-1"
    )

    assert attached.generation == placeholder.generation
    assert attached.last_event_seq == own.seq


def test_append_atomically_advances_only_emitting_participant(store):
    work = store.create_work()
    claude = store.bind_participant(work.work_id, "anthropic")
    codex = store.bind_participant(work.work_id, "openai")
    latest = store.list_events(work.work_id)[-1].seq
    claude = store.update_participant(claude.participant_id, last_event_seq=latest)
    codex = store.update_participant(codex.participant_id, last_event_seq=latest)

    event = store.append_event(
        work.work_id,
        "participant.contribution",
        {"text": "implemented"},
        emitting_participant_id=claude.participant_id,
        expected_participant_generation=claude.generation,
    )

    assert store.participant_for_provider(
        work.work_id, "anthropic"
    ).last_event_seq == event.seq
    assert (
        store.participant_for_provider(work.work_id, "openai").last_event_seq
        == codex.last_event_seq
    )


def test_cursor_ack_is_monotonic_and_generation_safe(store):
    work = store.create_work()
    participant = store.bind_participant(
        work.work_id, "anthropic", native_session_id="session-1"
    )
    event = store.append_event(work.work_id, "decision.recorded", {"text": "WAL"})
    acknowledged = store.update_participant(
        participant.participant_id,
        last_event_seq=event.seq,
        expected_generation=participant.generation,
        expected_native_session_id="session-1",
    )
    assert acknowledged.last_event_seq == event.seq

    with pytest.raises(WorkStoreError, match="backwards"):
        store.update_participant(participant.participant_id, last_event_seq=0)

    replacement = store.bind_participant(
        work.work_id, "anthropic", native_session_id="session-2"
    )
    with pytest.raises(StaleParticipantError):
        store.update_participant(
            replacement.participant_id,
            last_event_seq=event.seq,
            expected_generation=participant.generation,
        )


def test_concurrent_event_appends_receive_gap_free_sequence(store):
    work = store.create_work()

    def append(index: int) -> int:
        return store.append_event(
            work.work_id, "test.event", {"index": index}
        ).seq

    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = list(pool.map(append, range(40)))

    assert sorted(seqs) == list(range(2, 42))
    assert [e.seq for e in store.list_events(work.work_id)] == list(range(1, 42))


def test_events_reject_update_and_delete_even_through_raw_sql(store):
    work = store.create_work()
    event = store.list_events(work.work_id)[0]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._conn.execute(
            "UPDATE events SET event_type = 'changed' WHERE event_id = ?",
            (event.event_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store._conn.execute("DELETE FROM events WHERE event_id = ?", (event.event_id,))


def test_artifacts_are_content_addressed_immutable_and_private(store):
    work = store.create_work()
    first = store.publish_artifact(
        work.work_id,
        b"test output",
        media_type="text/plain",
        metadata={"kind": "test-log"},
    )
    same = store.publish_artifact(
        work.work_id,
        b"test output",
        media_type="text/plain",
        metadata={"kind": "test-log"},
    )

    assert same == first
    assert first.artifact_id.startswith("sha256:")
    assert first.path.read_bytes() == b"test output"
    assert first.path.stat().st_mode & 0o777 == 0o600
    assert store.list_artifacts(work.work_id) == [first]
    with pytest.raises(WorkStoreError, match="different metadata"):
        store.publish_artifact(
            work.work_id,
            b"test output",
            media_type="application/json",
        )


def test_default_path_honors_dynamic_state_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "one"))
    first = WorkStore()
    try:
        assert first.path == tmp_path / "one" / "work" / "work.db"
    finally:
        first.close()

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "two"))
    second = WorkStore()
    try:
        assert second.path == tmp_path / "two" / "work" / "work.db"
    finally:
        second.close()


def test_native_claim_is_atomic_across_store_connections(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    first = WorkStore(path)
    second = WorkStore(path)
    barrier = threading.Barrier(2)

    def claim(store):
        barrier.wait()
        return store.ensure_work_for_native("openai", "same-thread").work_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            work_ids = list(pool.map(claim, (first, second)))
        assert work_ids[0] == work_ids[1]
        assert len(first.list_works()) == 1
    finally:
        first.close()
        second.close()


def test_explicit_ensure_work_is_safe_across_store_connections(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    first = WorkStore(path)
    second = WorkStore(path)
    barrier = threading.Barrier(2)

    def ensure(store):
        barrier.wait()
        return store.ensure_work("wrk_shared").work_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            work_ids = list(pool.map(ensure, (first, second)))
        assert work_ids == ["wrk_shared", "wrk_shared"]
        assert len(first.list_works()) == 1
    finally:
        first.close()
        second.close()


def test_schema_one_migrates_binding_history_transactionally(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE works (
            work_id TEXT PRIMARY KEY, objective TEXT NOT NULL,
            definition_of_done TEXT NOT NULL, cwd TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL,
            lead_provider TEXT NOT NULL, metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE participants (
            participant_id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
            provider TEXT NOT NULL, native_session_id TEXT NOT NULL DEFAULT '',
            native_thread_id TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
            status TEXT NOT NULL, generation INTEGER NOT NULL,
            last_event_seq INTEGER NOT NULL, metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (work_id, provider)
        );
        INSERT INTO works VALUES (
            'wrk_old', '', '', '/repo', 'tandem', 'active', 'anthropic',
            '{}', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        );
        INSERT INTO participants VALUES (
            'part_old', 'wrk_old', 'anthropic', 'session-old', '', 'lead',
            'active', 1, 0, '{}', '2026-01-01T00:00:00Z',
            '2026-01-01T00:00:00Z'
        );
        PRAGMA user_version = 1;
        """
    )
    conn.close()

    store = WorkStore(path)
    try:
        version = store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
        migrated = store.get_work("wrk_old")
        assert migrated.contract_epoch == 0
        assert migrated.contract_start_seq == 0
        assert (
            store.work_id_for_native_session("anthropic", "session-old")
            == "wrk_old"
        )
    finally:
        store.close()


def test_schema_two_migrates_accepted_contract_to_existing_ledger_tail(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE works (
            work_id TEXT PRIMARY KEY, objective TEXT NOT NULL,
            definition_of_done TEXT NOT NULL, cwd TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL,
            lead_provider TEXT NOT NULL, metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
            seq INTEGER NOT NULL, event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL, provider TEXT NOT NULL,
            emitting_participant_id TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (work_id, seq)
        );
        INSERT INTO works VALUES (
            'wrk_accepted', 'Bounded objective', 'Focused tests pass', '/repo',
            'tandem', 'active', 'anthropic', '{}',
            '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
        );
        INSERT INTO events VALUES (
            'evt_1', 'wrk_accepted', 1, 'work.created', '{}', '', '',
            '2026-01-01T00:00:00Z'
        );
        INSERT INTO events VALUES (
            'evt_2', 'wrk_accepted', 2, 'participant.contribution',
            '{"text":"legacy speculative context"}', 'anthropic', '',
            '2026-01-01T00:00:01Z'
        );
        PRAGMA user_version = 2;
        """
    )
    conn.close()

    store = WorkStore(path)
    try:
        migrated = store.get_work("wrk_accepted")
        assert migrated.contract_epoch == 1
        assert migrated.contract_start_seq == 2
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        store.close()


def test_schema_three_migrates_execution_attempt_table(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    old = WorkStore(path)
    old._conn.executescript(
        """
        DROP INDEX execution_attempts_work_started;
        DROP INDEX execution_attempts_one_running_per_work;
        DROP INDEX IF EXISTS execution_attempts_one_running_non_claude;
        DROP TABLE execution_attempts;
        PRAGMA user_version = 3;
        """
    )
    old.close()

    migrated = WorkStore(path)
    try:
        assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        table = migrated._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'execution_attempts'"
        ).fetchone()
        assert table is not None
    finally:
        migrated.close()


def test_schema_four_development_database_gains_recovery_columns(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    work = current.create_work(objective="Existing v4 turn", cwd="/repo")
    participant = current.bind_participant(work.work_id, "openai")
    running = current.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        attempt_id="attempt_existing_v4",
    )
    _downgrade_execution_attempts_to_v4(current)
    current.close()

    migrated = WorkStore(path)
    try:
        assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {
            row["name"]
            for row in migrated._conn.execute(
                "PRAGMA table_info(execution_attempts)"
            ).fetchall()
        }
        assert {
            "prompt_digest",
            "native_binding_id",
            "provider_request_key",
            "accepted_turn_id",
            "terminal_receipt_json",
            "usage_json",
            "cost_micro_usd",
            "stop_acknowledgement_json",
            "queue_disposition",
        } <= columns
        preserved = migrated.active_execution_attempt()
        assert preserved.attempt_id == running.attempt_id
        assert preserved.prompt_digest == ""
        assert preserved.terminal_receipt == {}
        assert preserved.cost_micro_usd is None
    finally:
        migrated.close()


def test_schema_five_migrates_global_mutex_to_scoped_execution_lanes(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    claude_a = current.create_work(objective="Claude A", cwd="/repo/a")
    claude_part_a = current.bind_participant(claude_a.work_id, "anthropic")
    original = current.start_execution_attempt(
        claude_a.work_id,
        claude_part_a.participant_id,
        provider="anthropic",
        expected_participant_generation=claude_part_a.generation,
        attempt_id="attempt_schema_five",
    )
    _downgrade_execution_admission_to_v5(current)
    current.close()

    migrated = WorkStore(path)
    try:
        assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        index_rows = migrated._conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name LIKE 'execution_attempts_%'"
        ).fetchall()
        indexes = {row["name"]: row["sql"] for row in index_rows}
        assert "execution_attempts_single_running" not in indexes
        assert "execution_attempts_one_running_per_work" in indexes
        # v7 drops the shared non-Claude index rather than recreating it, so a
        # v5 or v6 database self-heals to the per-provider ceilings on open.
        assert "execution_attempts_one_running_non_claude" not in indexes
        assert migrated.active_execution_attempt().attempt_id == original.attempt_id

        claude_b = migrated.create_work(objective="Claude B", cwd="/repo/b")
        claude_part_b = migrated.bind_participant(claude_b.work_id, "anthropic")
        assert migrated.start_execution_attempt(
            claude_b.work_id,
            claude_part_b.participant_id,
            provider="anthropic",
            expected_participant_generation=claude_part_b.generation,
        ).status == "running"

        # Two GPT Works run concurrently after the migration; the ceiling
        # still bites once the lane is full.
        for index in range(_PROVIDER_CONCURRENCY_LIMITS["openai"]):
            gpt = migrated.create_work(objective=f"GPT {index}", cwd=f"/repo/gpt-{index}")
            gpt_part = migrated.bind_participant(gpt.work_id, "openai")
            assert migrated.start_execution_attempt(
                gpt.work_id,
                gpt_part.participant_id,
                provider="openai",
                expected_participant_generation=gpt_part.generation,
            ).status == "running"
        gpt_overflow = migrated.create_work(objective="GPT full", cwd="/repo/gpt-full")
        gpt_overflow_part = migrated.bind_participant(gpt_overflow.work_id, "openai")
        with pytest.raises(ExecutionAdmissionError, match="openai already has"):
            migrated.start_execution_attempt(
                gpt_overflow.work_id,
                gpt_overflow_part.participant_id,
                provider="openai",
                expected_participant_generation=gpt_overflow_part.generation,
            )
    finally:
        migrated.close()


def test_schema_v6_rebuilds_missing_and_weakened_identity_triggers(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    current._conn.executescript(
        """
        DROP TRIGGER execution_attempts_identity_insert;
        DROP TRIGGER execution_attempts_identity_update;
        DROP TRIGGER participants_execution_identity_update;
        CREATE TRIGGER execution_attempts_identity_insert
            BEFORE INSERT ON execution_attempts
            BEGIN SELECT 1; END;
        """
    )
    current.close()

    rebuilt = WorkStore(path)
    try:
        rows = rebuilt._conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name IN (?, ?, ?) ORDER BY name",
            (
                "execution_attempts_identity_insert",
                "execution_attempts_identity_update",
                "participants_execution_identity_update",
            ),
        ).fetchall()
        triggers = {row["name"]: " ".join(row["sql"].split()) for row in rows}
        assert set(triggers) == {
            "execution_attempts_identity_insert",
            "execution_attempts_identity_update",
            "participants_execution_identity_update",
        }
        assert "NOT EXISTS" in triggers["execution_attempts_identity_insert"]
        assert "identity is immutable" in triggers[
            "execution_attempts_identity_update"
        ]
        work = rebuilt.create_work(objective="Self-healed", cwd="/repo")
        participant = rebuilt.bind_participant(work.work_id, "openai")
        with pytest.raises(
            sqlite3.IntegrityError,
            match="execution attempt identity does not match participant",
        ):
            rebuilt._conn.execute(
                """
                INSERT INTO execution_attempts (
                    attempt_id, work_id, participant_id,
                    participant_generation, provider, status, metadata_json,
                    started_at, updated_at
                ) VALUES (
                    'attempt_after_trigger_rebuild', ?, ?, 1, 'anthropic',
                    'running', '{}', ?, ?
                )
                """,
                (
                    work.work_id,
                    participant.participant_id,
                    "2026-08-05T00:00:00Z",
                    "2026-08-05T00:00:00Z",
                ),
            )
    finally:
        rebuilt.close()


def test_malformed_v5_attempt_identity_rolls_back_trigger_rebuild(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    work = current.create_work(objective="Malformed identity", cwd="/repo")
    participant = current.bind_participant(work.work_id, "openai")
    current._conn.executescript(
        """
        DROP TRIGGER execution_attempts_identity_insert;
        DROP TRIGGER execution_attempts_identity_update;
        DROP TRIGGER participants_execution_identity_update;
        CREATE TRIGGER execution_attempts_identity_insert
            BEFORE INSERT ON execution_attempts
            BEGIN SELECT 1; END;
        """
    )
    current._conn.execute(
        """
        INSERT INTO execution_attempts (
            attempt_id, work_id, participant_id, participant_generation,
            provider, status, metadata_json, started_at, updated_at
        ) VALUES (
            'attempt_malformed_identity', ?, ?, 1, 'anthropic',
            'running', '{}', ?, ?
        )
        """,
        (
            work.work_id,
            participant.participant_id,
            "2026-08-05T00:00:00Z",
            "2026-08-05T00:00:00Z",
        ),
    )
    current._conn.execute("PRAGMA user_version = 5")
    weakened_trigger = current._conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'execution_attempts_identity_insert'"
    ).fetchone()[0]
    current.close()

    with pytest.raises(WorkStoreError, match="Work/provider mismatch"):
        WorkStore(path)

    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 5
        assert verification.execute(
            "SELECT provider FROM execution_attempts "
            "WHERE attempt_id = 'attempt_malformed_identity'"
        ).fetchone()[0] == "anthropic"
        assert verification.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' "
            "AND name = 'execution_attempts_identity_insert'"
        ).fetchone()[0] == weakened_trigger
    finally:
        verification.close()


def test_schema_four_exact_audit_name_collision_remains_untrusted_after_migration(
    tmp_path,
):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    work = current.create_work(
        objective="Preserve legacy collaborator event",
        definition_of_done="Exact collision is visible",
        cwd="/repo",
        mode="tandem",
    )
    participant = current.bind_participant(work.work_id, "anthropic")
    baseline = current.latest_event_seq(work.work_id)
    participant = current.update_participant(
        participant.participant_id,
        last_event_seq=baseline,
        expected_generation=participant.generation,
    )
    current._conn.execute(
        """
        INSERT INTO events (
            event_id, work_id, seq, event_type, payload_json, provider,
            emitting_participant_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "evt_legacy_exact_collision",
            work.work_id,
            baseline + 1,
            "execution.attempt.accepted",
            '{"text":"legacy exact collision"}',
            "openai",
            "",
            "2026-08-04T00:00:00Z",
        ),
    )
    _downgrade_execution_attempts_to_v4(current)
    current.close()

    migrated = WorkStore(path)
    try:
        collision = next(
            event
            for event in migrated.list_events(work.work_id)
            if event.event_id == "evt_legacy_exact_collision"
        )
        assert collision.control_plane is False
        snapshot = migrated.snapshot_collaboration_prompt(
            work.work_id,
            "anthropic",
        )
        assert collision in snapshot.events
        acknowledged = migrated.acknowledge_collaboration_prompt(
            participant.participant_id,
            through_seq=baseline,
            expected_generation=participant.generation,
        )
        assert acknowledged.last_event_seq == baseline
    finally:
        migrated.close()


def test_legacy_nonfinite_json_remains_readable_after_recovery_schema_migration(
    tmp_path,
):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    work = current.create_work(objective="Read legacy JSON", cwd="/repo")
    participant = current.bind_participant(work.work_id, "anthropic")
    current._conn.execute(
        "UPDATE works SET metadata_json = '{\"legacy_nan\":NaN}' " "WHERE work_id = ?",
        (work.work_id,),
    )
    current._conn.execute(
        "UPDATE participants SET metadata_json = '{\"legacy_nan\":NaN}' "
        "WHERE participant_id = ?",
        (participant.participant_id,),
    )
    seq = current.latest_event_seq(work.work_id) + 1
    current._conn.execute(
        """
        INSERT INTO events (
            event_id, work_id, seq, event_type, payload_json, provider,
            emitting_participant_id, created_at
        ) VALUES (?, ?, ?, 'legacy.nonfinite', '{"legacy_nan":NaN}', '', '', ?)
        """,
        ("evt_legacy_nan", work.work_id, seq, "2026-08-04T00:00:00Z"),
    )
    current._conn.execute(
        """
        INSERT INTO artifacts (
            artifact_id, work_id, digest, media_type, size, path,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, '{"legacy_nan":NaN}', ?)
        """,
        (
            "legacy_nan_artifact",
            work.work_id,
            "sha256:legacy",
            "application/octet-stream",
            0,
            "/tmp/legacy-nan-artifact",
            "2026-08-04T00:00:00Z",
        ),
    )
    _downgrade_execution_attempts_to_v4(current)
    current.close()

    migrated = WorkStore(path)
    try:
        assert math.isnan(migrated.get_work(work.work_id).metadata["legacy_nan"])
        assert math.isnan(
            migrated.list_participants(work.work_id)[0].metadata["legacy_nan"]
        )
        assert math.isnan(
            next(
                event
                for event in migrated.list_events(work.work_id)
                if event.event_id == "evt_legacy_nan"
            ).payload["legacy_nan"]
        )
        assert math.isnan(
            migrated.list_artifacts(work.work_id)[0].metadata["legacy_nan"]
        )
        with pytest.raises(ValueError, match="JSON-serializable"):
            migrated.update_work(
                work.work_id,
                metadata={"new_nan": float("nan")},
            )
    finally:
        migrated.close()


@pytest.mark.parametrize(
    ("metadata_json", "terminal_reason"),
    (
        ('{"prompt_text":"legacy raw prompt"}', ""),
        ('{"model":"gpt-test","authorization":"Bearer legacy-secret"}', ""),
        ('{"model":"gpt-a","model":"gpt-b"}', ""),
        ('{"model":"gpt-a","MODEL":"gpt-b"}', ""),
        ('{"model":NaN}', ""),
        ('{"model":"gpt-test"}', "raw provider result"),
    ),
)
def test_schema_four_migration_refuses_unsafe_recovery_data(
    tmp_path,
    metadata_json,
    terminal_reason,
):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    work = current.create_work(objective="Unsafe v4 turn", cwd="/repo")
    participant = current.bind_participant(work.work_id, "openai")
    running = current.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        metadata={"model": "gpt-test"},
        attempt_id="attempt_unsafe_v4",
    )
    _downgrade_execution_attempts_to_v4(current)
    current._conn.execute(
        "UPDATE execution_attempts SET metadata_json = ?, terminal_reason = ? "
        "WHERE attempt_id = ?",
        (metadata_json, terminal_reason, running.attempt_id),
    )
    before_schema = current._conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name LIKE 'execution_attempts%' ORDER BY type, name"
    ).fetchall()
    before_rows = current._conn.execute(
        "SELECT * FROM execution_attempts ORDER BY attempt_id"
    ).fetchall()
    current.close()

    with pytest.raises(WorkStoreError, match="migration refused"):
        WorkStore(path)

    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 4
        columns = {
            row[1]
            for row in verification.execute(
                "PRAGMA table_info(execution_attempts)"
            ).fetchall()
        }
        assert "prompt_digest" not in columns
        after_schema = verification.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name LIKE 'execution_attempts%' ORDER BY type, name"
        ).fetchall()
        after_rows = verification.execute(
            "SELECT * FROM execution_attempts ORDER BY attempt_id"
        ).fetchall()
        assert [tuple(row) for row in after_schema] == [
            tuple(row) for row in before_schema
        ]
        assert [tuple(row) for row in after_rows] == [tuple(row) for row in before_rows]
    finally:
        verification.close()


def test_concurrent_schema_one_migration_is_serialized_and_retried(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE works (
            work_id TEXT PRIMARY KEY, objective TEXT NOT NULL,
            definition_of_done TEXT NOT NULL, cwd TEXT NOT NULL,
            mode TEXT NOT NULL, status TEXT NOT NULL,
            lead_provider TEXT NOT NULL, metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE participants (
            participant_id TEXT PRIMARY KEY, work_id TEXT NOT NULL,
            provider TEXT NOT NULL, native_session_id TEXT NOT NULL DEFAULT '',
            native_thread_id TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
            status TEXT NOT NULL, generation INTEGER NOT NULL,
            last_event_seq INTEGER NOT NULL, metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (work_id, provider)
        );
        PRAGMA user_version = 1;
        """
    )
    conn.close()
    workers = 12
    barrier = threading.Barrier(workers)

    def migrate(_index):
        barrier.wait()
        opened = WorkStore(path)
        try:
            version = opened._conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {
                row["name"]
                for row in opened._conn.execute(
                    "PRAGMA table_info(execution_attempts)"
                ).fetchall()
            }
            return version, "prompt_digest" in columns
        finally:
            opened.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(migrate, range(workers)))

    assert results == [(SCHEMA_VERSION, True)] * workers


def test_concurrent_schema_five_openers_install_exact_v6_admission_indexes(
    tmp_path,
):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    work = current.create_work(objective="Preserved Claude", cwd="/repo")
    participant = current.bind_participant(work.work_id, "anthropic")
    current.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
    )
    _downgrade_execution_admission_to_v5(current)
    current.close()

    workers = 8
    barrier = threading.Barrier(workers)

    def migrate(_index):
        barrier.wait()
        opened = WorkStore(path)
        try:
            rows = opened._conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND name IN (?, ?, ?) ORDER BY name",
                (
                    "execution_attempts_one_running_non_claude",
                    "execution_attempts_one_running_per_work",
                    # Schema v9 removed the workspace write lease. It is listed
                    # here on purpose: the assertion below is that it is ABSENT
                    # after migration, which is what proves the unconditional
                    # DROP self-heals a v7/v8 database.
                    "execution_attempts_one_writer_per_workspace",
                ),
            ).fetchall()
            return (
                opened._conn.execute("PRAGMA user_version").fetchone()[0],
                {row["name"]: " ".join(row["sql"].split()) for row in rows},
            )
        finally:
            opened.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(migrate, range(workers)))

    for version, indexes in results:
        assert version == SCHEMA_VERSION
        # Racing openers converge on the same v9 index set: the per-Work
        # constraint present, and BOTH retired lanes — the shared non-Claude
        # lane (v8) and the workspace write lease (v9) — absent. A
        # partially-applied migration would show up here as a mixed result,
        # and a v7/v8 database that kept its lease would fail this exact
        # assertion.
        assert set(indexes) == {
            "execution_attempts_one_running_per_work",
        }
        assert "UNIQUE INDEX" in indexes[
            "execution_attempts_one_running_per_work"
        ]
        assert "ON execution_attempts (work_id)" in indexes[
            "execution_attempts_one_running_per_work"
        ]


def test_schema_v6_index_rebuild_rolls_back_on_duplicate_running_work(tmp_path):
    path = tmp_path / "state" / "work" / "work.db"
    current = WorkStore(path)
    work = current.create_work(objective="Corrupt duplicate", cwd="/repo")
    participant = current.bind_participant(work.work_id, "anthropic")
    current.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
        attempt_id="attempt_corrupt_a",
    )
    current._conn.executescript(
        """
        DROP INDEX execution_attempts_one_running_per_work;
        DROP INDEX IF EXISTS execution_attempts_one_running_non_claude;
        CREATE INDEX execution_attempts_one_running_per_work
            ON execution_attempts(started_at);
        CREATE INDEX execution_attempts_one_running_non_claude
            ON execution_attempts(started_at);
        INSERT INTO execution_attempts (
            attempt_id, work_id, participant_id, participant_generation,
            provider, status, metadata_json, started_at, updated_at
        ) VALUES (
            'attempt_corrupt_b',
            (SELECT work_id FROM execution_attempts
             WHERE attempt_id = 'attempt_corrupt_a'),
            (SELECT participant_id FROM execution_attempts
             WHERE attempt_id = 'attempt_corrupt_a'),
            1, 'anthropic', 'running', '{}',
            '2026-08-05T00:00:01Z', '2026-08-05T00:00:01Z'
        );
        PRAGMA user_version = 5;
        """
    )
    before = {
        row["name"]: row["sql"]
        for row in current._conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name IN (?, ?)",
            (
                "execution_attempts_one_running_non_claude",
                "execution_attempts_one_running_per_work",
            ),
        ).fetchall()
    }
    current.close()

    with pytest.raises(sqlite3.IntegrityError):
        WorkStore(path)

    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 5
        after = dict(
            verification.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND name IN (?, ?)",
                (
                    "execution_attempts_one_running_non_claude",
                    "execution_attempts_one_running_per_work",
                ),
            ).fetchall()
        )
        assert after == before
        assert verification.execute(
            "SELECT COUNT(*) FROM execution_attempts WHERE status = 'running'"
        ).fetchone()[0] == 2
    finally:
        verification.close()


# --- operator reset for the budget breaker ---------------


def test_budget_block_can_be_cleared_by_an_explicit_operator_action(store):
    """The breaker stays one-way for a process respawn, but a human decision
    must be able to reopen the Work — otherwise one trip abandons the
    conversation permanently, which is what happened on 2026-08-05."""
    work = store.create_work(objective="Bounded work", cwd="/repo")
    store.mark_budget_exhausted(work.work_id, provider="anthropic")

    cleared = store.clear_budget_exhausted(work.work_id, reason="operator")

    assert cleared.status == "active"
    assert [e.event_type for e in store.list_events(work.work_id)[-2:]] == [
        "work.updated",
        "budget.cleared",
    ]


def test_clearing_records_the_reason_for_audit(store):
    work = store.create_work(objective="Bounded work", cwd="/repo")
    store.mark_budget_exhausted(work.work_id, provider="anthropic")

    store.clear_budget_exhausted(work.work_id, reason="cleared from the menu")

    cleared = [
        e for e in store.list_events(work.work_id) if e.event_type == "budget.cleared"
    ]
    assert len(cleared) == 1
    assert cleared[0].payload["reason"] == "cleared from the menu"


def test_clearing_a_work_that_is_not_budget_limited_is_a_no_op(store):
    """Must not become a general-purpose 'reopen anything' lever."""
    work = store.create_work(objective="Bounded work", cwd="/repo")
    before = len(store.list_events(work.work_id))

    result = store.clear_budget_exhausted(work.work_id)

    assert result.status == work.status
    assert len(store.list_events(work.work_id)) == before


def test_a_cleared_work_can_be_blocked_again(store):
    """Clearing is a renewal, not an exemption."""
    work = store.create_work(objective="Bounded work", cwd="/repo")
    store.mark_budget_exhausted(work.work_id, provider="anthropic")
    store.clear_budget_exhausted(work.work_id)

    again = store.mark_budget_exhausted(work.work_id, provider="anthropic")

    assert again.status == "budgetLimited"


# ---: single-writer workspace lease -----------------------------------


def _writer(store, *, cwd, mode="default", objective="w"):
    """Admit one running attempt in `cwd` with the given permission mode."""
    work = store.create_work(objective=objective, cwd=cwd)
    participant = store.bind_participant(work.work_id, "anthropic")
    return work, store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
        workspace_root=cwd,
        write_intent=mode != "plan",
    )


def test_two_writers_in_one_directory_are_admitted(tmp_path):
    """Schema v9 removes the write lease.

    It was added 2026-08-05 against a hazard its own commit described as one
    that *could* happen, and it blocked the ordinary case of two chats in one
    repo. The guard is covered after the fact by pre-turn checkpoints
    (`backend/checkpoints.py`) — recoverable beats forbidden."""
    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    repo = str(tmp_path / "repo")
    _writer(store, cwd=repo, objective="first")

    _work, second = _writer(store, cwd=repo, objective="second")

    assert second.status == "running"

def test_writers_in_separate_worktrees_run_concurrently(tmp_path):
    """Independent git worktrees have distinct roots and must NOT be serialized
    — that is the whole reason this keys on cwd and not on a shared .git."""
    store = WorkStore(tmp_path / "state" / "work" / "work.db")

    _writer(store, cwd=str(tmp_path / "wt-a"), objective="a")
    _work, attempt = _writer(store, cwd=str(tmp_path / "wt-b"), objective="b")

    assert attempt.status == "running"


def test_a_reader_neither_takes_nor_is_denied_the_lease(tmp_path):
    """Plan is read-only, so it may run alongside a writer, and two readers may
    share a directory."""
    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    repo = str(tmp_path / "repo")

    _writer(store, cwd=repo, mode="default", objective="writer")
    _w2, reader = _writer(store, cwd=repo, mode="plan", objective="reader")
    assert reader.status == "running"

    _w3, reader2 = _writer(store, cwd=repo, mode="plan", objective="reader-2")
    assert reader2.status == "running"


def test_a_reader_does_not_block_a_later_writer(tmp_path):
    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    repo = str(tmp_path / "repo")

    _writer(store, cwd=repo, mode="plan", objective="reader")
    _w, writer = _writer(store, cwd=repo, mode="default", objective="writer")

    assert writer.status == "running"


def test_the_lease_releases_when_the_attempt_finishes(tmp_path):
    """A lease that never releases is a deadlock, not a guard.

    A never-dispatched attempt may only be released as a local abort, which is
    exactly the crash/stop path that has to free the directory.
    """
    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    repo = str(tmp_path / "repo")
    _first, attempt = _writer(store, cwd=repo, objective="first")

    store.finish_execution_attempt(attempt.attempt_id, status="aborted")

    _second, again = _writer(store, cwd=repo, objective="second")
    assert again.status == "running"


def test_path_aliases_are_still_canonicalized_in_the_recorded_root(tmp_path):
    """The lease is gone but `workspace_root` is still recorded, and it is still
    forensics: two spellings of one directory must not look like two places."""
    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)

    _w1, first = _writer(store, cwd=str(repo), objective="first")
    _w2, alias = _writer(store, cwd=f"{repo}/sub/..", objective="alias")

    assert alias.workspace_root == first.workspace_root

def test_an_unknown_root_does_not_collide_with_everything(tmp_path):
    """'' means "root unidentified". If it took a lease, one such attempt would
    block every other writer app-wide — a global mutex by accident."""
    store = WorkStore(tmp_path / "state" / "work" / "work.db")

    _writer(store, cwd="", objective="unknown-a")
    _w, second = _writer(store, cwd="", objective="unknown-b")

    assert second.status == "running"


def _admit_openai_writer(store, tmp_path, name: str):
    """Start one running openai attempt in its own workspace."""
    work = store.create_work(objective=name, cwd=str(tmp_path / name))
    participant = store.bind_participant(work.work_id, "openai")
    return work, participant, lambda: store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="openai",
        expected_participant_generation=participant.generation,
        workspace_root=str(tmp_path / name),
        write_intent=True,
    )


def test_two_non_claude_writers_in_different_workspaces_both_admit(tmp_path):
    """The point of schema v8: a second GPT Work is no longer denied.

    Superseded the v7 assertion that this raised. Under one shared non-Claude
    lane a second GPT Work — or a GPT Work beside an OpenRouter one — could
    never start, which is the constraint an earlier change removed for Claude and left in
    place for everyone else.
    """
    store = WorkStore(tmp_path / "state" / "work" / "work.db")
    _w1, _p1, start_first = _admit_openai_writer(store, tmp_path, "a")
    _w2, _p2, start_second = _admit_openai_writer(store, tmp_path, "b")

    start_first()
    start_second()  # must not raise

    running = [
        a for a in store.list_active_execution_attempts() if a.provider == "openai"
    ]
    assert len(running) == 2

    # The lane ceiling itself is covered by the migration test above; what is
    # unique here is that a writer holding a workspace_root still counts toward
    # it. Fill the remaining slots, then confirm the overflow is denied by the
    # PROVIDER lane and not by the lease.
    for i in range(_PROVIDER_CONCURRENCY_LIMITS["openai"] - 2):
        _w, _p, start = _admit_openai_writer(store, tmp_path, f"fill{i}")
        start()
    _w, _p, start_overflow = _admit_openai_writer(store, tmp_path, "overflow")
    with pytest.raises(ExecutionAdmissionError, match="openai already has"):
        start_overflow()




# --- crash recovery: attempts orphaned by a Helios that exited mid-turn -------


def test_an_orphaned_attempt_is_reclaimed_so_its_work_can_run_again(store):
    """Killing Helios mid-turn used to brick that chat permanently.

    The running row survives the process, per-Work admission then refuses every
    later message, and a dispatched attempt cannot be released the normal way
    because the only thing that could produce provider evidence is gone.
    """

    work = store.create_work(objective="stuck", cwd="/tmp/repo")
    participant = store.bind_participant(work.work_id, "anthropic")

    def admit():
        return store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="anthropic",
            expected_participant_generation=participant.generation,
            workspace_root="/tmp/repo",
        )

    orphan = admit()
    store.record_execution_dispatch(
        orphan.attempt_id,
        wire_prompt_text="hello",
        provider_request_key=orphan.attempt_id,
    )
    with pytest.raises(ExecutionAdmissionError, match="already executing"):
        admit()
    with pytest.raises(WorkStoreError):
        store.finish_execution_attempt(orphan.attempt_id, status="aborted")

    reclaimed = store.reclaim_orphaned_execution_attempts()

    assert [a.attempt_id for a in reclaimed] == [orphan.attempt_id]
    released = store.get_execution_attempt(orphan.attempt_id)
    assert released.status == "aborted"
    assert released.terminal_reason == "orphaned.helios_exited"
    # Honest about what it does NOT know: no receipt, no usage, no cost.
    assert released.terminal_receipt == {}
    assert released.usage == {}
    assert released.cost_micro_usd is None
    assert admit().status == "running"


def test_reclaim_can_name_one_attempt_and_leaves_the_others_running(store):
    """The startup sweep is app-wide, but recovering ONE stuck chat while a
    healthy Helios keeps running must not release the lanes it is using."""

    work_a = store.create_work(objective="stuck", cwd="/tmp/a")
    part_a = store.bind_participant(work_a.work_id, "anthropic")
    stuck = store.start_execution_attempt(
        work_a.work_id,
        part_a.participant_id,
        provider="anthropic",
        expected_participant_generation=part_a.generation,
    )
    work_b = store.create_work(objective="live", cwd="/tmp/b")
    part_b = store.bind_participant(work_b.work_id, "anthropic")
    live = store.start_execution_attempt(
        work_b.work_id,
        part_b.participant_id,
        provider="anthropic",
        expected_participant_generation=part_b.generation,
    )

    reclaimed = store.reclaim_orphaned_execution_attempts([stuck.attempt_id])

    assert [a.attempt_id for a in reclaimed] == [stuck.attempt_id]
    assert store.get_execution_attempt(live.attempt_id).status == "running"
    assert store.reclaim_orphaned_execution_attempts([]) == []
