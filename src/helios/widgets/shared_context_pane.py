"""Shared Context pane — a window into the shared scratchpad.

The scratchpad is the network-wide shared brain: handoffs, repo analyses, and
infra investigations that any Helios session on any machine can read. This
pane lists recent entries (newest first) and renders one on click. Reading
dominates; the one write is the header's hand-off button, which publishes
the currently open session (MainWindow supplies it via set_handoff_target
and owns the dialog — the pane only emits `handoff-requested`).

All network I/O runs on daemon threads (the service is on the tailnet; a
down service must never hitch the UI). Results marshal back via idle_add,
guarded by a generation counter.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, GObject, Gtk  # noqa: E402

from helios.backend import scratchpad, session_providers
from helios.log import get_logger
from helios.widgets._motion import BASE_MS

_log = get_logger("shared")

# Re-fetch on map only when the last successful load is older than this.
_STALE_AFTER_S = 5 * 60


class SharedContextPane(Gtk.Box):
    __gsignals__ = {
        # Emitted when the user clicks the hand-off button; carries the
        # Session set via set_handoff_target.
        "handoff-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._destroyed = False
        self.add_css_class("helios-shared-pane")
        self._handoff_target = None

        # Header: back (detail view only) · title · hand-off · refresh.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(12)
        header.set_margin_bottom(8)
        header.set_margin_start(12)
        header.set_margin_end(10)

        self._back_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self._back_btn.add_css_class("flat")
        self._back_btn.set_tooltip_text("Back to the entry list")
        self._back_btn.set_visible(False)
        self._back_btn.connect("clicked", lambda *_: self._show_list())
        header.append(self._back_btn)

        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_col.set_hexpand(True)
        title = Gtk.Label(label="Shared Context", xalign=0)
        title.add_css_class("title-4")
        text_col.append(title)
        self._subtitle = Gtk.Label(label="scratchpad", xalign=0)
        self._subtitle.add_css_class("dim-label")
        self._subtitle.add_css_class("caption")
        self._subtitle.set_ellipsize(3)
        text_col.append(self._subtitle)
        header.append(text_col)

        self._handoff_btn = Gtk.Button.new_from_icon_name("document-send-symbolic")
        self._handoff_btn.add_css_class("flat")
        self._handoff_btn.set_valign(Gtk.Align.CENTER)
        self._handoff_btn.connect("clicked", self._on_handoff_clicked)
        header.append(self._handoff_btn)

        self._refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._refresh_btn.add_css_class("flat")
        self._refresh_btn.set_valign(Gtk.Align.CENTER)
        self._refresh_btn.set_tooltip_text("Refresh shared context")
        self._refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.append(self._refresh_btn)

        self.append(header)

        # Stack: entry list | entry detail.
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._stack.set_transition_duration(BASE_MS)
        self._stack.set_vexpand(True)

        # — list page —
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("navigation-sidebar")
        self._listbox.connect("row-activated", self._on_row_activated)
        # row-activated needs activatable rows; NONE selection keeps clicks
        # from looking like a persistent selection in a read-only browser.
        self._listbox.set_activate_on_single_click(True)

        list_scroller = Gtk.ScrolledWindow()
        list_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroller.set_vexpand(True)
        list_scroller.set_child(self._listbox)
        self._stack.add_named(list_scroller, "list")

        # — placeholder page (initial / error / empty) —
        self._placeholder = Gtk.Label(label="Loading shared context…")
        self._placeholder.add_css_class("dim-label")
        self._placeholder.set_wrap(True)
        self._placeholder.set_justify(Gtk.Justification.CENTER)
        self._placeholder.set_vexpand(True)
        self._placeholder.set_valign(Gtk.Align.CENTER)
        self._placeholder.set_margin_start(16)
        self._placeholder.set_margin_end(16)
        self._stack.add_named(self._placeholder, "placeholder")

        # — detail page —
        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        detail_box.set_margin_start(12)
        detail_box.set_margin_end(12)
        detail_box.set_margin_bottom(12)
        self._detail_key = Gtk.Label(xalign=0)
        self._detail_key.add_css_class("heading")
        self._detail_key.set_wrap(True)
        self._detail_key.set_selectable(True)
        detail_box.append(self._detail_key)
        self._detail_meta = Gtk.Label(xalign=0)
        self._detail_meta.add_css_class("caption")
        self._detail_meta.add_css_class("dim-label")
        self._detail_meta.set_wrap(True)
        detail_box.append(self._detail_meta)
        self._detail_summary = Gtk.Label(xalign=0)
        self._detail_summary.set_wrap(True)
        self._detail_summary.set_selectable(True)
        detail_box.append(self._detail_summary)
        # TextView, not a Label: a wrapping selectable Gtk.Label lays out the
        # entire payload in one blocking Pango pass on the main loop, while
        # GtkTextView validates incrementally across idle callbacks. It does
        # NOT measure "only what is on screen" here — detail_box is a plain
        # Gtk.Box sized to natural height, so the view is allocated for the
        # whole buffer and validates all of it. The incremental validation is
        # the win; MAX_DISPLAY_CHARS is what actually bounds the work, so do
        # not read this comment as license to raise or drop the cap. Select and
        # copy come for free. Font family and size come from
        # .helios-shared-data, so no set_monospace() here.
        self._detail_data = Gtk.TextView()
        self._detail_data.set_editable(False)
        self._detail_data.set_cursor_visible(False)
        self._detail_data.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._detail_data.add_css_class("helios-shared-data")
        detail_box.append(self._detail_data)

        detail_scroller = Gtk.ScrolledWindow()
        detail_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        detail_scroller.set_vexpand(True)
        detail_scroller.set_child(detail_box)
        self._stack.add_named(detail_scroller, "detail")

        self._stack.set_visible_child_name("placeholder")
        self.append(self._stack)

        self._last_loaded = 0.0
        self._loading = False
        self._gen = 0
        self.set_handoff_target(None)

        # First fetch when the pane actually becomes visible; later maps
        # only re-fetch when the data has gone stale.
        self.connect("map", self._on_map)

    def shutdown(self) -> None:
        """Invalidate worker completions before the widget tree is detached."""
        if self._destroyed:
            return
        self._destroyed = True
        self._gen += 1
        self._loading = False

    # ── Hand-off ─────────────────────────────────────────────────────

    def set_handoff_target(self, session) -> None:
        """Point the hand-off button at the currently open session (or None).
        Pool sessions are excluded — their cwd/session-id don't exist on this
        machine, so the published resume command would be a lie."""
        if self._destroyed:
            return
        resolution = (
            session_providers.resolve_provider(session.session_id, session.path)
            if session is not None
            else None
        )
        if session is not None and session.project.read_only:
            session = None
            tooltip = "Pool sessions can't be handed off from this machine"
        elif session is not None and (resolution is None or not resolution.known):
            session = None
            tooltip = "Provider ownership must be verified before hand-off"
        elif session is None:
            tooltip = "Open a session to hand it off"
        else:
            tooltip = "Hand off this session to the shared scratchpad…"
        self._handoff_target = session
        self._handoff_btn.set_sensitive(session is not None)
        self._handoff_btn.set_tooltip_text(tooltip)

    def get_handoff_target(self):
        """The sanitized, hand-off-able session (or None) the pane button targets."""
        return self._handoff_target

    def _on_handoff_clicked(self, *_args) -> None:
        if not self._destroyed and self._handoff_target is not None:
            self.emit("handoff-requested", self._handoff_target)

    # ── Loading ──────────────────────────────────────────────────────

    def _on_map(self, *_args) -> None:
        if not self._destroyed and time.time() - self._last_loaded > _STALE_AFTER_S:
            self.refresh()

    def refresh(self) -> None:
        if self._destroyed or self._loading:
            return
        self._loading = True
        self._refresh_btn.set_sensitive(False)
        self._gen += 1
        gen = self._gen

        def worker() -> None:
            try:
                entries = scratchpad.list_entries()
                err = ""
            except scratchpad.ScratchpadError as e:
                entries, err = [], str(e)
            if not self._destroyed:
                GLib.idle_add(self._apply_entries, entries, err, gen)

        threading.Thread(target=worker, name="helios-scratch-list", daemon=True).start()

    def _apply_entries(self, entries: list, err: str, gen: int) -> bool:
        if self._destroyed or gen != self._gen:
            return False
        self._loading = False
        self._refresh_btn.set_sensitive(True)

        if err:
            self._placeholder.set_label(
                f"{err}\n\nIs the scratchpad service up?"
            )
            self._stack.set_visible_child_name("placeholder")
            self._subtitle.set_label("scratchpad · unreachable")
            return False

        self._last_loaded = time.time()
        while (row := self._listbox.get_first_child()) is not None:
            self._listbox.remove(row)

        if not entries:
            self._placeholder.set_label("Scratchpad is empty.")
            self._stack.set_visible_child_name("placeholder")
            self._subtitle.set_label("scratchpad · 0 entries")
            return False

        for e in entries:
            self._listbox.append(_EntryRow(e))
        self._subtitle.set_label(f"scratchpad · {len(entries)} entries")
        if self._stack.get_visible_child_name() != "detail":
            self._show_list()
        return False

    # ── Detail view ──────────────────────────────────────────────────

    def _show_list(self) -> None:
        if self._destroyed:
            return
        self._back_btn.set_visible(False)
        self._stack.set_visible_child_name("list")

    def _on_row_activated(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if self._destroyed:
            return
        entry = getattr(row, "entry", None)
        if entry is None:
            return
        key = entry.key
        self._detail_key.set_label(key)
        self._detail_meta.set_label("loading…")
        self._detail_summary.set_label(entry.summary)
        self._detail_data.get_buffer().set_text("")
        self._back_btn.set_visible(True)
        self._stack.set_visible_child_name("detail")
        gen = self._gen

        def worker() -> None:
            try:
                full = scratchpad.read_entry(key)
                err = ""
            except scratchpad.ScratchpadError as e:
                full, err = None, str(e)
            if not self._destroyed:
                GLib.idle_add(self._apply_detail, key, full, err, gen)

        threading.Thread(target=worker, name="helios-scratch-read", daemon=True).start()

    def _apply_detail(self, key: str, full, err: str, gen: int) -> bool:
        # Stale if the list was refreshed or the user opened another entry.
        if (
            self._destroyed
            or gen != self._gen
            or self._detail_key.get_label() != key
        ):
            return False
        if err:
            self._detail_meta.set_label(err)
            return False
        self._detail_meta.set_label(_meta_line(full))
        if full.summary:
            self._detail_summary.set_label(full.summary)
        self._detail_data.get_buffer().set_text(scratchpad.format_data(full.data))
        return False


class _EntryRow(Gtk.ListBoxRow):
    def __init__(self, entry) -> None:
        super().__init__()
        self.entry = entry
        self.add_css_class("helios-shared-row")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        key = Gtk.Label(label=entry.key, xalign=0)
        key.add_css_class("body")
        key.set_ellipsize(3)
        box.append(key)

        if entry.summary:
            summary = Gtk.Label(label=entry.summary, xalign=0)
            summary.add_css_class("caption")
            summary.add_css_class("dim-label")
            summary.set_wrap(True)
            summary.set_lines(2)
            summary.set_ellipsize(3)
            box.append(summary)

        meta = Gtk.Label(label=_meta_line(entry), xalign=0)
        meta.add_css_class("caption")
        meta.add_css_class("dim-label")
        box.append(meta)

        self.set_child(box)


def _meta_line(entry) -> str:
    parts = []
    if entry.created_at:
        parts.append(_humanize_age(entry.created_at))
    if entry.expires_at:
        ttl = entry.expires_at - time.time()
        parts.append(f"expires in {_humanize_span(ttl)}" if ttl > 0 else "expired")
    if entry.size_bytes:
        parts.append(f"{entry.size_bytes / 1024:.1f} KB")
    if entry.tags:
        parts.append(" ".join(f"#{t}" for t in entry.tags[:4]))
    return " · ".join(parts)


def _humanize_age(ts: float) -> str:
    delta = datetime.now(timezone.utc).timestamp() - ts
    if delta < 3600:
        return f"{max(1, int(delta / 60))}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def _humanize_span(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds / 60))}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"
