"""Run background work serially while keeping only the latest pending request."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar


Payload = TypeVar("Payload")
Result = TypeVar("Result")


class LatestTaskRunner(Generic[Payload, Result]):
    """Serial worker that coalesces pending work to the newest payload.

    While one task is running, repeated ``submit()`` calls replace the pending
    payload instead of starting more threads. When the current task completes,
    the worker processes only the latest pending payload, then exits.
    """

    def __init__(
        self,
        *,
        work: Callable[[Payload], Result],
        deliver: Callable[[Payload, Result | Exception], None],
        name: str,
        start_thread: Callable[[Callable[[], None], str], None] | None = None,
    ) -> None:
        self._work = work
        self._deliver = deliver
        self._name = name
        # TEST-ONLY injection to run work synchronously; no production caller
        # supplies it. That path is deliberately UNtracked (no self._thread),
        # so shutdown()'s join is a no-op for it — acceptable because a
        # synchronous runner has already finished its work by the time control
        # returns from submit(). The default (production) path owns the thread
        # so shutdown() can join it.
        self._start_thread = start_thread
        self._lock = threading.Lock()
        # Serialize the tiny result-delivery boundary with shutdown().  The
        # state lock alone is insufficient: shutdown could otherwise land
        # after _run's final _closed check but before _deliver schedules its
        # idle callback.  RLock keeps a deliver callback that itself calls
        # shutdown() from deadlocking (none of the production callbacks do,
        # but this is a generic helper and should remain safe if one does).
        self._delivery_lock = threading.RLock()
        self._pending: Payload | None = None
        self._running = False
        self._closed = False
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def submit(self, payload: Payload) -> None:
        should_start = False
        with self._lock:
            if self._closed:
                return
            self._pending = payload
            if not self._running:
                self._running = True
                should_start = True
                # Publish the thread handle *under the lock* (default path) so a
                # concurrent shutdown() either observes it and joins, or wins
                # the lock first and _spawn() below skips the start. Without
                # this, submit could start a worker after shutdown() already
                # read self._thread and returned — an unjoined thread.
                if self._start_thread is None:
                    self._thread = threading.Thread(
                        target=self._run, name=self._name, daemon=True
                    )
        if should_start:
            self._spawn()

    def shutdown(self, *, join: bool = True, timeout: float = 2.0) -> None:
        """Stop delivering and drop any pending payload.

        After this returns, ``submit()`` is a no-op, no delivery callback is in
        flight, and an unfinished task's result will be discarded.  Delivery
        and shutdown share a short serialization boundary, so shutdown either
        wins before delivery starts or waits for an already-started delivery
        to finish; no stale ``idle_add`` can be scheduled *after* shutdown has
        returned. Idempotent. If a delivery callback itself invokes shutdown,
        that callback is necessarily the one in-flight exception; the RLock
        prevents self-deadlock while still closing future work and delivery.

        ``join=True`` (default) additionally waits — bounded by *timeout* — for
        the worker to exit, so a background load isn't still walking a temp dir
        the caller is about to delete (the case tests rely on). This is a
        *bounded* join, not a guarantee: if a single ``_work`` call runs longer
        than *timeout*, ``join`` returns while the worker is still inside it.
        The worker still won't deliver, but it may briefly outlive shutdown().
        In practice ``_work`` here is a fast state-dir read, well under 2s.
        ``join=False`` skips waiting for background *work*. It may still wait
        for an already-entered delivery callback, which is intentionally tiny
        in production (it only schedules a GLib idle callback). This delivery
        boundary wait is required for the post-return delivery guarantee.

        Note: the submit/shutdown *start* race is separately closed (see
        ``_spawn``) — a worker is never started after shutdown observed it
        unstarted; it is either joined/finished or never started at all."""
        # Lock order is always delivery -> state when both are needed (also in
        # _run), preventing an AB/BA deadlock.  Do not hold _delivery_lock while
        # joining: a worker still inside _work needs that lock once it returns
        # so it can observe _closed and exit.
        with self._delivery_lock:
            with self._lock:
                self._closed = True
                self._pending = None
                thread = self._thread
        if (
            join
            and thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)

    def _spawn(self) -> None:
        if self._start_thread is not None:
            self._start_thread(self._run, self._name)
            return
        # Start the worker *while holding the lock* — and re-check _closed first.
        # shutdown() must take the same lock to set _closed and read
        # self._thread, so it can never observe this thread in the unstarted
        # state (is_alive() == False), skip the join, and have us start it
        # immediately afterwards. This closes the START race: a worker is never
        # started after shutdown ran — it is either started here (then joinable)
        # or refused below. (A worker already running a slow _work can still
        # outlive a bounded join; see shutdown().) thread.start() only creates
        # the OS thread and returns; the worker's _run() then blocks on this
        # same lock until we release it, so starting under the lock can't
        # deadlock.
        with self._lock:
            if self._closed:
                self._running = False
                self._thread = None
                return
            if self._thread is not None:
                self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                payload = self._pending
                self._pending = None
                if payload is None or self._closed:
                    self._running = False
                    return
            try:
                result = self._work(payload)
            except Exception as exc:  # noqa: BLE001 - delivered to owner
                result = exc
            # Serialize the final check + callback with shutdown(). Without the
            # delivery lock, shutdown could set _closed and return in the tiny
            # window between the check and a callback's GLib.idle_add.
            with self._delivery_lock:
                with self._lock:
                    if self._closed:
                        self._running = False
                        return
                try:
                    self._deliver(payload, result)
                except Exception:
                    # Preserve the original exception behaviour (the worker
                    # still fails loudly), but never leave is_running stuck
                    # True forever after a delivery bug.
                    with self._lock:
                        self._running = False
                    raise
