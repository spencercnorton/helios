"""Real-GTK contract tests for ChatToolbar's combined Execution control."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402


@pytest.fixture
def toolbar():
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        pytest.skip("GTK display unavailable")
    from helios.widgets.chat_toolbar import ChatToolbar

    widget = ChatToolbar()
    yield widget
    widget.shutdown()


def _labels(root: Gtk.Widget) -> list[str]:
    labels: list[str] = []

    def visit(widget: Gtk.Widget) -> None:
        if isinstance(widget, Gtk.Label):
            labels.append(widget.get_label())
        child = widget.get_first_child()
        while child is not None:
            visit(child)
            child = child.get_next_sibling()

    visit(root)
    return labels


def test_capsule_summary_and_popover_are_conversation_scoped(toolbar):
    assert toolbar.get_effort() == "high"
    assert toolbar.get_permission_mode() == "default"
    assert toolbar.get_workflow_mode() == "default"
    assert toolbar._workflow_label.get_label() == "Default"
    assert toolbar._effort_label.get_label() == "High"
    assert toolbar._permission_label.get_label() == "Ask"
    assert toolbar._execution_scope_label.get_label() == "Global default · Claude"
    assert "Reasoning High" in toolbar._execution_accessible_name
    assert "Workflow Default" in toolbar._execution_accessible_name
    assert "Permissions Ask" in toolbar._execution_accessible_name
    assert "Global default · Claude" in toolbar._execution_accessible_name

    popover = toolbar._execution_btn.get_popover()
    assert isinstance(popover, Gtk.Popover)
    visible_copy = " ".join(
        _labels(toolbar._execution_btn) + _labels(popover)
    ).lower()
    assert "execution" in visible_copy
    assert "reasoning" in visible_copy
    assert "workflow" in visible_copy
    assert "permissions" in visible_copy
    assert "next chat" not in visible_copy
    assert toolbar._permission_scroll.get_max_content_height() == 280
    assert toolbar._permission_scroll.get_policy()[1] == Gtk.PolicyType.AUTOMATIC


def test_compact_action_tracks_capability_and_busy_state(toolbar):
    button = toolbar._context_popover._compact_btn
    toolbar.set_compact_handler(lambda: None)
    assert not button.get_sensitive()

    toolbar.set_compact_capability(True)
    assert button.get_sensitive()

    toolbar.set_busy(True)
    assert not button.get_sensitive()
    assert "Wait for the current provider operation" in button.get_tooltip_text()

    toolbar.set_busy(False)
    assert button.get_sensitive()

    toolbar.set_compact_capability(False, "GPT conversation is not connected.")
    assert not button.get_sensitive()
    assert button.get_tooltip_text() == "GPT conversation is not connected."


def test_permission_sync_is_quiet_and_user_request_is_exactly_once(toolbar):
    requested: list[str] = []
    toolbar.connect(
        "permission-changed",
        lambda _toolbar, mode: requested.append(mode),
    )

    toolbar.set_permission_mode("plan")
    assert requested == []
    assert toolbar.get_permission_mode() == "plan"
    assert toolbar._permission_label.get_label() == "Plan"
    assert toolbar._permission_rows["plan"][1].get_opacity() == 1.0
    assert toolbar._permission_rows["default"][1].get_opacity() == 0.0

    # A click is an intent, not an optimistic commit. MainWindow/provider must
    # confirm it with set_permission_mode() after the async update succeeds.
    toolbar._permission_rows["auto"][0].emit("clicked")
    assert requested == ["auto"]
    assert toolbar.get_permission_mode() == "plan"
    assert toolbar._permission_label.get_label() == "Plan"


def test_workflow_is_quiet_capability_gated_and_independent_from_permission(toolbar):
    requested: list[str] = []
    toolbar.connect(
        "workflow-changed",
        lambda _toolbar, mode: requested.append(mode),
    )

    toolbar.set_permission_mode("auto")
    toolbar.set_workflow_options(("default", "plan"))
    toolbar.set_workflow_mode("plan")
    assert requested == []
    assert toolbar.get_workflow_mode() == "plan"
    assert toolbar.get_permission_mode() == "auto"
    assert toolbar._workflow_label.get_label() == "Plan"
    # Plan's runtime projection is read-only while the retained permission
    # choice remains Auto for when the workflow returns to Default.
    assert toolbar._permission_label.get_label() == "Plan"
    assert toolbar._permission_help.get_label() == (
        "Plan temporarily enforces Read only. Auto remains saved for Default workflow."
    )

    toolbar._workflow_rows["default"][0].emit("clicked")
    assert requested == ["default"]
    assert toolbar.get_workflow_mode() == "plan"

    toolbar.set_workflow_options(("default",))
    assert toolbar.get_workflow_mode() == "default"
    assert not toolbar._workflow_rows["plan"][0].get_visible()


def test_permission_selected_accessible_state_is_fatal_warning_clean():
    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")

    repo_src = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["G_DEBUG"] = "fatal-warnings"
    # The minimal CI image deliberately omits at-spi.  Keep this subprocess
    # focused on warnings emitted by our accessibility state updates instead
    # of aborting on GTK's expected missing-bus warning.
    env["GTK_A11Y"] = "none"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(repo_src), env.get("PYTHONPATH")))
    )

    script = """
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
assert Gtk.init_check()

from helios.widgets.chat_toolbar import ChatToolbar

toolbar = ChatToolbar()
for mode in ("plan", "acceptEdits", "auto", "default"):
    toolbar.set_permission_mode(mode)
toolbar.shutdown()
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_src.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, (
        f"child exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


def test_bypass_renders_as_bypass_for_claude(toolbar):
    toolbar.set_permission_mode("bypassPermissions")

    assert "bypassPermissions" in toolbar._permission_rows
    assert toolbar.get_permission_mode() == "bypassPermissions"
    assert toolbar._permission_label.get_label() == "Bypass"
    assert toolbar._permission_label.has_css_class("helios-execution-bypass")
    assert "Permissions Bypass" in toolbar._execution_accessible_name


def test_bypass_row_follows_provider_support(toolbar):
    """Every supported provider offers explicit full access."""
    from helios.backend import model_catalog

    bypass_row, _indicator = toolbar._permission_rows["bypassPermissions"]
    ask_row, _ask_indicator = toolbar._permission_rows["default"]

    for provider in (
        model_catalog.PROVIDER_ANTHROPIC,
        model_catalog.PROVIDER_OPENROUTER,
        model_catalog.PROVIDER_OPENAI,
    ):
        toolbar._provider_filter = provider
        toolbar._refresh_permission_rows()
        assert bypass_row.get_visible()
        assert ask_row.get_visible()



def test_permission_descriptions_refresh_when_provider_changes(toolbar):
    from helios.backend.project_perms import permission_description

    row, _indicator = toolbar._permission_rows["auto"]
    toolbar.set_permission_mode("auto")
    for provider in ("openrouter", "openai", "anthropic", "openrouter"):
        toolbar._provider_filter = provider
        toolbar._refresh_permission_rows()
        toolbar._refresh_execution_summary()
        expected = permission_description("auto", provider=provider)
        assert row._permission_description.get_label() == expected
        assert expected in row.get_tooltip_text()


def test_effort_updates_combined_summary_without_changing_permissions(toolbar):
    toolbar.set_permission_mode("acceptEdits")
    toolbar.set_effort("max")

    assert toolbar._effort_label.get_label() == "Max"
    assert toolbar._permission_label.get_label() == "Accept edits"
    assert "Reasoning Max" in toolbar._execution_accessible_name
    assert "Permissions Accept edits" in toolbar._execution_accessible_name


def test_effort_capability_only_gates_reasoning_not_permissions(toolbar):
    permission_button = toolbar._permission_rows["plan"][0]

    toolbar.set_effort_sensitive(False)

    assert toolbar._execution_btn.get_sensitive() is True
    assert toolbar._reasoning_box.get_sensitive() is False
    assert toolbar._effort_unavailable_label.get_visible() is True
    assert toolbar._effort_label.get_label() == "N/A"
    assert permission_button.get_sensitive() is True

    requested: list[str] = []
    toolbar.connect(
        "permission-changed",
        lambda _toolbar, mode: requested.append(mode),
    )
    permission_button.emit("clicked")
    assert requested == ["plan"]

    # Busy no longer gates anything: a running turn must not lock the
    # permission rows, since mid-run is exactly when you want to change course.
    # It still must not re-enable Reasoning for a model that has no efforts.
    toolbar.set_busy(True)
    assert toolbar._execution_btn.get_sensitive() is True
    assert permission_button.get_sensitive() is True
    assert toolbar._reasoning_box.get_sensitive() is False
    toolbar.set_busy(False)
    assert toolbar._execution_btn.get_sensitive() is True
    assert toolbar._reasoning_box.get_sensitive() is False


def test_pending_and_read_only_gates_are_independent(toolbar):
    toolbar.set_execution_pending(True)
    assert toolbar._execution_btn.get_sensitive() is True
    assert toolbar._reasoning_box.get_sensitive() is False
    assert all(
        not button.get_sensitive()
        for button, _indicator in toolbar._permission_rows.values()
    )
    assert toolbar._execution_spinner.get_visible() is True
    assert toolbar._execution_spinner.get_spinning() is True
    assert "Applying" in toolbar._execution_accessible_name

    toolbar.set_execution_pending(False)
    assert toolbar._execution_btn.get_sensitive() is True
    assert toolbar._execution_spinner.get_visible() is False

    toolbar.set_execution_scope("Read-only — session from shared pool")
    toolbar.set_execution_sensitive(False)
    assert toolbar._execution_btn.get_sensitive() is True
    assert toolbar._execution_state_label.get_visible() is True
    assert toolbar._execution_state_label.get_label() == "View only"
    assert toolbar._reasoning_box.get_sensitive() is False
    assert all(
        not button.get_sensitive()
        for button, _indicator in toolbar._permission_rows.values()
    )
    assert toolbar._execution_scope_label.get_label() == (
        "Read-only — session from shared pool"
    )
    assert "Read-only — session from shared pool" in (
        toolbar._execution_accessible_name
    )
    assert "Unavailable for this conversation" in (
        toolbar._execution_accessible_name
    )

    toolbar.set_execution_sensitive(True)
    assert toolbar._execution_btn.get_sensitive() is True
    assert toolbar._execution_state_label.get_visible() is False


@pytest.mark.parametrize(
    "scope",
    [
        "Live · Claude",
        "Saved · GPT",
        "Staged · Claude · view GPT",
        "Legacy safeguard · Claude",
        "Unknown provider · view only",
        "Conflict · view only",
    ],
)
def test_scope_projection_is_visible_and_accessible(toolbar, scope):
    detail = "Permissions: Saved; Reasoning: Global default"
    toolbar.set_execution_scope(scope, detail)

    assert toolbar._execution_scope_label.get_label() == scope
    assert toolbar._execution_scope_label.get_tooltip_text() == f"{scope}\n{detail}"
    assert scope in toolbar._execution_accessible_name
    assert detail in toolbar._execution_accessible_name
    assert scope in toolbar._execution_btn.get_tooltip_text()
    assert detail in toolbar._execution_btn.get_tooltip_text()


@pytest.mark.parametrize(
    "scope",
    ["Unknown provider · view only", "Conflict · view only"],
)
def test_unverified_provider_never_exposes_guessed_execution_values(toolbar, scope):
    detail = "Provider evidence is not authoritative."
    toolbar.set_execution_scope(scope, detail)
    toolbar.set_execution_sensitive(False)

    assert toolbar._execution_unverified_label.get_visible() is True
    assert toolbar._reasoning_box.get_visible() is False
    assert toolbar._permissions_head.get_visible() is False
    assert toolbar._permission_scroll.get_visible() is False
    assert "values unavailable" in toolbar._execution_btn.get_tooltip_text()
    assert "High reasoning" not in toolbar._execution_btn.get_tooltip_text()
    assert "Ask permissions" not in toolbar._execution_btn.get_tooltip_text()
    assert "values are unavailable" in toolbar._execution_accessible_name
    assert "Reasoning High" not in toolbar._execution_accessible_name
    assert "Permissions Ask" not in toolbar._execution_accessible_name


# ── reasoning slider: the glide must not change what gets sent ─────────────


def _drain_tween(toolbar) -> None:
    """Spin until the handle stops moving.

    Headless the glide is already over: Adw.Animation skips an unmapped
    widget straight to its end value, so `play()` returns FINISHED.
    """
    from gi.repository import Adw, GLib

    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + 2_000_000
    while (
        toolbar._effort_anim is not None
        and toolbar._effort_anim.get_state() == Adw.AnimationState.PLAYING
        and GLib.get_monotonic_time() < deadline
    ):
        context.iteration(False)


def test_picking_a_level_emits_it_once_not_every_stop_passed_over(toolbar):
    # The handle glides across four marks on the way from Off to Max.
    # Each one it touches is a valid level the provider would accept, so an
    # emit per intermediate position would apply four settings nobody chose.
    toolbar.set_effort("off")
    emitted: list[str] = []
    toolbar.connect("effort-changed", lambda _w, key: emitted.append(key))

    toolbar._effort_scale.emit("change-value", Gtk.ScrollType.JUMP, 5.0)
    assert emitted == ["max"]
    _drain_tween(toolbar)
    assert emitted == ["max"]


def test_effort_is_readable_while_the_handle_is_still_moving(toolbar):
    # A send during the glide must spawn with the level the user picked, not
    # the one the handle happens to be passing.
    toolbar.set_effort("off")
    toolbar._effort_scale.emit("change-value", Gtk.ScrollType.JUMP, 5.0)

    assert toolbar._effort_anim is not None  # a glide was handed to Adw
    assert toolbar.get_effort() == "max"
    _drain_tween(toolbar)
    assert toolbar.get_effort() == "max"
    assert toolbar._effort_scale.get_value() == 5.0


def test_an_abandoned_glide_leaves_the_handle_on_a_mark(toolbar):
    from gi.repository import GLib

    toolbar.set_effort("off")
    toolbar._effort_scale.emit("change-value", Gtk.ScrollType.JUMP, 4.0)
    GLib.MainContext.default().iteration(False)
    toolbar.set_effort("low")  # cancels the glide mid-flight

    assert toolbar._effort_anim is None
    assert toolbar._effort_scale.get_value() == 1.0
    assert toolbar.get_effort() == "low"


def test_switching_provider_stops_rebuilds_without_stranding_a_glide(toolbar):
    toolbar.set_effort("off")
    toolbar._effort_scale.emit("change-value", Gtk.ScrollType.JUMP, 5.0)
    effective = toolbar.set_effort_options(
        (("low", "Low"), ("medium", "Medium"), ("high", "High")),
        default_effort="medium",
    )

    assert toolbar._effort_anim is None
    assert effective == "medium"
    assert toolbar.get_effort() == "medium"


def test_effort_glide_is_an_adw_animation_on_the_shared_token(toolbar):
    """The glide is libadwaita's, driven by the frame clock, at _motion.FAST_MS.

    The hand-rolled predecessor counted steps on a 16 ms GLib timeout, so it
    stretched under load, ignored vsync, and kept ticking while the window was
    unmapped.
    """
    from gi.repository import Adw

    from helios.widgets._motion import FAST_MS

    toolbar.set_effort("off")
    toolbar._effort_scale.emit("change-value", Gtk.ScrollType.JUMP, 5.0)

    anim = toolbar._effort_anim
    assert isinstance(anim, Adw.TimedAnimation)
    assert anim.get_duration() == FAST_MS
    assert anim.get_value_to() == 5.0
    _drain_tween(toolbar)
    # Unmapped, Adw skips to the end value, so the handle is already on a mark.
    assert toolbar._effort_scale.get_value() == 5.0


def test_a_glide_in_flight_writes_no_provider_setting(toolbar):
    """Every mark the handle crosses emits value-changed and rounds to a real
    effort key. Only the level the user picked may commit — otherwise one
    provider setting is written per frame.

    Headless the animation never reaches PLAYING (an unmapped widget skips
    straight to the end), so that one state is stubbed. The guard itself is
    what runs.
    """
    from gi.repository import Adw

    toolbar.set_effort("off")
    emitted: list[str] = []
    toolbar.connect("effort-changed", lambda _w, key: emitted.append(key))

    class _StillPlaying:
        def get_state(self):
            return Adw.AnimationState.PLAYING

    toolbar._effort_anim = _StillPlaying()
    try:
        toolbar._effort_scale.set_value(3.0)  # a mark the glide passes over
        assert emitted == []
        assert toolbar.get_effort() == "off"
    finally:
        toolbar._effort_anim = None


def test_context_arc_reaches_its_target_fraction(toolbar, monkeypatch):
    """The drawn value has to actually be the animation's end value — an arc
    wired to a target it never receives just never moves.

    And it has to REPAINT. Asserting `_fraction` alone passes with
    `queue_draw()` deleted from `_set_fraction`, i.e. an arc frozen on screen
    for every user while 2052 tests stay green — the entire user-visible point
    of the animation, unpinned.
    """
    from helios.widgets._motion import SLOW_MS

    meter = toolbar._context_meter
    draws: list[int] = []
    monkeypatch.setattr(meter, "queue_draw", lambda *_a, **_k: draws.append(1))

    meter.set_usage(50, 100)

    # Unmapped, Adw writes the end value synchronously.
    assert meter._fraction == 0.5
    assert draws, "the animation target must repaint, not just store the value"
    # The duration token at the site that introduced it: test_style_tokens only
    # walks set_transition_duration and transition_duration=, so a positional
    # arg to Adw.TimedAnimation.new is invisible to it.
    assert meter._anim.get_duration() == SLOW_MS


def test_cancelling_a_glide_lands_the_handle_on_a_mark(toolbar):
    """`set_value` ignores round-digits, so a glide dropped between two marks
    leaves the handle stranded there. `skip()` jumps to the end value.

    Headless a glide finishes inside `play()`, so stage the animation parked
    mid-flight — the state an interrupted glide is really in.
    """
    from gi.repository import Adw

    from helios.widgets._motion import FAST_MS

    toolbar.set_effort("off")
    scale = toolbar._effort_scale
    toolbar._effort_anim = Adw.TimedAnimation.new(
        scale,
        0.0,
        3.0,
        FAST_MS,
        Adw.PropertyAnimationTarget.new(scale.get_adjustment(), "value"),
    )
    scale.get_adjustment().set_value(1.4)  # between two marks

    toolbar._cancel_effort_tween()

    assert scale.get_value() == 3.0
    assert toolbar._effort_anim is None
