from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from helios.backend import session_goals
from helios.backend.process.message_queue import PreparedPrompt
from helios.backend.transcript import Turn
from helios.backend.work_coordinator import WorkCoordinator, tag_driver
from helios.backend.work_context import strip_work_envelope
from helios.backend.work_store import WorkEvent, WorkStore


def _coordinator(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_goals, "_PATH", tmp_path / "state" / "session-goals.json"
    )
    session_goals.reload()
    return WorkCoordinator(WorkStore(tmp_path / "state" / "work" / "work.db"))


def _tandem_work(coordinator, *, lead_provider="anthropic"):
    return coordinator.ensure_work(
        cwd="/repo",
        lead_provider=lead_provider,
        objective="Ship the bounded change",
        definition_of_done="The requested change is verified by focused tests",
        mode="tandem",
    )


def test_unknown_work_execution_state_fails_closed(tmp_path, monkeypatch):
    coordinator = _coordinator(tmp_path, monkeypatch)

    assert "could not verify" in coordinator.execution_block_reason("missing-work")
    coordinator.store.close()


def test_context_degradation_is_durable_control_plane_evidence(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.ensure_work(cwd="/repo", mode="single")

    event = coordinator.record_context_degradation(
        work_id=work.work_id,
        provider="openai",
    )

    assert event.event_type == "context.degraded"
    assert event.control_plane is True
    assert event.payload == {
        "reason_code": "context.prepare_failed",
        "fallback": "goal-only",
    }
    coordinator.store.close()


def test_context_audit_is_acknowledged_but_never_rendered_to_a_partner(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    claude = coordinator.bind_participant(work.work_id, "anthropic")
    codex = coordinator.bind_participant(work.work_id, "openai")
    coordinator.store.append_control_event(
        work.work_id,
        "context.degraded",
        {"reason_code": "context.prepare_failed", "fallback": "goal-only"},
        provider="anthropic",
    )
    driver = SimpleNamespace()
    tag_driver(driver, claude)
    coordinator.record_user_message(driver, "Visible collaborator update")

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="Continue",
        goal=None,
    )

    assert isinstance(prepared, PreparedPrompt)
    assert "Visible collaborator update" in prepared.text
    assert "context.degraded" not in prepared.text
    assert "context.prepare_failed" not in prepared.text
    prepared.mark_sent()
    assert coordinator.participant(work.work_id, "openai").last_event_seq > (
        codex.last_event_seq
    )
    coordinator.store.close()


def test_resolve_native_work_lazily_reuses_binding_and_migrates_goal(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    session_goals.set_goal("claude-1", session_goals.GoalState("Legacy goal"))

    first = coordinator.resolve_native_work(
        "anthropic", "claude-1", cwd="/repo", model="opus"
    )
    second = coordinator.resolve_native_work(
        "anthropic", "claude-1", cwd="/repo", model="opus"
    )

    assert second.work_id == first.work_id
    assert session_goals.get_goal("claude-1") is None
    assert session_goals.get_goal(first.work_id).objective == "Legacy goal"
    coordinator.store.close()


def test_native_fork_gets_independent_work_and_cloned_user_goal(
    tmp_path,
    monkeypatch,
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    source = coordinator.ensure_work(
        cwd="/repo",
        lead_provider="openai",
        objective="Ship the branch",
        definition_of_done="Tests pass",
        mode="tandem",
    )
    source_participant = coordinator.bind_participant(
        source.work_id,
        "openai",
        native_id="source-thread",
        model="gpt-5.6",
    )
    session_goals.set_goal(
        source.work_id,
        session_goals.GoalState(
            objective="Ship the branch",
            definition_of_done="Tests pass",
            status="blocked",
            items=[
                session_goals.GoalPlanItem("Inspect", "completed"),
                session_goals.GoalPlanItem("Build", "in_progress"),
            ],
            cwd="/repo",
            provider="openai",
        ),
    )

    fork, participant = coordinator.fork_work(
        source_work_id=source.work_id,
        provider="openai",
        native_id="fork-thread",
        model="gpt-5.6",
    )

    assert fork.work_id != source.work_id
    assert fork.mode == "single"
    assert fork.status == "active"
    assert fork.objective == source.objective
    assert fork.definition_of_done == source.definition_of_done
    assert fork.metadata["forked_from_work_id"] == source.work_id
    assert participant.work_id == fork.work_id
    assert participant.native_id == "fork-thread"
    assert coordinator.participant(source.work_id, "openai") == source_participant
    fork_goal = session_goals.get_goal(fork.work_id)
    source_goal = session_goals.get_goal(source.work_id)
    assert fork_goal is not None and source_goal is not None
    assert fork_goal.status == "active"
    assert [(item.text, item.status) for item in fork_goal.items] == [
        ("Inspect", "completed"),
        ("Build", "in_progress"),
    ]
    assert source_goal.status == "blocked"
    assert coordinator.store.get_execution_plan(fork.work_id) is None
    fork_events = [
        event
        for event in coordinator.store.list_events(fork.work_id)
        if event.event_type == "work.forked"
    ]
    assert len(fork_events) == 1
    assert fork_events[0].control_plane
    coordinator.store.close()


def test_partner_receives_recorded_user_and_assistant_context_once(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator, lead_provider="anthropic")
    claude = coordinator.bind_participant(work.work_id, "anthropic", native_id="c1")
    codex = coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    driver = SimpleNamespace()
    tag_driver(driver, claude)
    coordinator.record_user_message(driver, "Implement the store")
    turn = Turn(role="assistant", text_parts=["Store and tests are ready"])
    coordinator.record_turn(driver, turn)

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="Review it",
        goal=None,
    )
    assert isinstance(prepared, PreparedPrompt)
    assert "Implement the store" in prepared.text
    assert "Store and tests are ready" in prepared.text
    prepared.mark_sent()

    after = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="Continue",
        goal=None,
    )
    assert isinstance(after, str)
    assert "Current accepted state" in after
    assert strip_work_envelope(after) == "Continue"
    assert coordinator.store.participant_for_provider(
        work.work_id, "openai"
    ).last_event_seq > codex.last_event_seq
    coordinator.store.close()


def test_user_message_receipt_is_none_without_binding_or_on_store_failure(
    tmp_path,
    monkeypatch,
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    unbound = SimpleNamespace()

    assert coordinator.record_user_message(unbound, "not owned") is None

    work = _tandem_work(coordinator)
    participant = coordinator.bind_participant(work.work_id, "anthropic")
    driver = SimpleNamespace()
    tag_driver(driver, participant)
    receipt = coordinator.record_user_message(driver, "persisted")

    assert isinstance(receipt, WorkEvent)

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("locked")

    monkeypatch.setattr(coordinator.store, "append_event", fail_append)
    assert coordinator.record_user_message(driver, "not persisted") is None
    coordinator.store.close()


def test_three_same_provider_attempts_do_not_echo_execution_audit_or_own_turns(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    participant = coordinator.bind_participant(
        work.work_id,
        "anthropic",
        native_id="c1",
    )
    driver = SimpleNamespace()
    tag_driver(driver, participant)
    baseline = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="baseline",
        goal=None,
    )
    if isinstance(baseline, PreparedPrompt):
        baseline.mark_sent()

    prior_text: list[str] = []
    for index in range(3):
        attempt = coordinator.start_execution_attempt(
            work_id=work.work_id,
            participant_id=participant.participant_id,
            provider="anthropic",
            participant_generation=participant.generation,
            attempt_id=f"attempt_same_{index}",
        )
        prepared = coordinator.prepare_prompt(
            work_id=work.work_id,
            provider="anthropic",
            text=f"user request {index}",
            goal=None,
        )
        wire_text = prepared.text if isinstance(prepared, PreparedPrompt) else prepared
        assert "execution.attempt" not in wire_text
        assert "execution attempt" not in wire_text.lower()
        for prior in prior_text:
            assert prior not in wire_text
        coordinator.record_execution_dispatch(
            attempt.attempt_id,
            wire_prompt_text=wire_text,
            provider_request_key=f"request-same-{index}",
        )
        if isinstance(prepared, PreparedPrompt):
            prepared.mark_sent()
        coordinator.record_execution_acceptance(
            attempt.attempt_id,
            accepted_turn_id=f"turn-same-{index}",
            provider_request_key=f"request-same-{index}",
        )

        user_text = f"same-provider-user-{index}"
        answer_text = f"same-provider-answer-{index}"
        coordinator.record_user_message(driver, user_text)
        coordinator.record_turn(
            driver,
            Turn(role="assistant", text_parts=[answer_text]),
        )
        coordinator.finish_execution_attempt(
            attempt.attempt_id,
            status="completed",
            terminal_reason=f"round_{index}",
            terminal_receipt={
                "evidence_type": "provider_terminal",
                "provider_status": "completed",
                "request_id": f"request-same-{index}",
                "turn_id": f"turn-same-{index}",
            },
        )
        prior_text.extend((user_text, answer_text))

    audit_events = [
        event
        for event in coordinator.store.list_events(work.work_id)
        if event.event_type.startswith("execution.attempt.")
    ]
    assert len(audit_events) == 12
    coordinator.store.close()


def test_alternating_providers_receive_peer_work_without_execution_audit_echo(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    claude = coordinator.bind_participant(work.work_id, "anthropic", native_id="c1")
    codex = coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    claude_driver = SimpleNamespace()
    codex_driver = SimpleNamespace()
    tag_driver(claude_driver, claude)
    tag_driver(codex_driver, codex)
    for provider in ("anthropic", "openai"):
        baseline = coordinator.prepare_prompt(
            work_id=work.work_id,
            provider=provider,
            text="baseline",
            goal=None,
        )
        if isinstance(baseline, PreparedPrompt):
            baseline.mark_sent()

    claude_attempt = coordinator.start_execution_attempt(
        work_id=work.work_id,
        participant_id=claude.participant_id,
        provider="anthropic",
        participant_generation=claude.generation,
        attempt_id="attempt_claude_first",
    )
    claude_prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="implement",
        goal=None,
    )
    claude_wire = (
        claude_prepared.text
        if isinstance(claude_prepared, PreparedPrompt)
        else claude_prepared
    )
    coordinator.record_execution_dispatch(
        claude_attempt.attempt_id,
        wire_prompt_text=claude_wire,
        provider_request_key=claude_attempt.attempt_id,
    )
    if isinstance(claude_prepared, PreparedPrompt):
        claude_prepared.mark_sent()
    coordinator.record_user_message(claude_driver, "Claude user direction")
    coordinator.record_turn(
        claude_driver,
        Turn(role="assistant", text_parts=["Claude implementation evidence"]),
    )
    coordinator.finish_execution_attempt(
        claude_attempt.attempt_id,
        status="completed",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": claude_attempt.attempt_id,
            "native_id": "c1",
        },
    )

    codex_attempt = coordinator.start_execution_attempt(
        work_id=work.work_id,
        participant_id=codex.participant_id,
        provider="openai",
        participant_generation=codex.generation,
        attempt_id="attempt_codex_second",
    )
    codex_prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="review",
        goal=None,
    )
    assert isinstance(codex_prepared, PreparedPrompt)
    assert "Claude user direction" in codex_prepared.text
    assert "Claude implementation evidence" in codex_prepared.text
    assert "execution.attempt" not in codex_prepared.text
    coordinator.record_execution_dispatch(
        codex_attempt.attempt_id,
        wire_prompt_text=codex_prepared.text,
        provider_request_key=codex_attempt.attempt_id,
    )
    codex_prepared.mark_sent()
    coordinator.record_user_message(codex_driver, "Codex review direction")
    coordinator.record_turn(
        codex_driver,
        Turn(role="assistant", text_parts=["Codex foreign finding"]),
    )
    coordinator.finish_execution_attempt(
        codex_attempt.attempt_id,
        status="completed",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": codex_attempt.attempt_id,
            "native_id": "g1",
        },
    )

    claude_second = coordinator.start_execution_attempt(
        work_id=work.work_id,
        participant_id=claude.participant_id,
        provider="anthropic",
        participant_generation=claude.generation,
        attempt_id="attempt_claude_third",
    )
    claude_review = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="continue",
        goal=None,
    )
    assert isinstance(claude_review, PreparedPrompt)
    assert "Codex review direction" in claude_review.text
    assert "Codex foreign finding" in claude_review.text
    assert "Claude implementation evidence" not in claude_review.text
    assert "execution.attempt" not in claude_review.text
    coordinator.record_execution_dispatch(
        claude_second.attempt_id,
        wire_prompt_text=claude_review.text,
        provider_request_key=claude_second.attempt_id,
    )
    claude_review.mark_sent()
    coordinator.finish_execution_attempt(
        claude_second.attempt_id,
        status="completed",
        terminal_receipt={
            "evidence_type": "provider_terminal",
            "provider_status": "completed",
            "request_id": claude_second.attempt_id,
            "native_id": "c1",
        },
    )
    coordinator.store.close()


def test_foreign_event_after_prompt_snapshot_is_not_skipped_by_audit_ack(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    second = WorkStore(coordinator.store.path)
    work = _tandem_work(coordinator)
    claude = coordinator.bind_participant(work.work_id, "anthropic", native_id="c1")
    codex = coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    baseline = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="baseline",
        goal=None,
    )
    if isinstance(baseline, PreparedPrompt):
        baseline.mark_sent()
    attempt = coordinator.start_execution_attempt(
        work_id=work.work_id,
        participant_id=claude.participant_id,
        provider="anthropic",
        participant_generation=claude.generation,
        attempt_id="attempt_before_foreign_race",
    )
    claude = coordinator.bind_participant(
        work.work_id,
        "anthropic",
        native_id="c2",
    )
    rebound_baseline = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="rebound baseline",
        goal=None,
    )
    if isinstance(rebound_baseline, PreparedPrompt):
        rebound_baseline.mark_sent()
    coordinator.finish_execution_attempt(
        attempt.attempt_id,
        status="aborted",
        terminal_reason="local_abort",
    )

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="continue",
        goal=None,
    )
    assert isinstance(prepared, PreparedPrompt)
    assert "execution.attempt" not in prepared.text
    foreign = second.append_event(
        work.work_id,
        "participant.contribution",
        {"text": "foreign event after snapshot"},
        provider="openai",
        emitting_participant_id=codex.participant_id,
        expected_participant_generation=codex.generation,
    )
    trailing_audit = coordinator.start_execution_attempt(
        work_id=work.work_id,
        participant_id=claude.participant_id,
        provider="anthropic",
        participant_generation=claude.generation,
        attempt_id="attempt_after_foreign_race",
    )
    prepared.mark_sent()
    assert coordinator.participant(
        work.work_id, "anthropic"
    ).last_event_seq < foreign.seq

    replay = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="continue again",
        goal=None,
    )
    assert isinstance(replay, PreparedPrompt)
    assert "foreign event after snapshot" in replay.text
    replay.mark_sent()
    coordinator.finish_execution_attempt(
        trailing_audit.attempt_id,
        status="aborted",
        terminal_reason="local_abort",
    )
    second.close()
    coordinator.store.close()


def test_legacy_unknown_execution_attempt_prefix_event_is_not_hidden_or_acked(
    tmp_path,
    monkeypatch,
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    coordinator.bind_participant(work.work_id, "anthropic", native_id="c1")
    codex = coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    baseline = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="baseline",
        goal=None,
    )
    if isinstance(baseline, PreparedPrompt):
        baseline.mark_sent()

    with coordinator.store._transaction() as conn:
        seq = coordinator.store._latest_seq(conn, work.work_id) + 1
        for offset, event_type, payload in (
            (
                0,
                "execution.attempt.user-message",
                '{"text":"legacy prefix collision must remain visible"}',
            ),
            (
                1,
                "execution.attempt.accepted",
                '{"text":"legacy exact collision must remain visible"}',
            ),
        ):
            conn.execute(
                """
                INSERT INTO events (
                    event_id, work_id, seq, event_type, payload_json, provider,
                    emitting_participant_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"evt_legacy_collision_{offset}",
                    work.work_id,
                    seq + offset,
                    event_type,
                    payload,
                    "openai",
                    codex.participant_id,
                    "2026-08-04T00:00:00Z",
                ),
            )

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="continue",
        goal=None,
    )
    assert isinstance(prepared, PreparedPrompt)
    assert "legacy prefix collision must remain visible" in prepared.text
    assert "legacy exact collision must remain visible" in prepared.text
    participant = coordinator.participant(work.work_id, "anthropic")
    assert participant.last_event_seq < seq
    prepared.mark_sent()
    assert coordinator.participant(work.work_id, "anthropic").last_event_seq == seq + 1
    coordinator.store.close()


def test_native_goal_transport_keeps_work_packet_without_duplicate_goal_envelope(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator, lead_provider="openai")
    coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    goal = session_goals.GoalState("Ship the native transport")

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="continue",
        goal=goal,
        include_goal_envelope=False,
    )

    assert "Current accepted state" in prepared.text
    assert "<helios-goal" not in prepared.text
    assert "User request:\ncontinue" in prepared.text
    coordinator.store.close()


def test_native_goal_transport_adds_only_missing_acceptance_context(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator, lead_provider="openai")
    coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    goal = session_goals.GoalState(
        "Ship the native transport",
        definition_of_done="Focused tests pass",
        items=[session_goals.GoalPlanItem("Verify resume")],
    )

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="continue",
        goal=goal,
        include_goal_envelope=False,
        include_goal_supplement=True,
    )

    assert "Objective: Ship the native transport" not in prepared.text
    assert "Done when: Focused tests pass" in prepared.text
    assert "- [pending] Verify resume" in prepared.text
    assert "Current accepted state" in prepared.text
    coordinator.store.close()


def test_coordinator_records_and_interrupts_native_execution_plan(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.ensure_work(cwd="/repo", lead_provider="openai")
    participant = coordinator.bind_participant(
        work.work_id,
        "openai",
        native_id="thread-plan",
    )
    driver = SimpleNamespace()
    tag_driver(driver, participant)

    plan = coordinator.record_execution_plan(
        driver,
        {
            "turnId": "turn-plan",
            "plan": [
                {"step": "Inspect", "status": "completed"},
                {"step": "Build", "status": "inProgress"},
            ],
        },
    )
    interrupted = coordinator.interrupt_execution_plan(
        driver,
        {
            "turnId": "turn-plan",
            "status": "interrupted",
            "error": None,
        },
    )

    assert plan is not None
    assert interrupted is not None
    assert interrupted.status == "interrupted"
    assert interrupted.completed_count == 1
    assert coordinator.store.get_execution_plan(work.work_id) == interrupted
    assert coordinator.record_execution_plan(
        driver,
        {
            "turnId": "turn-plan",
            "plan": [
                {"step": "Inspect", "status": "completed"},
                {"step": "Build", "status": "completed"},
            ],
        },
    ) == interrupted
    # Plan revisions have their own ledger and do not enter collaborator context.
    assert not any(
        event.event_type.startswith("execution.plan")
        for event in coordinator.store.list_events(work.work_id)
    )
    coordinator.store.close()


def test_unacknowledged_packet_is_redelivered(tmp_path, monkeypatch):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    coordinator.bind_participant(work.work_id, "anthropic", native_id="c1")
    coordinator.bind_participant(work.work_id, "openai", native_id="g1")

    first = coordinator.prepare_prompt(
        work_id=work.work_id, provider="openai", text="review", goal=None
    )
    second = coordinator.prepare_prompt(
        work_id=work.work_id, provider="openai", text="retry", goal=None
    )

    assert isinstance(first, PreparedPrompt)
    assert isinstance(second, PreparedPrompt)
    assert "anthropic participant binding" in first.text
    assert "anthropic participant binding" in second.text
    coordinator.store.close()


def test_replacement_thread_receives_prior_same_provider_history(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator, lead_provider="openai")
    original = coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    driver = SimpleNamespace()
    tag_driver(driver, original)
    coordinator.record_user_message(driver, "Original user direction")
    coordinator.record_turn(
        driver, Turn(role="assistant", text_parts=["Original GPT result"])
    )
    replacement = coordinator.bind_participant(
        work.work_id, "openai", native_id="g2"
    )
    assert replacement.last_event_seq == 0

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id, provider="openai", text="resume", goal=None
    )

    assert isinstance(prepared, PreparedPrompt)
    assert "Original user direction" in prepared.text
    assert "Original GPT result" in prepared.text
    coordinator.store.close()


def test_retired_native_binding_reopens_same_work_instead_of_splitting(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.resolve_native_work("openai", "g1", cwd="/repo")
    coordinator.bind_participant(work.work_id, "openai", native_id="g2")

    reopened = coordinator.resolve_native_work("openai", "g1", cwd="/repo")

    assert reopened.work_id == work.work_id
    assert len(coordinator.store.list_works()) == 1
    assert coordinator.resume_id(work.work_id, "openai") == "g2"
    coordinator.store.close()


def test_stale_ack_cannot_advance_rebound_participant(tmp_path, monkeypatch):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    prepared = coordinator.prepare_prompt(
        work_id=work.work_id, provider="openai", text="first", goal=None
    )
    assert isinstance(prepared, PreparedPrompt)
    replacement = coordinator.bind_participant(
        work.work_id, "openai", native_id="g2"
    )

    prepared.mark_sent()  # mark_sent contains/logs the stale-generation error

    current = coordinator.participant(work.work_id, "openai")
    assert current.generation == replacement.generation
    assert current.last_event_seq == 0
    coordinator.store.close()


def test_detaching_deleted_native_session_preserves_work_and_goal(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.resolve_native_work("anthropic", "c1", cwd="/repo")
    session_goals.set_goal(work.work_id, session_goals.GoalState("Keep this"))

    detached_work_id = coordinator.detach_native("c1")

    assert detached_work_id == work.work_id
    assert coordinator.resume_id(work.work_id, "anthropic") == ""
    assert coordinator.store.get_work(work.work_id) is not None
    assert session_goals.get_goal(work.work_id).objective == "Keep this"
    coordinator.store.close()


def test_deleting_retired_binding_does_not_detach_newer_active_binding(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.resolve_native_work("openai", "g1")
    coordinator.bind_participant(work.work_id, "openai", native_id="g2")

    detached_work_id = coordinator.detach_native("g1")

    assert detached_work_id == work.work_id
    assert coordinator.resume_id(work.work_id, "openai") == "g2"
    coordinator.store.close()


def test_ui_goal_edit_does_not_skip_unseen_partner_contribution(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    claude = coordinator.bind_participant(work.work_id, "anthropic", native_id="c1")
    codex = coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    initial = coordinator.prepare_prompt(
        work_id=work.work_id, provider="anthropic", text="start", goal=None
    )
    assert isinstance(initial, PreparedPrompt)
    initial.mark_sent()
    gpt_driver = SimpleNamespace()
    tag_driver(gpt_driver, codex)
    coordinator.record_turn(
        gpt_driver, Turn(role="assistant", text_parts=["Partner finding"])
    )

    coordinator.record_goal(
        work_id=work.work_id,
        provider="anthropic",
        goal=session_goals.GoalState("Updated objective"),
    )
    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="continue",
        goal=session_goals.GoalState("Updated objective"),
    )

    assert isinstance(prepared, PreparedPrompt)
    assert "Partner finding" in prepared.text
    assert "Updated objective" in prepared.text
    assert coordinator.participant(work.work_id, "anthropic").participant_id == (
        claude.participant_id
    )
    coordinator.store.close()


def test_selected_provider_becomes_only_lead_without_changing_work_identity(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.ensure_work(lead_provider="anthropic")
    coordinator.bind_participant(work.work_id, "anthropic", native_id="c1")
    coordinator.bind_participant(work.work_id, "openai", native_id="g1")

    selected = coordinator.select_lead(work.work_id, "openai")

    assert selected.work_id == work.work_id
    assert selected.lead_provider == "openai"
    roles = {
        participant.provider: participant.role
        for participant in coordinator.store.list_participants(work.work_id)
    }
    assert roles == {"anthropic": "partner", "openai": "lead"}
    coordinator.store.close()


def test_ledger_bounds_large_native_text_without_capturing_thinking(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    participant = coordinator.bind_participant(work.work_id, "anthropic")
    driver = SimpleNamespace()
    tag_driver(driver, participant)
    turn = Turn(
        role="assistant",
        text_parts=["x" * 20_000],
        thinking_parts=["hidden reasoning"],
    )

    event = coordinator.record_turn(driver, turn)

    assert event.payload["truncated"] is True
    assert len(event.payload["text"]) == 12_000
    assert "hidden reasoning" not in str(event.payload)
    coordinator.store.close()


def test_record_turn_returns_durable_work_event(tmp_path, monkeypatch):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    participant = coordinator.bind_participant(work.work_id, "anthropic")
    driver = SimpleNamespace()
    tag_driver(driver, participant)

    event = coordinator.record_turn(
        driver,
        Turn(role="assistant", text_parts=["Durably recorded contribution"]),
    )

    assert isinstance(event, WorkEvent)
    assert event.event_type == "participant.contribution"
    assert event.payload["text"] == "Durably recorded contribution"
    coordinator.store.close()


def test_record_turn_returns_none_when_durable_append_fails(
    tmp_path,
    monkeypatch,
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    participant = coordinator.bind_participant(work.work_id, "anthropic")
    driver = SimpleNamespace()
    tag_driver(driver, participant)

    def fail_append(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(coordinator.store, "append_event", fail_append)

    event = coordinator.record_turn(
        driver,
        Turn(role="assistant", text_parts=["Contribution that cannot persist"]),
    )

    assert event is None
    coordinator.store.close()


def test_own_result_cannot_advance_past_unseen_concurrent_partner_event(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    claude = coordinator.bind_participant(work.work_id, "anthropic")
    codex = coordinator.bind_participant(work.work_id, "openai")
    initial = coordinator.prepare_prompt(
        work_id=work.work_id, provider="anthropic", text="start", goal=None
    )
    assert isinstance(initial, PreparedPrompt)
    initial.mark_sent()
    claude_driver = SimpleNamespace()
    codex_driver = SimpleNamespace()
    tag_driver(claude_driver, claude)
    tag_driver(codex_driver, codex)
    coordinator.record_user_message(claude_driver, "Implement it")

    coordinator.record_turn(
        codex_driver,
        Turn(role="assistant", text_parts=["Concurrent GPT finding"]),
    )
    coordinator.record_turn(
        claude_driver,
        Turn(role="assistant", text_parts=["Claude finished implementation"]),
    )

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="anthropic",
        text="continue",
        goal=None,
    )
    assert isinstance(prepared, PreparedPrompt)
    assert "Concurrent GPT finding" in prepared.text
    assert "Claude finished implementation" in prepared.text
    coordinator.store.close()


def test_cleared_goal_suppresses_and_quarantines_stale_context(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    coordinator.bind_participant(work.work_id, "anthropic")
    coordinator.record_goal(
        work_id=work.work_id,
        provider="anthropic",
        goal=session_goals.GoalState("Old objective"),
    )
    assert coordinator.store.get_work(work.work_id).objective == "Old objective"

    coordinator.clear_goal(work_id=work.work_id, provider="anthropic")
    coordinator.bind_participant(work.work_id, "openai")
    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="What next?",
        goal=None,
    )

    assert coordinator.store.get_work(work.work_id).objective == ""
    assert prepared == "What next?"
    assert "Old objective" not in prepared
    participant = coordinator.participant(work.work_id, "openai")
    assert participant.last_event_seq == coordinator.store.latest_event_seq(
        work.work_id
    )
    coordinator.store.close()


def test_credentials_are_redacted_before_ledger_and_partner_prompt(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    claude = coordinator.bind_participant(work.work_id, "anthropic")
    coordinator.bind_participant(work.work_id, "openai")
    driver = SimpleNamespace()
    tag_driver(driver, claude)
    secrets = (
        "api_key=" + "sk-ant-" + "api03-SUPERSECRET123456789 "
        "password=hunter2 MY_CLIENT_SECRET=\"hunter two words\" "
        "Bearer " + "eyJ" + "abcdefghijk.abcdefghijk.abcdefghijk"
    )

    event = coordinator.record_user_message(driver, secrets)
    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="review",
        goal=None,
    )

    assert event.payload["redacted"] is True
    assert event.payload["truncated"] is False
    assert "SUPERSECRET" not in str(event.payload)
    assert "hunter2" not in str(event.payload)
    assert "hunter two words" not in str(event.payload)
    assert "eyJabcdefghijk" not in str(event.payload)
    assert isinstance(prepared, PreparedPrompt)
    assert "SUPERSECRET" not in prepared.text
    assert "hunter2" not in prepared.text
    assert "hunter two words" not in prepared.text
    assert "[REDACTED BY HELIOS]" in prepared.text
    assert b"SUPERSECRET" not in coordinator.store.path.read_bytes()
    coordinator.store.close()


def test_incomplete_contract_never_replays_events_beyond_first_page(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = _tandem_work(coordinator)
    coordinator.bind_participant(work.work_id, "anthropic")
    coordinator.record_goal(
        work_id=work.work_id,
        provider="anthropic",
        goal=session_goals.GoalState("Superseded objective"),
    )
    for index in range(30):
        coordinator.store.append_event(
            work.work_id,
            "evidence.recorded",
            {"text": f"evidence {index}"},
        )
    coordinator.clear_goal(work_id=work.work_id, provider="anthropic")
    coordinator.bind_participant(work.work_id, "openai")

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="continue",
        goal=None,
    )

    assert prepared == "continue"
    assert "Superseded objective" not in prepared
    participant = coordinator.participant(work.work_id, "openai")
    assert participant.last_event_seq == coordinator.store.latest_event_seq(work.work_id)
    coordinator.store.close()


def test_pre_contract_events_are_quarantined_before_tandem_is_accepted(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.ensure_work(cwd="/repo")
    claude = coordinator.bind_participant(work.work_id, "anthropic")
    coordinator.bind_participant(work.work_id, "openai")
    driver = SimpleNamespace()
    tag_driver(driver, claude)
    coordinator.record_user_message(driver, "speculative old direction")

    suppressed = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="ordinary chat",
        goal=None,
    )
    assert suppressed == "ordinary chat"

    coordinator.store.update_work(
        work.work_id,
        objective="Accepted objective",
        definition_of_done="Focused tests pass",
        mode="tandem",
    )
    coordinator.store.append_event(
        work.work_id,
        "decision.recorded",
        {"text": "new accepted evidence"},
    )
    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="review",
        goal=None,
    )

    assert isinstance(prepared, PreparedPrompt)
    assert "new accepted evidence" in prepared.text
    assert "speculative old direction" not in prepared.text
    coordinator.store.close()


def test_participant_joining_after_acceptance_cannot_replay_pre_contract_events(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.ensure_work(cwd="/repo")
    claude = coordinator.bind_participant(work.work_id, "anthropic")
    driver = SimpleNamespace()
    tag_driver(driver, claude)
    coordinator.record_user_message(driver, "late-join speculative direction")

    accepted = coordinator.store.update_work(
        work.work_id,
        objective="Accepted objective",
        definition_of_done="Focused tests pass",
        mode="tandem",
    )
    coordinator.store.append_event(
        work.work_id,
        "decision.recorded",
        {"text": "accepted evidence for late joiner"},
    )
    coordinator.bind_participant(work.work_id, "openai", native_id="g1")

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="review",
        goal=None,
    )

    assert isinstance(prepared, PreparedPrompt)
    assert "accepted evidence for late joiner" in prepared.text
    assert "late-join speculative direction" not in prepared.text
    participant = coordinator.participant(work.work_id, "openai")
    assert participant.last_event_seq >= accepted.contract_start_seq
    coordinator.store.close()


def test_rebound_participant_replays_only_current_contract_epoch(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    work = coordinator.ensure_work(cwd="/repo")
    claude = coordinator.bind_participant(work.work_id, "anthropic")
    coordinator.bind_participant(work.work_id, "openai", native_id="g1")
    driver = SimpleNamespace()
    tag_driver(driver, claude)
    coordinator.record_user_message(driver, "rebind speculative direction")

    accepted = coordinator.store.update_work(
        work.work_id,
        objective="Accepted objective",
        definition_of_done="Focused tests pass",
        mode="tandem",
    )
    coordinator.store.append_event(
        work.work_id,
        "decision.recorded",
        {"text": "accepted evidence for replacement"},
    )
    replacement = coordinator.bind_participant(
        work.work_id,
        "openai",
        native_id="g2",
    )
    assert replacement.last_event_seq == 0

    prepared = coordinator.prepare_prompt(
        work_id=work.work_id,
        provider="openai",
        text="review",
        goal=None,
    )

    assert isinstance(prepared, PreparedPrompt)
    assert "accepted evidence for replacement" in prepared.text
    assert "rebind speculative direction" not in prepared.text
    participant = coordinator.participant(work.work_id, "openai")
    assert participant.last_event_seq >= accepted.contract_start_seq
    coordinator.store.close()


def test_contract_acceptance_and_prompt_snapshot_serialize_across_connections(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    second = WorkStore(coordinator.store.path)
    work = coordinator.ensure_work(cwd="/repo")
    claude = coordinator.bind_participant(work.work_id, "anthropic")
    coordinator.bind_participant(work.work_id, "openai")
    driver = SimpleNamespace()
    tag_driver(driver, claude)
    coordinator.record_user_message(driver, "concurrent speculative direction")
    barrier = threading.Barrier(2)

    def prepare_during_transition():
        barrier.wait()
        return coordinator.prepare_prompt(
            work_id=work.work_id,
            provider="openai",
            text="ordinary chat",
            goal=None,
        )

    def accept_contract():
        barrier.wait()
        return second.update_work(
            work.work_id,
            objective="Accepted concurrent objective",
            definition_of_done="Focused tests pass",
            mode="tandem",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            prepared_future = pool.submit(prepare_during_transition)
            accepted_future = pool.submit(accept_contract)
            prepared_future.result()
            accepted = accepted_future.result()

        evidence = second.append_event(
            work.work_id,
            "decision.recorded",
            {"text": "accepted concurrent evidence"},
        )
        prepared = coordinator.prepare_prompt(
            work_id=work.work_id,
            provider="openai",
            text="review",
            goal=None,
        )

        assert accepted.contract_start_seq < evidence.seq
        assert isinstance(prepared, PreparedPrompt)
        assert "accepted concurrent evidence" in prepared.text
        assert "concurrent speculative direction" not in prepared.text
    finally:
        second.close()
        coordinator.store.close()


def test_budget_breaker_wins_over_stale_goal_status_from_second_connection(
    tmp_path, monkeypatch
):
    coordinator = _coordinator(tmp_path, monkeypatch)
    second_store = WorkStore(coordinator.store.path)
    stale_coordinator = WorkCoordinator(second_store)
    work = _tandem_work(coordinator)
    barrier = threading.Barrier(2)
    budget_committed = threading.Event()
    original_update = second_store.update_work

    def delayed_stale_update(*args, **kwargs):
        # record_goal has already read the old active status before it reaches
        # this barrier, reproducing the former cross-connection TOCTOU window.
        barrier.wait()
        assert budget_committed.wait(timeout=5)
        return original_update(*args, **kwargs)

    monkeypatch.setattr(second_store, "update_work", delayed_stale_update)

    def record_stale_goal():
        return stale_coordinator.record_goal(
            work_id=work.work_id,
            provider="anthropic",
            goal=session_goals.GoalState(
                "Stale completed objective",
                status=session_goals.GOAL_COMPLETE,
            ),
        )

    def exhaust_budget():
        barrier.wait()
        try:
            return coordinator.mark_budget_exhausted(
                work_id=work.work_id,
                provider="openai",
                details={"tokens_used": 100},
            )
        finally:
            budget_committed.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            stale_future = pool.submit(record_stale_goal)
            budget_future = pool.submit(exhaust_budget)
            limited = budget_future.result()
            stale_future.result()

        final = coordinator.store.get_work(work.work_id)
        assert limited.status == "budgetLimited"
        assert final.status == "budgetLimited"
        assert final.objective == "Stale completed objective"

        events = coordinator.store.list_events(work.work_id)
        exhausted = [event for event in events if event.event_type == "budget.exhausted"]
        assert len(exhausted) == 1
        assert exhausted[0].payload == {"provider": "openai", "tokens_used": 100}
        later_status_updates = [
            event.payload.get("status")
            for event in events
            if event.event_type == "work.updated" and event.seq > exhausted[0].seq
        ]
        assert "active" not in later_status_updates
        assert "complete" not in later_status_updates
    finally:
        second_store.close()
        coordinator.store.close()
