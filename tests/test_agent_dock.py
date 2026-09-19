"""Real-GTK presentation contracts for the in-conversation Agent Dock."""

from __future__ import annotations

from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

from helios.backend.agent_activity import AgentActivityModel, AgentActivityScope  # noqa: E402
from helios.widgets._motion import BASE_MS  # noqa: E402
from helios.widgets.agent_dock import AgentDock  # noqa: E402


def _scope(turn: str = "turn-one") -> AgentActivityScope:
    return AgentActivityScope("openai", "work-one", turn, "root-thread")


def _snapshot(states: dict, *, turn: str = "turn-one"):
    model = AgentActivityModel()
    scope = _scope(turn)
    model.begin_scope(scope)
    return model.observe(scope, states)


def _children(widget) -> list:
    children = []
    child = widget.get_first_child()
    while child is not None:
        children.append(child)
        child = child.get_next_sibling()
    return children


def _descendants(widget) -> list:
    found = []
    for child in _children(widget):
        found.append(child)
        found.extend(_descendants(child))
    return found


def test_dock_is_hidden_empty_and_uses_standard_motion_durations() -> None:
    dock = AgentDock()
    assert dock.get_reveal_child() is False
    minimum, natural, _minimum_baseline, _natural_baseline = dock.measure(
        Gtk.Orientation.VERTICAL,
        -1,
    )
    assert (minimum, natural) == (0, 0)
    assert dock.get_transition_duration() == BASE_MS
    assert dock._details_revealer.get_transition_duration() == BASE_MS
    assert dock._summary_label.get_transition_duration() == BASE_MS


def test_agent_dock_icons_resolve_in_baseline_adwaita() -> None:
    theme = Gtk.IconTheme.new()
    theme.set_theme_name("Adwaita")
    for icon_name in (
        "system-users-symbolic",
        "pan-down-symbolic",
        "pan-up-symbolic",
        "content-loading-symbolic",
        "media-playback-start-symbolic",
        "dialog-warning-symbolic",
        "object-select-symbolic",
        "dialog-error-symbolic",
        "media-playback-stop-symbolic",
        "view-more-symbolic",
    ):
        assert theme.has_icon(icon_name), icon_name


def test_collapsed_nodes_are_stable_capped_and_summarized_with_text() -> None:
    """Widget identity survives a status change, so the CSS transition is real.

    The fixture stays under the node cap on purpose. Above it, membership is
    ranked and a completing actor can legitimately be evicted — which is a
    different invariant, pinned by the ranked-selection tests below.
    """
    dock = AgentDock()
    states = {
        f"child-{index}": {
            "status": (
                "needsInput" if index == 5 else "completed" if index == 6 else "running"
            ),
            "name": f"Agent {index}",
        }
        for index in range(7)
    }
    first = _snapshot(states)
    dock.set_snapshot(first)

    assert dock.get_reveal_child() is True
    assert len(dock._nodes) == 7
    assert dock._overflow.get_visible() is False
    assert dock._summary_label._text == "5 working · 1 needs you · 1 complete"
    first_node = dock._nodes["child-0"]

    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)
    model.observe(scope, states)
    updated = model.observe(scope, {"child-0": {"status": "completed"}})
    dock.set_snapshot(updated)

    assert dock._nodes["child-0"] is first_node
    assert first_node.has_css_class("helios-agent-completed")
    assert first_node.has_css_class("helios-agent-terminal-arrival")
    assert dock._summary_label._text == "4 working · 1 needs you · 2 complete"


def test_the_capped_strip_shows_the_actors_that_want_a_human() -> None:
    """The bug: first-eight-by-arrival showed the least useful eight.

    Twenty agents, a failure at #12 and a question at #17 — both landed outside
    the arrival window, so the strip was eight identical working squares and the
    only trace of either was a phrase in the summary text.
    """
    dock = AgentDock()
    states = {
        f"child-{index:02d}": {
            "status": (
                "failed" if index == 12 else "needsInput" if index == 17 else "running"
            ),
            "name": f"Agent {index}",
        }
        for index in range(20)
    }
    dock.set_snapshot(_snapshot(states))

    assert len(dock._nodes) == 8
    assert dock._overflow.get_label() == "+12"
    assert "child-12" in dock._nodes, "the failure never made it into the strip"
    assert "child-17" in dock._nodes, "the question never made it into the strip"
    assert dock._nodes["child-12"].has_css_class("helios-agent-failed")
    assert dock._nodes["child-17"].has_css_class("helios-agent-needs_input")


def test_the_capped_strip_keeps_arrival_order_left_to_right() -> None:
    """Rank picks WHICH survive; it must not reorder the survivors.

    Sorting the strip itself by rank makes squares jump sideways on every
    status change, and "the third square is my migration" is most of what makes
    eight anonymous squares readable at a glance.
    """
    dock = AgentDock()
    states = {
        f"child-{index:02d}": {
            "status": "needsInput" if index == 17 else "running",
            "name": f"Agent {index}",
        }
        for index in range(20)
    }
    dock.set_snapshot(_snapshot(states))

    order = [
        actor_id
        for actor_id, node in dock._nodes.items()
        if node in _children(dock._nodes_box)
    ]
    positions = {
        node: index for index, node in enumerate(_children(dock._nodes_box))
    }
    assert order == sorted(order), "ranked survivors were re-sorted by rank"
    assert positions[dock._nodes["child-17"]] == len(dock._nodes) - 1, (
        "the needs-you actor arrived last and must still sit last"
    )
    assert positions[dock._overflow] == len(dock._nodes), (
        "the +N label must stay at the tail after reordering"
    )


def test_an_evicted_node_does_not_replay_the_arrival_flash_when_it_returns() -> None:
    """Statuses are remembered for every actor, not just the visible ones.

    Otherwise an actor that leaves the strip while terminal and later returns
    reads as having just finished, which is a lie about when it happened.
    """
    dock = AgentDock()
    model = AgentActivityModel()
    scope = _scope()
    model.begin_scope(scope)
    states = {
        f"child-{index:02d}": {"status": "running", "name": f"Agent {index}"}
        for index in range(10)
    }
    dock.set_snapshot(model.observe(scope, states))

    # child-00 completes: it drops to the bottom of the ranking and is evicted.
    dock.set_snapshot(model.observe(scope, {"child-00": {"status": "completed"}}))
    assert "child-00" not in dock._nodes

    # Everything else finishes, so child-00 is back in the visible eight — but
    # it finished long ago and must not flash as a fresh arrival.
    dock.set_snapshot(
        model.observe(
            scope,
            {f"child-{index:02d}": {"status": "completed"} for index in range(1, 10)},
        )
    )
    assert "child-00" in dock._nodes
    assert not dock._nodes["child-00"].has_css_class("helios-agent-terminal-arrival")


def test_expand_groups_use_glyph_and_status_text_without_stop_controls() -> None:
    dock = AgentDock()
    dock.set_snapshot(
        _snapshot(
            {
                "active": {"status": "running", "name": "Builder"},
                "waiting": {"status": "needsInput", "name": "Researcher"},
                "done": {"status": "completed", "name": "Reviewer"},
            }
        )
    )
    dock._summary_button.set_active(True)

    assert dock._details_revealer.get_reveal_child() is True
    labels = [
        child.get_label()
        for child in _descendants(dock._details)
        if isinstance(child, Gtk.Label)
    ]
    for expected in (
        "Active · 1",
        "Needs you · 1",
        "Done · 1",
        "Working",
        "Needs you",
        "Complete",
    ):
        assert expected in labels
    # A separate change owns acknowledged per-actor cancellation. The only button is the
    # dock's keyboard-accessible summary toggle, outside the details surface.
    assert not [
        child for child in _descendants(dock._details) if isinstance(child, Gtk.Button)
    ]
    assert "Observed agent activity" in dock._summary_button.get_tooltip_text()


def test_unknown_status_is_observed_and_never_working_or_pulsing() -> None:
    dock = AgentDock()
    dock.set_snapshot(
        _snapshot(
            {
                "missing": {"name": "Unclassified"},
                "unrecognized": {"status": "providerMysteryState"},
                "waiting": {"status": "waiting"},
                "blocked": {"status": "blocked"},
            }
        )
    )
    dock._summary_button.set_active(True)

    assert dock._summary_label._text == "4 observed"
    assert not dock._aggregate_icon.has_css_class("helios-agent-running-pulse")
    labels = [
        child.get_label()
        for child in _descendants(dock._details)
        if isinstance(child, Gtk.Label)
    ]
    assert "Observed · 4" in labels
    assert labels.count("Observed") == 4
    assert not any("working" in label.lower() for label in labels)


def test_compact_summary_narrates_terminal_and_unknown_states_exactly() -> None:
    dock = AgentDock()
    dock.set_snapshot(
        _snapshot(
            {
                "starting": {"status": "pendingInit"},
                "running": {"status": "running"},
                "input": {"status": "pendingApproval"},
                "complete": {"status": "completed"},
                "failed": {"status": "failed"},
                "stopped": {"status": "interrupted"},
                "unknown": {"status": "providerMysteryState"},
            }
        )
    )

    exact = (
        "1 starting · 1 working · 1 needs you · 1 complete · "
        "1 error · 1 stopped · 1 observed"
    )
    assert dock._summary_label._text == exact
    assert exact in dock._summary_button.get_tooltip_text()


def test_expanded_details_are_capped_in_vertical_scroller() -> None:
    dock = AgentDock()
    dock.set_snapshot(
        _snapshot(
            {
                f"child-{index}": {"status": "running", "name": f"Agent {index}"}
                for index in range(48)
            }
        )
    )
    dock._summary_button.set_active(True)

    horizontal, vertical = dock._details_scroller.get_policy()
    assert horizontal is Gtk.PolicyType.NEVER
    assert vertical is Gtk.PolicyType.AUTOMATIC
    assert dock._details_scroller.get_max_content_height() == 240
    _minimum, natural, _minimum_baseline, _natural_baseline = (
        dock._details_scroller.measure(Gtk.Orientation.VERTICAL, -1)
    )
    assert natural <= 240


def test_new_scope_replaces_nodes_and_empty_snapshot_hides_dock() -> None:
    dock = AgentDock()
    dock.set_snapshot(_snapshot({"first": {"status": "running"}}))
    old_node = dock._nodes["first"]

    dock.set_snapshot(
        _snapshot({"second": {"status": "running"}}, turn="turn-two")
    )

    assert "first" not in dock._nodes
    assert dock._nodes["second"] is not old_node
    dock.clear()
    assert dock.get_reveal_child() is False
    assert dock._nodes == {}


def test_shutdown_refuses_late_projection() -> None:
    dock = AgentDock()
    dock.shutdown()
    dock.set_snapshot(_snapshot({"late": {"status": "running"}}))
    dock.clear()
    assert dock.get_reveal_child() is False
    assert dock._nodes == {}


def test_reduced_motion_disables_both_agent_keyframes() -> None:
    css = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "helios"
        / "resources"
        / "style"
        / "helios.css"
    ).read_text(encoding="utf-8")
    selector = (
        ".helios-window.reduced-motion .helios-agent-running-pulse,\n"
        ".helios-window.reduced-motion .helios-agent-terminal-arrival"
    )
    assert selector in css


def _spend(delegated: int, total: int):
    """A SpendSnapshot-shaped stand-in; the dock reads it duck-typed."""
    from helios.backend.process.spend_accounting import SpendSnapshot

    return SpendSnapshot(
        total_tokens=total,
        root_tokens=total - delegated,
        delegated_tokens=delegated,
        models=(),
    )


def test_spend_is_hidden_until_a_turn_actually_delegates() -> None:
    """A lone root's total is the context meter's job. Repeating it beside the
    actors would add noise to the one surface that should mean "something else
    is spending your budget"."""
    dock = AgentDock()

    dock.set_spend(_spend(0, 120_000))

    assert dock._spend_label.get_visible() is False


def test_spend_reports_delegated_tokens_and_share() -> None:
    dock = AgentDock()

    dock.set_spend(_spend(55_791, 203_975))

    assert dock._spend_label.get_visible() is True
    assert dock._spend_label.get_label() == "55.8k delegated · 27%"


def test_spend_tooltip_carries_the_exact_figures_and_names_the_other_meter() -> None:
    """The compact label is for noticing; the tooltip is for believing. It has
    to say which number this is, because the context meter sits a few pixels
    away showing a deliberately different one."""
    dock = AgentDock()

    dock.set_spend(_spend(55_791, 203_975))
    tooltip = dock._spend_label.get_tooltip_text()

    assert "55,791" in tooltip and "203,975" in tooltip
    assert "context meter" in tooltip


def test_spend_never_shows_a_dollar_figure() -> None:
    """`costUSD` is a notional API-equivalent estimate on a subscription
    account, not a charge. A precise, prominent number that is not the thing it
    appears to be is the bug this replaces, not a smaller version of it."""
    dock = AgentDock()

    dock.set_spend(_spend(55_791, 203_975))

    assert "$" not in dock._spend_label.get_label()
    assert "$" not in (dock._spend_label.get_tooltip_text() or "")


def test_clearing_the_dock_also_clears_spend() -> None:
    """Spend belongs to the actors beside it. Leaving a delegated figure on
    screen with no actors reads as a live burn that is not happening."""
    dock = AgentDock()
    dock.set_spend(_spend(55_791, 203_975))

    dock.clear()

    assert dock._spend_label.get_visible() is False


def test_large_fan_out_stays_compact() -> None:
    dock = AgentDock()

    dock.set_spend(_spend(2_400_000, 4_000_000))

    assert dock._spend_label.get_label() == "2.4M delegated · 60%"


# ── elapsed ──────────────────────────────────────────────────────────────


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _timed_snapshot(states, clock, *, model=None, scope=None):
    if model is None:
        model = AgentActivityModel(clock=clock)
        scope = _scope()
        model.begin_scope(scope)
    return model, scope, model.observe(scope, states)


def test_a_running_agent_shows_how_long_it_has_been_observed() -> None:
    """The whole point: a fan-out that feels stuck should say which one is old."""
    dock = AgentDock()
    clock = _FakeClock()
    model, scope, snapshot = _timed_snapshot(
        {"child": {"status": "running", "name": "Migrate the schema"}}, clock
    )
    dock.set_snapshot(snapshot)
    dock._summary_button.set_active(True)

    clock.advance(367)
    dock._refresh_elapsed_at(clock.now)

    labels = [
        widget.get_label()
        for widget in _descendants(dock)
        if isinstance(widget, Gtk.Label) and widget.has_css_class("helios-agent-elapsed")
    ]
    assert "6m 07s" in labels, labels


def test_a_finished_agent_stops_counting() -> None:
    dock = AgentDock()
    clock = _FakeClock()
    model, scope, _ = _timed_snapshot({"child": {"status": "running"}}, clock)
    clock.advance(12)
    dock.set_snapshot(model.observe(scope, {"child": {"status": "completed"}}))
    dock._summary_button.set_active(True)

    clock.advance(3600)
    dock._refresh_elapsed_at(clock.now)

    labels = [
        widget.get_label()
        for widget in _descendants(dock)
        if isinstance(widget, Gtk.Label) and widget.has_css_class("helios-agent-elapsed")
    ]
    assert "12s" in labels, labels


def test_the_tick_only_runs_while_something_can_still_change() -> None:
    """A wedged agent emits nothing, so the clock cannot be event-driven — but
    an all-terminal dock must not burn a wakeup a second rewriting constants."""
    dock = AgentDock()
    clock = _FakeClock()
    model, scope, snapshot = _timed_snapshot({"child": {"status": "running"}}, clock)
    dock.set_snapshot(snapshot)
    assert dock._tick_id, "no tick while an agent is still running"

    dock.set_snapshot(model.observe(scope, {"child": {"status": "completed"}}))
    assert not dock._tick_id, "the tick outlived the last non-terminal agent"


def test_shutdown_and_clear_both_stop_the_tick() -> None:
    """A timer firing into a finalizing widget on window close is the failure
    this mirrors ActivityIndicator's discipline to avoid."""
    dock = AgentDock()
    clock = _FakeClock()
    _, _, snapshot = _timed_snapshot({"child": {"status": "running"}}, clock)

    dock.set_snapshot(snapshot)
    dock.clear()
    assert not dock._tick_id

    dock.set_snapshot(snapshot)
    dock.shutdown()
    assert not dock._tick_id


def test_the_collapsed_tooltip_names_the_longest_thing_still_running() -> None:
    """Answers "which of these is old" without expanding the dock."""
    dock = AgentDock()
    clock = _FakeClock()
    model = AgentActivityModel(clock=clock)
    scope = _scope()
    model.begin_scope(scope)
    model.observe(scope, {"old": {"status": "running", "name": "Old one"}})
    clock.advance(300)
    model.observe(scope, {"new": {"status": "running", "name": "New one"}})
    dock.set_snapshot(model.snapshot())

    dock._refresh_elapsed_at(clock.now)
    tooltip = dock._summary_button.get_tooltip_text()
    assert "Longest still running: Old one · 5m 00s" in tooltip, tooltip


def test_a_terminal_agent_is_never_the_longest_running() -> None:
    dock = AgentDock()
    clock = _FakeClock()
    model = AgentActivityModel(clock=clock)
    scope = _scope()
    model.begin_scope(scope)
    model.observe(scope, {"slow": {"status": "running", "name": "Slow one"}})
    clock.advance(900)
    model.observe(scope, {"slow": {"status": "completed"}})
    model.observe(scope, {"live": {"status": "running", "name": "Live one"}})
    clock.advance(20)
    dock.set_snapshot(model.snapshot())

    dock._refresh_elapsed_at(clock.now)
    tooltip = dock._summary_button.get_tooltip_text()
    assert "Longest still running: Live one" in tooltip, tooltip


def test_a_stop_handler_puts_a_button_only_on_live_rows() -> None:
    """Per-agent stop (2026-09-02 audit, gap 9): a live actor gets a stop
    button once a handler is installed; a finished one never does, and with
    no handler the details surface stays button-free as before."""
    dock = AgentDock()
    stopped: list[str] = []
    dock.set_stop_handler(stopped.append)
    dock.set_snapshot(
        _snapshot(
            {
                "live": {"status": "working", "name": "Explorer"},
                "finished": {"status": "complete", "name": "Done"},
            }
        )
    )
    dock._summary_button.set_active(True)
    buttons = [
        child for child in _descendants(dock._details) if isinstance(child, Gtk.Button)
    ]
    assert len(buttons) == 1
    assert buttons[0].get_tooltip_text() == "Stop Explorer"
    buttons[0].emit("clicked")
    assert stopped == ["live"]

    dock.set_stop_handler(None)
    dock.set_snapshot(_snapshot({"live": {"status": "working", "name": "Explorer"}}))
    dock._summary_button.set_active(True)
    assert not [
        child for child in _descendants(dock._details) if isinstance(child, Gtk.Button)
    ]
