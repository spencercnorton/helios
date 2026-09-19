from __future__ import annotations

import threading
import time

from helios.backend.latest_worker import LatestTaskRunner


def test_latest_runner_delivers_result_with_injected_runner():
    delivered = []

    runner = LatestTaskRunner[str, str](
        work=lambda payload: payload.upper(),
        deliver=lambda payload, result: delivered.append((payload, result)),
        name="test-latest",
        start_thread=lambda target, _name: target(),
    )

    runner.submit("one")

    assert delivered == [("one", "ONE")]
    assert runner.is_running is False


def test_latest_runner_delivers_exceptions_as_results():
    delivered = []

    def work(_payload: str) -> str:
        raise RuntimeError("boom")

    runner = LatestTaskRunner[str, str](
        work=work,
        deliver=lambda payload, result: delivered.append((payload, result)),
        name="test-latest",
        start_thread=lambda target, _name: target(),
    )

    runner.submit("one")

    assert delivered[0][0] == "one"
    assert isinstance(delivered[0][1], RuntimeError)


def test_latest_runner_coalesces_pending_payloads_while_busy():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    worked: list[str] = []
    delivered: list[tuple[str, str | Exception]] = []

    def work(payload: str) -> str:
        worked.append(payload)
        if payload == "first":
            started.set()
            assert release.wait(2)
        return payload.upper()

    def deliver(payload: str, result: str | Exception) -> None:
        delivered.append((payload, result))
        if payload == "third":
            finished.set()

    runner = LatestTaskRunner[str, str](
        work=work,
        deliver=deliver,
        name="test-latest",
    )

    runner.submit("first")
    assert started.wait(2)
    runner.submit("second")
    runner.submit("third")
    release.set()

    assert finished.wait(2)
    assert worked == ["first", "third"]
    assert delivered == [("first", "FIRST"), ("third", "THIRD")]
    assert runner.is_running is False


def test_shutdown_before_delivery_discards_in_flight_and_pending():
    started = threading.Event()
    proceed = threading.Event()
    worked: list[str] = []
    delivered: list[tuple[str, object]] = []

    def work(payload: str) -> str:
        worked.append(payload)
        started.set()
        # Hold the worker inside work() until the test has begun shutdown, so
        # the post-work `_closed` recheck deterministically observes the close
        # — no reliance on scheduler timing (which would make asserting the
        # dropped delivery a race).
        assert proceed.wait(2)
        return payload.upper()

    runner = LatestTaskRunner[str, str](
        work=work,
        deliver=lambda payload, result: delivered.append((payload, result)),
        name="test-latest",
    )

    runner.submit("first")
    assert started.wait(2)          # worker is now inside work("first")
    runner.submit("second")         # coalesced pending — must be dropped

    # shutdown() sets the close flag, then joins the (still-blocked) worker, so
    # run it off-thread and release the worker only once the flag is set. That
    # ordering makes "shutdown wins the race" deterministic.
    shut = threading.Thread(target=runner.shutdown, name="shutdown")
    shut.start()
    deadline = time.monotonic() + 2
    while not runner._closed and time.monotonic() < deadline:
        time.sleep(0.005)
    assert runner._closed, "shutdown() did not set the close flag"
    proceed.set()                   # worker returns; recheck sees _closed=True
    shut.join(3)

    assert not shut.is_alive()
    assert delivered == []          # in-flight "first" result dropped
    assert worked == ["first"]      # coalesced "second" never ran
    assert runner.is_running is False


def test_submit_after_shutdown_is_ignored():
    delivered = []
    runner = LatestTaskRunner[str, str](
        work=lambda payload: payload.upper(),
        deliver=lambda payload, result: delivered.append((payload, result)),
        name="test-latest",
        start_thread=lambda target, _name: target(),
    )

    runner.shutdown()
    runner.submit("late")
    assert delivered == []

    # Idempotent — a second shutdown is a harmless no-op.
    runner.shutdown()


def test_shutdown_between_submit_and_spawn_never_starts_worker(monkeypatch):
    """The submit/shutdown race: submit() publishes the worker thread under the
    lock, but _spawn() starts it after releasing the lock. If shutdown() lands
    in that window it must win — the worker must never start (so the runner's
    'either joined or never started' guarantee holds)."""
    started: list[bool] = []

    class FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            started.append(True)

        def is_alive(self):
            return False

    monkeypatch.setattr(
        "helios.backend.latest_worker.threading.Thread", FakeThread
    )

    runner = LatestTaskRunner[int, int](
        work=lambda p: p, deliver=lambda p, r: None, name="race"
    )
    # Reproduce submit()'s under-lock setup, stopping before _spawn().
    with runner._lock:
        runner._pending = 1
        runner._running = True
        runner._thread = FakeThread()
    # shutdown() lands in the window between submit() and _spawn().
    runner.shutdown()
    # The deferred _spawn() must now refuse to start the worker.
    runner._spawn()
    assert started == []
    assert runner._running is False
    assert runner._thread is None


def test_shutdown_join_false_stops_delivery_without_blocking():
    """Fast close (window teardown): shutdown(join=False) returns promptly even
    while the worker is mid-work, and still suppresses the delivery."""
    started = threading.Event()
    proceed = threading.Event()
    delivered: list[tuple[int, object]] = []

    def work(payload: int) -> int:
        started.set()
        assert proceed.wait(2)
        return payload

    runner = LatestTaskRunner[int, int](
        work=work,
        deliver=lambda payload, result: delivered.append((payload, result)),
        name="nonblocking",
    )
    runner.submit(1)
    assert started.wait(2)

    t0 = time.monotonic()
    runner.shutdown(join=False)          # must not block on the busy worker
    assert time.monotonic() - t0 < 1.0

    proceed.set()
    if runner._thread is not None:       # let the worker finish deterministically
        runner._thread.join(2)
    assert delivered == []               # delivery still suppressed
    assert runner.is_running is False


def test_shutdown_serializes_with_delivery_boundary():
    """Once delivery has entered, shutdown must wait for it to finish.

    This deterministically closes the old check-then-deliver race: no callback
    can make a late UI scheduling effect after shutdown(join=False) returns.
    """
    delivery_entered = threading.Event()
    release_delivery = threading.Event()
    shutdown_started = threading.Event()
    shutdown_returned = threading.Event()
    order: list[str] = []

    def deliver(_payload: int, _result: int | Exception) -> None:
        order.append("delivery-enter")
        delivery_entered.set()
        assert release_delivery.wait(2)
        order.append("delivery-return")

    runner = LatestTaskRunner[int, int](
        work=lambda payload: payload,
        deliver=deliver,
        name="delivery-boundary",
    )
    runner.submit(1)
    assert delivery_entered.wait(2)

    def close() -> None:
        shutdown_started.set()
        runner.shutdown(join=False)
        order.append("shutdown-return")
        shutdown_returned.set()

    shut = threading.Thread(target=close, name="delivery-boundary-shutdown")
    shut.start()
    assert shutdown_started.wait(1)
    assert not shutdown_returned.wait(0.1)

    release_delivery.set()
    assert shutdown_returned.wait(2)
    shut.join(2)
    if runner._thread is not None:
        runner._thread.join(2)

    assert order == ["delivery-enter", "delivery-return", "shutdown-return"]
    assert runner.is_running is False
