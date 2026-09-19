"""Compact Goal Mode strip and editor dialog."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk, Pango  # noqa: E402

from helios.backend import session_goals
from helios.backend.session_goals import GoalPlanItem, GoalState
from helios.widgets._motion import BASE_MS

_STATUS_CHOICES = [
    ("Active", session_goals.GOAL_ACTIVE),
    ("Paused", session_goals.GOAL_PAUSED),
    ("Complete", session_goals.GOAL_COMPLETE),
    ("Blocked", session_goals.GOAL_BLOCKED),
]

_NATIVE_STATUS_TO_HELIOS = {
    "active": session_goals.GOAL_ACTIVE,
    "paused": session_goals.GOAL_PAUSED,
    "blocked": session_goals.GOAL_BLOCKED,
    "usageLimited": session_goals.GOAL_BLOCKED,
    "budgetLimited": session_goals.GOAL_BLOCKED,
    "complete": session_goals.GOAL_COMPLETE,
}


class GoalStrip(Gtk.Revealer):
    __gsignals__ = {
        "edit-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "pause-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "resume-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "complete-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "clear-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(BASE_MS)
        self.set_reveal_child(False)

        self._goal: GoalState | None = None
        self._native_goal: dict | None = None
        self._destroyed = False

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.add_css_class("helios-goal")
        box.set_margin_start(16)
        box.set_margin_end(16)
        box.set_margin_top(6)
        box.set_margin_bottom(4)

        icon = Gtk.Image.new_from_icon_name("starred-symbolic")
        icon.set_pixel_size(14)
        icon.add_css_class("dim-label")
        box.append(icon)

        self._objective = Gtk.Label(xalign=0)
        self._objective.set_ellipsize(Pango.EllipsizeMode.END)
        self._objective.set_single_line_mode(True)
        self._objective.set_hexpand(True)
        self._objective.add_css_class("caption-heading")
        box.append(self._objective)

        self._status = Gtk.Label()
        self._status.add_css_class("helios-goal-status")
        self._status.add_css_class("caption")
        box.append(self._status)

        self._progress = Gtk.Label()
        self._progress.add_css_class("helios-goal-progress")
        self._progress.add_css_class("caption")
        box.append(self._progress)

        self._native_usage = Gtk.Label()
        self._native_usage.add_css_class("caption")
        self._native_usage.add_css_class("dim-label")
        self._native_usage.set_visible(False)
        box.append(self._native_usage)

        self._edit_btn = _icon_button("document-edit-symbolic", "Edit goal")
        self._edit_btn.connect("clicked", lambda *_: self.emit("edit-requested"))
        box.append(self._edit_btn)

        self._pause_btn = _icon_button("media-playback-pause-symbolic", "Pause goal")
        self._pause_btn.connect("clicked", self._on_pause_resume)
        box.append(self._pause_btn)

        self._complete_btn = _icon_button("object-select-symbolic", "Mark goal complete")
        self._complete_btn.connect("clicked", lambda *_: self.emit("complete-requested"))
        box.append(self._complete_btn)

        self._clear_btn = _icon_button("edit-clear-symbolic", "Clear goal")
        self._clear_btn.connect("clicked", lambda *_: self.emit("clear-requested"))
        box.append(self._clear_btn)

        self.set_child(box)

    def shutdown(self) -> None:
        """Refuse further mutation once the window is closing. Each public
        mutator (set_goal / set_native_goal / clear_native_goal / clear_goal)
        checks this flag at entry, so a late native goal-status event — which
        persists to disk, then updates this strip — neither writes the internal
        overlay state nor touches the finalizing widget. Idempotent."""
        self._destroyed = True

    def set_goal(self, goal: GoalState | None) -> None:
        # A native goal event can still arrive as the window closes; its ledger
        # write completes, but this widget update must not (single choke point:
        # set_native_goal/clear_native_goal/clear_goal all route through here).
        if self._destroyed:
            return
        self._goal = goal
        if goal is None or not goal.objective.strip():
            self._native_goal = None
            self._native_usage.set_visible(False)
            self.set_reveal_child(False)
            return
        native = self._native_goal
        if native is not None:
            native_objective = str(native.get("objective") or "").strip()
            native_status = str(native.get("status") or "")
            mapped_status = _NATIVE_STATUS_TO_HELIOS.get(native_status)
            if (
                (native_objective and native_objective != goal.objective.strip())
                or (mapped_status is not None and mapped_status != goal.status)
            ):
                self._native_goal = None
                native = None
        self._objective.set_label(goal.objective)
        self._objective.set_tooltip_text(goal.objective)
        if native is not None and native.get("status"):
            self._status.set_label(str(native["status"]))
            self._status.set_tooltip_text("Native Codex goal status")
        else:
            self._status.set_label(_status_label(goal.status))
            self._status.set_tooltip_text(None)
        done, total = session_goals.progress_counts(goal)
        self._progress.set_label(f"{done}/{total}" if total else "0/0")
        self._render_native_usage(native)
        if goal.status == session_goals.GOAL_ACTIVE:
            self._pause_btn.set_icon_name("media-playback-pause-symbolic")
            self._pause_btn.set_tooltip_text("Pause goal")
        else:
            self._pause_btn.set_icon_name("media-playback-start-symbolic")
            self._pause_btn.set_tooltip_text("Resume goal")
        self._complete_btn.set_sensitive(goal.status != session_goals.GOAL_COMPLETE)
        self.set_reveal_child(True)

    def clear_goal(self) -> None:
        self.set_goal(None)

    def set_native_goal(self, payload: dict | None) -> None:
        """Overlay exact provider-native goal status and usage counters."""

        if self._destroyed:
            return
        goal = payload.get("goal") if isinstance(payload, dict) else None
        self._native_goal = dict(goal) if isinstance(goal, dict) else None
        self.set_goal(self._goal)

    def clear_native_goal(self) -> None:
        if self._destroyed:
            return
        self._native_goal = None
        self.set_goal(self._goal)

    def _render_native_usage(self, native: dict | None) -> None:
        if native is None:
            self._native_usage.set_label("")
            self._native_usage.set_tooltip_text(None)
            self._native_usage.set_visible(False)
            return
        tokens_used = _nonnegative_int(native.get("tokensUsed"))
        token_budget = _optional_nonnegative_int(native.get("tokenBudget"))
        time_used = _nonnegative_int(native.get("timeUsedSeconds"))
        token_text = (
            f"{tokens_used:,}/{token_budget:,} tokens"
            if token_budget is not None
            else f"{tokens_used:,} tokens"
        )
        self._native_usage.set_label(f"{token_text} · {time_used:,}s")
        self._native_usage.set_tooltip_text(
            f"tokensUsed={tokens_used:,} · "
            f"tokenBudget={token_budget:,} · "
            f"timeUsedSeconds={time_used:,}"
            if token_budget is not None
            else (
                f"tokensUsed={tokens_used:,} · tokenBudget=none · "
                f"timeUsedSeconds={time_used:,}"
            )
        )
        self._native_usage.set_visible(True)

    def _on_pause_resume(self, *_args) -> None:
        if self._goal is not None and self._goal.status == session_goals.GOAL_ACTIVE:
            self.emit("pause-requested")
        else:
            self.emit("resume-requested")


def present_goal_dialog(
    parent: Gtk.Widget,
    existing: GoalState | None,
    on_save: Callable[[GoalState], None],
) -> None:
    dialog = Adw.AlertDialog.new(
        "Set a goal",
        "Pin an objective for this conversation. Helios shows it above the "
        "transcript with live progress, and shares it with the other model "
        "when you work in tandem.",
    )
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

    status_row = Adw.ComboRow(title="Status")
    status_row.set_model(Gtk.StringList.new([label for label, _status in _STATUS_CHOICES]))
    selected = 0
    if existing is not None:
        for i, (_label, status) in enumerate(_STATUS_CHOICES):
            if status == existing.status:
                selected = i
                break
    status_row.set_selected(selected)
    group = Adw.PreferencesGroup()
    group.add(status_row)
    box.append(group)

    objective_label = Gtk.Label(label="Objective", xalign=0)
    objective_label.add_css_class("caption")
    objective_label.add_css_class("dim-label")
    box.append(objective_label)

    objective_view = Gtk.TextView()
    objective_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    objective_view.set_top_margin(8)
    objective_view.set_bottom_margin(8)
    objective_view.set_left_margin(8)
    objective_view.set_right_margin(8)
    objective_view.set_size_request(420, -1)
    objective_view.get_buffer().set_text(existing.objective if existing else "")

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_min_content_height(110)
    scroller.set_max_content_height(220)
    scroller.set_child(objective_view)
    scroller.add_css_class("card")
    box.append(scroller)

    objective_limit = Gtk.Label(xalign=0)
    objective_limit.add_css_class("caption")
    objective_limit.add_css_class("dim-label")
    box.append(objective_limit)

    # the stopping condition. This is the field that has
    # existed in the schema since v2 with no writer anywhere, which is why
    # contract_epoch was 0 on every Work. The placeholder is a worked example
    # rather than a hint, because "definition of done" is exactly the kind of
    # label people fill in with a restatement of the objective.
    done_label = Gtk.Label(label="Done when", xalign=0)
    done_label.add_css_class("caption")
    done_label.add_css_class("dim-label")
    box.append(done_label)

    done_hint = Gtk.Label(
        label=(
            "The stopping condition. The agent is told to stop when this is "
            "satisfied and to say what blocks it rather than widening scope."
        ),
        xalign=0,
        wrap=True,
    )
    done_hint.add_css_class("caption")
    done_hint.add_css_class("dim-label")
    box.append(done_hint)

    done_view = Gtk.TextView()
    done_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    done_view.set_top_margin(8)
    done_view.set_bottom_margin(8)
    done_view.set_left_margin(8)
    done_view.set_right_margin(8)
    done_view.set_size_request(420, -1)
    done_view.get_buffer().set_text(existing.definition_of_done if existing else "")

    done_scroller = Gtk.ScrolledWindow()
    done_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    done_scroller.set_min_content_height(70)
    done_scroller.set_max_content_height(160)
    done_scroller.set_child(done_view)
    done_scroller.add_css_class("card")
    box.append(done_scroller)

    checklist_label = Gtk.Label(label="Checklist", xalign=0)
    checklist_label.add_css_class("caption")
    checklist_label.add_css_class("dim-label")
    box.append(checklist_label)

    checklist_hint = Gtk.Label(
        label=(
            "User-owned acceptance items shared with the other model in tandem. "
            "The agent's execution plan is tracked separately."
        ),
        xalign=0,
        wrap=True,
    )
    checklist_hint.add_css_class("caption")
    checklist_hint.add_css_class("dim-label")
    box.append(checklist_hint)

    items_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    item_rows: list[tuple[Gtk.CheckButton, Gtk.Entry, str, str]] = []

    def _add_item_row(text: str = "", status: str = session_goals.PLAN_PENDING) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        check = Gtk.CheckButton()
        check.set_active(status == session_goals.PLAN_COMPLETED)
        check.set_valign(Gtk.Align.CENTER)
        row.append(check)
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_text(text)
        entry.set_placeholder_text("Subtask…")
        row.append(entry)
        remove = Gtk.Button.new_from_icon_name("list-remove-symbolic")
        remove.add_css_class("flat")
        remove.set_valign(Gtk.Align.CENTER)
        remove.set_tooltip_text("Remove subtask")
        record = (check, entry, status, text)

        def _reflect(*_a) -> None:
            if check.get_active():
                entry.add_css_class("dim-label")
            else:
                entry.remove_css_class("dim-label")

        check.connect("toggled", _reflect)
        _reflect()

        def _remove(*_a) -> None:
            items_box.remove(row)
            if record in item_rows:
                item_rows.remove(record)

        remove.connect("clicked", _remove)
        row.append(remove)
        items_box.append(row)
        item_rows.append(record)

    if existing is not None:
        for item in existing.items:
            _add_item_row(item.text, item.status)

    items_scroller = Gtk.ScrolledWindow()
    items_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    items_scroller.set_max_content_height(180)
    items_scroller.set_propagate_natural_height(True)
    items_scroller.set_child(items_box)
    box.append(items_scroller)

    add_btn = Gtk.Button(label="Add subtask")
    add_btn.add_css_class("flat")
    add_btn.set_halign(Gtk.Align.START)
    add_btn.connect("clicked", lambda *_: _add_item_row())
    box.append(add_btn)

    def _collect_items() -> list[GoalPlanItem]:
        collected: list[GoalPlanItem] = []
        for check, entry, orig_status, orig_text in item_rows:
            text = entry.get_text().strip()
            if not text:
                continue
            # Inherit an agent-set in_progress/blocked status only if the
            # row text is unchanged; an edited row is a different task.
            original = (
                orig_status if text == orig_text else session_goals.PLAN_PENDING
            )
            status = session_goals.item_status_for(
                checked=check.get_active(), original=original
            )
            collected.append(GoalPlanItem(text=text, status=status))
        return collected

    dialog.set_extra_child(box)

    def _objective_text() -> str:
        buf = objective_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

    def _sync_save_enabled(*_args) -> None:
        objective = _objective_text()
        error = session_goals.objective_validation_error(objective)
        dialog.set_response_enabled("save", not error)
        if error and objective:
            objective_limit.set_label(error)
            objective_limit.add_css_class("error")
        else:
            objective_limit.set_label(
                f"{len(objective):,}/{session_goals.MAX_GOAL_OBJECTIVE_CHARS:,}"
            )
            objective_limit.remove_css_class("error")

    objective_view.get_buffer().connect("changed", _sync_save_enabled)
    _sync_save_enabled()

    def _on_response(_dialog, response: str) -> None:
        if response != "save":
            return
        objective = _objective_text()
        if session_goals.objective_validation_error(objective):
            return
        status_index = min(status_row.get_selected(), len(_STATUS_CHOICES) - 1)
        status = _STATUS_CHOICES[status_index][1]
        done_buffer = done_view.get_buffer()
        definition_of_done = done_buffer.get_text(
            done_buffer.get_start_iter(), done_buffer.get_end_iter(), False
        ).strip()
        saved = GoalState(
            objective=objective,
            definition_of_done=definition_of_done,
            status=status,
            items=_collect_items(),
            cwd=existing.cwd if existing is not None else "",
            provider=existing.provider if existing is not None else "",
            created_at=existing.created_at if existing is not None else "",
            updated_at=existing.updated_at if existing is not None else "",
        )
        on_save(saved)

    dialog.connect("response", _on_response)
    dialog.present(parent)


def _icon_button(icon_name: str, tooltip: str) -> Gtk.Button:
    btn = Gtk.Button.new_from_icon_name(icon_name)
    btn.add_css_class("flat")
    btn.add_css_class("helios-goal-action")
    btn.set_tooltip_text(tooltip)
    return btn


def _status_label(status: str) -> str:
    return status.replace("_", " ").title()


def _nonnegative_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_nonnegative_int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
