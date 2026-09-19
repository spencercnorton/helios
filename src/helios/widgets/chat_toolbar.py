"""Chat toolbar — sits above the composer. Model picker, effort/thinking
slider, and a circular context-window meter."""

from __future__ import annotations

import math
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk, Pango  # noqa: E402

from helios.backend import context_breakdown, model_catalog, ui_state
from helios.backend.model_catalog import ModelEntry
from helios.backend.project_perms import (
    AUTONOMY_MODE,
    PERMISSION_MODE_DESCRIPTORS,
    SAFE_FALLBACK_MODE,
    permission_description,
    provider_allows_mode,
)
from helios.backend.workflow_modes import (
    DEFAULT_WORKFLOW_MODE,
    PLAN_WORKFLOW_MODE,
    WORKFLOW_MODE_DESCRIPTORS,
    WORKFLOW_MODES,
    canonical_workflow_mode,
)
from helios.widgets._motion import FAST_MS, SLOW_MS


# The picker is populated dynamically from helios.backend.model_catalog:
# Anthropic rows come from scanning the installed claude binary (new models
# and even new FAMILIES appear when the CLI updates — no more hand-patching
# this file every release), OpenAI rows from Codex App Server once authenticated
# in Settings → Providers. The fallback below only covers the moment before
# the first catalog load lands (and pathological no-binary setups).
MODEL_CHOICES: list[tuple[str, str, str]] = [
    (e.id, e.label, e.group) for e in model_catalog.FALLBACK_ANTHROPIC
]

# Snap stops for the effort/thinking-budget slider.
# Each entry: (key, label, description)
#   key  — string emitted via the effort-changed signal and stored in ui-state
#   label — short label for the button chip and slider mark
#   description — tooltip / value-area text
EFFORT_STOPS: list[tuple[str, str, str]] = [
    ("off",       "Off",       "off — no extended thinking"),
    ("low",       "Low",       "low"),
    ("medium",    "Medium",    "medium"),
    ("high",      "High",      "high"),
    ("xhigh",     "X-High",    "x-high"),
    ("max",       "Max",       "max — the deepest reasoning the model offers. Slowest, highest token use."),
]
DEFAULT_EFFORT = "high"
EFFORT_ICON_NAME = "power-profile-performance-symbolic"

# The accent flash on the collapsed chip. (The handle-glide duration is
# _motion.FAST_MS — one token for every Helios micro-transition.)
_EFFORT_BUMP_MS = 420

#: libadwaita's default *blue* standalone accent, light and dark. Only used when
#: the accent API is missing — see _accent_rgb.
_FALLBACK_ACCENT_RGB = {False: (0.110, 0.443, 0.847), True: (0.471, 0.682, 0.929)}


def _accent_rgb(is_dark: bool) -> tuple[float, float, float]:
    """The system accent as a Cairo RGB triple, for hand-drawn widgets.

    Cairo cannot read `@accent_color`, so anything drawn with a DrawingArea has
    to fetch the accent itself or it drifts out of step with the stylesheet —
    which is exactly what happened here: this function replaced a hardcoded
    terracotta that no longer matched anything in helios.css.

    Version-guarded because the accent API arrived in libadwaita 1.6 and the
    `gtk_tests` CI lane runs ubuntu:24.04, which ships 1.5.0 with no accent
    support whatsoever (`dir(Adw)` has no `accent_color_to_standalone_rgba`).
    An unguarded call is an AttributeError inside a draw handler, where
    PyGObject swallows it at the C boundary and the ring silently stops
    painting. Falls back to Adwaita blue, the accent 1.5.0 always had.
    """
    to_standalone = getattr(Adw, "accent_color_to_standalone_rgba", None)
    if to_standalone is None:
        return _FALLBACK_ACCENT_RGB[bool(is_dark)]
    sm = Adw.StyleManager.get_default()
    rgba = to_standalone(sm.get_accent_color(), bool(is_dark))
    return (rgba.red, rgba.green, rgba.blue)

# Above this many models the picker grows a search field. The
# OpenRouter catalog is several hundred entries; Claude and GPT alone
# are a handful, where a filter box would be clutter.
_MODEL_SEARCH_THRESHOLD = 25

# Anthropic groups that hold the aliases you actually want to pick — the ones
# that always mean "newest of this family". Everything else the binary scan
# finds is a pinned older version, which belongs behind the disclosure.
_ANTHROPIC_CURRENT_GROUPS = ("Latest", "Other")

# A provider whose catalog is already curated (Codex App Server returns a
# handful of agent models) shows this many rows before the rest fold away.
_CURRENT_ROWS_CAP = 8

_EFFORT_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "X-High",
    "max": "Max",
    "ultra": "Ultra",
}

_PERMISSION_BY_KEY = {
    descriptor.key: descriptor for descriptor in PERMISSION_MODE_DESCRIPTORS
}
_WORKFLOW_BY_KEY = {
    descriptor.key: descriptor for descriptor in WORKFLOW_MODE_DESCRIPTORS
}

# App default model. The `fable` alias always resolves to the latest Fable
# (the current top-of-the-line family), and `[1m]` selects the
# 1-million-token context variant — so new chats default to "latest Fable,
# 1M context" and keep doing so as new versions ship, with no code change.
DEFAULT_MODEL = "fable[1m]"


def context_window_for_model(model: str) -> int:
    """Best-effort context-window size (tokens) for a model alias, used to size
    the meter for the *selected* model before a turn reports real usage.
    Delegates to the catalog, which also knows the OpenAI families. A live
    turn's `usage-updated` later confirms the exact window from the server."""
    return model_catalog.context_window_for(model, default_anthropic=DEFAULT_MODEL)


def _format_window(total: int) -> str:
    """`1000000 -> '1M'`, `200000 -> '200k'`."""
    if total >= 1_000_000:
        v = total / 1_000_000
        return f"{v:g}M"
    return f"{total // 1000}k"


class ChatToolbar(Gtk.Box):
    """Lives above the composer.

    Signals:
      model-changed(str)       -- new model alias (empty string = "default")
      effort-changed(str)      -- effort key: "off"|"low"|"medium"|"high"|"xhigh"|"max"
      permission-changed(str)  -- requested mode; owner commits via set_permission_mode()
      workflow-changed(str)    -- requested native workflow; owner confirms it
    """

    __gsignals__ = {
        "model-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "effort-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "permission-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "workflow-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._destroyed = False
        self.add_css_class("helios-chat-toolbar")
        self.set_margin_start(16)
        self.set_margin_end(16)
        self.set_margin_top(4)
        self.set_margin_bottom(2)

        # --- Left: model picker ----------------------------------------
        self._model_alias = DEFAULT_MODEL
        self._provider_filter = model_catalog.provider_for(DEFAULT_MODEL)
        # Seeded with the fallback; MainWindow swaps in the discovered
        # catalog (claude binary scan + OpenAI fetch) via set_choices().
        self._choices: list[ModelEntry] = [
            ModelEntry(alias, label, group) for alias, label, group in MODEL_CHOICES
        ]
        self._model_btn = Gtk.MenuButton()
        self._model_btn.add_css_class("flat")
        self._model_btn.set_tooltip_text("Model")
        model_label = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        icon.set_pixel_size(14)
        icon.add_css_class("dim-label")
        self._model_label = Gtk.Label(label="Default")
        self._model_label.add_css_class("caption-heading")
        model_label.append(icon)
        model_label.append(self._model_label)
        chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        chevron.set_pixel_size(12)
        chevron.add_css_class("dim-label")
        model_label.append(chevron)
        self._model_btn.set_child(model_label)
        self._build_model_popover()
        self.append(self._model_btn)
        # Sync the button label to the default model.
        self.set_model(DEFAULT_MODEL)

        # Separator dot
        dot = Gtk.Label(label="·")
        dot.add_css_class("dim-label")
        self.append(dot)

        # --- Middle: one Execution control (reasoning + permissions) ---
        #
        # These are two axes of the selected conversation's execution policy,
        # so keep them in one compact capsule rather than splitting effort by
        # the composer and permissions into a future-oriented header badge.
        self._effort_stops = list(EFFORT_STOPS)
        self._selected_effort_key = DEFAULT_EFFORT
        self._permission_mode = SAFE_FALLBACK_MODE
        self._workflow_mode = DEFAULT_WORKFLOW_MODE
        self._workflow_options = (DEFAULT_WORKFLOW_MODE,)
        self._execution_scope = "Global default · Claude"
        self._execution_scope_detail = ""
        self._execution_busy = False
        self._execution_pending = False
        self._execution_external_sensitive = True
        self._effort_provider_ok = True
        # One glide animation, retargeted in flight; dropped (and rebuilt on
        # the next glide) whenever the popover replaces its Gtk.Scale.
        self._effort_anim: Adw.TimedAnimation | None = None

        self._execution_btn = Gtk.MenuButton()
        self._execution_btn.add_css_class("flat")
        self._execution_btn.add_css_class("helios-execution-button")

        execution_btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
        )
        self._workflow_label = Gtk.Label(label="Default")
        self._workflow_label.add_css_class("caption-heading")
        self._workflow_label.set_width_chars(7)
        self._workflow_label.set_max_width_chars(7)
        self._workflow_label.set_xalign(0.0)
        execution_btn_box.append(self._workflow_label)

        workflow_divider = Gtk.Label(label="·")
        workflow_divider.add_css_class("dim-label")
        workflow_divider.add_css_class("helios-execution-divider")
        execution_btn_box.append(workflow_divider)

        self._effort_icon = Gtk.Image.new_from_icon_name(EFFORT_ICON_NAME)
        self._effort_icon.set_pixel_size(14)
        self._effort_icon.add_css_class("dim-label")
        execution_btn_box.append(self._effort_icon)
        self._effort_label = Gtk.Label(label="High")
        self._effort_label.add_css_class("caption-heading")
        # Reserve constant width for the widest stop ("Medium"/"X-High", 7
        # chars) so dragging the slider never resizes the button and reflows
        # the whole toolbar. Left-aligned within the reserved box.
        self._effort_label.set_width_chars(7)
        self._effort_label.set_max_width_chars(7)
        self._effort_label.set_xalign(0.0)
        execution_btn_box.append(self._effort_label)

        execution_divider = Gtk.Label(label="·")
        execution_divider.add_css_class("dim-label")
        execution_divider.add_css_class("helios-execution-divider")
        execution_btn_box.append(execution_divider)

        self._permission_icon = Gtk.Image.new_from_icon_name(
            "security-high-symbolic"
        )
        self._permission_icon.set_pixel_size(14)
        self._permission_icon.add_css_class("dim-label")
        execution_btn_box.append(self._permission_icon)
        self._permission_label = Gtk.Label(label="Ask")
        self._permission_label.add_css_class("caption-heading")
        # "Accept edits" is the widest current permission label. Reserving its
        # width keeps mode changes from shifting the context meter/composer.
        self._permission_label.set_width_chars(12)
        self._permission_label.set_max_width_chars(12)
        self._permission_label.set_xalign(0.0)
        execution_btn_box.append(self._permission_label)
        self._execution_value_widgets = (
            self._workflow_label,
            workflow_divider,
            self._effort_icon,
            self._effort_label,
            execution_divider,
            self._permission_icon,
            self._permission_label,
        )

        self._execution_state_label = Gtk.Label(label="View only")
        self._execution_state_label.add_css_class("caption-heading")
        self._execution_state_label.add_css_class("dim-label")
        self._execution_state_label.set_visible(False)
        execution_btn_box.append(self._execution_state_label)

        self._execution_spinner = Gtk.Spinner()
        self._execution_spinner.set_size_request(14, 14)
        self._execution_spinner.set_visible(False)
        execution_btn_box.append(self._execution_spinner)

        execution_chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        execution_chevron.set_pixel_size(12)
        execution_chevron.add_css_class("dim-label")
        execution_btn_box.append(execution_chevron)
        self._execution_btn.set_child(execution_btn_box)

        # Build the shared popover. Keep `_effort_scale` and `_effort_label`
        # stable as the public compatibility seam used by the existing
        # accessibility and snap-stop tests.
        self._effort_scale = self._build_effort_popover()
        self.append(self._execution_btn)
        self._refresh_execution_summary()
        self._update_execution_sensitivity()

        # Spacer pushes the meter to the right.
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.append(spacer)

        # --- Right: context-window meter (with hover details popover) ---
        self._context_meter = _ContextMeter()
        self.append(self._context_meter)

        # The hover popover sits over the meter when the mouse enters.
        # We track hover state on BOTH the meter and the popover surface,
        # because moving the pointer from the meter down into the popover
        # to click the "Open full account usage" link would otherwise
        # leave the meter → instant popdown → link unclickable.
        #
        # Implementation: two `EventControllerMotion`s, one per widget,
        # both update `_hover_count`. On leave we schedule a 220ms grace
        # period; if `_hover_count` is still 0 at the end, popdown.
        self._context_popover = _ContextPopover()
        self._context_popover.set_parent(self._context_meter)
        self._context_popover.set_autohide(False)
        self._context_popover.set_has_arrow(True)
        self._context_popover.set_position(Gtk.PositionType.BOTTOM)

        self._hover_count = 0
        self._popdown_timer_id = 0

        meter_motion = Gtk.EventControllerMotion()
        meter_motion.connect("enter", self._on_hover_enter)
        meter_motion.connect("leave", self._on_hover_leave)
        self._context_meter.add_controller(meter_motion)

        popover_motion = Gtk.EventControllerMotion()
        popover_motion.connect("enter", self._on_hover_enter)
        popover_motion.connect("leave", self._on_hover_leave)
        self._context_popover.add_controller(popover_motion)

    # --- Public API ---------------------------------------------------

    def shutdown(self) -> None:
        """Cancel callbacks that could otherwise outlive the window."""
        if self._destroyed:
            return
        self._destroyed = True
        if self._popdown_timer_id:
            try:
                GLib.source_remove(self._popdown_timer_id)
            except Exception:
                pass
            self._popdown_timer_id = 0
        self._cancel_effort_tween()
        self._context_meter.shutdown()

    def _cancel_effort_tween(self) -> None:
        """Drop an in-flight glide — its Gtk.Scale may be about to be replaced.

        `skip()` jumps to the animation's end value, which lands the handle on
        a mark: `set_value` does not apply round-digits, so an abandoned glide
        would leave the scale resting between two.
        """
        anim = self._effort_anim
        self._effort_anim = None
        if anim is not None:
            anim.skip()

    def set_choices(self, entries: list[ModelEntry]) -> None:
        """Swap in a freshly discovered model list and rebuild the popover.
        Keeps the current selection even if it's no longer listed (claude
        still accepts it; the label just falls back to the raw id)."""
        if self._destroyed or not entries:
            return
        self._choices = list(entries)
        self._build_model_popover()
        self.set_model(self._model_alias)

    def set_model(self, alias: str) -> None:
        if self._destroyed:
            return
        self._model_alias = alias
        label = next(
            (e.label for e in self._choices if e.id == alias), alias or "Default"
        )
        self._model_label.set_label(label)
        # The picker lists one provider at a time — the one the System toggle
        # is on, which is by construction the provider of the selected model
        # (MainWindow._apply_model_choice keeps the two in lockstep). Showing
        # OpenAI and OpenRouter rows under a Claude session was never a
        # reachable choice; picking one silently switched providers.
        provider = model_catalog.provider_for(alias)
        if provider != self._provider_filter:
            self._provider_filter = provider
            self._build_model_popover()
            # Provider-scoped permission rows follow the same toggle.
            self._refresh_permission_rows()
            self._refresh_execution_summary()

    def get_model(self) -> str:
        return self._model_alias

    def get_effort(self) -> str:
        """The committed level — which is not always where the handle is.

        Read the committed key, never the scale position: during the glide to
        a newly-picked stop the handle is still in transit, and a send landing
        in that window used to spawn with the PREVIOUS effort level.
        """
        return self._selected_effort_key

    def get_permission_mode(self) -> str:
        """Return the last owner-confirmed permission mode."""

        return self._permission_mode

    def set_permission_mode(self, mode: str) -> None:
        """Quietly commit the effective permission mode shown by the control.

        User clicks emit ``permission-changed`` but deliberately do not call
        this method: the window/driver owns the async provider update and calls
        back only after it succeeds. That keeps a failed Bypass request from
        ever looking active in the UI.
        """

        if self._destroyed:
            return
        if mode not in _PERMISSION_BY_KEY:
            mode = SAFE_FALLBACK_MODE
        self._permission_mode = mode
        self._refresh_permission_rows()
        self._refresh_execution_summary()

    def get_workflow_mode(self) -> str:
        """Return the last owner-confirmed workflow mode."""

        return self._workflow_mode

    def set_workflow_mode(self, mode: str) -> None:
        """Quietly commit the selected conversation workflow."""

        if self._destroyed:
            return
        mode = canonical_workflow_mode(mode)
        if mode not in self._workflow_options:
            mode = DEFAULT_WORKFLOW_MODE
        self._workflow_mode = mode
        self._refresh_workflow_rows()
        self._refresh_execution_summary()

    def set_workflow_options(self, modes) -> str:
        """Install provider-advertised workflows and return the active mode."""

        if self._destroyed:
            return DEFAULT_WORKFLOW_MODE
        options = tuple(
            descriptor.key
            for descriptor in WORKFLOW_MODE_DESCRIPTORS
            if descriptor.key in modes
        )
        self._workflow_options = options or (DEFAULT_WORKFLOW_MODE,)
        if self._workflow_mode not in self._workflow_options:
            self._workflow_mode = DEFAULT_WORKFLOW_MODE
        self._refresh_workflow_rows()
        self._refresh_execution_summary()
        return self._workflow_mode

    def set_execution_scope(self, text: str, detail: str = "") -> None:
        """Set the visible/accessibility scope for the combined control.

        Values disclose whether settings are live, saved, staged, inherited,
        or view-only; MainWindow supplies the projection.
        """

        if self._destroyed:
            return
        self._execution_scope = str(text or "Global default · Claude")
        self._execution_scope_detail = str(detail or "").strip()
        scope_label = getattr(self, "_execution_scope_label", None)
        if scope_label is not None:
            scope_label.set_label(self._execution_scope)
            scope_tooltip = self._execution_scope
            if self._execution_scope_detail:
                scope_tooltip += f"\n{self._execution_scope_detail}"
            scope_label.set_tooltip_text(scope_tooltip)
        self._update_execution_sensitivity()
        self._refresh_execution_summary()

    def set_execution_pending(self, pending: bool) -> None:
        """Mark an owner/provider update in flight and prevent double-submit."""

        if self._destroyed:
            return
        self._execution_pending = bool(pending)
        self._execution_spinner.set_spinning(self._execution_pending)
        self._execution_spinner.set_visible(self._execution_pending)
        self._update_execution_sensitivity()
        self._refresh_execution_summary()

    def set_execution_sensitive(self, sensitive: bool) -> None:
        """Independent external gate, used for read-only/unsupported targets."""

        if self._destroyed:
            return
        self._execution_external_sensitive = bool(sensitive)
        self._update_execution_sensitivity()
        self._refresh_execution_summary()

    def set_effort(self, key: str) -> None:
        if self._destroyed:
            return
        self._cancel_effort_tween()
        idx = _index_for_key(key, self._effort_stops)
        # Programmatic provider/session restoration is UI synchronisation, not
        # a user choice.  Seed first so the value observer stays quiet.
        self._selected_effort_key = self._effort_stops[idx][0]
        self._effort_scale.set_value(idx)
        self._refresh_effort_labels(idx)

    def set_effort_options(
        self,
        efforts: tuple[tuple[str, str], ...] | list[tuple[str, str]],
        *,
        default_effort: str = "",
        selected_effort: str = "",
    ) -> str:
        """Install the selected model's advertised reasoning-effort choices.

        Returns the effective selected key so MainWindow can persist the same
        value it shows. An empty list disables the control without inventing
        unsupported options.
        """
        if self._destroyed:
            return ""
        stops = [
            (key, _EFFORT_LABELS.get(key, key.replace("_", " ").title()), desc or key)
            for key, desc in efforts
            if key
        ]
        if not stops:
            self._effort_provider_ok = False
            self._update_effort_sensitivity()
            self._refresh_execution_summary()
            return ""

        keys = {key for key, _label, _desc in stops}
        effective = selected_effort if selected_effort in keys else ""
        if not effective and default_effort in keys:
            effective = default_effort
        if not effective:
            effective = next(
                (candidate for candidate in ("medium", "high", "low") if candidate in keys),
                stops[0][0],
            )
        self._effort_stops = stops
        # Set the capability gate before rebuilding so the new summary is not
        # briefly rendered as unavailable by the previous provider's state.
        self._effort_provider_ok = True
        self._effort_scale = self._build_effort_popover(effective)
        self._update_effort_sensitivity()
        return effective

    def set_anthropic_effort_options(
        self,
        selected_effort: str = "",
        supported_levels: list[str] | None = None,
    ) -> str:
        """Restore Claude's Helios-specific effort and Ultracode choices.

        `supported_levels` is the CLI's own `supportedEffortLevels` for the
        selected model, from the `initialize` control_response. When present it
        narrows the stops to what the model actually offers instead of showing
        a fixed six every time. Empty or missing means the CLI did not say —
        fall back to the full list rather than rendering an empty slider.

        `off` is always kept: it is a HELIOS concept implemented by forcing
        max_thinking_tokens to 0, not an effortLevel the CLI accepts, so it
        will never appear in `supportedEffortLevels` and must not be filtered
        out by its absence.
        """

        if self._destroyed:
            return ""
        stops = list(EFFORT_STOPS)
        if supported_levels:
            allowed = {str(level) for level in supported_levels} | {"off"}
            narrowed = [stop for stop in stops if stop[0] in allowed]
            # Never leave the slider with nothing but "off" — that reads as a
            # broken control rather than a capability statement.
            if len([s for s in narrowed if s[0] != "off"]) >= 1:
                stops = narrowed
        self._effort_stops = stops
        effective = selected_effort if any(
            key == selected_effort for key, _label, _desc in self._effort_stops
        ) else DEFAULT_EFFORT
        if not any(key == effective for key, _l, _d in self._effort_stops):
            effective = self._effort_stops[-1][0]
        self._effort_provider_ok = True
        self._effort_scale = self._build_effort_popover(effective)
        self._update_effort_sensitivity()
        return effective

    def set_effort_sensitive(self, sensitive: bool) -> None:
        """Capability gate combined with the independent busy-state gate."""
        if self._destroyed:
            return
        self._effort_provider_ok = sensitive
        self._update_effort_sensitivity()

    def _update_effort_sensitivity(self) -> None:
        if self._destroyed:
            return
        # Provider capability gates only the Reasoning section. Permissions
        # remain usable even for a model with no advertised effort choices.
        unavailable = getattr(self, "_effort_unavailable_label", None)
        if unavailable is not None:
            unavailable.set_visible(not self._effort_provider_ok)
        if self._effort_provider_ok and hasattr(self, "_effort_scale"):
            idx = int(math.floor(self._effort_scale.get_value() + 0.5))
            idx = max(0, min(idx, len(self._effort_stops) - 1))
            self._effort_label.set_label(self._effort_stops[idx][1])
        self._update_execution_sensitivity()
        self._refresh_execution_summary()

    def _update_execution_sensitivity(self) -> None:
        if self._destroyed:
            return
        # The capsule remains inspectable even when changes are blocked: the
        # popover is where provenance and the view-only reason are disclosed.
        self._execution_btn.set_sensitive(True)
        # `_execution_busy` deliberately absent: a running turn no longer
        # blocks a settings change. `_execution_pending` still does — that is
        # an in-flight provider ACK, not a turn.
        mutations_enabled = (
            self._execution_external_sensitive
            and not self._execution_pending
        )
        reasoning_box = getattr(self, "_reasoning_box", None)
        if reasoning_box is not None:
            reasoning_box.set_sensitive(
                mutations_enabled and self._effort_provider_ok
            )
        for button, _indicator in getattr(self, "_permission_rows", {}).values():
            button.set_sensitive(mutations_enabled)
        for button, _indicator in getattr(self, "_workflow_rows", {}).values():
            button.set_sensitive(mutations_enabled)
        externally_locked = not self._execution_external_sensitive
        identity_unverified = externally_locked and (
            self._execution_scope.startswith("Unknown")
            or self._execution_scope.startswith("Conflict")
        )
        if reasoning_box is not None:
            reasoning_box.set_visible(not identity_unverified)
        for name in (
            "_workflow_box",
            "_workflow_separator",
            "_permissions_separator",
            "_permissions_head",
            "_permission_scroll",
            "_permission_help",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.set_visible(not identity_unverified)
        unverified_label = getattr(self, "_execution_unverified_label", None)
        if unverified_label is not None:
            unverified_label.set_visible(identity_unverified)
        for widget in getattr(self, "_execution_value_widgets", ()):
            widget.set_visible(not externally_locked)
        state_label = getattr(self, "_execution_state_label", None)
        if state_label is not None:
            state = (
                "Conflict"
                if self._execution_scope.startswith("Conflict")
                else "Unknown"
                if self._execution_scope.startswith("Unknown")
                else "View only"
            )
            state_label.set_label(state)
            state_label.set_visible(externally_locked)

    def set_context_usage(self, used: int, total: int) -> None:
        if self._destroyed:
            return
        self._context_meter.set_usage(used, total)
        self._context_popover.set_usage(used, total)

    def set_context_breakdown(self, breakdown) -> None:
        """Per-category split of the usage already shown (see
        helios.backend.context_breakdown). None clears it."""
        if self._destroyed:
            return
        self._context_popover.set_breakdown(breakdown)

    def set_measured_breakdown(self, payload: dict) -> None:
        """The provider's own per-category split, which outranks the estimate.

        `_ContextPopover` has owned this since v0.69.0; the delegator did not,
        so every `get_context_usage` answer raised AttributeError out of
        `_on_cli_context_usage` and the meter kept showing the estimate.
        """
        if self._destroyed:
            return
        self._context_popover.set_measured_breakdown(payload)

    def set_compact_handler(self, handler) -> None:
        """Wire the popover's "Compact now" to the window."""
        if self._destroyed:
            return
        self._context_popover.set_compact_handler(handler)

    def set_compact_capability(
        self,
        supported: bool,
        unavailable_reason: str = "",
    ) -> None:
        """Project the selected provider's manual-compaction capability."""
        if self._destroyed:
            return
        self._context_popover.set_compact_capability(
            supported,
            unavailable_reason,
        )

    def note_compaction(self, trigger: str, pre_tokens: int) -> None:
        if self._destroyed:
            return
        self._context_popover.note_compaction(trigger, pre_tokens)

    def set_context_model(self, model: str) -> None:
        if self._destroyed:
            return
        self._context_popover.set_model(model)
        # Rescale the gauge to THIS model's context window immediately, so
        # switching models (or opening a fresh chat) reflects the right cap
        # even before a turn reports actual usage.
        window = context_window_for_model(model)
        self._context_meter.set_window(window)
        self._context_popover.set_window(window)

    def update_rate_limit(self, info: dict) -> None:
        if self._destroyed:
            return
        self._context_popover.update_rate_limit(info)

    def set_busy(self, busy: bool) -> None:
        """Track turn state without locking anything.

        Model, permissions and reasoning all stay mutable during a turn: every
        driver now accepts the change and applies it at the next opportunity
        (Claude over the control channel, Codex/OpenRouter on the next turn's
        params), and the model picker was already a next-chat choice that said
        so in its own toast. Greying these out was the single most-reported
        annoyance — you notice the run is going wrong precisely while it runs.
        """
        if self._destroyed:
            return
        self._execution_busy = bool(busy)
        self._context_popover.set_busy(busy)
        self._update_execution_sensitivity()
        self._refresh_execution_summary()

    # ── Hover handlers ────────────────────────────────────────────────

    _POPOVER_HOVER_GRACE_MS = 220

    def _on_hover_enter(self, *_args) -> None:
        if self._destroyed:
            return
        self._hover_count += 1
        if self._popdown_timer_id:
            try:
                GLib.source_remove(self._popdown_timer_id)
            except Exception:
                pass
            self._popdown_timer_id = 0
        if not self._context_popover.is_visible():
            self._context_popover.popup()

    def _on_hover_leave(self, *_args) -> None:
        if self._destroyed:
            return
        self._hover_count = max(0, self._hover_count - 1)
        if self._hover_count > 0:
            return
        # Don't popdown immediately — give the user time to move the
        # pointer between the meter and the popover surface.
        if self._popdown_timer_id == 0:
            self._popdown_timer_id = GLib.timeout_add(
                self._POPOVER_HOVER_GRACE_MS, self._maybe_popdown
            )

    def _maybe_popdown(self) -> bool:
        self._popdown_timer_id = 0
        if self._destroyed:
            return False
        if self._hover_count == 0:
            self._context_popover.popdown()
        return False

    # --- Internals ----------------------------------------------------

    def _build_effort_popover(self, selected_key: str = DEFAULT_EFFORT) -> Gtk.Scale:
        """Build the shared Execution popover and return its effort scale.

        The historical method name is intentionally retained: provider model
        changes rebuild the available effort marks through this seam, and
        focused tests rely on ``_effort_scale`` remaining a real Gtk.Scale.
        """

        # A rebuild replaces the Gtk.Scale a glide is driving.
        self._cancel_effort_tween()

        popover = Gtk.Popover()
        popover.add_css_class("helios-execution-popover")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_size_request(372, -1)

        # Overall heading + explicit conversation scope. Scope is visible, not
        # buried in a tooltip, but compact enough that the controls remain the
        # visual focus.
        execution_head = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        execution_title = Gtk.Label(label="Execution", xalign=0)
        execution_title.add_css_class("heading")
        execution_title.set_hexpand(True)
        execution_head.append(execution_title)
        self._execution_scope_label = Gtk.Label(
            label=self._execution_scope,
            xalign=1,
        )
        self._execution_scope_label.add_css_class("caption")
        self._execution_scope_label.add_css_class("dim-label")
        self._execution_scope_label.add_css_class("helios-execution-scope")
        self._execution_scope_label.set_max_width_chars(28)
        self._execution_scope_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._execution_scope_label.set_tooltip_text(self._execution_scope)
        execution_head.append(self._execution_scope_label)
        box.append(execution_head)

        top_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        top_sep.set_margin_top(6)
        top_sep.set_margin_bottom(6)
        box.append(top_sep)

        self._execution_unverified_label = Gtk.Label(
            label=(
                "Execution values are unavailable until provider ownership "
                "is verified."
            ),
            xalign=0,
        )
        self._execution_unverified_label.add_css_class("caption")
        self._execution_unverified_label.add_css_class("dim-label")
        self._execution_unverified_label.set_wrap(True)
        self._execution_unverified_label.set_visible(False)
        box.append(self._execution_unverified_label)

        self._workflow_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=3,
        )
        workflow_head = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        workflow_title = Gtk.Label(label="Workflow", xalign=0)
        workflow_title.add_css_class("caption-heading")
        workflow_title.set_hexpand(True)
        workflow_head.append(workflow_title)
        self._workflow_value_label = Gtk.Label(xalign=1)
        self._workflow_value_label.add_css_class("caption")
        self._workflow_value_label.add_css_class("dim-label")
        workflow_head.append(self._workflow_value_label)
        self._workflow_box.append(workflow_head)

        workflow_list = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        self._workflow_rows: dict[str, tuple[Gtk.Button, Gtk.Image]] = {}
        for descriptor in WORKFLOW_MODE_DESCRIPTORS:
            button, indicator = self._build_workflow_row(descriptor, popover)
            self._workflow_rows[descriptor.key] = (button, indicator)
            workflow_list.append(button)
        self._workflow_box.append(workflow_list)
        self._workflow_help = Gtk.Label(
            label=(
                "Workflow controls how the next response is produced. Native "
                "Plan is always read-only and stops with a reviewable plan."
            ),
            xalign=0,
        )
        self._workflow_help.add_css_class("caption")
        self._workflow_help.add_css_class("dim-label")
        self._workflow_help.set_wrap(True)
        self._workflow_box.append(self._workflow_help)
        box.append(self._workflow_box)

        workflow_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        workflow_sep.set_margin_top(8)
        workflow_sep.set_margin_bottom(6)
        self._workflow_separator = workflow_sep
        box.append(workflow_sep)

        self._reasoning_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=4,
        )

        # Reasoning section: preserve the existing snap scale, descriptions,
        # and accessible direct Gtk.Range updates.
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Reasoning", xalign=0)
        title.add_css_class("caption-heading")
        title.set_hexpand(True)
        head.append(title)
        self._effort_value_label = Gtk.Label(xalign=1)
        self._effort_value_label.add_css_class("caption")
        self._effort_value_label.add_css_class("dim-label")
        head.append(self._effort_value_label)
        self._reasoning_box.append(head)

        # Slider over the snap stops, value is the index.
        adj = Gtk.Adjustment.new(
            value=_index_for_key(selected_key, self._effort_stops),
            lower=0,
            upper=len(self._effort_stops) - 1,
            step_increment=1,
            page_increment=1,
            page_size=0,
        )
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        scale.set_round_digits(0)
        scale.set_digits(0)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        for i, (_key, label, _desc) in enumerate(self._effort_stops):
            scale.add_mark(i, Gtk.PositionType.BOTTOM, label)
        # Intercept the requested target so Gtk does not animate a click across
        # many fractional values.  Still observe value-changed separately:
        # accessibility updates call Gtk.Range.set_value() directly.
        scale.connect("change-value", self._on_effort_change_requested)
        scale.connect("value-changed", self._on_effort_value_changed)
        self._reasoning_box.append(scale)

        # Tooltip label for the currently selected stop — shows the description
        # of whichever stop is active, updated as the slider moves.
        self._effort_desc_label = Gtk.Label(xalign=0)
        self._effort_desc_label.add_css_class("caption")
        self._effort_desc_label.add_css_class("dim-label")
        self._effort_desc_label.set_wrap(True)
        self._effort_desc_label.set_valign(Gtk.Align.START)
        # Reserve height for the tallest description (the top stop wraps to
        # ~2 lines). Without this the popover grows/shrinks as you drag
        # between stops with short vs long descriptions. (Flicker fix.)
        self._effort_desc_label.set_size_request(-1, 56)
        self._effort_desc_label.set_margin_top(8)
        self._reasoning_box.append(self._effort_desc_label)

        # Causally precise copy: effort cannot change an inference already in
        # flight, but it does affect the next response in this conversation.
        help_lbl = Gtk.Label(
            label=(
                "Applies to the next response. Higher uses more deliberation "
                "and may respond more slowly."
            ),
            xalign=0,
        )
        help_lbl.add_css_class("caption")
        help_lbl.add_css_class("dim-label")
        help_lbl.set_wrap(True)
        help_lbl.set_margin_top(2)
        self._reasoning_box.append(help_lbl)

        self._effort_unavailable_label = Gtk.Label(
            label="Reasoning selection is not available for this model.",
            xalign=0,
        )
        self._effort_unavailable_label.add_css_class("caption")
        self._effort_unavailable_label.add_css_class("dim-label")
        self._effort_unavailable_label.set_wrap(True)
        self._effort_unavailable_label.set_visible(not self._effort_provider_ok)
        self._reasoning_box.append(self._effort_unavailable_label)
        self._reasoning_box.set_sensitive(self._effort_provider_ok)
        box.append(self._reasoning_box)

        permissions_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        permissions_sep.set_margin_top(8)
        permissions_sep.set_margin_bottom(6)
        self._permissions_separator = permissions_sep
        box.append(permissions_sep)

        permissions_head = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        permissions_title = Gtk.Label(label="Permissions", xalign=0)
        permissions_title.add_css_class("caption-heading")
        permissions_title.set_hexpand(True)
        permissions_head.append(permissions_title)
        self._permission_value_label = Gtk.Label(xalign=1)
        self._permission_value_label.add_css_class("caption")
        self._permission_value_label.add_css_class("dim-label")
        permissions_head.append(self._permission_value_label)
        self._permissions_head = permissions_head
        box.append(permissions_head)

        permission_list = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
        )
        permission_list.add_css_class("helios-permission-list")
        self._permission_rows: dict[str, tuple[Gtk.Button, Gtk.Image]] = {}
        for descriptor in PERMISSION_MODE_DESCRIPTORS:
            button, indicator = self._build_permission_row(
                descriptor,
                popover,
            )
            self._permission_rows[descriptor.key] = (button, indicator)
            permission_list.append(button)
        self._permission_scroll = Gtk.ScrolledWindow()
        self._permission_scroll.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self._permission_scroll.set_propagate_natural_height(True)
        self._permission_scroll.set_max_content_height(280)
        self._permission_scroll.set_child(permission_list)
        box.append(self._permission_scroll)

        permission_help = Gtk.Label(
            label=(
                "Applies only to this conversation; it does not alter other "
                "chats."
            ),
            xalign=0,
        )
        permission_help.add_css_class("caption")
        permission_help.add_css_class("dim-label")
        permission_help.set_wrap(True)
        permission_help.set_margin_top(5)
        self._permission_help = permission_help
        box.append(permission_help)

        popover.set_child(box)
        self._execution_btn.set_popover(popover)
        # Seed both value labels, row selection, and the compact capsule.
        idx = int(round(adj.get_value()))
        self._selected_effort_key = self._effort_stops[idx][0]
        self._refresh_effort_labels(idx)
        self._refresh_workflow_rows()
        self._refresh_permission_rows()
        self._refresh_execution_summary()
        return scale

    def _build_permission_row(
        self,
        descriptor,
        popover: Gtk.Popover,
    ) -> tuple[Gtk.Button, Gtk.Image]:
        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("helios-permission-option")
        button.set_has_frame(False)
        button.set_halign(Gtk.Align.FILL)
        button.set_hexpand(True)
        description_text = permission_description(descriptor.key, provider=self._provider_filter)
        button.set_tooltip_text(f"{descriptor.label}. {description_text}")
        button.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [descriptor.label, description_text],
        )
        if descriptor.key == AUTONOMY_MODE:
            button.add_css_class("helios-permission-option-bypass")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        indicator = Gtk.Image.new_from_icon_name("object-select-symbolic")
        indicator.set_pixel_size(14)
        indicator.set_valign(Gtk.Align.CENTER)
        # Opacity, rather than removing the image, preserves alignment across
        # every row while still giving the selected mode a visible checkmark.
        indicator.set_opacity(0.0)
        row.append(indicator)

        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        copy.set_hexpand(True)
        title = Gtk.Label(label=descriptor.label, xalign=0)
        title.add_css_class("caption-heading")
        copy.append(title)
        description = Gtk.Label(label=description_text, xalign=0)
        button._permission_description = description
        description.add_css_class("caption")
        description.add_css_class("dim-label")
        description.set_wrap(True)
        description.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        description.set_max_width_chars(48)
        copy.append(description)
        row.append(copy)

        if descriptor.key == AUTONOMY_MODE:
            warning = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            warning.set_pixel_size(16)
            warning.set_valign(Gtk.Align.CENTER)
            warning.add_css_class("warning")
            warning.set_tooltip_text("Unrestricted agent access")
            row.append(warning)

        button.set_child(row)
        button.connect(
            "clicked",
            self._on_permission_requested,
            descriptor.key,
            popover,
        )
        return button, indicator

    def _build_workflow_row(
        self,
        descriptor,
        popover: Gtk.Popover,
    ) -> tuple[Gtk.Button, Gtk.Image]:
        button = Gtk.Button()
        button.add_css_class("flat")
        button.add_css_class("helios-permission-option")
        button.set_has_frame(False)
        button.set_halign(Gtk.Align.FILL)
        button.set_hexpand(True)
        button.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [descriptor.label, descriptor.description],
        )

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        indicator = Gtk.Image.new_from_icon_name("object-select-symbolic")
        indicator.set_pixel_size(14)
        indicator.set_opacity(0.0)
        row.append(indicator)
        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        copy.set_hexpand(True)
        title = Gtk.Label(label=descriptor.label, xalign=0)
        title.add_css_class("caption-heading")
        copy.append(title)
        description = Gtk.Label(label=descriptor.description, xalign=0)
        description.add_css_class("caption")
        description.add_css_class("dim-label")
        description.set_wrap(True)
        description.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        description.set_max_width_chars(48)
        copy.append(description)
        row.append(copy)
        button.set_child(row)
        button.connect(
            "clicked",
            self._on_workflow_requested,
            descriptor.key,
            popover,
        )
        return button, indicator

    def _refresh_workflow_rows(self) -> None:
        rows = getattr(self, "_workflow_rows", None)
        if not rows:
            return
        descriptor = _WORKFLOW_BY_KEY[self._workflow_mode]
        value_label = getattr(self, "_workflow_value_label", None)
        if value_label is not None:
            value_label.set_label(descriptor.label)
        for mode, (button, indicator) in rows.items():
            button.set_visible(mode in self._workflow_options)
            selected = mode == self._workflow_mode
            indicator.set_opacity(1.0 if selected else 0.0)
            button.update_state(
                [Gtk.AccessibleState.SELECTED],
                [int(selected)],
            )
            if selected:
                button.add_css_class("helios-permission-option-selected")
            else:
                button.remove_css_class("helios-permission-option-selected")

    def _refresh_permission_rows(self) -> None:
        rows = getattr(self, "_permission_rows", None)
        if not rows:
            return
        for mode, (button, indicator) in rows.items():
            # Unsupported provider modes are hidden rather than
            # offered and then rejected by the driver. Same reason the model
            # picker lists one provider at a time.
            button.set_visible(provider_allows_mode(self._provider_filter, mode))
            descriptor = _PERMISSION_BY_KEY[mode]
            description = permission_description(mode, provider=self._provider_filter)
            button._permission_description.set_label(description)
            button.set_tooltip_text(f"{descriptor.label}. {description}")
            button.update_property(
                [Gtk.AccessibleProperty.DESCRIPTION], [description],
            )
            selected = mode == self._permission_mode
            indicator.set_opacity(1.0 if selected else 0.0)
            button.update_state(
                [Gtk.AccessibleState.SELECTED],
                [int(selected)],
            )
            if selected:
                button.add_css_class("helios-permission-option-selected")
            else:
                button.remove_css_class("helios-permission-option-selected")

    def _refresh_execution_summary(self) -> None:
        if self._destroyed or not hasattr(self, "_execution_btn"):
            return
        workflow = _WORKFLOW_BY_KEY.get(
            self._workflow_mode,
            _WORKFLOW_BY_KEY[DEFAULT_WORKFLOW_MODE],
        )
        self._workflow_label.set_label(workflow.label)
        descriptor = _PERMISSION_BY_KEY.get(
            "plan" if self._workflow_mode == PLAN_WORKFLOW_MODE else self._permission_mode,
            _PERMISSION_BY_KEY[SAFE_FALLBACK_MODE],
        )
        self._permission_label.set_label(descriptor.label)
        value_label = getattr(self, "_permission_value_label", None)
        if value_label is not None:
            value_label.set_label(descriptor.label)
        permission_help = getattr(self, "_permission_help", None)
        if permission_help is not None:
            if self._workflow_mode == PLAN_WORKFLOW_MODE:
                saved = _PERMISSION_BY_KEY.get(
                    self._permission_mode,
                    _PERMISSION_BY_KEY[SAFE_FALLBACK_MODE],
                )
                permission_help.set_label(
                    "Plan temporarily enforces Read only. "
                    f"{saved.label} remains saved for Default workflow."
                )
            else:
                permission_help.set_label(
                    "Applies only to this conversation; it does not alter other "
                    "chats."
                )

        bypass = descriptor.key == AUTONOMY_MODE
        self._permission_icon.set_from_icon_name(
            "channel-insecure-symbolic" if bypass else "security-high-symbolic"
        )
        if bypass:
            self._permission_label.add_css_class("helios-execution-bypass")
            if value_label is not None:
                value_label.add_css_class("helios-execution-bypass")
        else:
            self._permission_label.remove_css_class("helios-execution-bypass")
            if value_label is not None:
                value_label.remove_css_class("helios-execution-bypass")

        if not self._effort_provider_ok:
            self._effort_label.set_label("N/A")
        effort_label = self._effort_label.get_label()
        identity_unverified = not self._execution_external_sensitive and (
            self._execution_scope.startswith("Unknown")
            or self._execution_scope.startswith("Conflict")
        )
        state = ""
        if self._execution_pending:
            state = " Applying."
        elif not self._execution_external_sensitive:
            state = " Unavailable for this conversation."
        elif self._execution_busy:
            state = " Applies to the next turn."
        detail = (
            f" {self._execution_scope_detail}."
            if self._execution_scope_detail
            else ""
        )
        if identity_unverified:
            accessible = (
                "Execution settings. Workflow, reasoning and permission values are "
                f"unavailable. {self._execution_scope}.{detail}{state}"
            )
        else:
            accessible = (
                f"Execution settings. Workflow {workflow.label}. "
                f"Reasoning {effort_label}. "
                f"Permissions {descriptor.label}. {self._execution_scope}."
                f"{detail}{state}"
        )
        self._execution_accessible_name = accessible
        if identity_unverified:
            accessible_description = (
                self._execution_scope_detail
                or "Provider ownership must be verified before values are shown."
            )
        else:
            description = permission_description(descriptor.key, provider=self._provider_filter)
            accessible_description = (
                f"{description} {self._execution_scope_detail}"
                if self._execution_scope_detail
                else description
            )
        self._execution_btn.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [accessible, accessible_description],
        )
        tooltip = (
            f"Execution — values unavailable\n{self._execution_scope}"
            if identity_unverified
            else (
                f"Execution — {workflow.label} · {effort_label} reasoning · "
                f"{descriptor.label} permissions\n{self._execution_scope}"
            )
        )
        if self._execution_scope_detail:
            tooltip += f"\n{self._execution_scope_detail}"
        if self._execution_pending:
            tooltip += "\nApplying…"
        self._execution_btn.set_tooltip_text(tooltip)

    def _on_permission_requested(
        self,
        _button: Gtk.Button,
        mode: str,
        popover: Gtk.Popover,
    ) -> None:
        if (
            self._destroyed
            or self._execution_pending
            or not self._execution_external_sensitive
            or mode not in _PERMISSION_BY_KEY
        ):
            return
        popover.popdown()
        if mode == self._permission_mode:
            return
        # Intent only. MainWindow/provider owns confirmation and calls
        # set_permission_mode() after success; no optimistic Bypass display.
        self.emit("permission-changed", mode)

    def _on_workflow_requested(
        self,
        _button: Gtk.Button,
        mode: str,
        popover: Gtk.Popover,
    ) -> None:
        if (
            self._destroyed
            or self._execution_pending
            or not self._execution_external_sensitive
            or mode not in WORKFLOW_MODES
            or mode not in self._workflow_options
        ):
            return
        popover.popdown()
        if mode == self._workflow_mode:
            return
        self.emit("workflow-changed", mode)

    def _refresh_effort_labels(self, idx: int) -> None:
        idx = max(0, min(idx, len(self._effort_stops) - 1))
        key, label, desc = self._effort_stops[idx]
        self._effort_label.set_label(label)
        self._effort_value_label.set_label(key)
        # Update the description label (may not exist yet during __init__).
        desc_lbl = getattr(self, "_effort_desc_label", None)
        if desc_lbl is not None:
            desc_lbl.set_label(desc)
        # Give the top stop a distinct accent via CSS.
        for label in (self._effort_label, self._effort_value_label):
            if key == "max":
                label.add_css_class("helios-effort-max")
            else:
                label.remove_css_class("helios-effort-max")
        self._refresh_execution_summary()

    def _build_model_popover(self) -> None:
        """(Re)build the picker from self._choices — called at init and again
        whenever set_choices delivers a newer catalog."""
        popover = Gtk.Popover()
        popover.add_css_class("helios-model-popover")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_max_content_height(380)
        scroller.set_propagate_natural_height(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        self._model_rows_box = box
        self._model_popover_ref = popover
        self._fill_model_rows(box, popover, "")
        scroller.set_child(box)

        # A search field appears only when the list is long enough to need one.
        # The OpenRouter catalog is several hundred models; scrolling that many
        # buttons in a 380px viewport is not a picker, it is a haystack. Claude
        # and GPT alone are a handful of rows, where a search box is clutter.
        # Counted over the visible provider only — a big OpenRouter catalog
        # used to grow a search box on the Claude list too.
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        if len(self._provider_pool()) > _MODEL_SEARCH_THRESHOLD:
            search = Gtk.SearchEntry()
            search.set_placeholder_text("Filter models…")
            search.set_margin_top(6)
            search.set_margin_start(6)
            search.set_margin_end(6)
            search.set_margin_bottom(2)
            search.connect(
                "search-changed",
                lambda entry: self._fill_model_rows(
                    box, popover, entry.get_text() or ""
                ),
            )
            outer.append(search)
            popover.connect("show", lambda *_a: (search.set_text(""), search.grab_focus()))
        outer.append(scroller)
        popover.set_child(outer)
        self._model_btn.set_popover(popover)

    def _provider_pool(self) -> list[ModelEntry]:
        """The choices belonging to the provider the System toggle is on."""
        return [
            e
            for e in self._choices
            if getattr(e, "provider", model_catalog.PROVIDER_ANTHROPIC)
            == self._provider_filter
        ]

    def _split_by_currency(
        self, pool: list[ModelEntry]
    ) -> tuple[list[ModelEntry], list[ModelEntry]]:
        """Split into "the ones you actually pick" and "everything older".

        Claude publishes family aliases (`opus`, `fable`, `sonnet`) that always
        resolve to the newest release — those are the whole first tier, and the
        dated/pinned ids behind them are history. OpenRouter's tier is whatever
        Settings → Providers selected. OpenAI's catalog arrives pre-curated and
        short, so it only folds when it is unexpectedly long.
        """
        if self._provider_filter == model_catalog.PROVIDER_ANTHROPIC:
            current = [e for e in pool if e.group in _ANTHROPIC_CURRENT_GROUPS]
            older = [e for e in pool if e.group not in _ANTHROPIC_CURRENT_GROUPS]
        elif self._provider_filter == model_catalog.PROVIDER_OPENROUTER:
            chosen = set(ui_state.store().get(ui_state.OPENROUTER_PICKER_KEY, []) or [])
            current = [e for e in pool if e.id in chosen]
            older = [e for e in pool if e.id not in chosen]
        else:
            current, older = pool[:_CURRENT_ROWS_CAP], pool[_CURRENT_ROWS_CAP:]
        if not current:
            # Nothing configured/matched — never show an empty first tier with
            # the real list hidden behind a disclosure.
            return pool, []
        return current, older

    def _fill_model_rows(self, box, popover, query: str) -> None:
        """(Re)populate the picker rows, optionally filtered by ``query``."""
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

        needle = query.strip().lower()
        pool = [
            e for e in self._provider_pool()
            if not needle
            or needle in e.id.lower()
            or needle in e.label.lower()
            or needle in (e.group or "").lower()
        ]
        if not pool:
            empty = Gtk.Label(label="No models match", xalign=0)
            empty.add_css_class("dim-label")
            empty.set_margin_start(10)
            empty.set_margin_top(8)
            empty.set_margin_bottom(8)
            box.append(empty)
            return

        if needle:
            # Searching means the user knows what they want; tiering it would
            # hide half the hits behind a disclosure.
            self._append_model_rows(box, popover, pool)
            return

        current, older = self._split_by_currency(pool)
        self._append_model_rows(box, popover, current)
        if older:
            expander = Gtk.Expander()
            expander.set_label(f"Older models ({len(older)})")
            expander.add_css_class("helios-model-older")
            expander.set_margin_start(10)
            expander.set_margin_top(6)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            inner.set_margin_top(4)
            self._append_model_rows(inner, popover, older)
            expander.set_child(inner)
            box.append(expander)

    def _append_model_rows(self, box, popover, entries: list[ModelEntry]) -> None:
        last_group = None
        for entry in entries:
            alias, label, group = entry.id, entry.label, entry.group
            if group != last_group:
                if last_group is not None:
                    sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
                    sep.set_margin_top(4)
                    sep.set_margin_bottom(4)
                    box.append(sep)
                header = Gtk.Label(label=group, xalign=0)
                header.add_css_class("caption-heading")
                header.add_css_class("dim-label")
                header.set_margin_start(10)
                header.set_margin_top(4)
                header.set_margin_bottom(2)
                box.append(header)
                last_group = group

            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            btn.set_has_frame(False)
            btn.set_halign(Gtk.Align.FILL)
            btn.set_size_request(220, -1)
            # Make text left-aligned within the button.
            child = btn.get_first_child()
            if isinstance(child, Gtk.Label):
                child.set_xalign(0)
            details = []
            if entry.description:
                details.append(entry.description)
            if entry.reasoning_efforts:
                details.append(
                    "Reasoning: "
                    + ", ".join(key for key, _desc in entry.reasoning_efforts)
                )
            if entry.input_modalities:
                details.append("Input: " + ", ".join(entry.input_modalities))
            if details:
                btn.set_tooltip_text("\n".join(details))
            btn.connect("clicked", self._on_pick_model, alias, popover)
            box.append(btn)

    def _on_pick_model(self, _btn: Gtk.Button, alias: str, popover: Gtk.Popover) -> None:
        if self._destroyed:
            return
        self.set_model(alias)
        self.emit("model-changed", alias)
        popover.popdown()

    def _on_effort_change_requested(
        self,
        scale: Gtk.Scale,
        _scroll_type: Gtk.ScrollType,
        requested_value: float,
    ) -> bool:
        """Snap one user request and suppress Gtk's fractional animation."""
        if self._destroyed:
            return True
        # Match Gtk.Range:round-digits=0 (half-up for this non-negative range),
        # rather than Python's bankers rounding at exact mark midpoints.
        idx = int(math.floor(requested_value + 0.5))
        idx = max(0, min(idx, len(self._effort_stops) - 1))
        if abs(scale.get_value() - idx) < 0.01:
            # The synchronous value-changed callback owns labels and state.
            scale.set_value(idx)
            return True
        # Glide the handle to the chosen mark. The level itself commits NOW —
        # animating the commit would let a fast second click race the first,
        # and would emit every stop the handle passed through on the way.
        self._commit_effort(idx)
        self._start_effort_tween(scale, idx)
        return True

    def _start_effort_tween(self, scale: Gtk.Scale, target_idx: int) -> None:
        """Glide the handle to a mark, retargeting any glide already running.

        Adw.Animation runs off the frame clock and consults
        `gtk-enable-animations` itself, so there is no reduced-motion guard
        here: with animations off — or while the window is unmapped — `play()`
        writes the end value and finishes without scheduling anything.
        """
        anim = self._effort_anim
        if anim is None:
            # Gtk.Range has no "value" property; its Gtk.Adjustment does.
            # Only ever the live scale: _build_effort_popover cancels the
            # glide before it replaces one.
            anim = Adw.TimedAnimation.new(
                scale,
                0.0,
                0.0,
                FAST_MS,
                Adw.PropertyAnimationTarget.new(scale.get_adjustment(), "value"),
            )
            anim.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
            self._effort_anim = anim
        # No pause() first: play() restarts from the beginning regardless of
        # state (see the meter's _start_tween for the same note).
        anim.set_value_from(scale.get_value())
        anim.set_value_to(float(target_idx))
        anim.play()

    def _reduced_motion(self) -> bool:
        settings = Gtk.Settings.get_default()
        if settings is None:
            return False
        try:
            return not settings.get_property("gtk-enable-animations")
        except Exception:
            return False

    def _on_effort_value_changed(self, scale: Gtk.Scale) -> None:
        """Synchronise one effective stop, deduplicating fractional updates."""
        if self._destroyed:
            return
        anim = self._effort_anim
        if anim is not None and anim.get_state() == Adw.AnimationState.PLAYING:
            # Mid-glide: the handle is passing over marks it was never set to.
            # The level already committed when the click was handled. Without
            # this, every mark crossed writes a provider setting.
            return
        idx = int(math.floor(scale.get_value() + 0.5))
        idx = max(0, min(idx, len(self._effort_stops) - 1))
        self._commit_effort(idx)

    def _commit_effort(self, idx: int) -> None:
        """Adopt one stop: labels, then the outward signal if it really moved."""
        self._refresh_effort_labels(idx)
        key, _label, _desc = self._effort_stops[idx]
        if key == self._selected_effort_key:
            return
        self._selected_effort_key = key
        self._pulse_effort_label()
        self.emit("effort-changed", key)

    def _pulse_effort_label(self) -> None:
        """Brief accent flash so a level change is visible on the collapsed
        chip too — the popover is usually covering the slider itself."""
        if self._reduced_motion():
            return
        self._effort_label.add_css_class("helios-effort-bump")
        GLib.timeout_add(_EFFORT_BUMP_MS, self._clear_effort_pulse)

    def _clear_effort_pulse(self) -> bool:
        if not self._destroyed:
            self._effort_label.remove_css_class("helios-effort-bump")
        return False


def _index_for_key(
    key: str,
    stops: list[tuple[str, str, str]] | None = None,
) -> int:
    """Return the slider index for a given effort key, defaulting to 'high'."""
    choices = EFFORT_STOPS if stops is None else stops
    for i, (k, _label, _desc) in enumerate(choices):
        if k == key:
            return i
    # Fall back to high (index 3) for any unrecognised key.
    for i, (k, _label, _desc) in enumerate(choices):
        if k == "high":
            return i
    return 0


# --- Circular context-window meter -----------------------------------------


class _ContextMeter(Gtk.DrawingArea):
    """Custom Cairo-drawn arc + percentage text."""

    SIZE = 36

    def __init__(self) -> None:
        super().__init__()
        self._destroyed = False
        self.set_content_width(self.SIZE)
        self.set_content_height(self.SIZE)
        self.set_draw_func(self._draw)
        # `_fraction` is what we actually draw — animates toward `_target_fraction`.
        self._fraction = 0.0
        self._target_fraction = 0.0
        # Adw.Animation runs off the frame clock and honours
        # `gtk-enable-animations` itself — with animations off, or while the
        # widget is unmapped, play() writes the end value and finishes without
        # scheduling anything. Hence no reduced-motion guard anywhere below.
        self._anim = Adw.TimedAnimation.new(
            self,
            0.0,
            0.0,
            SLOW_MS,
            Adw.CallbackAnimationTarget.new(self._set_fraction),
        )
        self._anim.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        self._used = 0
        self._total = 0
        self._refresh_tooltip()

    def shutdown(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        # pause(), not reset(): reset() writes value_from back through the
        # target, repainting the arc during teardown.
        self._anim.pause()

    def set_usage(self, used: int, total: int) -> None:
        if self._destroyed:
            return
        self._used = used
        self._total = total
        new_fraction = max(0.0, min(1.0, (used / total) if total > 0 else 0.0))
        self._refresh_tooltip()
        # Animate from current `_fraction` to `new_fraction` so jumping from
        # 19% → 38% sweeps the arc rather than snapping.
        self._target_fraction = new_fraction
        self._start_tween()

    def set_window(self, total: int) -> None:
        """Update just the denominator (the selected model's context window)
        without changing the used-token count. Lets the gauge rescale the
        instant the model changes, before any turn reports real usage."""
        if self._destroyed or total <= 0 or total == self._total:
            return
        self._total = total
        new_fraction = max(0.0, min(1.0, (self._used / total) if total > 0 else 0.0))
        self._refresh_tooltip()
        self._target_fraction = new_fraction
        self._start_tween()

    def _set_fraction(self, value: float) -> None:
        self._fraction = value
        self.queue_draw()

    def _start_tween(self) -> None:
        if self._destroyed:
            return
        # (Re)start the ease from wherever the arc is drawn RIGHT NOW toward
        # the current target, so a new target arriving mid-flight retargets
        # smoothly over the full duration instead of snapping. That is play()'s
        # doing, not a pause() before it: adw_animation_play restarts from the
        # beginning whether the animation is playing, paused or finished
        # (resume() is the other semantic), so an explicit pause() here was a
        # no-op — removable with the suite green.
        self._anim.set_value_from(self._fraction)
        self._anim.set_value_to(self._target_fraction)
        self._anim.play()

    def _refresh_tooltip(self) -> None:
        if self._total > 0:
            # The arc deliberately animates through ``_fraction``; the
            # tooltip reports the authoritative usage immediately.  Reading
            # the animated value here made a first update to 50% say 0% until
            # the 320 ms tween happened to finish.
            fraction = max(0.0, min(1.0, self._used / self._total))
            pct = int(fraction * 100)
            self.set_tooltip_text(
                f"Context: {self._used:,} / {self._total:,} tokens ({pct}%)"
            )
        else:
            self.set_tooltip_text("Context window")

    def _draw(self, _area, cr, width: int, height: int) -> None:
        # Resolve colors from the libadwaita CSS by querying the widget style.
        sm = Adw.StyleManager.get_default()
        is_dark = sm.get_dark()
        # Track color (dimmed ring)
        track_rgb = (0.85, 0.85, 0.85) if not is_dark else (0.30, 0.30, 0.30)
        text_rgb = (0.2, 0.2, 0.2) if not is_dark else (0.85, 0.85, 0.85)

        # Pick arc color based on usage tier. The two alarm tiers stay fixed
        # (an alarm must not turn the user's accent color), the nominal tier
        # follows the system accent like the rest of the app.
        if self._fraction >= 0.95:
            arc_rgb = (0.78, 0.30, 0.30)  # error/red
        elif self._fraction >= 0.80:
            arc_rgb = (0.85, 0.55, 0.20)  # warning/amber
        else:
            arc_rgb = _accent_rgb(is_dark)

        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 3
        line_w = 3.0

        # Track ring
        cr.set_line_width(line_w)
        cr.set_source_rgb(*track_rgb)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()

        # Filled arc (start at top, go clockwise)
        if self._fraction > 0:
            start = -math.pi / 2
            end = start + 2 * math.pi * self._fraction
            cr.set_source_rgb(*arc_rgb)
            cr.set_line_cap(1)  # ROUND
            cr.arc(cx, cy, radius, start, end)
            cr.stroke()

        # Centered percentage text
        if self._total > 0:
            pct = int(self._fraction * 100)
            text = f"{pct}"
            cr.set_source_rgb(*text_rgb)
            cr.select_font_face("sans", 0, 0)
            cr.set_font_size(10)
            extents = cr.text_extents(text)
            cr.move_to(cx - extents.width / 2 - extents.x_bearing,
                      cy - extents.height / 2 - extents.y_bearing)
            cr.show_text(text)
        else:
            # Empty state — a small dash
            cr.set_source_rgb(*text_rgb)
            cr.select_font_face("sans", 0, 0)
            cr.set_font_size(10)
            extents = cr.text_extents("·")
            cr.move_to(cx - extents.width / 2 - extents.x_bearing,
                      cy - extents.height / 2 - extents.y_bearing)
            cr.show_text("·")


# ── Stacked context breakdown ─────────────────────────────────────────────

# One palette, two themes. The fixed session cost reads as neutral (it is not
# something you can act on); tool output gets the app's terracotta accent
# because it is nearly always the biggest actionable slice; the rest step
# around the wheel far enough to be told apart at 12px.
_SEGMENT_RGB_LIGHT: dict[str, tuple[float, float, float]] = {
    "baseline":  (0.55, 0.57, 0.62),
    "tools":     (0.80, 0.43, 0.28),
    "user":      (0.78, 0.60, 0.29),
    "assistant": (0.31, 0.56, 0.53),
    "thinking":  (0.54, 0.42, 0.61),
}
_SEGMENT_RGB_DARK: dict[str, tuple[float, float, float]] = {
    "baseline":  (0.48, 0.51, 0.57),
    "tools":     (0.87, 0.52, 0.36),
    "user":      (0.85, 0.68, 0.36),
    "assistant": (0.40, 0.68, 0.64),
    "thinking":  (0.64, 0.51, 0.72),
}


def _segment_rgb(key: str, dark: bool) -> tuple[float, float, float]:
    table = _SEGMENT_RGB_DARK if dark else _SEGMENT_RGB_LIGHT
    return table.get(key, table["baseline"])


def _rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    if w <= 0:
        return
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class _StackedContextBar(Gtk.DrawingArea):
    """Horizontal stacked bar: one band per category, then free space.

    Scaled to the whole context window, not to the used portion — the empty
    tail is the point of the widget.
    """

    HEIGHT = 12

    def __init__(self) -> None:
        super().__init__()
        self.set_content_height(self.HEIGHT)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)
        self._segments: tuple = ()
        self._window = 0

    def set_segments(self, segments, window: int) -> None:
        self._segments = tuple(segments)
        if window > 0:
            self._window = window
        self.queue_draw()

    def set_plain(self, used: int, window: int) -> None:
        """One undifferentiated band — what we can show before the split."""
        self._window = window
        self._segments = (_PlainSegment(used),) if used > 0 else ()
        self.queue_draw()

    def set_window(self, window: int) -> None:
        if window > 0:
            self._window = window
            self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        dark = Adw.StyleManager.get_default().get_dark()
        radius = height / 2
        track = (0.30, 0.30, 0.33) if dark else (0.87, 0.87, 0.88)

        _rounded_rect(cr, 0, 0, width, height, radius)
        cr.set_source_rgb(*track)
        cr.fill()

        if self._window <= 0 or not self._segments:
            return

        # Clip to the rounded track so band edges inherit its shape without
        # each band needing its own rounded path.
        cr.save()
        _rounded_rect(cr, 0, 0, width, height, radius)
        cr.clip()
        x = 0.0
        for segment in self._segments:
            span = width * (segment.tokens / self._window)
            if span <= 0:
                continue
            cr.set_source_rgb(*_segment_rgb(segment.key, dark))
            cr.rectangle(x, 0, span, height)
            cr.fill()
            x += span
        cr.restore()


class _PlainSegment:
    """Stand-in used before the transcript split has been computed."""

    key = "tools"

    def __init__(self, tokens: int) -> None:
        self.tokens = tokens


def _legend_row(segment, total: int) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

    swatch = Gtk.DrawingArea()
    swatch.set_content_width(9)
    swatch.set_content_height(9)
    swatch.set_valign(Gtk.Align.CENTER)

    def draw(_a, cr, w, h, key=segment.key):
        dark = Adw.StyleManager.get_default().get_dark()
        cr.set_source_rgb(*_segment_rgb(key, dark))
        _rounded_rect(cr, 0, 0, w, h, 2.5)
        cr.fill()

    swatch.set_draw_func(draw)
    row.append(swatch)

    label = Gtk.Label(label=segment.label, xalign=0)
    label.add_css_class("caption")
    label.set_hexpand(True)
    row.append(label)

    pct = int(round(100 * segment.tokens / total)) if total else 0
    value = Gtk.Label(label=f"{_format_tokens(segment.tokens)} · {pct}%", xalign=1)
    value.add_css_class("caption")
    value.add_css_class("dim-label")
    row.append(value)
    return row


def _format_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


# ── Context-meter hover popover ───────────────────────────────────────────


def rate_limit_label(rl_type: str, info: dict | None = None) -> str:
    """Provider-supplied label first, then the Claude table, then the raw key.

    Codex rows carry their own `label` (built from the App Server's limitName +
    windowDurationMins) because `limitId` is an opaque backend id — a lookup
    table keyed on it renders `codex_primary` the day OpenAI renames one.
    """

    if isinstance(info, dict):
        supplied = str(info.get("label") or "").strip()
        if supplied:
            return supplied
    return _RATE_LIMIT_LABELS.get(rl_type, rl_type)


# Human-readable labels for known rate-limit types claude emits.
_RATE_LIMIT_LABELS = {
    "five_hour": "5-hour usage",
    "fivehour": "5-hour usage",
    "5h": "5-hour usage",
    "weekly": "Weekly usage",
    "week": "Weekly usage",
    "sonnet_weekly": "Weekly (Sonnet only)",
    "opus_weekly": "Weekly (Opus only)",
    "haiku_weekly": "Weekly (Haiku only)",
    "daily": "Daily usage",
    "codex_primary": "Codex primary window",
    "codex_secondary": "Codex secondary window",
    "openrouter_credits": "OpenRouter credit",
    "openrouter": "OpenRouter rate limit",
}


# What "nothing here yet" means depends entirely on the provider, and the old
# one-size message ("send a message to populate") was wrong for two of three:
# Claude reports a window only once a turn runs, Codex reports on connect, and
# OpenRouter reports account credit rather than a rolling window.
_LIMITS_EMPTY: dict[str, str] = {
    model_catalog.PROVIDER_ANTHROPIC: (
        "Claude reports its limit windows on the first response of a session — "
        "send a message to populate."
    ),
    model_catalog.PROVIDER_OPENAI: (
        "Codex reports usage when the App Server connects. Nothing yet means "
        "the session has not started or you are signed out."
    ),
    model_catalog.PROVIDER_OPENROUTER: (
        "OpenRouter reports account credit rather than a rolling window. Add "
        "a key in Settings → Providers to see it."
    ),
}


_RATE_STATUS_DOT_CLASS = {
    "allowed": "helios-rate-ok",
    "warning": "helios-rate-warn",
    "exceeded": "helios-rate-error",
    "denied": "helios-rate-error",
    "blocked": "helios-rate-error",
}


class _ContextPopover(Gtk.Popover):
    """Hover popover that breaks down context window usage + rate limits."""

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("helios-context-popover")
        self.set_size_request(320, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(14)
        box.set_margin_bottom(12)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # ── Context window section ──
        self._ctx_title = Gtk.Label(label="Session context", xalign=0)
        self._ctx_title.add_css_class("caption-heading")
        self._ctx_title.set_tooltip_text(
            "How much of this session's model context window is filled — used "
            "to know when to /clear and start fresh. This is per-session, not "
            "your overall account usage."
        )
        box.append(self._ctx_title)

        bar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._bar = _StackedContextBar()
        bar_row.append(self._bar)

        self._max_label = Gtk.Label(label="—", xalign=1)
        self._max_label.add_css_class("caption")
        self._max_label.add_css_class("dim-label")
        bar_row.append(self._max_label)
        box.append(bar_row)

        self._usage_label = Gtk.Label(xalign=0)
        self._usage_label.add_css_class("caption")
        self._usage_label.add_css_class("dim-label")
        box.append(self._usage_label)

        # ── What is filling it ──
        self._legend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self._legend.set_margin_top(2)
        box.append(self._legend)

        #: True once the provider has given real per-category figures, after
        #: which the transcript estimate must not overwrite them.
        self._legend_measured = False
        #: Deferred tool-schema tokens — real, but NOT in the window.
        self._legend_deferred = 0
        self._legend_note = Gtk.Label(xalign=0)
        self._legend_note.add_css_class("caption")
        self._legend_note.add_css_class("dim-label")
        self._legend_note.set_wrap(True)
        self._legend_note.set_visible(False)
        box.append(self._legend_note)
        # What the categories are made of: memory files, skills, agents, the
        # message split, and where autocompaction fires (measured only).
        self._details_label = Gtk.Label(xalign=0)
        self._details_label.add_css_class("caption")
        self._details_label.add_css_class("dim-label")
        self._details_label.set_wrap(True)
        self._details_label.set_selectable(True)
        self._details_label.set_visible(False)
        box.append(self._details_label)

        # ── Model line ──
        self._model_label = Gtk.Label(xalign=0)
        self._model_label.add_css_class("caption")
        self._model_label.add_css_class("dim-label")
        box.append(self._model_label)

        # ── Rate limits section ──
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(6)
        sep.set_margin_bottom(2)
        box.append(sep)

        self._limits_title = Gtk.Label(label="Account usage limits", xalign=0)
        self._limits_title.add_css_class("caption-heading")
        self._limits_title.set_tooltip_text(
            "Live provider status — reset windows and whether each limit "
            "type is currently allowed or exceeded."
        )
        box.append(self._limits_title)

        self._limits_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self._limits_box)

        self._limits_empty = Gtk.Label(
            label=_LIMITS_EMPTY[model_catalog.PROVIDER_ANTHROPIC],
            xalign=0,
        )
        self._limits_empty.add_css_class("caption")
        self._limits_empty.add_css_class("dim-label")
        self._limits_empty.set_wrap(True)
        self._limits_box.append(self._limits_empty)

        # Link to the actual claude.ai dashboard for full breakdown.
        self._usage_link = Gtk.LinkButton.new_with_label(
            "https://claude.ai/settings/usage",
            "Open full account usage in browser ↗",
        )
        self._usage_link.add_css_class("flat")
        self._usage_link.add_css_class("caption")
        self._usage_link.set_halign(Gtk.Align.START)
        self._usage_link.set_margin_top(6)
        box.append(self._usage_link)

        # ── Compaction ──
        # The CLI auto-compacts silently. Say when it last happened, and give
        # the manual trigger the terminal has and this app did not.
        self._compact_label = Gtk.Label(xalign=0)
        self._compact_label.add_css_class("caption")
        self._compact_label.add_css_class("dim-label")
        self._compact_label.set_wrap(True)
        self._compact_label.set_visible(False)
        self._compact_label.set_margin_top(4)
        box.append(self._compact_label)

        self._compact_btn = Gtk.Button(label="Compact now")
        self._compact_btn.add_css_class("flat")
        self._compact_btn.set_halign(Gtk.Align.START)
        self._compact_btn.set_margin_top(2)
        self._compact_tooltip = (
            "Summarize the conversation so far and free context, without "
            "starting a new chat. Same operation the CLI runs automatically "
            "when the window fills."
        )
        self._compact_btn.set_tooltip_text(self._compact_tooltip)
        self._compact_btn.set_sensitive(False)
        self._compact_btn.connect("clicked", self._on_compact_clicked)
        box.append(self._compact_btn)

        self._on_compact = None  # set by set_compact_handler()
        self._compact_supported = False
        self._compact_unavailable_reason = "No native compaction capability."
        self._busy = False

        self.set_child(box)

        # Per-rate-limit-type accumulator; each entry is the most recent
        # rate_limit_info dict for that type.
        self._rate_limits: dict[str, dict] = {}
        self._rate_provider = ""

        # Remember last usage + the selected model's window so set_window can
        # update the cap label before any turn, and set_usage can recompute.
        self._used = 0
        self._window = 0
        self._has_turn = False

    # ── Public API ────────────────────────────────────────────────

    def _on_compact_clicked(self, _btn) -> None:
        if self._on_compact is not None:
            self.popdown()
            self._on_compact()

    def set_compact_handler(self, handler) -> None:
        """Enable "Compact now". None disables it — a provider with no manual
        compaction must not show a button that silently does nothing."""
        self._on_compact = handler
        self._update_compact_sensitivity()

    def set_compact_capability(
        self,
        supported: bool,
        unavailable_reason: str = "",
    ) -> None:
        self._compact_supported = bool(supported)
        self._compact_unavailable_reason = str(unavailable_reason or "")
        self._update_compact_sensitivity()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._update_compact_sensitivity()

    def _update_compact_sensitivity(self) -> None:
        available = bool(
            self._on_compact is not None
            and self._compact_supported
            and not self._busy
        )
        self._compact_btn.set_sensitive(available)
        if self._busy and self._compact_supported:
            reason = "Wait for the current provider operation to finish."
        else:
            reason = self._compact_unavailable_reason
        self._compact_btn.set_tooltip_text(
            self._compact_tooltip if available or not reason else reason
        )

    def note_compaction(self, trigger: str, pre_tokens: int) -> None:
        """Record that compaction happened, so it stops being invisible."""
        freed = f" from {pre_tokens:,} tokens" if pre_tokens > 0 else ""
        how = "Automatically compacted" if trigger != "manual" else "Compacted"
        self._compact_label.set_label(f"{how}{freed} earlier in this session.")
        self._compact_label.set_visible(True)

    def set_usage(self, used: int, total: int) -> None:
        if total > 0:
            self._has_turn = True
            self._used = used
            self._window = total
            frac = max(0.0, min(1.0, used / total))
            self._max_label.set_label(f"{_format_window(total)} max")
            pct = int(frac * 100)
            self._usage_label.set_label(
                f"{used:,} / {total:,} tokens ({pct}%)"
            )
            # The split arrives separately (it needs the transcript). Until it
            # does, show the measured total as one solid bar rather than a
            # stale breakdown from the previous turn.
            self._bar.set_plain(used, total)
        else:
            # Reset (fresh chat / session switch). Keep showing the selected
            # model's window cap if we know it, just with no usage yet.
            self._has_turn = False
            self._used = 0
            # Measured-ness is per-session too, and for exactly the same
            # reason as the compaction line below: leaving it set would make a
            # new session refuse its own estimate while showing the previous
            # session's provider figures.
            self._legend_measured = False
            self._legend_deferred = 0
            # The compaction line is per-session and was never cleared, so
            # once ANY session compacted, every session opened afterwards
            # claimed "compacted … earlier in this session" about a session
            # that never compacted. A false statement is worse than a missing
            # one — this is the reset, so clear it here.
            self._compact_label.set_visible(False)
            self._compact_label.set_label("")
            self._bar.set_plain(0, self._window)
            self._set_legend(())
            self._max_label.set_label(f"{_format_window(self._window)} max" if self._window else "—")
            self._usage_label.set_label("No turn yet — usage appears after the first response.")

    def set_breakdown(self, breakdown) -> None:
        """Install the per-category split for the usage already shown."""
        if breakdown is None or not breakdown.segments:
            self._set_legend(())
            return
        # An estimate must never overwrite a measurement. The provider answers
        # once per turn; the transcript walk can land later and would otherwise
        # replace real figures with scaled ones.
        if self._legend_measured:
            return
        self._bar.set_segments(breakdown.segments, breakdown.window)
        self._set_legend(breakdown.segments)

    def set_measured_breakdown(self, payload: dict) -> None:
        """Install the provider's own per-category split.

        Outranks `set_breakdown` for the rest of the session: once the CLI has
        told us what is in the window, there is no reason to show an estimate
        of the same thing.
        """

        # No `_destroyed` guard here on purpose: this popover has no shutdown()
        # and every caller routes through a ChatToolbar delegator that already
        # guards (see set_measured_breakdown at the top of this file). The guard
        # that used to sit here read an attribute nothing assigned, so this
        # method raised AttributeError on every call and PyGObject swallowed it.
        breakdown = context_breakdown.from_measured(payload)
        if breakdown is None:
            return
        self._legend_measured = True
        self._legend_deferred = context_breakdown.deferred_tokens(payload)
        self._bar.set_segments(breakdown.segments, breakdown.window)
        self._set_legend(breakdown.segments)
        details = context_breakdown.details_from_measured(payload)
        lines = (
            context_breakdown.describe_details(details, breakdown.window)
            if details is not None
            else []
        )
        self._details_label.set_label("\n".join(lines))
        self._details_label.set_visible(bool(lines))

    def _set_legend(self, segments) -> None:
        child = self._legend.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._legend.remove(child)
            child = nxt
        if not segments:
            self._legend_note.set_visible(False)
            return
        total = sum(s.tokens for s in segments) or 1
        for segment in segments:
            self._legend.append(_legend_row(segment, total))
        if self._legend_measured:
            # Every figure here came from the provider. Saying "estimated"
            # anyway would train the reader to discount a real measurement —
            # and the whole point of this change is that the two must never
            # look alike.
            note = f"{total:,} tokens, measured by the provider."
            if self._legend_deferred:
                note += (
                    f" A further {self._legend_deferred:,} tokens of tool"
                    " schemas are deferred — the CLI loads them on demand, so"
                    " they are NOT in the window and are not counted here."
                )
        else:
            note = (
                f"{total:,} tokens measured. The split across categories is "
                "estimated from the transcript."
            )
        self._legend_note.set_label(note)
        self._legend_note.set_visible(True)

    def set_window(self, total: int) -> None:
        """Set the selected model's context window. Updates the cap label
        immediately; only rescales the bar/usage line once a turn has run."""
        if total <= 0:
            return
        self._window = total
        self._max_label.set_label(f"{_format_window(total)} max")
        if self._has_turn:
            frac = max(0.0, min(1.0, self._used / total))
            pct = int(frac * 100)
            self._usage_label.set_label(f"{self._used:,} / {total:,} tokens ({pct}%)")
            self._bar.set_window(total)

    def set_model(self, model: str) -> None:
        if model:
            provider = model_catalog.provider_for(model)
            if self._rate_provider and provider != self._rate_provider:
                self._rate_limits.clear()
                self._rebuild_limits_rows()
            self._rate_provider = provider
            self._model_label.set_label(f"Model: {model}")
            self._model_label.set_visible(True)
            self._limits_empty.set_label(
                _LIMITS_EMPTY.get(
                    provider, _LIMITS_EMPTY[model_catalog.PROVIDER_ANTHROPIC]
                )
            )
            if provider == model_catalog.PROVIDER_OPENROUTER:
                self._limits_title.set_tooltip_text(
                    "Credit balance and spend on the OpenRouter key Helios holds."
                )
                self._usage_link.set_uri("https://openrouter.ai/credits")
                self._usage_link.set_label("Open OpenRouter credits in browser ↗")
            elif provider == model_catalog.PROVIDER_OPENAI:
                self._limits_title.set_tooltip_text(
                    "Live limits reported by Codex for the signed-in OpenAI account."
                )
                self._usage_link.set_uri("https://chatgpt.com/codex")
                self._usage_link.set_label("Open Codex in browser ↗")
            else:
                self._limits_title.set_tooltip_text(
                    "Live limits reported by Claude for the signed-in Anthropic account."
                )
                self._usage_link.set_uri("https://claude.ai/settings/usage")
                self._usage_link.set_label("Open full account usage in browser ↗")
        else:
            self._model_label.set_label("")
            self._model_label.set_visible(False)

    def update_rate_limit(self, info: dict) -> None:
        rl_type = info.get("rateLimitType") or ""
        if not rl_type:
            return
        provider = str(info.get("provider") or "")
        if provider and self._rate_provider and provider != self._rate_provider:
            return
        self._rate_limits[rl_type] = info
        self._rebuild_limits_rows()

    # ── Internals ────────────────────────────────────────────────

    def _rebuild_limits_rows(self) -> None:
        # Remove all children.
        child = self._limits_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._limits_box.remove(child)
            child = nxt

        if not self._rate_limits:
            self._limits_box.append(self._limits_empty)
            return

        # Ordered list: known limit types first, others after.
        ordered_keys = list(self._rate_limits.keys())
        priority = {k: i for i, k in enumerate(("five_hour", "fivehour", "weekly", "sonnet_weekly", "opus_weekly", "haiku_weekly"))}
        ordered_keys.sort(key=lambda k: priority.get(k, 99))

        for k in ordered_keys:
            row = self._build_limit_row(k, self._rate_limits[k])
            self._limits_box.append(row)

    def _build_limit_row(self, rl_type: str, info: dict) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        dot = Gtk.Box()
        dot.set_size_request(8, 8)
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_halign(Gtk.Align.CENTER)
        dot.add_css_class("helios-status-dot")
        status = (info.get("status") or "allowed").lower()
        dot.add_css_class(_RATE_STATUS_DOT_CLASS.get(status, "helios-status-idle"))
        row.append(dot)

        label = Gtk.Label(label=rate_limit_label(rl_type, info), xalign=0)
        label.add_css_class("caption")
        row.append(label)

        # Status badge text.
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)

        resets_str = _humanize_resets(info.get("resetsAt"))
        used_percent = info.get("usedPercent")
        parts: list[str] = []
        if isinstance(used_percent, (int, float)):
            parts.append(f"{max(0, min(100, int(used_percent)))}% used")
        detail = str(info.get("detail") or "")
        if detail:
            parts.append(detail)
        if status != "allowed":
            parts.append(status)
        elif not parts:
            # Claude's rate_limit_event carries a window and a verdict but no
            # percentage. "OK · resets in 3h" beats a bare reset time, which
            # reads like a countdown to something bad.
            parts.append("OK")
        if resets_str:
            parts.append(f"resets in {resets_str}")
        status_txt = " · ".join(parts) or status
        rhs = Gtk.Label(label=status_txt, xalign=1)
        rhs.add_css_class("caption")
        rhs.add_css_class("dim-label")
        row.append(rhs)

        return row


def _humanize_resets(epoch) -> str:
    if not epoch:
        return ""
    try:
        secs = int(epoch) - int(time.time())
    except (TypeError, ValueError):
        return ""
    if secs <= 0:
        return "now"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d = secs // 86400
    h = (secs % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"
