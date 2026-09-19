from __future__ import annotations

from helios.backend.process.driver_manager import DriverManager


class FakeDriver:
    def __init__(
        self,
        session_id: str = "",
        *,
        busy: bool = False,
        accepting: bool = True,
        last_activity: float = 0.0,
    ) -> None:
        self.session_id = session_id
        self.is_busy = busy
        self.is_accepting_input = accepting
        self.last_activity = last_activity
        self.ended = 0
        self.started = 0
        self.stopped: list[bool] = []
        self.fail_start = False
        self._next_handler_id = 0
        self.disconnected: list[int] = []

    def start(self) -> None:
        self.started += 1
        if self.fail_start:
            raise RuntimeError("spawn failed")

    def connect(self, _signal: str) -> int:
        self._next_handler_id += 1
        return self._next_handler_id

    def end_input(self) -> None:
        self.ended += 1
        self.is_accepting_input = False

    def disconnect(self, handler: int) -> None:
        self.disconnected.append(handler)

    def stop(self, *, interrupt: bool = True) -> None:
        self.stopped.append(interrupt)


def test_register_forget_and_live_ids_callback():
    seen: list[set[str]] = []
    mgr = DriverManager(
        max_live=4,
        idle_seconds=60,
        on_live_ids_changed=lambda ids: seen.append(ids),
    )
    driver = FakeDriver("s1")

    mgr.add_starting(driver, [10, 11])
    mgr.register_started(driver, "s1")

    assert mgr.current is driver
    assert mgr.starting is None
    assert mgr.live_ids() == {"s1"}
    assert seen[-1] == {"s1"}

    mgr.forget(driver)

    assert mgr.current is None
    assert mgr.live == {}
    assert driver.disconnected == [10, 11]
    assert seen[-1] == set()


def test_idle_reap_skips_current_busy_and_non_accepting():
    current = FakeDriver("current", last_activity=0)
    busy = FakeDriver("busy", busy=True, last_activity=0)
    closed = FakeDriver("closed", accepting=False, last_activity=0)
    stale = FakeDriver("stale", last_activity=0)
    fresh = FakeDriver("fresh", last_activity=95)
    mgr = DriverManager(max_live=10, idle_seconds=60)
    mgr.current = current
    mgr.live = {
        "current": current,
        "busy": busy,
        "closed": closed,
        "stale": stale,
        "fresh": fresh,
    }

    mgr.reap_idle(now=100)

    assert stale.ended == 1
    assert current.ended == busy.ended == closed.ended == fresh.ended == 0


def test_evict_for_cap_uses_lru_idle_background_only():
    current = FakeDriver("current", last_activity=0)
    oldest = FakeDriver("oldest", last_activity=1)
    middle = FakeDriver("middle", last_activity=2)
    busy = FakeDriver("busy", busy=True, last_activity=0)
    mgr = DriverManager(max_live=3, idle_seconds=60)
    mgr.current = current
    mgr.live = {
        "current": current,
        "oldest": oldest,
        "middle": middle,
        "busy": busy,
    }

    mgr.evict_for_cap()

    assert oldest.ended == 1
    assert middle.ended == 1
    assert busy.ended == 0


def test_provider_helpers():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    claude = FakeDriver("c")
    claude.provider = "anthropic"
    gpt = FakeDriver("g")
    gpt.provider = "openai"
    unknown = FakeDriver("u")
    mgr.current = gpt

    assert mgr.driver_matches_provider(claude, "anthropic")
    assert not mgr.driver_matches_provider(claude, "openai")
    assert mgr.driver_provider(unknown) == ""
    assert not mgr.driver_matches_provider(unknown, "anthropic")
    assert mgr.current_accepting_matches_provider("openai")
    gpt.is_accepting_input = False
    assert not mgr.current_accepting_matches_provider("openai")


def test_start_new_registers_starting_and_cleans_up_on_start_failure():
    seen: list[set[str]] = []
    mgr = DriverManager(
        max_live=4,
        idle_seconds=60,
        on_live_ids_changed=lambda ids: seen.append(ids),
    )
    driver = FakeDriver("starting")
    driver.fail_start = True

    try:
        mgr.start_new(lambda: driver, lambda d: [d.connect("x")])
    except RuntimeError as e:
        assert "spawn failed" in str(e)
    else:
        raise AssertionError("start_new should re-raise start failure")

    assert driver.started == 1
    assert driver.disconnected == [1]
    assert mgr.current is None
    assert mgr.starting is None
    assert seen[-1] == set()


def test_start_new_success_returns_driver():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    driver = FakeDriver("starting")

    started = mgr.start_new(lambda: driver, lambda d: [d.connect("x")])

    assert started is driver
    assert driver.started == 1
    assert mgr.current is driver
    assert mgr.starting is driver


def test_teardown_forgets_then_stops_driver_without_interrupt_by_default():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    driver = FakeDriver("s1")
    mgr.current = driver
    mgr.live = {"s1": driver}
    mgr.handlers[driver] = [1]

    old = mgr.teardown()

    assert old is driver
    assert mgr.current is None
    assert mgr.live == {}
    assert driver.disconnected == [1]
    assert driver.stopped == [False]


def test_stop_all_includes_starting_driver():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    live = FakeDriver("live")
    starting = FakeDriver("starting")
    mgr.live = {"live": live}
    mgr.starting = starting

    mgr.stop_all(interrupt=False)

    assert live.stopped == [False]
    assert starting.stopped == [False]


def test_work_participant_lookup_finds_driver_before_native_id_arrives():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    starting = FakeDriver()
    starting._helios_work_id = "work-1"
    starting._helios_participant_provider = "openai"
    starting._helios_participant_generation = 2
    mgr.add_starting(starting, [1])

    assert mgr.driver_for_session("") is None
    assert mgr.driver_for_work_participant("work-1", "openai") is starting
    assert (
        mgr.driver_for_work_participant("work-1", "openai", generation=2)
        is starting
    )
    assert mgr.driver_for_work_participant("work-1", "openai", generation=1) is None
    assert mgr.driver_for_work_participant("work-1", "anthropic") is None


def test_work_sibling_lookup_keeps_provider_native_bindings_distinct():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    claude = FakeDriver("claude-native")
    claude.provider = "anthropic"
    claude._helios_work_id = "work-1"
    claude._helios_participant_provider = "anthropic"
    claude._helios_participant_generation = 1
    gpt = FakeDriver("gpt-native")
    gpt.provider = "openai"
    gpt._helios_work_id = "work-1"
    gpt._helios_participant_provider = "openai"
    gpt._helios_participant_generation = 1
    mgr.handlers = {claude: [1], gpt: [2]}
    mgr.live = {claude.session_id: claude, gpt.session_id: gpt}

    assert mgr.driver_for_work_participant("work-1", "anthropic") is claude
    assert mgr.driver_for_work_participant("work-1", "openai") is gpt

    mgr.bind_current(gpt)
    mgr.bind_current(claude)

    assert mgr.driver_for_session("claude-native") is claude
    assert mgr.driver_for_session("gpt-native") is gpt
    assert mgr.live_ids() == {"claude-native", "gpt-native"}


def test_shutdown_includes_all_handlers_when_multiple_providers_are_starting():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    claude = FakeDriver()
    codex = FakeDriver()
    mgr.add_starting(claude, [1])
    mgr.add_starting(codex, [2])  # legacy pointer now references only Codex

    mgr.stop_all(interrupt=False)

    assert claude.stopped == [False]
    assert codex.stopped == [False]


def test_drivers_for_work_includes_live_and_starting_siblings_only():
    mgr = DriverManager(max_live=4, idle_seconds=60)
    claude = FakeDriver("claude-native")
    claude._helios_work_id = "work-1"
    codex = FakeDriver()
    codex._helios_work_id_hint = "work-1"
    unrelated = FakeDriver("other")
    unrelated._helios_work_id = "work-2"
    mgr.handlers = {claude: [1], codex: [2], unrelated: [3]}
    mgr.live = {claude.session_id: claude, unrelated.session_id: unrelated}
    mgr.starting = codex

    assert mgr.drivers_for_work("work-1") == {claude, codex}
    assert mgr.drivers_for_work("work-2") == {unrelated}
    assert mgr.drivers_for_work("") == set()
