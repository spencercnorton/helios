"""Durable dollar reservations, including uncertain and historical spend."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from helios.backend.openrouter.budget import SpendLimitReached, micro_usd
from helios.backend.work_store import WorkStore, WorkStoreError


def admit(store, *, work_id=None, provider="openrouter"):
    work = store.get_work(work_id) if work_id else store.create_work(cwd="/repo")
    participant = store.bind_participant(work.work_id, provider, native_session_id=f"native-{work.work_id}")
    attempt = store.start_execution_attempt(
        work.work_id, participant.participant_id, provider=provider,
        expected_participant_generation=participant.generation,
    )
    store.record_execution_dispatch(
        attempt.attempt_id, wire_prompt_text="budget test",
        provider_request_key=attempt.attempt_id,
    )
    return attempt


def complete(store, attempt, *, cost=None):
    store.record_execution_acceptance(
        attempt.attempt_id, accepted_turn_id="response-1",
        provider_request_key=attempt.attempt_id,
    )
    store.finish_execution_attempt(
        attempt.attempt_id, status="completed",
        terminal_receipt={"evidence_type": "provider_terminal", "provider_status": "completed",
                          "request_id": attempt.attempt_id, "turn_id": "response-1"},
        cost_micro_usd=cost,
    )


@pytest.fixture
def store(tmp_path):
    with WorkStore(tmp_path / "work.db") as value:
        yield value


def test_reservations_and_reported_cost_survive_new_driver_and_store(tmp_path):
    path = tmp_path / "work.db"
    with WorkStore(path) as first:
        attempt = admit(first)
        first.reserve_openrouter_request(attempt.attempt_id, "http-1", 3_000_000)
        first.settle_openrouter_request(attempt.attempt_id, "http-1", cost_micro_usd=1_234)
        first.reserve_openrouter_request(attempt.attempt_id, "http-2", 4_000_000)
        # Closing/reopening the store is not evidence that http-2 cost nothing.
        complete(first, attempt, cost=1_234)
    with WorkStore(path) as second:
        following = admit(second, work_id=attempt.work_id)
        assert second.openrouter_spend_micro_usd(following.attempt_id) == 4_001_234
        with pytest.raises(SpendLimitReached):
            second.reserve_openrouter_request(following.attempt_id, "http-3", 1_000_000)


def test_settlement_is_idempotent_and_attempt_bound(store):
    attempt = admit(store)
    other = admit(store)
    store.reserve_openrouter_request(attempt.attempt_id, "http-1", 1000)
    with pytest.raises(WorkStoreError):
        store.settle_openrouter_request(other.attempt_id, "http-1", cost_micro_usd=0)
    for _ in range(2):
        assert store.settle_openrouter_request(attempt.attempt_id, "http-1", cost_micro_usd=10) == 10
    with pytest.raises(WorkStoreError):
        store.settle_openrouter_request(attempt.attempt_id, "http-1", cost_micro_usd=0, rejected=True)


def test_verified_rejection_releases_only_its_reservation(store):
    attempt = admit(store)
    store.reserve_openrouter_request(attempt.attempt_id, "old", 1_000_000)
    store.reserve_openrouter_request(attempt.attempt_id, "rejected", 2_000_000)
    assert store.settle_openrouter_request(
        attempt.attempt_id, "rejected", cost_micro_usd=0, rejected=True,
    ) == 1_000_000


def test_legacy_reported_cost_counts_and_unknown_spend_blocks(store):
    paid = admit(store)
    complete(store, paid, cost=3_000_000)
    next_paid = admit(store, work_id=paid.work_id)
    assert store.openrouter_spend_micro_usd(next_paid.attempt_id) == 3_000_000
    unknown = admit(store)
    complete(store, unknown)
    following = admit(store, work_id=unknown.work_id)
    with pytest.raises(WorkStoreError, match="unknown spend"):
        store.openrouter_spend_micro_usd(following.attempt_id)


def test_terminal_or_wrong_provider_attempt_cannot_reserve(store):
    attempt = admit(store)
    complete(store, attempt, cost=0)
    with pytest.raises(WorkStoreError):
        store.reserve_openrouter_request(attempt.attempt_id, "late", 0)
    other = admit(store, provider="openai")
    with pytest.raises(WorkStoreError):
        store.reserve_openrouter_request(other.attempt_id, "wrong-provider", 0)


def test_late_receipt_can_settle_but_old_generation_cannot_send(store):
    attempt = admit(store)
    store.reserve_openrouter_request(attempt.attempt_id, "http-1", 1000)
    store.bind_participant(attempt.work_id, "openrouter", native_session_id="replacement")
    with pytest.raises(WorkStoreError):
        store.reserve_openrouter_request(attempt.attempt_id, "late", 1000)
    assert store.settle_openrouter_request(attempt.attempt_id, "http-1", cost_micro_usd=3) == 3


def test_cross_connection_reservations_cannot_overbook(tmp_path):
    path = tmp_path / "work.db"
    with WorkStore(path) as first:
        attempt = admit(first)
        with WorkStore(path) as second:
            def reserve(pair):
                store, request_id = pair
                try:
                    store.reserve_openrouter_request(attempt.attempt_id, request_id, 3_000_000)
                    return True
                except SpendLimitReached:
                    return False
            with ThreadPoolExecutor(2) as pool:
                assert sorted(pool.map(reserve, [(first, "one"), (second, "two")])) == [False, True]
            assert first.openrouter_spend_micro_usd(attempt.attempt_id) == 3_000_000


def test_duplicate_reservation_is_not_permission_to_send_again(store):
    attempt = admit(store)
    store.reserve_openrouter_request(attempt.attempt_id, "same", 1)
    with pytest.raises(Exception):
        store.reserve_openrouter_request(attempt.attempt_id, "same", 1)
    assert store.openrouter_spend_micro_usd(attempt.attempt_id) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_dollar_receipt_never_means_free(value):
    with pytest.raises(ValueError):
        micro_usd(value)


def test_sub_micro_dollar_charges_round_up():
    assert micro_usd(0.0000001) == 1
    assert micro_usd(0) == 0


def test_v10_migration_preserves_existing_cost(tmp_path):
    path = tmp_path / "work.db"
    with WorkStore(path) as old:
        attempt = admit(old)
        complete(old, attempt, cost=12_345)
        old._conn.execute("DROP TABLE openrouter_request_budgets")
        old._conn.execute("DROP TABLE openrouter_budget_attempts")
        old._conn.execute("PRAGMA user_version=10")
    with WorkStore(path) as new:
        following = admit(new, work_id=attempt.work_id)
        assert new.openrouter_spend_micro_usd(following.attempt_id) == 12_345


def test_guarded_attempt_without_http_does_not_poison_the_next_turn(store):
    attempt = admit(store)
    assert store.initialize_openrouter_budget(attempt.attempt_id) == 0
    # A small context window can end a turn locally before any HTTP request.
    complete(store, attempt)
    following = admit(store, work_id=attempt.work_id)
    assert store.initialize_openrouter_budget(following.attempt_id) == 0


def test_actual_charge_over_estimate_is_retained_and_prevents_another_request(store):
    attempt = admit(store)
    store.reserve_openrouter_request(attempt.attempt_id, "underestimate", 1000)
    assert store.settle_openrouter_request(
        attempt.attempt_id, "underestimate", cost_micro_usd=5_000_001,
    ) == 5_000_001
    with pytest.raises(SpendLimitReached):
        store.reserve_openrouter_request(attempt.attempt_id, "next", 0)
