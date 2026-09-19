"""Full-text search across all local sessions.

A modal search window: type a query, see matching sessions (newest first) with
a snippet of the match; activate one to jump to that session. The scan runs on
a worker thread (see backend/search.py) with generation-guarded cancellation so
fast typing doesn't pile up stale results.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from helios.backend.search import SearchHit, search_sessions


class SearchDialog(Adw.Window):
    __gsignals__ = {
        # (project_dirname, session_id)
        "activated": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__()
        self._closed = False
        self._owner = parent
        self.set_title("Search sessions")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(640, 560)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_title(False)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        self._entry = Gtk.SearchEntry()
        self._entry.set_placeholder_text("Search all sessions…")
        self._entry.connect("search-changed", self._on_search_changed)
        self._entry.connect("activate", lambda *_: self._activate_selected())
        box.append(self._entry)

        self._status = Gtk.Label(xalign=0)
        self._status.add_css_class("dim-label")
        self._status.add_css_class("caption")
        box.append(self._status)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self._list = Gtk.ListBox()
        self._list.add_css_class("boxed-list")
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.connect("row-activated", self._on_row_activated)
        scroller.set_child(self._list)
        box.append(scroller)

        toolbar.set_content(box)
        self.set_content(toolbar)

        # Esc closes.
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        self._gen = 0
        self._debounce_id = 0
        self.connect("close-request", self._on_close_request)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._gen += 1
        if self._debounce_id:
            try:
                GLib.source_remove(self._debounce_id)
            except Exception:
                pass
            self._debounce_id = 0

    def _inactive(self) -> bool:
        return self._closed or bool(getattr(self._owner, "_destroyed", False))

    def _on_close_request(self, *_args) -> bool:
        self.shutdown()
        return False

    def focus_entry(self) -> None:
        if self._inactive():
            return
        self._entry.grab_focus()

    # ── search lifecycle ─────────────────────────────────────────────

    def _on_search_changed(self, _entry) -> None:
        if self._inactive():
            return
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(220, self._run_search)

    def _run_search(self) -> bool:
        self._debounce_id = 0
        if self._inactive():
            return False
        query = self._entry.get_text().strip()
        self._gen += 1
        gen = self._gen
        self._clear_results()
        if len(query) < 2:
            self._status.set_label("Type at least 2 characters.")
            return False
        self._status.set_label("Searching…")

        def worker() -> None:
            hits = search_sessions(
                query,
                should_cancel=lambda: self._inactive() or gen != self._gen,
            )
            if not self._inactive() and gen == self._gen:
                GLib.idle_add(self._apply_results, gen, query, hits)

        threading.Thread(target=worker, name="helios-search", daemon=True).start()
        return False

    def _apply_results(self, gen: int, query: str, hits: list[SearchHit]) -> bool:
        if self._inactive() or gen != self._gen:
            return False  # superseded by a newer query
        self._clear_results()
        if not hits:
            self._status.set_label(f"No matches for “{query}”.")
            return False
        self._status.set_label(
            f"{len(hits)} session{'s' if len(hits) != 1 else ''} matched"
        )
        for hit in hits:
            self._list.append(_HitRow(hit))
        first = self._list.get_row_at_index(0)
        if first is not None:
            self._list.select_row(first)
        return False

    def _clear_results(self) -> None:
        if self._inactive():
            return
        while (row := self._list.get_first_child()) is not None:
            self._list.remove(row)

    # ── activation ───────────────────────────────────────────────────

    def _on_row_activated(self, _list, row) -> None:
        if self._inactive():
            return
        self._activate_row(row)

    def _activate_selected(self) -> None:
        if self._inactive():
            return
        self._activate_row(self._list.get_selected_row())

    def _activate_row(self, row) -> None:
        if self._inactive():
            return
        hit = getattr(row, "hit", None)
        if hit is None:
            return
        self.emit("activated", hit.project_dirname, hit.session_id)
        self.close()

    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        if self._inactive():
            return False
        from gi.repository import Gdk
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


class _HitRow(Gtk.ListBoxRow):
    def __init__(self, hit: SearchHit) -> None:
        super().__init__()
        self.hit = hit
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        where = Gtk.Label(label=hit.title or f"Session {hit.session_id[:8]}", xalign=0)
        where.add_css_class("body")
        where.set_hexpand(True)
        where.set_ellipsize(3)
        top.append(where)
        when = Gtk.Label(label=_humanize(hit.when), xalign=1)
        when.add_css_class("caption")
        when.add_css_class("dim-label")
        top.append(when)
        box.append(top)

        snip = Gtk.Label(label=hit.snippet, xalign=0, wrap=True)
        snip.add_css_class("caption")
        snip.add_css_class("dim-label")
        snip.set_lines(2)
        snip.set_ellipsize(3)
        box.append(snip)

        # The cwd used to be the row's title. Keep it visible as a subtitle:
        # it is the only thing telling apart two sessions in different repos.
        where_sub = Gtk.Label(label=_leaf(hit.project_cwd), xalign=0)
        where_sub.add_css_class("caption")
        where_sub.add_css_class("dim-label")
        where_sub.set_ellipsize(3)
        box.append(where_sub)
        self.set_child(box)


def _leaf(cwd: str) -> str:
    parts = [p for p in cwd.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else (cwd or "(root)")


def _humanize(ts: float) -> str:
    if not ts:
        return ""
    delta = datetime.now(timezone.utc).timestamp() - ts
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    if delta < 86400 * 30:
        return f"{int(delta / 86400)}d ago"
    return datetime.fromtimestamp(ts).astimezone().strftime("%b %-d")
