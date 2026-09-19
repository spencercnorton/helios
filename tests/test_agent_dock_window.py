"""MainWindow adapter seam for causal Codex Agent Dock projection."""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios import main_window as main_window_module  # noqa: E402
from helios.backend.agent_activity import AgentActivityModel  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402


class _Driver:
    provider = "openai"

    def __init__(self) -> None:
        self.session_id = "root-thread"
        self._helios_work_id = "work-one"
        self.observed_agent_root_turn_id = "turn-one"
        self.snapshot = {"child": {"status": "running", "name": "Reviewer"}}

    def observed_agent_snapshot(self):
        return dict(self.snapshot)


class _Sink:
    def __init__(self) -> None:
        self.snapshots = []

    def set_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot)


class _Plan:
    def __init__(self) -> None:
        self.snapshots = []
        self.turn_starts = 0

    def show_agent_activity(self, snapshot) -> None:
        self.snapshots.append(snapshot)

    def begin_native_turn(self) -> None:
        self.turn_starts += 1


def _window(driver: _Driver):
    dock = _Sink()
    plan = _Plan()
    window = types.SimpleNamespace(
        _destroyed=False,
        _drv_is_current=lambda candidate: candidate is driver,
        _agent_activity=AgentActivityModel(),
        _agent_dock=dock,
        _plan=plan,
    )
    return window, dock, plan


def test_codex_snapshot_projects_once_to_dock_and_plan(monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "CodexAppServerDriver", _Driver)
    driver = _Driver()
    window, dock, plan = _window(driver)

    MainWindow._on_native_agents_updated(window, driver, driver.snapshot)

    latest = dock.snapshots[-1]
    assert [item.actor_id for item in latest.activities] == ["child"]
    assert plan.snapshots[-1] == latest


def test_background_driver_snapshot_never_changes_visible_dock(monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "CodexAppServerDriver", _Driver)
    current = _Driver()
    background = _Driver()
    background.session_id = "background-root"
    window, dock, plan = _window(current)

    MainWindow._on_native_agents_updated(window, background, background.snapshot)

    assert dock.snapshots == []
    assert plan.snapshots == []


def test_new_root_turn_clears_old_projection_before_activity(monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "CodexAppServerDriver", _Driver)
    driver = _Driver()
    window, dock, plan = _window(driver)
    MainWindow._on_native_agents_updated(window, driver, driver.snapshot)

    driver.observed_agent_root_turn_id = "turn-two"
    MainWindow._on_native_turn_status(
        window,
        driver,
        {"status": "inProgress", "turnId": "turn-two"},
    )

    assert plan.turn_starts == 1
    assert dock.snapshots[-1].scope.root_turn_id == "turn-two"
    assert dock.snapshots[-1].activities == ()


def test_live_driver_rebind_reprojects_only_its_current_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "CodexAppServerDriver", _Driver)
    driver = _Driver()
    window, dock, plan = _window(driver)

    MainWindow._sync_visible_agent_activity(window, driver)

    assert [item.actor_id for item in dock.snapshots[-1].activities] == ["child"]
    assert plan.snapshots[-1] == dock.snapshots[-1]


def test_missing_work_identity_hides_unowned_provider_activity(monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "CodexAppServerDriver", _Driver)
    driver = _Driver()
    window, dock, plan = _window(driver)
    MainWindow._on_native_agents_updated(window, driver, driver.snapshot)
    driver._helios_work_id = ""

    MainWindow._on_native_agents_updated(window, driver, driver.snapshot)

    assert dock.snapshots[-1].scope is None
    assert dock.snapshots[-1].activities == ()
    assert plan.snapshots[-1] == dock.snapshots[-1]


def test_a_background_fan_out_is_said_on_its_row(monkeypatch) -> None:
    """Audit gap 17: a non-visible Work's subagents used to be invisible."""
    monkeypatch.setattr(main_window_module, "CodexAppServerDriver", _Driver)
    driver = _Driver()
    window, dock, plan = _window(driver)
    pushed: list[tuple[str, str]] = []
    window._drv_is_current = lambda candidate: False
    window._push_background_activity = lambda drv, state, detail: pushed.append((state, detail))

    MainWindow._on_native_agents_updated(
        window,
        driver,
        {"a": {"status": "running"}, "b": {"status": "complete"}, "c": {"status": "starting"}},
    )

    assert pushed == [("agent", "2 subagents running")]
    assert dock.snapshots == [], "a background driver never paints the visible dock"


def test_a_finished_background_fan_out_clears_its_caption(monkeypatch) -> None:
    monkeypatch.setattr(main_window_module, "CodexAppServerDriver", _Driver)
    driver = _Driver()
    window, dock, plan = _window(driver)
    pushed: list[tuple[str, str]] = []
    window._drv_is_current = lambda candidate: False
    window._push_background_activity = lambda drv, state, detail: pushed.append((state, detail))

    MainWindow._on_native_agents_updated(window, driver, {"a": {"status": "running"}})
    MainWindow._on_native_agents_updated(window, driver, {"a": {"status": "complete"}})
    # A second all-terminal snapshot must not keep re-pushing.
    MainWindow._on_native_agents_updated(window, driver, {"a": {"status": "complete"}})

    assert pushed == [("agent", "1 subagent running"), ("idle", "")]
