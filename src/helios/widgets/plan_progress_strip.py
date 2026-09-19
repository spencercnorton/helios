"""Persistent execution-plan progress chip above the transcript."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk, Pango  # noqa: E402

from helios.backend.execution_plan import (
    ExecutionPlan,
    STEP_BLOCKED,
    STEP_INTERRUPTED,
)
from helios.widgets._motion import BASE_MS


class PlanProgressStrip(Gtk.Revealer):
    """Show durable ``X/Y tasks complete`` state and open the Plan pane."""

    __gsignals__ = {
        "open-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(BASE_MS)
        self.set_reveal_child(False)
        self._plan: ExecutionPlan | None = None
        self._destroyed = False

        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_has_frame(False)
        button.connect("clicked", lambda *_: self.emit("open-requested"))

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("helios-execution-plan")
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(2)
        box.set_margin_bottom(4)

        icon = Gtk.Image.new_from_icon_name("checkbox-checked-symbolic")
        icon.set_pixel_size(14)
        icon.add_css_class("dim-label")
        box.append(icon)

        self._progress = Gtk.Label(xalign=0)
        self._progress.add_css_class("caption-heading")
        self._progress.add_css_class("helios-execution-plan-progress")
        box.append(self._progress)

        self._active = Gtk.Label(xalign=0)
        self._active.set_hexpand(True)
        self._active.set_ellipsize(Pango.EllipsizeMode.END)
        self._active.set_single_line_mode(True)
        self._active.add_css_class("caption")
        self._active.add_css_class("dim-label")
        box.append(self._active)

        disclosure = Gtk.Image.new_from_icon_name("go-next-symbolic")
        disclosure.set_pixel_size(12)
        disclosure.add_css_class("dim-label")
        box.append(disclosure)

        button.set_child(box)
        self.set_child(button)

    def shutdown(self) -> None:
        self._destroyed = True

    def set_plan(self, plan: ExecutionPlan | None) -> None:
        if self._destroyed:
            return
        self._plan = plan
        if plan is None:
            self._progress.set_label("")
            self._active.set_label("")
            self.set_tooltip_text(None)
            self.set_reveal_child(False)
            return

        noun = "task" if plan.total_count == 1 else "tasks"
        self._progress.set_label(
            f"{plan.completed_count}/{plan.total_count} {noun} complete"
        )
        detail = _plan_detail(plan)
        self._active.set_label(detail)
        tooltip = [self._progress.get_label()]
        if detail:
            tooltip.append(detail)
        tooltip.extend(
            f"{_status_marker(step.status)} {step.text}"
            for step in plan.steps
        )
        self.set_tooltip_text("\n".join(tooltip))
        self.set_reveal_child(True)

    def clear(self) -> None:
        self.set_plan(None)


def _plan_detail(plan: ExecutionPlan) -> str:
    if plan.status == "completed":
        return "Tasks complete · validating done condition"
    active = plan.active_step
    if active is not None:
        return active.text
    interrupted = next(
        (step for step in plan.steps if step.status == STEP_INTERRUPTED),
        None,
    )
    if interrupted is not None:
        return f"Interrupted · {interrupted.text}"
    blocked = next(
        (step for step in plan.steps if step.status == STEP_BLOCKED),
        None,
    )
    if blocked is not None:
        return f"Blocked · {blocked.text}"
    return plan.explanation


def _status_marker(status: str) -> str:
    return {
        "completed": "✓",
        "inProgress": "→",
        "blocked": "!",
        "interrupted": "■",
        "dropped": "–",
    }.get(status, "○")
