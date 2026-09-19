"""Driver registry and lifecycle policy for live Claude/Codex sessions.

This is intentionally small: drivers still emit their native signals and the
window still owns UI callbacks. The manager owns the bookkeeping that should
not live in MainWindow: current vs background driver, handler ids, live ids,
idle reaping, LRU eviction, and hard-forget cleanup.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


Driver = Any

# Signals EVERY driver Helios spawns must define. `connect_handlers()` wires
# these for all providers, and GObject raises `TypeError: unknown signal name`
# on an unknown one — so a Claude-only signal added to this contract takes
# every GPT/OpenRouter chat down at spawn with an uncaught exception and no UI
# feedback (v0.69.0 did exactly that with "capabilities-updated"). A signal
# only some drivers emit belongs in an isinstance branch, never here.
# Pinned by tests/test_driver_signal_contract.py.
COMMON_DRIVER_SIGNALS = (
    "session-started",
    "assistant-streaming",
    "turn-appended",
    "result",
    "usage-updated",
    "rate-limit-updated",
    "question-asked",
    "queued-user-sent",
    "error",
    "exited",
)


class DriverManager:
    def __init__(
        self,
        *,
        max_live: int,
        idle_seconds: int,
        on_live_ids_changed: Callable[[set[str]], None] | None = None,
        log=None,
    ) -> None:
        self.max_live = max_live
        self.idle_seconds = idle_seconds
        self._on_live_ids_changed = on_live_ids_changed
        self._log = log
        self.current: Driver | None = None
        self.starting: Driver | None = None
        self.live: dict[str, Driver] = {}
        self.handlers: dict[Driver, list[int]] = {}

    def is_current(self, driver: Driver) -> bool:
        return driver is self.current

    def driver_provider(self, driver: Driver, default: str = "") -> str:
        return getattr(driver, "provider", default)

    def driver_matches_provider(self, driver: Driver, provider: str) -> bool:
        return self.driver_provider(driver) == provider

    def current_accepting_matches_provider(self, provider: str) -> bool:
        return (
            self.current is not None
            and self.current.is_accepting_input
            and self.driver_matches_provider(self.current, provider)
        )

    def bind_current(self, driver: Driver | None) -> None:
        self.current = driver

    def unbind_current(self) -> None:
        self.current = None

    def add_starting(self, driver: Driver, handlers: list[int]) -> None:
        self.current = driver
        self.starting = driver
        self.handlers[driver] = handlers
        self._emit_live_ids()

    def start_new(
        self,
        make_driver: Callable[[], Driver],
        connect_handlers: Callable[[Driver], list[int]],
    ) -> Driver:
        """Create, wire, register as starting, and start a new driver.

        If ``driver.start()`` raises, the half-registered driver is forgotten
        and the original exception is re-raised for the UI layer to present.
        Driver construction errors happen before registration and bubble up
        directly.
        """
        self.evict_for_cap()
        driver = make_driver()
        self.add_starting(driver, connect_handlers(driver))
        try:
            driver.start()
        except Exception:
            self.forget(driver)
            raise
        return driver

    def register_started(self, driver: Driver, session_id: str) -> None:
        if session_id:
            self.live[session_id] = driver
            if self.starting is driver:
                self.starting = None
            self._emit_live_ids()

    def disconnect_handlers(self, driver: Driver) -> None:
        for handler in self.handlers.pop(driver, []):
            try:
                driver.disconnect(handler)
            except Exception:
                pass

    def forget(self, driver: Driver) -> None:
        self.disconnect_handlers(driver)
        for sid, live_driver in list(self.live.items()):
            if live_driver is driver:
                del self.live[sid]
        if self.starting is driver:
            self.starting = None
        if self.current is driver:
            self.current = None
        self._emit_live_ids()

    def driver_for_session(self, session_id: str) -> Driver | None:
        return self.live.get(session_id)

    def driver_for_work_participant(
        self,
        work_id: str,
        provider: str,
        *,
        generation: int | None = None,
    ) -> Driver | None:
        """Find a native participant even before it reports a session id."""

        if not work_id or not provider:
            return None
        for driver in self.handlers:
            if (
                getattr(driver, "_helios_work_id", "") == work_id
                and getattr(driver, "_helios_participant_provider", "") == provider
                and (
                    generation is None
                    or getattr(driver, "_helios_participant_generation", None)
                    == generation
                )
                and driver.is_accepting_input
            ):
                return driver
        return None

    def drivers_for_work(self, work_id: str) -> set[Driver]:
        """Return every live/starting driver tagged to a Work family."""

        if not work_id:
            return set()
        return {
            driver
            for driver in self.drivers_for_shutdown()
            if str(
                getattr(driver, "_helios_work_id", "")
                or getattr(driver, "_helios_work_id_hint", "")
                or ""
            )
            == work_id
        }

    def live_ids(self) -> set[str]:
        ids = set(self.live)
        if self.starting is not None and getattr(self.starting, "session_id", ""):
            ids.add(self.starting.session_id)
        return ids

    def drivers_for_shutdown(self) -> set[Driver]:
        # `handlers` also includes drivers still waiting for their native
        # session id. More than one provider can be starting concurrently, but
        # the legacy `starting` pointer tracks only the most recent one.
        drivers = set(self.handlers)
        drivers.update(self.live.values())
        if self.starting is not None:
            drivers.add(self.starting)
        return drivers

    def stop_driver(self, driver: Driver, *, interrupt: bool = True) -> None:
        try:
            driver.stop(interrupt=interrupt)
        except Exception:
            pass

    def teardown(
        self, driver: Driver | None = None, *, interrupt: bool = False
    ) -> Driver | None:
        old = driver if driver is not None else self.current
        if old is None:
            return None
        self.forget(old)
        self.stop_driver(old, interrupt=interrupt)
        return old

    def stop_all(self, *, interrupt: bool = False) -> None:
        for driver in self.drivers_for_shutdown():
            self.stop_driver(driver, interrupt=interrupt)

    def reap_idle(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for driver in list(self.live.values()):
            if (
                driver is self.current
                or driver.is_busy
                or not driver.is_accepting_input
            ):
                continue
            if now - driver.last_activity >= self.idle_seconds:
                self._info("reaping idle background driver %s", _short_id(driver))
                _end_input_quietly(driver)

    def evict_for_cap(self) -> None:
        overshoot = len(self.live) - self.max_live + 1
        if overshoot <= 0:
            return
        candidates = sorted(
            (
                driver
                for driver in self.live.values()
                if (
                    driver is not self.current
                    and not driver.is_busy
                    and driver.is_accepting_input
                )
            ),
            key=lambda driver: driver.last_activity,
        )
        for driver in candidates[:overshoot]:
            self._info(
                "evicting LRU background driver %s (cap %d)",
                _short_id(driver),
                self.max_live,
            )
            _end_input_quietly(driver)

    def _emit_live_ids(self) -> None:
        if self._on_live_ids_changed is not None:
            self._on_live_ids_changed(self.live_ids())

    def _info(self, msg: str, *args) -> None:
        if self._log is not None:
            try:
                self._log.info(msg, *args)
            except Exception:
                pass


def _short_id(driver: Driver) -> str:
    return (getattr(driver, "session_id", "") or "?")[:8]


def _end_input_quietly(driver: Driver) -> None:
    try:
        driver.end_input()
    except Exception:
        pass
