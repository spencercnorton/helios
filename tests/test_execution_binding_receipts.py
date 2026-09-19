"""A participant rebind fences sends, not receipts for admitted work."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence,
    ExecutionDispatchEvidence,
    ExecutionStopEvidence,
    ExecutionTerminalEvidence,
)
from helios.backend.work_coordinator import WorkCoordinator, tag_driver
from helios.backend.work_store import WorkStore
from helios.main_window import MainWindow


@pytest.fixture(params=["different-native", "retire-and-reattach"])
def admitted(tmp_path, request):
    store = WorkStore(tmp_path / "work.db")
    coordinator = WorkCoordinator(store)
    work = coordinator.ensure_work(cwd=str(tmp_path), lead_provider="anthropic")
    original = coordinator.bind_participant(
        work.work_id, "anthropic", native_id="session-original"
    )
    driver = SimpleNamespace(_helios_identity_confirmed=True)
    tag_driver(driver, original)
    window = SimpleNamespace(_work_coordinator=coordinator)
    attempt = coordinator.start_execution_attempt(
        work_id=work.work_id,
        participant_id=original.participant_id,
        provider="anthropic",
        participant_generation=original.generation,
    )
    MainWindow._record_execution_dispatch_for_driver(
        window, driver, attempt.attempt_id,
        ExecutionDispatchEvidence("bounded task", "request-1", "session-original"),
    )
    if request.param == "retire-and-reattach":
        store.update_participant(original.participant_id, native_session_id="")
    replacement = coordinator.bind_participant(
        work.work_id, "anthropic",
        native_id=("session-original" if request.param == "retire-and-reattach"
                   else "session-replacement"),
    )
    try:
        yield window, driver, attempt, replacement
    finally:
        store.close()


def test_delayed_acceptance_and_terminal_receipt_release_original_generation(admitted):
    window, driver, attempt, replacement = admitted
    coordinator = window._work_coordinator
    MainWindow._record_execution_acceptance_for_driver(
        window, driver, attempt.attempt_id,
        ExecutionAcceptanceEvidence("turn-1", "request-1", "session-original"),
    )
    evidence = ExecutionTerminalEvidence(
        "provider_terminal", "completed", "provider_terminal",
        provider_status="completed", request_id="request-1", turn_id="turn-1",
        native_id="session-original", usage={"input_tokens": 10, "output_tokens": 5},
    )
    MainWindow._finish_execution_attempt_with_evidence_for_driver(
        window, driver, attempt.attempt_id, evidence,
    )
    finished = coordinator.store.get_execution_attempt(attempt.attempt_id)
    assert finished.status == "completed"
    assert finished.participant_generation == attempt.participant_generation
    assert finished.usage == {"input_tokens": 10, "output_tokens": 5}
    assert coordinator.participant(attempt.work_id, "anthropic") == replacement
    assert driver._helios_participant_generation == attempt.participant_generation
    assert coordinator.store.list_active_execution_attempts() == []
    next_attempt = coordinator.start_execution_attempt(
        work_id=attempt.work_id, participant_id=replacement.participant_id,
        provider="anthropic", participant_generation=replacement.generation,
    )
    assert next_attempt.participant_generation == replacement.generation


def test_delayed_cancellation_acknowledgement_releases_only_original_attempt(admitted):
    window, driver, attempt, replacement = admitted
    acknowledgement = {
        "acknowledged": True, "cancellation_confirmed": True,
        "provider_status": "cancelled", "request_id": "request-1",
        "native_id": "session-original",
    }
    MainWindow._record_execution_stop_for_driver(
        window, driver, attempt.attempt_id,
        ExecutionStopEvidence(acknowledgement, "restored"),
    )
    MainWindow._finish_execution_attempt_with_evidence_for_driver(
        window, driver, attempt.attempt_id,
        ExecutionTerminalEvidence(
            "cancellation_ack", "aborted", "cancellation_ack",
            native_id="session-original", stop_acknowledgement=acknowledgement,
            queue_disposition="restored",
        ),
    )
    coordinator = window._work_coordinator
    assert coordinator.store.get_execution_attempt(attempt.attempt_id).status == "aborted"
    assert coordinator.participant(attempt.work_id, "anthropic") == replacement


def test_retired_driver_cannot_dispatch_again(admitted):
    window, driver, attempt, _ = admitted
    with pytest.raises(RuntimeError, match="conflicts with Work binding|changed participant generation"):
        MainWindow._record_execution_dispatch_for_driver(
            window, driver, attempt.attempt_id,
            ExecutionDispatchEvidence("replay", "request-1", "session-original"),
        )
    assert window._work_coordinator.store.get_execution_attempt(attempt.attempt_id).status == "running"


@pytest.mark.parametrize("field,value", [
    ("_helios_participant_generation", 2),
    ("_helios_participant_id", "another-participant"),
    ("_helios_work_id", "another-work"),
    ("_helios_participant_provider", "openrouter"),
    ("_helios_identity_confirmed", False),
])
def test_receipt_from_wrong_driver_cannot_release_attempt(admitted, field, value):
    window, driver, attempt, _ = admitted
    setattr(driver, field, value)
    with pytest.raises(RuntimeError):
        MainWindow._finish_execution_attempt_with_evidence_for_driver(
            window, driver, attempt.attempt_id,
            ExecutionTerminalEvidence(
                "provider_terminal", "completed", "provider_terminal",
                provider_status="completed", request_id="request-1",
                native_id="session-original",
            ),
        )
    assert window._work_coordinator.store.get_execution_attempt(attempt.attempt_id).status == "running"


def test_receipt_for_new_native_binding_cannot_finish_old_attempt(admitted):
    window, driver, attempt, _ = admitted
    with pytest.raises(RuntimeError, match="conflicts with admission"):
        MainWindow._finish_execution_attempt_with_evidence_for_driver(
            window, driver, attempt.attempt_id,
            ExecutionTerminalEvidence(
                "provider_terminal", "completed", "provider_terminal",
                provider_status="completed", request_id="request-1",
                native_id="session-replacement",
            ),
        )
    assert window._work_coordinator.store.get_execution_attempt(attempt.attempt_id).status == "running"


def test_first_native_id_still_binds_current_placeholder(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    try:
        coordinator = WorkCoordinator(store)
        work = coordinator.ensure_work(cwd=str(tmp_path), lead_provider="anthropic")
        participant = coordinator.bind_participant(work.work_id, "anthropic")
        driver = SimpleNamespace(_helios_identity_confirmed=True)
        tag_driver(driver, participant)
        window = SimpleNamespace(_work_coordinator=coordinator)
        attempt = coordinator.start_execution_attempt(
            work_id=work.work_id, participant_id=participant.participant_id,
            provider="anthropic", participant_generation=participant.generation,
        )
        coordinator.record_execution_dispatch(
            attempt.attempt_id, wire_prompt_text="first turn", provider_request_key="request-1",
        )
        MainWindow._record_execution_acceptance_for_driver(
            window, driver, attempt.attempt_id,
            ExecutionAcceptanceEvidence("turn-1", "request-1", "session-first"),
        )
        assert store.get_execution_attempt(attempt.attempt_id).native_binding_id == "session-first"
        assert coordinator.participant(work.work_id, "anthropic").generation == participant.generation
    finally:
        store.close()
