"""In-conversation projection of provider-observed agent activity."""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from helios.backend.agent_activity import (
    TERMINAL_AGENT_STATUSES,
    AgentActivity,
    AgentActivitySnapshot,
    AgentObservedStatus,
    agent_status_label,
)
# The turn strip's formatter, not a second one: an agent that has run "4m 07s"
# should read identically to the root turn that spawned it, and two spellings
# of a duration in one column is how they drift.
from helios.widgets.activity_indicator import _format_elapsed
from helios.widgets._motion import BASE_MS


_MAX_COLLAPSED_NODES = 8
_STATUS_CLASSES = tuple(f"helios-agent-{status.value}" for status in AgentObservedStatus)

#: Which actors survive the node cap once a fan-out outgrows the strip.
#:
#: The strip used to show the first eight BY ARRIVAL, which is the least useful
#: eight there are: on a 20-agent fan-out with one failure and one question, you
#: saw eight identical working squares and the two that wanted you were only in
#: the text. Ranked, the ones that need a human come first, then the ones still
#: in flight, then the ones already dealt with.
#:
#: STARTING outranks RUNNING deliberately — it means the delegation is
#: provider-proven but the child has never reported, which is the shape a wedged
#: subagent has.
_NODE_PRIORITY = {
    AgentObservedStatus.NEEDS_INPUT: 0,
    AgentObservedStatus.FAILED: 1,
    AgentObservedStatus.STARTING: 2,
    AgentObservedStatus.RUNNING: 3,
    AgentObservedStatus.UNKNOWN: 4,
    AgentObservedStatus.STOPPED: 5,
    AgentObservedStatus.COMPLETED: 6,
}

#: How often the elapsed labels re-read the clock. Only armed while something
#: is non-terminal, and a wedged agent emits nothing at all — which is exactly
#: the case where a duration that only advanced on provider events would sit
#: frozen at the number that made it look fine.
_ELAPSED_TICK_MS = 1000


def _collapsed_selection(
    activities: tuple[AgentActivity, ...],
) -> tuple[AgentActivity, ...]:
    """The <=8 actors the collapsed strip shows, in arrival order.

    Rank decides WHICH survive; arrival order decides where they sit. Sorting
    the strip itself by rank would make squares jump sideways every time a
    status changed, and positional identity ("the third one is my migration")
    is most of what makes the strip readable at a glance. Under the cap this
    returns the input untouched, so the common case never reorders at all.
    """
    if len(activities) <= _MAX_COLLAPSED_NODES:
        return activities
    ranked = sorted(
        activities,
        key=lambda item: (_NODE_PRIORITY[item.status], item.ordinal),
    )
    keep = {item.actor_id for item in ranked[:_MAX_COLLAPSED_NODES]}
    return tuple(item for item in activities if item.actor_id in keep)


class _CrossfadeLabel(Gtk.Stack):
    """Fixed A/B label pair: bounded memory with a BASE_MS text crossfade."""

    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.set_transition_duration(BASE_MS)
        self.set_hexpand(True)
        self._labels: list[Gtk.Label] = []
        for name in ("a", "b"):
            label = Gtk.Label(xalign=0)
            label.set_hexpand(True)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.add_css_class("caption-heading")
            self.add_named(label, name)
            self._labels.append(label)
        self._current = 0
        self._text = ""

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        target = 1 - self._current
        self._labels[target].set_label(text)
        self.set_visible_child(self._labels[target])
        self._current = target


def _compact_tokens(count: int) -> str:
    """Token counts at a glance: 950, 12.4k, 1.2M.

    A fan-out's whole point is that the number gets large fast, and a
    nine-digit run of commas in a status strip reads as noise rather than as
    alarm. The exact figure stays in the tooltip.
    """
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.1f}M".replace(".0M", "M")


class AgentDock(Gtk.Revealer):
    """Compact, expandable dock kept separate from primary session navigation."""

    def __init__(self) -> None:
        super().__init__()
        #: Called with an actor id when the user asks to stop that one agent.
        self._on_stop: object = None
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.set_transition_duration(BASE_MS)
        self.set_reveal_child(False)
        self.add_css_class("helios-agent-dock-shell")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Margins belong to revealed content. Margins on the outer Revealer
        # reserve layout space even while an empty dock is closed.
        card.set_margin_start(16)
        card.set_margin_end(16)
        card.set_margin_top(2)
        card.set_margin_bottom(4)
        card.add_css_class("helios-agent-dock")

        self._summary_button = Gtk.ToggleButton()
        self._summary_button.add_css_class("flat")
        self._summary_button.add_css_class("helios-agent-dock-summary")
        self._summary_button.set_has_frame(False)
        self._summary_button.set_hexpand(True)
        self._summary_button.connect("toggled", self._on_summary_toggled)

        summary = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        summary.set_hexpand(True)

        self._aggregate_icon = Gtk.Image.new_from_icon_name("system-users-symbolic")
        self._aggregate_icon.set_pixel_size(15)
        self._aggregate_icon.add_css_class("helios-agent-aggregate-icon")
        summary.append(self._aggregate_icon)

        self._nodes_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._nodes_box.set_valign(Gtk.Align.CENTER)
        summary.append(self._nodes_box)

        self._overflow = Gtk.Label()
        self._overflow.add_css_class("caption")
        self._overflow.add_css_class("dim-label")
        self._overflow.set_visible(False)
        self._nodes_box.append(self._overflow)

        self._summary_label = _CrossfadeLabel()
        summary.append(self._summary_label)

        # Spend sits beside the actors on purpose: the dock answers
        # "who is running", and the number nobody had was "what are they
        # costing". Hidden until a turn actually delegates — a lone root's
        # total is already the context meter's job and would just add noise.
        self._spend_label = Gtk.Label()
        self._spend_label.add_css_class("caption")
        self._spend_label.add_css_class("dim-label")
        self._spend_label.set_visible(False)
        summary.append(self._spend_label)

        self._chevron = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        self._chevron.set_pixel_size(13)
        self._chevron.add_css_class("dim-label")
        summary.append(self._chevron)

        self._summary_button.set_child(summary)
        card.append(self._summary_button)

        self._details_revealer = Gtk.Revealer()
        self._details_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._details_revealer.set_transition_duration(BASE_MS)
        self._details_revealer.set_reveal_child(False)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        details.add_css_class("helios-agent-dock-details")
        note = Gtk.Label(
            label="Observed provider activity · status is not a cancellation guarantee",
            xalign=0,
        )
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        note.set_wrap(True)
        details.append(note)
        self._details = details
        self._details_scroller = Gtk.ScrolledWindow()
        self._details_scroller.set_policy(
            Gtk.PolicyType.NEVER,
            Gtk.PolicyType.AUTOMATIC,
        )
        self._details_scroller.set_propagate_natural_height(True)
        self._details_scroller.set_max_content_height(240)
        self._details_scroller.set_child(details)
        self._details_revealer.set_child(self._details_scroller)
        card.append(self._details_revealer)
        self.set_child(card)

        self._scope = None
        self._revision = -1
        self._spend = None
        self._nodes: dict[str, Gtk.Box] = {}
        self._node_statuses: dict[str, AgentObservedStatus] = {}
        self._snapshot: AgentActivitySnapshot | None = None
        self._rows: list[_AgentDetailRow] = []
        self._tick_id = 0
        self._destroyed = False

    def set_stop_handler(self, handler: object) -> None:
        """Install (or clear, with None) the per-agent stop callback.

        Re-renders the detail rows when the handler actually changes:
        `set_snapshot` dedupes on (scope, revision), so a handler change
        alone would otherwise leave stale buttons on rows already built.
        """
        handler = handler if callable(handler) else None
        if handler is self._on_stop:
            return
        self._on_stop = handler
        snapshot = getattr(self, "_snapshot", None)
        if snapshot is not None and not self._destroyed:
            self._render_details(snapshot)

    def shutdown(self) -> None:
        """Refuse late provider projections during GTK tree finalization, and
        stop the elapsed tick so it cannot fire against a finalizing widget."""

        self._destroyed = True
        self._stop_tick()

    # ── Elapsed tick ───────────────────────────────────────────────────

    def _ensure_tick(self) -> None:
        if not self._tick_id:
            self._tick_id = GLib.timeout_add(_ELAPSED_TICK_MS, self._on_tick)

    def _stop_tick(self) -> None:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _on_tick(self) -> bool:
        # A tick already dispatched into the main loop when shutdown() ran must
        # drop itself without touching the finalizing widget. Same discipline as
        # ActivityIndicator, which this is modelled on.
        if self._destroyed:
            self._tick_id = 0
            return False
        snapshot = self._snapshot
        if snapshot is None or all(
            activity.is_terminal for activity in snapshot.activities
        ):
            self._tick_id = 0
            return False
        self._refresh_elapsed_at(time.monotonic())
        return True

    def _refresh_elapsed_at(self, now: float) -> None:
        """Re-read every duration against `now`.

        Takes the clock reading rather than calling monotonic() itself so a
        test can assert what "6m 07s" renders from instead of racing a real
        second boundary.
        """
        snapshot = self._snapshot
        if snapshot is None:
            return
        for row in self._rows:
            row.refresh_elapsed(now)
        for activity in snapshot.activities:
            node = self._nodes.get(activity.actor_id)
            if node is not None:
                node.set_tooltip_text(_node_tooltip(activity, now))
        self._summary_button.set_tooltip_text(_summary_tooltip(snapshot, now))

    def clear(self) -> None:
        if self._destroyed:
            return
        self._scope = None
        self._revision = -1
        self._spend = None
        self._snapshot = None
        self._stop_tick()
        self._spend_label.set_visible(False)
        self._summary_button.set_active(False)
        self._details_revealer.set_reveal_child(False)
        self._clear_nodes()
        self._clear_details()
        self.set_reveal_child(False)

    def set_spend(self, snapshot: object) -> None:
        """Show delegated token spend for the visible Work.

        Shown only once a turn has actually delegated. Tokens, never dollars:
        `costUSD` is a notional API-equivalent estimate on a subscription
        account, and a precise, prominent number that is not the thing it
        appears to be is the bug this replaces, not a smaller version of it.
        """
        if self._destroyed:
            return
        delegated = int(getattr(snapshot, "delegated_tokens", 0) or 0)
        total = int(getattr(snapshot, "total_tokens", 0) or 0)
        self._spend = snapshot
        if delegated <= 0 or total <= 0:
            self._spend_label.set_visible(False)
            return
        share = round(delegated * 100 / total)
        text = f"{_compact_tokens(delegated)} delegated · {share}%"
        self._spend_label.set_label(text)
        self._spend_label.set_tooltip_text(
            f"{delegated:,} of {total:,} tokens this session went to delegated "
            f"agents ({share}%).\nCumulative for this provider process, "
            "including cache reads and writes. Not the context meter, which "
            "shows the current window and excludes subagent traffic."
        )
        self._spend_label.set_visible(True)

    def set_snapshot(self, snapshot: AgentActivitySnapshot) -> None:
        if self._destroyed:
            return
        if not isinstance(snapshot, AgentActivitySnapshot) or not snapshot.activities:
            self.clear()
            return
        if snapshot.scope == self._scope and snapshot.revision == self._revision:
            return

        scope_changed = snapshot.scope != self._scope
        if scope_changed:
            self._clear_nodes()
            self._node_statuses.clear()
        self._scope = snapshot.scope
        self._revision = snapshot.revision
        self._snapshot = snapshot
        self._sync_nodes(snapshot.activities)
        self._render_details(snapshot)

        summary = _summary_text(snapshot)
        self._summary_label.set_text(summary)
        description = (
            "Provider-observed child activity for the current Work and root turn. "
            "Expand for Active, Needs you, Done, and Observed groups."
        )
        self._summary_button.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            [f"Agent activity. {summary}", description],
        )
        self._summary_button.set_tooltip_text(
            _summary_tooltip(snapshot, time.monotonic())
        )

        has_running = any(
            activity.status
            in {AgentObservedStatus.STARTING, AgentObservedStatus.RUNNING}
            for activity in snapshot.activities
        )
        if has_running:
            self._aggregate_icon.add_css_class("helios-agent-running-pulse")
        else:
            self._aggregate_icon.remove_css_class("helios-agent-running-pulse")

        # Armed only while something can still change. An all-terminal snapshot
        # has frozen durations, so a tick would burn a wakeup a second to
        # rewrite the same strings.
        if all(activity.is_terminal for activity in snapshot.activities):
            self._stop_tick()
        else:
            self._ensure_tick()
        self.set_reveal_child(True)

    def _on_summary_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._destroyed:
            return
        expanded = button.get_active()
        self._details_revealer.set_reveal_child(expanded)
        self._chevron.set_from_icon_name(
            "pan-up-symbolic" if expanded else "pan-down-symbolic"
        )

    def _clear_nodes(self) -> None:
        while (child := self._nodes_box.get_first_child()) is not None:
            self._nodes_box.remove(child)
        self._nodes.clear()
        self._node_statuses.clear()
        self._overflow = Gtk.Label()
        self._overflow.add_css_class("caption")
        self._overflow.add_css_class("dim-label")
        self._overflow.set_visible(False)
        self._nodes_box.append(self._overflow)

    def _sync_nodes(self, activities: tuple[AgentActivity, ...]) -> None:
        visible = _collapsed_selection(activities)
        now = time.monotonic()
        anchor = None
        for activity in visible:
            node = self._nodes.get(activity.actor_id)
            previous = self._node_statuses.get(activity.actor_id)
            if node is None:
                node = Gtk.Box()
                node.set_size_request(10, 10)
                node.set_valign(Gtk.Align.CENTER)
                node.add_css_class("helios-agent-node")
                self._nodes[activity.actor_id] = node
                self._nodes_box.insert_child_after(node, anchor)
            else:
                # Ranked selection can swap membership mid-turn, so position is
                # re-asserted rather than assumed. reorder, never remove+add:
                # the same widget keeps its CSS state, which is what makes the
                # 180ms colour transition and the one-shot arrival flash real
                # rather than a fresh widget starting from scratch.
                self._nodes_box.reorder_child_after(node, anchor)
            for css_class in _STATUS_CLASSES:
                node.remove_css_class(css_class)
            node.add_css_class(f"helios-agent-{activity.status.value}")
            if activity.is_terminal and previous not in (
                AgentObservedStatus.COMPLETED,
                AgentObservedStatus.FAILED,
                AgentObservedStatus.STOPPED,
            ):
                node.add_css_class("helios-agent-terminal-arrival")
            node.set_tooltip_text(_node_tooltip(activity, now))
            anchor = node

        # Statuses are remembered for EVERY actor, not just the visible ones:
        # an actor that drops out of the strip and returns already terminal
        # would otherwise replay the arrival flash and read as "just finished".
        self._node_statuses = {
            activity.actor_id: activity.status for activity in activities
        }

        for actor_id in [
            actor_id
            for actor_id in self._nodes
            if actor_id not in {activity.actor_id for activity in visible}
        ]:
            self._nodes_box.remove(self._nodes.pop(actor_id))

        # The +N label trails the nodes; reorder_child_after with the last node
        # keeps it there through insertions, removals and reordering alike.
        self._nodes_box.reorder_child_after(self._overflow, anchor)
        overflow = max(0, len(activities) - len(visible))
        self._overflow.set_label(f"+{overflow}" if overflow else "")
        self._overflow.set_visible(bool(overflow))

    def _clear_details(self) -> None:
        child = self._details.get_first_child()
        # Preserve the explanatory first label; replace only dynamic groups.
        if child is not None:
            child = child.get_next_sibling()
        while child is not None:
            following = child.get_next_sibling()
            self._details.remove(child)
            child = following

    def _render_details(self, snapshot: AgentActivitySnapshot) -> None:
        self._clear_details()
        self._rows = []
        for title, activities in (
            ("Active", snapshot.active),
            ("Needs you", snapshot.needs_you),
            ("Done", snapshot.done),
            ("Observed", snapshot.observed),
        ):
            if not activities:
                continue
            heading = Gtk.Label(label=f"{title} · {len(activities)}", xalign=0)
            heading.add_css_class("caption-heading")
            heading.add_css_class("helios-agent-group-heading")
            self._details.append(heading)
            for activity in activities:
                row = _AgentDetailRow(activity, on_stop=self._on_stop)
                self._rows.append(row)
                self._details.append(row)


class _AgentDetailRow(Gtk.Box):
    def __init__(self, activity: AgentActivity, on_stop: object = None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("helios-agent-detail-row")
        self.add_css_class(f"helios-agent-detail-{activity.status.value}")

        icon = Gtk.Image.new_from_icon_name(_status_icon(activity.status))
        icon.set_pixel_size(14)
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class("helios-agent-detail-icon")
        self.append(icon)

        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        copy.set_hexpand(True)
        title = Gtk.Label(label=activity.name, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.add_css_class("caption-heading")
        title.set_tooltip_text(activity.actor_id)
        copy.append(title)
        secondary = activity.role
        if activity.detail:
            secondary = f"{secondary} · {activity.detail}" if secondary else activity.detail
        if secondary:
            detail = Gtk.Label(label=secondary, xalign=0)
            detail.set_ellipsize(Pango.EllipsizeMode.END)
            detail.add_css_class("caption")
            detail.add_css_class("dim-label")
            detail.set_tooltip_text(secondary)
            copy.append(detail)
        self.append(copy)

        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        status_box.set_valign(Gtk.Align.START)
        status = Gtk.Label(label=agent_status_label(activity.status), xalign=1)
        status.add_css_class("caption")
        status.add_css_class("helios-agent-status-text")
        status_box.append(status)

        self.activity = activity
        self._elapsed = Gtk.Label(xalign=1)
        self._elapsed.add_css_class("caption")
        self._elapsed.add_css_class("dim-label")
        self._elapsed.add_css_class("helios-agent-elapsed")
        self._elapsed.set_visible(False)
        status_box.append(self._elapsed)
        self.append(status_box)
        if callable(on_stop) and activity.status not in TERMINAL_AGENT_STATUSES:
            # Per-agent stop (2026-09-02 audit, gap 9). Only a live actor gets
            # one, and only when the provider can honour it — the window
            # installs the handler for drivers that implement stop_task.
            stop = Gtk.Button.new_from_icon_name("media-playback-stop-symbolic")
            stop.add_css_class("flat")
            stop.add_css_class("circular")
            stop.add_css_class("helios-agent-stop")
            stop.set_valign(Gtk.Align.CENTER)
            stop.set_tooltip_text(f"Stop {activity.name}")
            stop.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"Stop {activity.name}"],
            )
            stop.connect(
                "clicked",
                lambda *_args, actor_id=activity.actor_id: on_stop(actor_id),
            )
            self.append(stop)
        self.refresh_elapsed(time.monotonic())

    def refresh_elapsed(self, now: float) -> None:
        """Re-read the clock. Called on the dock's tick, not on a provider
        event: a wedged agent emits nothing, so an event-driven duration would
        freeze at the number that made it look healthy."""
        elapsed = self.activity.elapsed(now)
        if elapsed < 1:
            self._elapsed.set_visible(False)
            return
        self._elapsed.set_label(_format_elapsed(elapsed))
        self._elapsed.set_tooltip_text(
            "Finished after this long"
            if self.activity.is_terminal
            else "Observed for this long, still running"
        )
        self._elapsed.set_visible(True)


def _summary_tooltip(snapshot: AgentActivitySnapshot, now: float) -> str:
    """Collapsed-strip tooltip: the counts, plus the longest thing still going.

    Lives in the tooltip rather than in the strip itself because the strip is
    already the widest thing above the composer, and "longest 6m" is a question
    you ask when something feels wrong, not one you read continuously.
    """
    text = f"Observed agent activity · {_summary_text(snapshot)}"
    longest = _longest_running(snapshot, now)
    if longest is not None:
        text += (
            f"\nLongest still running: {longest.name} · "
            f"{_format_elapsed(longest.elapsed(now))}"
        )
    return text


def _node_tooltip(activity: AgentActivity, now: float) -> str:
    """Name, status and duration for one square in the collapsed strip."""
    parts = [activity.name, agent_status_label(activity.status)]
    elapsed = activity.elapsed(now)
    if elapsed >= 1:
        parts.append(_format_elapsed(elapsed))
    return " · ".join(parts)


def _longest_running(
    snapshot: AgentActivitySnapshot, now: float
) -> AgentActivity | None:
    """The non-terminal actor that has been observed longest.

    The one number worth surfacing without expanding the dock: on a fan-out
    that feels stuck, "which of these has been going the longest" is the first
    question, and a terminal actor cannot be the answer no matter how long it
    took.
    """
    candidates = [
        activity
        for activity in snapshot.activities
        if not activity.is_terminal and activity.elapsed(now) >= 1
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda activity: activity.elapsed(now))


def _summary_text(snapshot: AgentActivitySnapshot) -> str:
    parts: list[str] = []
    for status, label in (
        (AgentObservedStatus.STARTING, "starting"),
        (AgentObservedStatus.RUNNING, "working"),
        (AgentObservedStatus.NEEDS_INPUT, "needs you"),
        (AgentObservedStatus.COMPLETED, "complete"),
        (AgentObservedStatus.FAILED, "error"),
        (AgentObservedStatus.STOPPED, "stopped"),
        (AgentObservedStatus.UNKNOWN, "observed"),
    ):
        count = sum(activity.status is status for activity in snapshot.activities)
        if count:
            suffix = (
                "errors"
                if status is AgentObservedStatus.FAILED and count != 1
                else label
            )
            parts.append(f"{count} {suffix}")
    return " · ".join(parts) or "Observed activity"


def _status_icon(status: AgentObservedStatus) -> str:
    return {
        AgentObservedStatus.STARTING: "content-loading-symbolic",
        AgentObservedStatus.RUNNING: "media-playback-start-symbolic",
        AgentObservedStatus.NEEDS_INPUT: "dialog-warning-symbolic",
        AgentObservedStatus.COMPLETED: "object-select-symbolic",
        AgentObservedStatus.FAILED: "dialog-error-symbolic",
        AgentObservedStatus.STOPPED: "media-playback-stop-symbolic",
        AgentObservedStatus.UNKNOWN: "view-more-symbolic",
    }[status]
