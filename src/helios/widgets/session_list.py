"""Unified session list — the primary navigation surface.

One merged, newest-first list of every session on this machine plus the
other hosts' sessions from the shared iCloud pool. The old per-cwd "project"
level is gone as navigation (it collapsed: ~70% of sessions live in the
/home/<user> catch-all, and the MR workflow's `_tmp_*` clone dirs made the
rest noise) — a session's project dir is now just metadata:

  * host chip — pool sessions show their origin host (read-only)
  * the local cwd is not shown on the row (per Spencer); it stays available
    via the per-cwd filter choices and the row's right-click "Copy folder path"

A filter dropdown (All / This machine / per-cwd labels / per-host /
Temporary) replaces project selection. Throwaway cwds (/tmp/*, _tmp_*) are
hidden from All and live behind the Temporary filter.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from helios.backend import session_providers
from helios.backend.project_names import store as name_store
from helios.backend.projects import (
    Session,
    discover_local_sessions,
    discover_pool_sessions,
    drop_empty_sessions,
    is_throwaway_cwd,
)
from helios.backend.session_state import (
    STATE_AWAITING,
    STATE_ERRORED,
    STATE_IDLE,
    STATE_READY,
    STATE_WORKING,
    SessionStatus,
    discover_active_session_ids,
    status_for,
)
from helios.backend.process.title_generator import TitleGenerator, store as title_store
from helios.backend.ui_state import store as ui_state_store
from helios.log import get_logger
from helios.widgets._motion import BASE_MS

_log = get_logger("sessions")


# Sort priority — lowest sorts to the top. Mirrors what most wants attention:
#   working  -- claude is actually doing something right now
#   awaiting -- you typed and never got a reply; act now
#   errored  -- last action returned an error
#   ready    -- a process is alive but idle, waiting for input
#   idle     -- nothing pending, no process
_STATE_SORT_RANK = {
    STATE_WORKING: 0,
    STATE_AWAITING: 1,
    STATE_ERRORED: 2,
    STATE_READY: 3,
    STATE_IDLE: 4,
}

FILTER_ALL = "all"
FILTER_LOCAL = "local"
FILTER_TEMP = "temp"
# plus dynamic "host:<origin>" and "cwd:<path>" values.


class SessionList(Gtk.Box):
    __gsignals__ = {
        "session-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Emitted after a delete completes; carries a list[str] of session ids.
        "sessions-deleted": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Emitted from a row's right-click "Stop session"; carries session_id.
        "session-stop-requested": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    # Cap how many uncached titles we eagerly LLM-generate per render. The top
    # N most-recent rows get auto-generation; older rows fall back to the
    # first-message title until the user actually clicks one, at which point
    # _on_row_selected queues that row's title individually.
    _TITLE_GEN_TOP_N = 30

    _STATUS_REFRESH_MS = 2000

    # Frames to keep re-asserting the restored scroll offset for. One is not
    # enough: see _restore_scroll.
    _SCROLL_RESTORE_FRAMES = 3

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("helios-session-list")

        self._ui_state = ui_state_store()

        # Header — title + subtitle on the left, select-mode toggle on the right.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(12)
        header.set_margin_bottom(4)
        header.set_margin_start(16)
        header.set_margin_end(10)

        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_col.set_hexpand(True)
        self._title = Gtk.Label(label="Sessions", xalign=0)
        self._title.add_css_class("title-4")
        self._title.set_ellipsize(3)
        self._subtitle = Gtk.Label(xalign=0)
        self._subtitle.add_css_class("dim-label")
        self._subtitle.add_css_class("caption")
        text_col.append(self._title)
        text_col.append(self._subtitle)
        header.append(text_col)

        self._select_btn = Gtk.ToggleButton()
        self._select_btn.set_icon_name("object-select-symbolic")
        self._select_btn.add_css_class("flat")
        self._select_btn.set_tooltip_text("Select multiple sessions to delete")
        self._select_btn.set_valign(Gtk.Align.CENTER)
        self._select_btn.connect("toggled", self._on_select_mode_toggled)
        header.append(self._select_btn)

        self.append(header)

        # Filter dropdown — replaces the old projects panel. Choices rebuilt
        # on every reload from what's actually on disk.
        self._filter: str = str(self._ui_state.get("session_filter", FILTER_ALL))
        self._filter_ids: list[str] = [FILTER_ALL]
        self._filter_updating = False
        self._filter_dd = Gtk.DropDown.new_from_strings(["All sessions"])
        self._filter_dd.add_css_class("helios-session-filter")
        self._filter_dd.set_margin_start(12)
        self._filter_dd.set_margin_end(12)
        self._filter_dd.set_margin_bottom(8)
        self._filter_dd.connect("notify::selected", self._on_filter_changed)
        self.append(self._filter_dd)

        # List.
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._listbox.add_css_class("navigation-sidebar")
        self._row_selected_handler = self._listbox.connect(
            "row-selected", self._on_row_selected
        )
        # selected-rows-changed fires in MULTIPLE mode, used to update the
        # "Delete N" counter in the action bar.
        self._listbox.connect("selected-rows-changed", self._on_selected_rows_changed)

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_vexpand(True)
        self.append(self._scroller)
        # Scroll offset to put back after a rebuild, and how many more frames
        # to keep asserting it for. Non-None also means "a restore tick is in
        # flight", which is what keeps a second render from stacking another.
        self._pending_scroll: float | None = None
        self._scroll_frames = 0
        # Handle for the in-flight restore tick. Held so a cancel can actually
        # unregister it: clearing _pending_scroll alone leaves the callback
        # registered, and a later preserving render would then register a
        # SECOND one. Both decrement the shared _scroll_frames, so the 3-frame
        # budget burns in ~2 frames — below the 2-frame minimum measured on
        # GTK 4.22, i.e. a restore that lands at the wrong offset.
        self._scroll_tick: int | None = None

        # Action bar — revealed only in select mode.
        self._action_revealer = Gtk.Revealer()
        self._action_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._action_revealer.set_transition_duration(BASE_MS)
        self._action_revealer.set_reveal_child(False)

        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.add_css_class("helios-action-bar")
        action_bar.set_margin_start(12)
        action_bar.set_margin_end(12)
        action_bar.set_margin_top(8)
        action_bar.set_margin_bottom(12)

        self._select_all_btn = Gtk.Button.new_with_label("Select all")
        self._select_all_btn.add_css_class("flat")
        self._select_all_btn.connect("clicked", lambda *_: self._select_all())
        action_bar.append(self._select_all_btn)

        cancel_btn = Gtk.Button.new_with_label("Cancel")
        cancel_btn.add_css_class("flat")
        cancel_btn.connect("clicked", lambda *_: self._set_select_mode(False))
        action_bar.append(cancel_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        action_bar.append(spacer)

        self._delete_btn = Gtk.Button.new_with_label("Delete")
        self._delete_btn.add_css_class("destructive-action")
        self._delete_btn.set_sensitive(False)
        self._delete_btn.connect("clicked", lambda *_: self._confirm_delete())
        action_bar.append(self._delete_btn)

        self._action_revealer.set_child(action_bar)
        self.append(self._action_revealer)

        # Empty state placeholder.
        self._empty = Gtk.Label(label="No sessions yet — start a new chat.")
        self._empty.add_css_class("dim-label")
        self._empty.set_vexpand(True)
        self._empty.set_valign(Gtk.Align.CENTER)
        self._scroller.set_child(self._empty)

        # Merged session caches. Local is rescanned cheaply on every reload;
        # the pool lives on a soft CIFS mount and is rescanned off-thread
        # only when asked (manual reload / startup).
        self._local_sessions: list[Session] = []
        self._pool_sessions: list[Session] = []
        # Bumped per reload; in-flight pool scans / title workers check it
        # and abandon work that belongs to a list we've since rebuilt.
        self._reload_gen = 0

        self._select_mode = False

        # Session ids that MainWindow knows are live (e.g. its own driver
        # whose id was just emitted in `system/init`). The /proc scan can't
        # see these because they aren't in the subprocess's argv until
        # after `--resume <id>` would have been passed (which it wasn't for
        # fresh chats). MainWindow pushes updates via set_live_session_ids.
        self._live_session_ids: set[str] = set()

        # LLM-backed title cache + async generator.
        self._title_store = title_store()
        self._title_gen = TitleGenerator()
        self._title_gen_handler_id = self._title_gen.connect(
            "title-generated", self._on_title_generated
        )
        # session_id -> row, for live updates when a title arrives.
        self._rows_by_id: dict[str, _SessionRow] = {}
        # session_id -> "Reading foo.py"; survives a re-render.
        self._live_activity: dict[str, str] = {}
        # Sessions with a background question waiting on the user (a "needs you"
        # dot), driven by MainWindow. Survives row rebuilds (re-applied on render).
        self._pending_q_ids: set[str] = set()

        # Periodic status refresh — re-derives every visible LOCAL row's
        # status on a background thread and recolors changed dots in place.
        # Pool rows are skipped: they're archive copies on a slow mount and
        # always idle.
        self._status_tick_running = False
        # Reload generation whose POOL chips have been resolved; -1 so the
        # first tick includes them.
        self._pool_provider_gen = -1
        self._status_timer_id = GLib.timeout_add(
            self._STATUS_REFRESH_MS, self._tick_status
        )
        # Set on shutdown() so the status timer and any in-flight idle_add from
        # the pool/title/status daemon threads bail out instead of touching a
        # finalizing widget tree.
        self._destroyed = False

    # ── Public API ───────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Stop the status timer and ignore in-flight background callbacks.

        Called by the window on close so a daemon thread that resolves a title
        or status right as the widgets finalize can't crash on idle_add."""
        self._destroyed = True
        # A title subprocess/local-LLM request can finish long after the list
        # has been torn down. Disconnect the producer as well as guarding the
        # callback below: the guard covers an emission already dispatched,
        # while disconnect prevents future emissions from retaining this list.
        if self._title_gen_handler_id:
            try:
                self._title_gen.disconnect(self._title_gen_handler_id)
            except Exception:
                pass
            self._title_gen_handler_id = 0
        if self._status_timer_id:
            try:
                GLib.source_remove(self._status_timer_id)
            except Exception:
                pass
            self._status_timer_id = 0

    def reload(
        self, *, preserve_selection: bool = False, rescan_pool: bool = True
    ) -> None:
        """Re-discover sessions and rebuild the list.

        Local discovery is synchronous (cheap). When `rescan_pool` is set the
        pool is re-scanned on a daemon thread and merged in when ready —
        otherwise the previously-scanned pool sessions are reused (callers on
        hot paths like turn-end pass rescan_pool=False so a CIFS walk isn't
        triggered every turn).

        Pool sessions are opt-in (`show_pool_sessions`, default off): the
        read-only rows from other hosts can't be resumed here and mostly add
        noise. When the setting is off, previously merged pool rows are
        dropped too, so flipping the toggle takes effect on the next reload."""
        self._reload_gen += 1
        gen = self._reload_gen
        # Hide sidecar-only ghost rows (CLI-written ai-title/last-prompt stubs
        # with no conversation). Always keep: sessions Helios is actively driving
        # (live_ids), the currently-selected row (so a reload never yanks the
        # user out of the chat they're viewing, even in the content-less edge
        # case), and ones just created. See projects.drop_empty_sessions.
        keep_ids = set(self._live_session_ids)
        sel_row = self._listbox.get_selected_row()
        if sel_row is not None and getattr(sel_row, "session", None) is not None:
            keep_ids.add(sel_row.session.session_id)
        self._local_sessions = drop_empty_sessions(
            discover_local_sessions(),
            live_ids=keep_ids,
            now=time.time(),
        )
        if not ui_state_store().get("show_pool_sessions", False):
            self._pool_sessions = []
            rescan_pool = False
        self._rebuild_filter_choices()
        self._render(preserve_selection=preserve_selection)

        if not rescan_pool:
            return

        def _scan_pool() -> None:
            try:
                # Discovery also proves/removes descendant-only transcripts
                # while this worker owns the slow pool I/O. The GTK callback
                # below only assigns the already-filtered result.
                remote = discover_pool_sessions()
            except Exception:
                remote = []
            GLib.idle_add(self._apply_pool_sessions, remote, gen)

        threading.Thread(
            target=_scan_pool, name="helios-pool-discover", daemon=True
        ).start()

    def has_rows(self) -> bool:
        """Any session rows currently rendered? (Used by startup to decide
        between selecting a session and showing the fresh-chat UI.)"""
        return bool(self._rows_by_id)

    def set_live_session_ids(self, ids: set[str]) -> None:
        """Tell the list which session ids belong to drivers Helios itself is
        currently running, so dots flip to "active" without waiting for /proc
        detection. Re-renders preserving selection — must NOT auto-select the
        first row (that would fire session-selected and yank the user out of
        the chat they just started)."""
        # driver_manager.register_started()/forget() emit live-id changes; a
        # driver registering or exiting during window close would otherwise
        # re-render the finalizing list. Refuse the projection after shutdown.
        if self._destroyed:
            return
        if ids == self._live_session_ids:
            return
        self._live_session_ids = set(ids)
        if self._local_sessions or self._pool_sessions:
            self._render(preserve_selection=True)

    def set_live_activity(self, session_id: str, text: str) -> None:
        """Push one session's current activity onto its row, if it has one.

        Deliberately does NOT re-render: this arrives on every stream delta of
        every running session, and a full list rebuild at that rate would make
        the sidebar unusable. Held in a dict as well as on the widget so a
        re-render for some unrelated reason does not blank a live row.
        """

        if self._destroyed or not session_id:
            return
        text = (text or "").strip()
        if text:
            self._live_activity[session_id] = text
        else:
            self._live_activity.pop(session_id, None)
        row = self._rows_by_id.get(session_id)
        if row is not None:
            row.set_live_activity(text)

    def select_session(self, session_id: str) -> bool:
        """Select the row for `session_id` if it's currently visible.

        Returns True if a matching row was found and selected. Safe to call
        for the live session: MainWindow's session-selected handler early-
        returns when the id matches the running driver."""
        row = self._rows_by_id.get(session_id)
        if row is None:
            return False
        if self._listbox.get_selected_row() is row:
            # Programmatic provider switching may need to reopen the already-
            # selected native row after a temporary blank/new sibling view.
            # Gtk emits no row-selected change for selecting the same row.
            self._on_row_selected(self._listbox, row)
        else:
            self._listbox.select_row(row)
        return True

    def reveal_session(self, session_id: str) -> bool:
        """Like select_session, but if the row is filtered out, switch to a
        filter that can contain it and try again. ALL holds every non-throwaway
        session (incl. pool rows); TEMP holds the throwaway (`_tmp_`/`/tmp`)
        ones — so a background session anywhere can always be surfaced."""
        if self.select_session(session_id):
            return True
        original = self._filter
        for candidate in (FILTER_ALL, FILTER_TEMP):
            if candidate != self._filter and candidate in self._filter_ids:
                self._set_filter(candidate)
                if self.select_session(session_id):
                    return True
        # Not found anywhere — don't strand the user on a wrong filter.
        if self._filter != original and original in self._filter_ids:
            self._set_filter(original)
        return False

    def invalidate_session_project(self, session: Session) -> None:
        """Drop the cached transcript list for one session's project, so the
        next reload re-reads it from disk."""
        session.project.invalidate_sessions()

    # ── Filter ───────────────────────────────────────────────────────

    def _rebuild_filter_choices(self) -> None:
        """Derive the dropdown's choices from what's actually on disk:
        All · This machine · one entry per distinct local cwd (custom label
        if renamed) · one entry per pool host · Temporary (only if any)."""
        names = name_store()
        labels: list[str] = ["All sessions", "This machine"]
        ids: list[str] = [FILTER_ALL, FILTER_LOCAL]

        cwds: dict[str, float] = {}
        has_temp = False
        for s in self._local_sessions:
            cwd = s.project.cwd
            if is_throwaway_cwd(cwd):
                has_temp = True
                continue
            cwds[cwd] = max(cwds.get(cwd, 0.0), s.mtime)
        for cwd in sorted(cwds, key=lambda c: -cwds[c]):
            label = names.get(cwd) or _short_cwd(cwd)
            labels.append(label)
            ids.append(f"cwd:{cwd}")

        hosts = sorted({s.project.origin for s in self._pool_sessions})
        for host in hosts:
            labels.append(host)
            ids.append(f"host:{host}")

        if has_temp:
            labels.append("Temporary")
            ids.append(FILTER_TEMP)

        if self._filter not in ids:
            self._filter = FILTER_ALL

        self._filter_ids = ids
        self._filter_updating = True
        try:
            self._filter_dd.set_model(Gtk.StringList.new(labels))
            self._filter_dd.set_selected(ids.index(self._filter))
        finally:
            self._filter_updating = False

    def _on_filter_changed(self, dd: Gtk.DropDown, _pspec) -> None:
        if self._filter_updating:
            return
        idx = dd.get_selected()
        if idx < 0 or idx >= len(self._filter_ids):
            return
        new = self._filter_ids[idx]
        if new == self._filter:
            return
        self._filter = new
        self._ui_state.set("session_filter", new)
        # Filter change behaves like a fresh load: auto-select the first row.
        self._render(preserve_selection=False)

    def _set_filter(self, filter_id: str) -> None:
        """Programmatic filter change (reveal_session probing). Renders with
        preserve_selection=True so it never auto-selects row 0 — that would
        fire a spurious session-selected (and, via _pump_questions, could
        present the WRONG session's question). reveal_session selects the
        target explicitly afterward."""
        self._filter = filter_id
        self._ui_state.set("session_filter", filter_id)
        if filter_id in self._filter_ids:
            self._filter_updating = True
            try:
                self._filter_dd.set_selected(self._filter_ids.index(filter_id))
            finally:
                self._filter_updating = False
        self._render(preserve_selection=True)

    def _matches_filter(self, s: Session) -> bool:
        f = self._filter
        ro = s.project.read_only
        temp = not ro and is_throwaway_cwd(s.project.cwd)
        if f == FILTER_ALL:
            return not temp
        if f == FILTER_LOCAL:
            return not ro and not temp
        if f == FILTER_TEMP:
            return temp
        if f.startswith("host:"):
            return ro and s.project.origin == f[5:]
        if f.startswith("cwd:"):
            return not ro and s.project.cwd == f[4:]
        return True

    # ── Rendering ────────────────────────────────────────────────────

    def _apply_pool_sessions(self, remote: list[Session], gen: int) -> bool:
        # Drop results from a scan a newer reload has superseded.
        if self._destroyed or gen != self._reload_gen:
            return False
        self._pool_sessions = remote
        self._rebuild_filter_choices()
        self._render(preserve_selection=True)
        return False

    def _render(self, *, preserve_selection: bool) -> None:
        # Remember which session id was selected before the rebuild so we
        # can restore it when re-rendering only to update metadata.
        prior_selected_id = ""
        if preserve_selection:
            sel = self._listbox.get_selected_row()
            if sel is not None and isinstance(sel, _SessionRow):
                prior_selected_id = sel.session.session_id

        # Emptying the listbox destroys whichever row had focus, and GTK
        # answers that by moving focus to the top of the list and letting the
        # viewport's scroll-to-focus animate the sidebar up there — so the
        # list snapped to the top on every completed turn. Read the offset
        # before the rebuild; _restore_scroll puts it back.
        #
        # `is None` does double duty: it keeps the FIRST, pre-rebuild reading
        # and it means at most one restore tick is ever in flight. Note the
        # window is the whole restore, not just one frame -- a second
        # preserve_selection render landing during those ~3 frames is skipped,
        # so a user who scrolls inside that window gets yanked back to the
        # pre-first-render offset. Narrow (~50ms) and preferable to the
        # alternative, which is re-capturing a mid-animation offset.
        #
        # Only under preserve_selection: a preserve_selection=False render
        # deliberately selects row 0, and the top is the right place to be.
        offset = 0.0
        if preserve_selection and self._pending_scroll is None:
            offset = self._scroller.get_vadjustment().get_value()

        # When preserving selection, block the row-selected handler for the
        # ENTIRE rebuild — removing the currently-selected row from a
        # Gtk.ListBox automatically fires row-selected(None), which would
        # otherwise route through MainWindow → welcome page mid-chat.
        if preserve_selection:
            self._listbox.handler_block(self._row_selected_handler)
        try:
            self._render_inner(prior_selected_id, preserve_selection)
        finally:
            if preserve_selection:
                self._listbox.handler_unblock(self._row_selected_handler)

        if offset > 0:
            self._pending_scroll = offset
            self._scroll_frames = self._SCROLL_RESTORE_FRAMES
            # Unreachable today and kept anyway: the `_pending_scroll is None`
            # capture gate above already means a second preserving render
            # inside the window computes offset 0 and never gets here, so
            # removing this guard breaks no test. It states the invariant at
            # the line that would violate it — two ticks would share
            # `_scroll_frames` and halve the window — rather than leaving it
            # implied by a condition 20 lines up.
            if self._scroll_tick is None:
                self._scroll_tick = self.add_tick_callback(self._restore_scroll)
        elif not preserve_selection:
            # A non-preserving render means "land at the top", and it can arrive
            # while a previous restore is still in flight. Leaving the old
            # offset armed lets those remaining frames drag the sidebar back to
            # where it was before a render that deliberately selected row 0 —
            # the restore would silently overrule the newer intent. Clearing is
            # enough to stop it: the already-registered tick reads None on its
            # next frame and removes itself.
            self._cancel_scroll_restore()

    def _cancel_scroll_restore(self) -> None:
        """Drop a pending restore and unregister its tick.

        Both halves matter. Clearing the offset stops the value being
        reapplied; removing the callback stops a later render registering a
        second one alongside it, which would share `_scroll_frames` and halve
        the effective restore window.
        """
        self._pending_scroll = None
        self._scroll_frames = 0
        if self._scroll_tick is not None:
            self.remove_tick_callback(self._scroll_tick)
            self._scroll_tick = None

    def _render_inner(self, prior_selected_id: str, preserve_selection: bool) -> None:
        # A one-render PAINT HINT, not a cache: it is written only into a
        # freshly built row's initial chip, never consulted for routing, and
        # the next _apply_status_updates overwrites it unconditionally. A
        # session appearing for the first time correctly gets None ("…").
        prior_providers = {
            sid: row.provider_resolution for sid, row in self._rows_by_id.items()
        }
        while (row := self._listbox.get_first_child()) is not None:
            self._listbox.remove(row)

        sessions = [
            s
            for s in self._local_sessions + self._pool_sessions
            if self._matches_filter(s)
        ]

        if not sessions:
            self._rows_by_id.clear()
            self._subtitle.set_label("no sessions")
            self._scroller.set_child(self._empty)
            return

        self._scroller.set_child(self._listbox)

        # Probe each LOCAL session for its current status. Pool sessions are
        # frozen archive copies on a slow CIFS mount — statting/tailing them
        # here (and on every 2s tick) would hitch the UI for nothing, so they
        # get a static idle status.
        active_ids = discover_active_session_ids(extra=self._live_session_ids)
        statuses: dict[str, SessionStatus] = {}
        for s in sessions:
            if s.project.read_only:
                statuses[s.session_id] = SessionStatus(
                    state=STATE_IDLE, last_response_at=0.0
                )
            else:
                statuses[s.session_id] = status_for(s, active_ids)

        # Sort by (state-priority, last_response_at desc) — see _STATE_SORT_RANK.
        sessions.sort(
            key=lambda s: (
                _STATE_SORT_RANK.get(statuses[s.session_id].state, 99),
                -(statuses[s.session_id].last_response_at or s.mtime),
            ),
        )

        n_remote = sum(1 for s in sessions if s.project.read_only)
        label = _pluralize(len(sessions), "session", "sessions")
        if n_remote:
            label += f" · {n_remote} remote"
        self._subtitle.set_label(label)

        # Insert rows; titles come from (1) the LLM cache if present, (2) the
        # first-user-message fallback resolved lazily, (3) live generation
        # via TitleGenerator filling in cache misses in the background.
        self._rows_by_id.clear()
        # The host chip is redundant when the filter already pins that host.
        show_host_chips = not self._filter.startswith("host:")
        for s in sessions:
            cached = self._title_store.get(s.session_id)
            if cached:
                s.title = cached
                s.first_message_loaded = True
            row = _SessionRow(
                s,
                statuses[s.session_id],
                on_delete=self._row_delete_one,
                on_stop=lambda sess: self.emit("session-stop-requested", sess.session_id),
                show_host_chip=show_host_chips,
                provider=prior_providers.get(s.session_id),
            )
            live = self._live_activity.get(s.session_id)
            if live:
                row.set_live_activity(live)
            self._rows_by_id[s.session_id] = row
            self._listbox.append(row)

        # Resolve the chips (and statuses) now rather than up to
        # _STATUS_REFRESH_MS later. Queued as an idle so it lands after
        # _render has armed the scroll restore.
        GLib.idle_add(self._kick_status_tick)

        # Re-assert pending-question dots onto the freshly built rows.
        for sid in self._pending_q_ids:
            row = self._rows_by_id.get(sid)
            if row is not None:
                row.set_pending_question(True)

        # Selection logic. When preserve_selection is on, the outer caller
        # has already blocked row-selected — these selects are silent.
        if preserve_selection and prior_selected_id:
            target_row = self._rows_by_id.get(prior_selected_id)
            if target_row is not None:
                self._listbox.select_row(target_row)
        elif not preserve_selection and self._listbox.get_row_at_index(0):
            self._listbox.select_row(self._listbox.get_row_at_index(0))

        # Kick off the fallback-title pass + LLM generation in parallel.
        # LLM titles are a *bonus* over the first-message fallback. Skip them
        # for read-only pooled (remote) sessions: the fallback already titles
        # them, and titling-via-claude would (a) re-read the jsonl over the
        # slow CIFS mount and (b) spend tokens on another machine's history.
        self._resolve_titles_async(sessions)
        local_recent = [s for s in sessions if not s.project.read_only]
        for s in local_recent[: self._TITLE_GEN_TOP_N]:
            if not self._title_store.get(s.session_id):
                self._title_gen.request(s)

    def _restore_scroll(self, _widget: Gtk.Widget, _clock: object) -> bool:
        """Put the sidebar back where it was, for the first few frames after a rebuild.

        A frame-clock callback rather than GLib.idle_add, and repeated rather
        than one-shot, because what moves the offset is an *animation*, not a
        single assignment: the focus lost with the old rows makes the viewport
        smooth-scroll to the top over ~20 frames. A restore that fires once
        either lands before that animation starts (and is overwritten) or
        after (and fights it), which is why a single idle only held in about
        half of the runs measured. Re-asserting the value on consecutive
        frames cancels the animation instead of racing it.

        Measured on GTK 4.22, 14 interleaved runs per arm of a rebuild that
        drops the focused row: no restore 0/14, single idle ~3/6, this 14/14.

        Getting the timing wrong lands at the wrong offset; it cannot crash.
        """
        value = self._pending_scroll
        if value is None or self._destroyed:
            self._pending_scroll = None
            self._scroll_tick = None
            return GLib.SOURCE_REMOVE

        # No manual clamp: Gtk.Adjustment.set_value already pins to
        # [lower, upper - page-size], so a rebuild that produced a shorter
        # list lands at its own bottom rather than refusing to move.
        self._scroller.get_vadjustment().set_value(value)

        self._scroll_frames -= 1
        if self._scroll_frames > 0:
            return GLib.SOURCE_CONTINUE
        self._pending_scroll = None
        # Release the handle on the way out. GTK unregisters a callback that
        # returns SOURCE_REMOVE, so _cancel_scroll_restore must never call
        # remove_tick_callback on this id afterwards — dropping it here is what
        # guarantees that, and it lets the next render register cleanly.
        self._scroll_tick = None
        return GLib.SOURCE_REMOVE

    def _resolve_titles_async(self, sessions: list[Session]) -> None:
        """Pre-LLM fallback: pull each session's first-user-message text out of
        the JSONL so rows show *something* before/instead of LLM titles.

        Runs on a BACKGROUND THREAD: pooled sessions live on a soft
        CIFS/iCloud mount where each open can take a second or more. Row label
        updates are marshalled back to the main thread via idle_add."""
        pending = [s for s in sessions if not self._title_store.get(s.session_id)]
        if not pending:
            return
        gen = self._reload_gen

        def worker() -> None:
            for s in pending:
                if gen != self._reload_gen:
                    return  # list rebuilt — abandon stale work
                try:
                    s.ensure_title()
                except Exception:
                    continue
                GLib.idle_add(self._apply_resolved_title, s.session_id, gen)

        threading.Thread(target=worker, name="helios-title-resolve", daemon=True).start()

    def _apply_resolved_title(self, session_id: str, gen: int) -> bool:
        if self._destroyed or gen != self._reload_gen:
            return False  # belongs to a list we've since rebuilt
        row = self._rows_by_id.get(session_id)
        if row is not None:
            row.refresh_title()
        return False

    def _on_title_generated(self, _gen, session_id: str, title: str) -> None:
        if self._destroyed:
            return
        row = self._rows_by_id.get(session_id)
        if row is None:
            return
        row.session.title = title
        row.session.first_message_loaded = True
        row.refresh_title()

    def set_pending_questions(self, session_ids) -> None:
        """Reconcile which rows show a 'needs you' pending-question dot.

        MainWindow passes the full set each time (derived from its question
        queue); we diff against the last set and repaint only what changed."""
        ids = {sid for sid in session_ids if sid}
        if ids == self._pending_q_ids:
            return
        changed = ids ^ self._pending_q_ids
        self._pending_q_ids = ids
        for sid in changed:
            row = self._rows_by_id.get(sid)
            if row is not None:
                row.set_pending_question(sid in ids)

    # ── Live status refresh ──────────────────────────────────────────

    def _tick_status(self) -> bool:
        if self._destroyed:
            return False  # drop the timer
        # Repeating GLib timer. Nothing loaded → idle but keep ticking.
        if not self._rows_by_id:
            return True
        # Don't stack workers if the previous tick is still scanning.
        if self._status_tick_running:
            return True

        gen = self._reload_gen
        live = set(self._live_session_ids)
        # Provider evidence is re-derived here for every row on every tick and
        # is never memoized on the row: it is a fail-closed safety check, not a
        # one-shot answer to cache. The only memo is
        # session_providers._provider_from_transcript's lru_cache, keyed on
        # (path, session_id, mtime_ns, size), which re-walks a file the instant
        # that file changes.
        #
        # Statuses stay LOCAL-only: pool rows are static archives on a slow
        # CIFS mount. Pool CHIPS keep their pre-existing once-per-reload
        # cadence for the same reason — resolve_provider stats the transcript
        # unconditionally, ahead of that lru_cache, so a 2s timer would put one
        # stat per pool row on the shared mount forever. _pool_provider_gen is
        # advanced in _apply_status_updates, not here, so a tick whose results
        # are discarded is retried instead of counted.
        include_pool = self._pool_provider_gen != gen
        provider_rows = [
            (sid, row.session)
            for sid, row in self._rows_by_id.items()
            if include_pool or not row.session.project.read_only
        ]
        rows = [
            (sid, sess)
            for sid, sess in provider_rows
            if not sess.project.read_only
        ]
        if not provider_rows:
            return True
        self._status_tick_running = True

        def worker() -> None:
            try:
                active_ids = discover_active_session_ids(extra=live)
                updates = {sid: status_for(sess, active_ids) for sid, sess in rows}
            except Exception:
                updates = {}
            providers = {}
            for sid, sess in provider_rows:
                try:
                    providers[sid] = session_providers.resolve_provider(
                        sid, sess.path
                    )
                except Exception:
                    providers[sid] = session_providers.ProviderResolution()
            GLib.idle_add(self._apply_status_updates, gen, updates, providers)

        threading.Thread(
            target=worker, name="helios-status-tick", daemon=True
        ).start()
        return True  # keep the timer alive

    def _kick_status_tick(self) -> bool:
        """Resolve statuses + chips right after a rebuild instead of waiting
        out the timer. One-shot: _tick_status's own return value drives the
        repeating timer, not this idle."""
        if not self._destroyed:
            self._tick_status()
        return False

    def _apply_status_updates(self, gen: int, updates: dict, providers: dict) -> bool:
        self._status_tick_running = False
        # List rebuilt while the worker ran → its statuses are stale.
        if self._destroyed or gen != self._reload_gen:
            return False
        # Only burn the generation if this tick actually RESOLVED a pool row.
        # _reload_gen advances solely in reload(); every other render path
        # (_apply_pool_sessions, _on_filter_changed, _set_filter,
        # set_live_session_ids) re-renders on the same one. So a tick that ran
        # before the CIFS scan landed — i.e. with no pool rows in
        # _rows_by_id — used to mark the generation done, and every pool row
        # arriving afterwards was excluded from provider_rows for the life of
        # that generation, leaving its chip at "…" permanently. Cold start hits
        # this every time: reload(rescan_pool=True) renders with
        # _pool_sessions == [] and kicks a tick, and the scan lands seconds
        # later on the same generation.
        if any(
            (row := self._rows_by_id.get(sid)) is not None
            and row.session.project.read_only
            for sid in providers
        ):
            self._pool_provider_gen = gen
        for sid, status in updates.items():
            row = self._rows_by_id.get(sid)
            if row is None:
                continue
            if row.status.state != status.state:
                row.update_status(status)
            else:
                row.update_age(status)
        for sid, resolution in providers.items():
            row = self._rows_by_id.get(sid)
            if row is not None:
                row.update_provider(resolution)
        return False  # one-shot idle callback

    def _on_row_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        # Suppressed entirely while in select mode — we use multi-select
        # there and don't want to switch the transcript view on each click.
        if self._select_mode:
            return
        if row is None:
            self.emit("session-selected", None)
            return
        # Lazy title generation: when the user actually focuses an older
        # row, queue its title if uncached.
        s = getattr(row, "session", None)
        if (
            s is not None
            and not s.project.read_only
            and not self._title_store.get(s.session_id)
        ):
            self._title_gen.request(s)
        if s is not None:
            self.emit("session-selected", s)

    # ── Select / delete mode ─────────────────────────────────────────

    def _on_select_mode_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._set_select_mode(btn.get_active())

    def _set_select_mode(self, on: bool) -> None:
        if on == self._select_mode:
            if self._select_btn.get_active() != on:
                self._select_btn.set_active(on)
            return
        self._select_mode = on
        self._select_btn.handler_block_by_func(self._on_select_mode_toggled)
        self._select_btn.set_active(on)
        self._select_btn.handler_unblock_by_func(self._on_select_mode_toggled)

        if on:
            self._listbox.unselect_all()
            self._listbox.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        else:
            self._listbox.unselect_all()
            self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            first = self._listbox.get_row_at_index(0)
            if first is not None:
                self._listbox.select_row(first)

        self._action_revealer.set_reveal_child(on)
        self._update_delete_label()

    def _select_all(self) -> None:
        if not self._select_mode:
            return
        i = 0
        while True:
            row = self._listbox.get_row_at_index(i)
            if row is None:
                break
            # Pool sessions are another machine's archive — never bulk-select
            # them for deletion from here.
            s = getattr(row, "session", None)
            if s is not None and not s.project.read_only:
                self._listbox.select_row(row)
            i += 1

    def _on_selected_rows_changed(self, *_args) -> None:
        if not self._select_mode:
            return
        self._update_delete_label()

    def _deletable_selected_sessions(self) -> list[Session]:
        rows = self._listbox.get_selected_rows()
        sessions = [getattr(r, "session", None) for r in rows]
        return [s for s in sessions if s is not None and not s.project.read_only]

    def _update_delete_label(self) -> None:
        n = len(self._deletable_selected_sessions()) if self._select_mode else 0
        self._delete_btn.set_label(f"Delete {n}" if n else "Delete")
        self._delete_btn.set_sensitive(n > 0)

    def _confirm_delete(self) -> None:
        sessions = self._deletable_selected_sessions()
        if sessions:
            self._show_delete_dialog(sessions)

    def _row_delete_one(self, session: Session) -> None:
        """Inline/right-click delete path — single session, no select mode."""
        if session.project.read_only:
            return
        self._show_delete_dialog([session])

    def _show_delete_dialog(self, sessions: list[Session]) -> None:
        n = len(sessions)
        if n == 1:
            heading = "Delete this session?"
            title = sessions[0].display_title
            body = (
                f"“{title[:80]}” will be permanently deleted. "
                "This cannot be undone."
            )
            delete_label = "Delete"
        else:
            heading = f"Delete {n} sessions?"
            body = (
                f"Permanently remove {n} transcript files. This cannot be undone."
            )
            delete_label = f"Delete {n}"

        dlg = Adw.AlertDialog.new(heading, body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", delete_label)
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._on_delete_response, sessions)

        root = self.get_root()
        if root is None:
            return
        dlg.present(root)

    def _on_delete_response(self, _dlg, response: str, sessions: list[Session]) -> None:
        if response != "delete":
            return
        deleted_ids: list[str] = []
        touched_projects = set()
        for s in sessions:
            try:
                s.path.unlink()
                deleted_ids.append(s.session_id)
                touched_projects.add(id(s.project))
            except OSError as e:
                _log.warning("failed to delete %s: %s", s.path, e)
            # Also clean any auxiliary directory with the same uuid name —
            # claude sometimes writes there for partial state.
            aux = s.path.with_suffix("")
            if aux.is_dir():
                import shutil
                try:
                    shutil.rmtree(aux)
                except OSError:
                    pass
        self.emit("sessions-deleted", deleted_ids)
        # MainWindow's synchronous handler still needs the provider mapping to
        # remove the matching conversation-scoped execution record. Forget it
        # only after observers have handled the deletion.
        for session_id in deleted_ids:
            session_providers.forget(session_id)
        self._set_select_mode(False)
        # Re-discover from disk (cheap, local only). discover_local_sessions
        # builds fresh Project objects, so no stale per-Project caches survive.
        self.reload(rescan_pool=False)


_DOT_STATE_CLASS = {
    STATE_WORKING: "helios-status-working",
    STATE_READY: "helios-status-ready",
    STATE_AWAITING: "helios-status-awaiting",
    STATE_ERRORED: "helios-status-errored",
    STATE_IDLE: "helios-status-idle",
}

# A background session that asked YOU a question — surfaced as a distinct dot
# instead of a surprise modal. Overrides the disk-derived state while pending.
_QUESTION_DOT_CLASS = "helios-status-question"

# Every state class a dot can carry — used to clear stale classes before
# applying the current one on a live update.
_ALL_DOT_CLASSES = tuple(_DOT_STATE_CLASS.values()) + (_QUESTION_DOT_CLASS,)

# Worded to match the dot colours: green says "complete" for BOTH ready and
# idle, which differ only in whether a process is still loaded behind it.
_DOT_TOOLTIP = {
    STATE_WORKING: "Working — this session is running a turn right now",
    STATE_READY: "Complete — the last turn finished; the session is still open and waiting for your next message",
    STATE_AWAITING: "Stuck — the last message has no reply and nothing is running",
    STATE_ERRORED: "Errored — the last action returned an error",
    STATE_IDLE: "Complete — the last turn finished cleanly (nothing running)",
}


class _SessionRow(Gtk.ListBoxRow):
    def __init__(
        self,
        session: Session,
        status,
        on_delete,
        on_stop=None,
        *,
        show_host_chip: bool = True,
        provider=None,
    ) -> None:
        super().__init__()
        self.session = session
        self.status = status
        self.provider_resolution = provider
        self._pending_question = False  # a queued question is waiting on the user
        self._on_delete = on_delete  # callback (session: Session) -> None
        self._on_stop = on_stop      # callback (session: Session) -> None
        self.add_css_class("helios-session-row")
        ro = session.project.read_only
        if ro:
            self.add_css_class("helios-session-row-remote")

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        # Status dot on the left. Kept as an attribute so the periodic
        # refresh can recolor it in place without rebuilding the row.
        dot = Gtk.Box()
        dot.set_size_request(8, 8)
        dot.add_css_class("helios-status-dot")
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_halign(Gtk.Align.CENTER)
        self._dot = dot
        self._apply_dot()
        outer.append(dot)

        # Text column.
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_hexpand(True)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._title = Gtk.Label(label=session.display_title, xalign=0)
        self._title.set_ellipsize(3)
        self._title.add_css_class("body")
        self._title.set_hexpand(True)
        title_row.append(self._title)

        # Provider chip: which backend produced this session (GPT vs Claude).
        # Resolved off the GTK thread by SessionList._tick_status; None here
        # renders as "…" rather than asserting an unestablished owner.
        self._provider_lbl = _provider_chip(provider)
        title_row.append(self._provider_lbl)

        # Host chip for pool rows (another machine's archive, read-only) — which
        # host it came from. The local-cwd/worktree chip is intentionally not
        # shown (per Spencer): the full cwd is on the row tooltip + the filter.
        if ro and show_host_chip:
            title_row.append(_chip(session.project.origin, icon="network-server-symbolic",
                                   tooltip=f"Session from {session.project.origin} (shared pool) — view only"))
        box.append(title_row)

        sub = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        when_ts = status.last_response_at or session.mtime
        when = Gtk.Label(label=_humanize_age(when_ts), xalign=0)
        when.add_css_class("caption")
        when.add_css_class("dim-label")
        when.set_hexpand(True)
        when.set_halign(Gtk.Align.START)
        self._when = when
        sub.append(when)

        # What this session is doing RIGHT NOW. Empty (and invisible) unless a
        # driver pushes activity for it — which, for the visible session, the
        # activity strip above the composer already covers. Its whole reason
        # for existing is the background case: before this, a session
        # running in another chat rendered as one 8px dot, and there was no
        # way to tell a busy one from a wedged one without switching to it.
        activity = Gtk.Label(label="", xalign=0)
        activity.add_css_class("caption")
        activity.add_css_class("dim-label")
        activity.set_ellipsize(3)
        activity.set_visible(False)
        activity.set_halign(Gtk.Align.START)
        self._activity = activity
        sub.append(activity)

        size = Gtk.Label(label=_humanize_size(session.size), xalign=0)
        size.add_css_class("caption")
        size.add_css_class("dim-label")
        size.add_css_class("helios-size-label")
        size.set_halign(Gtk.Align.END)
        sub.append(size)

        box.append(sub)
        outer.append(box)

        # No inline stop control: the composer's Stop button is the one place a
        # turn is stopped. Background sessions keep the capability on the
        # row's right-click menu, where it costs no visual weight.

        # Inline delete button — dim by default, brightens on row hover (CSS).
        # Hidden for pool rows: deleting would silently unlink another
        # machine's archive copy over CIFS.
        if not ro:
            delete_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            delete_btn.add_css_class("flat")
            delete_btn.add_css_class("helios-row-delete")
            delete_btn.set_valign(Gtk.Align.CENTER)
            delete_btn.set_tooltip_text("Delete session")
            delete_btn.connect("clicked", lambda *_: self._on_delete(self.session))
            outer.append(delete_btn)

        self.set_child(outer)
        self._refresh_identity_tooltip()

        # Right-click also offers delete — via a plain popover button (direct
        # callback), NOT a Gio.Menu model, for routing reliability.
        if not ro:
            right_click = Gtk.GestureClick()
            right_click.set_button(3)  # BUTTON_SECONDARY
            right_click.connect("pressed", self._on_right_click)
            self.add_controller(right_click)

    def set_live_activity(self, text: str) -> None:
        """Show (or clear) the one-line "doing X now" caption on this row."""

        text = text.strip()
        if text == self._activity.get_text():
            return
        self._activity.set_text(text)
        self._activity.set_visible(bool(text))
        # The age caption expands to fill the row; hand that job to the
        # activity line while it is showing so both stay on one line.
        self._when.set_hexpand(not text)
        self._activity.set_hexpand(bool(text))

    def _on_right_click(self, gesture: Gtk.GestureClick, _n_press: int, x: float, y: float) -> None:
        # Select this row so it's clear which session the menu acts on.
        listbox = self.get_parent()
        if isinstance(listbox, Gtk.ListBox):
            if listbox.get_selection_mode() == Gtk.SelectionMode.SINGLE:
                listbox.select_row(self)

        popover = Gtk.Popover()
        popover.set_parent(self)
        popover.set_has_arrow(False)
        # Unparent on close so repeated right-clicks don't leak detached popovers.
        popover.connect("closed", lambda p: p.unparent())
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)

        def _item(label: str, handler, *css: str) -> Gtk.Button:
            b = Gtk.Button.new_with_label(label)
            b.add_css_class("flat")
            for c in css:
                b.add_css_class(c)
            child = b.get_child()
            if isinstance(child, Gtk.Label):
                child.set_xalign(0.0)

            def _run(*_a):
                popover.popdown()
                handler()

            b.connect("clicked", _run)
            return b

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        # Only offered while the session actually has a live process. This menu
        # is rebuilt on every right-click, so `self.status` is whatever
        # `update_status()` last wrote — the item cannot go stale.
        #
        # The read-only term is redundant today: the right-click gesture is only
        # installed for `not ro` rows, so a pool row never opens this popover at
        # all. It is repeated here on purpose. Read-only enforcement for pooled
        # rows is a real boundary (they are another machine's archives), and the
        # guard that enforces it should be visible at the site that needs it
        # rather than inferred from a controller installed 30 lines away.
        if (
            self._on_stop is not None
            and not self.session.project.read_only
            and self.status.state in (STATE_WORKING, STATE_READY)
        ):
            box.append(_item("Stop session", lambda: self._on_stop(self.session)))
            box.append(Gtk.Separator())
        box.append(_item("Rename…", self._show_rename_dialog))
        box.append(_item("Copy title", lambda: self._copy(self.session.display_title)))
        box.append(_item("Copy folder path", lambda: self._copy(self.session.project.cwd)))
        box.append(_item("Copy session ID", lambda: self._copy(self.session.session_id)))
        box.append(Gtk.Separator())
        box.append(
            _item("Delete session", lambda: self._on_delete(self.session), "destructive-action")
        )
        popover.set_child(box)
        popover.popup()

    def _copy(self, value: str) -> None:
        # Never clobber the clipboard with an empty string.
        if value:
            self.get_clipboard().set(value)

    def _show_rename_dialog(self) -> None:
        root = self.get_root()
        if root is None:
            return
        dlg = Adw.AlertDialog.new(
            "Rename session", "Give this conversation a custom title."
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")
        entry = Gtk.Entry()
        entry.set_text(self.session.display_title)
        entry.set_activates_default(True)
        dlg.set_extra_child(entry)
        dlg.connect("response", self._on_rename_response, entry)
        dlg.present(root)

    def _on_rename_response(self, _dlg, response: str, entry: Gtk.Entry) -> None:
        if response != "save":
            return
        new_title = entry.get_text().strip()
        if not new_title:
            return
        # Persisting to the TitleStore also pins it: TitleGenerator.request()
        # early-returns when a title is already set, so it survives regeneration.
        title_store().set(self.session.session_id, new_title)
        self.session.title = new_title
        self.refresh_title()

    def refresh_title(self) -> None:
        self._title.set_label(self.session.display_title)
        self._refresh_identity_tooltip()

    def _refresh_identity_tooltip(self) -> None:
        """Keep both hidden identity cues available after ellipsizing/rename."""
        title = self.session.display_title.strip()
        cwd = self.session.project.cwd.strip()
        self.set_tooltip_text("\n".join(part for part in (title, cwd) if part))

    def _apply_dot(self) -> None:
        """Paint the status dot. A pending question overrides the disk-derived
        state, since 'this session needs your answer' outranks working/idle."""
        for cls in _ALL_DOT_CLASSES:
            self._dot.remove_css_class(cls)
        if self._pending_question:
            self._dot.add_css_class(_QUESTION_DOT_CLASS)
            self._dot.set_tooltip_text(
                "Waiting for your answer — this session asked you a question"
            )
        else:
            self._dot.add_css_class(
                _DOT_STATE_CLASS.get(self.status.state, "helios-status-idle")
            )
            self._dot.set_tooltip_text(
                _DOT_TOOLTIP.get(self.status.state, self.status.state)
            )

    def set_pending_question(self, on: bool) -> None:
        if self._pending_question == on:
            return
        self._pending_question = on
        self._apply_dot()

    def update_status(self, status) -> None:
        """Recolor the status dot + refresh the age label in place, without
        rebuilding the row (so selection, scroll position, and focus are
        untouched). Called by the periodic refresh tick."""
        self.status = status
        self._apply_dot()
        self.update_age(status)

    def update_provider(self, resolution) -> None:
        """Repaint the backend chip from a FRESH resolution.

        Never memoized on the row: re-deriving evidence every tick is the
        safety property. A transcript that gains a conflicting sessionId must
        be able to flip this chip from Claude back to Conflict, and a row that
        kept its first answer could not.
        """
        self.provider_resolution = resolution
        _style_provider_chip(self._provider_lbl, resolution)

    def update_age(self, status) -> None:
        """Store the latest status and refresh just the age label — used when
        the state is unchanged but we still want "2 minutes ago" to advance."""
        self.status = status
        when_ts = status.last_response_at or self.session.mtime
        self._when.set_label(_humanize_age(when_ts))


def _chip(text: str, *, icon: str = "", tooltip: str = "") -> Gtk.Widget:
    """Small metadata pill rendered after a row title."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
    box.add_css_class("helios-chip")
    box.set_valign(Gtk.Align.CENTER)
    if icon:
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(10)
        box.append(img)
    lbl = Gtk.Label(label=text)
    lbl.add_css_class("caption")
    lbl.set_ellipsize(3)
    lbl.set_max_width_chars(22)
    box.append(lbl)
    if tooltip:
        box.set_tooltip_text(tooltip)
    return box


_PROVIDER_CSS = (
    "helios-provider-gpt",
    "helios-provider-openrouter",
    "helios-provider-claude",
    "helios-provider-conflict",
    "helios-provider-unknown",
)


def _style_provider_chip(lbl: Gtk.Label, resolution) -> None:
    label, css_class, tooltip = session_providers.chip_style(resolution)
    for cls in _PROVIDER_CSS:
        lbl.remove_css_class(cls)
    lbl.set_label(label)
    lbl.add_css_class(css_class)
    lbl.set_tooltip_text(tooltip)


def _provider_chip(resolution) -> Gtk.Label:
    """Compact backend badge. The resolution is supplied by the caller — this
    function performs no I/O; see SessionList._tick_status."""
    lbl = Gtk.Label()
    lbl.add_css_class("caption")
    lbl.add_css_class("helios-provider-chip")
    lbl.set_valign(Gtk.Align.CENTER)
    _style_provider_chip(lbl, resolution)
    return lbl


def _short_cwd(cwd: str) -> str:
    """`.../parent/leaf` style label for a working directory."""
    parts = [p for p in cwd.split("/") if p]
    if not parts:
        return cwd or "(root)"
    if len(parts) == 1:
        return "/" + parts[0]
    return "/".join(parts[-2:])


def _humanize_age(ts: float) -> str:
    if not ts:
        return ""
    now = datetime.now(timezone.utc).timestamp()
    delta = now - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = int(delta / 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if delta < 86400:
        h = int(delta / 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if delta < 86400 * 30:
        d = int(delta / 86400)
        return f"{d} day{'s' if d != 1 else ''} ago"
    return datetime.fromtimestamp(ts).astimezone().strftime("%b %-d, %Y")


def _humanize_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _pluralize(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"
