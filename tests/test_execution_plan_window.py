from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.main_window import MainWindow  # noqa: E402


class _PlanPane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def show_execution_plan(self, plan) -> None:
        self.calls.append(("durable", plan))

    def show_native_plan(self, payload) -> None:
        self.calls.append(("native", payload))

    def begin_native_turn(self) -> None:
        self.calls.append(("begin", None))


class _Progress:
    def __init__(self) -> None:
        self.plans = []

    def set_plan(self, plan) -> None:
        self.plans.append(plan)


def test_background_native_plan_is_persisted_without_mutating_visible_work():
    driver = object()
    durable = object()
    recorded = []
    pane = _PlanPane()
    progress = _Progress()
    window = types.SimpleNamespace(
        _destroyed=False,
        _drv_is_current=lambda _candidate: False,
        _work_coordinator=types.SimpleNamespace(
            record_execution_plan=lambda candidate, payload: (
                recorded.append((candidate, payload)),
                durable,
            )[1]
        ),
        _plan=pane,
        _plan_progress=progress,
    )
    payload = {
        "source": "turn",
        "turnId": "turn-bg",
        "plan": [{"step": "Keep working", "status": "inProgress"}],
    }

    MainWindow._on_native_plan_updated(window, driver, payload)

    assert recorded == [(driver, payload)]
    assert pane.calls == []
    assert progress.plans == []


def test_current_native_plan_updates_both_durable_surfaces():
    driver = object()
    durable = object()
    pane = _PlanPane()
    progress = _Progress()
    window = types.SimpleNamespace(
        _destroyed=False,
        _drv_is_current=lambda candidate: candidate is driver,
        _work_coordinator=types.SimpleNamespace(
            record_execution_plan=lambda _driver, _payload: durable
        ),
        _plan=pane,
        _plan_progress=progress,
    )

    MainWindow._on_native_plan_updated(
        window,
        driver,
        {
            "turnId": "turn-current",
            "plan": [{"step": "Build", "status": "inProgress"}],
        },
    )

    assert pane.calls == [("durable", durable)]
    assert progress.plans == [durable]


def test_turn_completion_never_infers_plan_completion_but_interrupt_does_persist():
    driver = object()
    durable = object()
    interrupted = []
    pane = _PlanPane()
    progress = _Progress()
    coordinator = types.SimpleNamespace(
        interrupt_execution_plan=lambda candidate, payload: (
            interrupted.append((candidate, payload)),
            durable,
        )[1]
    )
    window = types.SimpleNamespace(
        _destroyed=False,
        _drv_is_current=lambda candidate: candidate is driver,
        _work_coordinator=coordinator,
        _plan=pane,
        _plan_progress=progress,
        _begin_observed_agent_scope=lambda *_args: None,
    )

    MainWindow._on_native_turn_status(
        window,
        driver,
        {"turnId": "turn-current", "status": "completed"},
    )
    assert interrupted == []
    assert pane.calls == []

    MainWindow._on_native_turn_status(
        window,
        driver,
        {"turnId": "turn-current", "status": "interrupted"},
    )
    assert len(interrupted) == 1
    assert pane.calls == [("durable", durable)]
    assert progress.plans == [durable]


def test_selected_work_reload_projects_the_persisted_plan():
    durable = object()
    pane = _PlanPane()
    progress = _Progress()
    reads = []
    driver = types.SimpleNamespace(_helios_work_id="work-selected")
    window = types.SimpleNamespace(
        _driver=driver,
        _next_chat=None,
        _work_coordinator=types.SimpleNamespace(
            store=types.SimpleNamespace(
                get_execution_plan=lambda work_id: (
                    reads.append(work_id),
                    durable,
                )[1]
            )
        ),
        _plan=pane,
        _plan_progress=progress,
    )

    MainWindow._refresh_execution_plan(window)

    assert reads == ["work-selected"]
    assert pane.calls == [("durable", durable)]
    assert progress.plans == [durable]
