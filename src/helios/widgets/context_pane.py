from __future__ import annotations

import time

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from helios.backend.memory import ContextFile, context_files_for
from helios.backend.projects import Project
from helios.log import get_logger
from helios.widgets.memory_editor import MemoryEditor
from helios.widgets._motion import FAST_MS

_log = get_logger("context_pane")

# Debounce for the memory-dir file monitor — collapses a burst of
# create/write/rename events from one Claude memory write into one refresh.
#: How long after our own save a matching monitor event is still ours. The
#: rename and the inotify delivery are milliseconds apart; a second is ample
#: and small enough that a real Claude write moments later still announces.
_OWN_SAVE_GRACE_S = 1.0

_MEMORY_DEBOUNCE_MS = 500


class ContextPane(Gtk.Box):
    """Right-side pane: shows CLAUDE.md hierarchy + memory files for the
    currently selected project. CLAUDE.md, MEMORY.md and individual memory
    files are all editable (MemoryEditor, plain mode for the first two);
    the project's memory directory is watched so a file Claude writes
    mid-session shows up without a project switch."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._destroyed = False
        self.add_css_class("helios-context")
        self.set_size_request(280, -1)

        # Header.
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header.set_margin_top(12)
        header.set_margin_bottom(8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        title = Gtk.Label(label="Claude memory", xalign=0)
        title.add_css_class("title-4")
        self._subtitle = Gtk.Label(xalign=0)
        self._subtitle.add_css_class("dim-label")
        self._subtitle.add_css_class("caption")
        header.append(title)
        header.append(self._subtitle)
        self.append(header)

        # "Claude updated memory: <file>" — shown by the memory-dir monitor,
        # dismissed via its own action button (no reload semantics needed).
        self._memory_monitor: Gio.FileMonitor | None = None
        #: The memory directory the monitor is bound to (None while it still
        #: watches the project dir for memory/ to appear).
        self._memory_monitor_dir: Path | None = None
        #: Files this pane saved itself, and when. The monitor sees our own
        #: atomic rename exactly like an external write, so without this a
        #: self-save reads as "Claude updated memory".
        self._own_saves: dict[str, float] = {}
        self._memory_debounce_id = 0
        self._memory_changed_names: set[str] = set()
        self._memory_banner = Adw.Banner.new("")
        self._memory_banner.set_button_label("Dismiss")
        self._memory_banner.connect(
            "button-clicked", lambda *_a: self._memory_banner.set_revealed(False)
        )
        self._memory_banner.add_css_class("helios-context-memory-banner")
        self._memory_banner.set_revealed(False)
        self.append(self._memory_banner)

        # Swap the real file surface with an explicit empty state.  The status
        # page used to be constructed but never parented, leaving an empty
        # pane that could not explain why it was blank.
        self._content_stack = Gtk.Stack()
        self._content_stack.set_vexpand(True)
        self.append(self._content_stack)

        # Split: list of files (top) + preview pane (bottom).
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self._paned.set_vexpand(True)
        self._paned.set_position(220)
        self._content_stack.add_named(self._paned, "files")

        # Top: file list.
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._listbox.add_css_class("navigation-sidebar")
        # Handler id kept so the dirty-navigation guard can move the
        # selection back without re-entering itself.
        self._row_sel_handler = self._listbox.connect(
            "row-selected", self._on_row_selected
        )
        # The row whose content is currently shown (preview or editor) —
        # what we restore to when the user declines to discard edits.
        self._current_row: Gtk.ListBoxRow | None = None

        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scroll.set_child(self._listbox)
        self._paned.set_start_child(list_scroll)

        # Bottom: a stack swapping between
        #   * MemoryEditor  (every existing file — plain mode for CLAUDE.md
        #                    and MEMORY.md, frontmatter form for memory/*.md)
        #   * Read-only preview (only ever used for a row whose file doesn't
        #                    exist yet, e.g. no project CLAUDE.md written)
        self._bottom_stack = Gtk.Stack()
        self._bottom_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._bottom_stack.set_transition_duration(FAST_MS)

        # Read-only preview side — "(file does not exist)" only.
        self._preview_scroller = Gtk.ScrolledWindow()
        self._preview_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._preview = Gtk.TextView()
        self._preview.set_editable(False)
        self._preview.set_cursor_visible(False)
        self._preview.set_monospace(True)
        self._preview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._preview.set_left_margin(12)
        self._preview.set_right_margin(12)
        self._preview.set_top_margin(8)
        self._preview.set_bottom_margin(8)
        self._preview.add_css_class("helios-context-preview")
        self._preview_scroller.set_child(self._preview)
        self._bottom_stack.add_named(self._preview_scroller, "preview")

        # Editor side.
        self._editor = MemoryEditor()
        self._editor.connect("dirty-changed", self._on_editor_dirty_changed)
        self._editor.connect("saved", self._on_editor_saved)
        self._bottom_stack.add_named(self._editor, "editor")

        self._bottom_stack.set_visible_child_name("preview")
        self._paned.set_end_child(self._bottom_stack)

        # Empty state.
        self._empty = Adw.StatusPage()
        self._empty.set_icon_name("text-x-generic-symbolic")
        self._empty.set_title("No project")
        self._empty.set_description("Context appears here when a project is selected.")
        self._content_stack.add_named(self._empty, "empty")

        self._files: list[ContextFile] = []
        self._project: Project | None = None
        self.set_project(None)

    def shutdown(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._stop_memory_monitor()

    def set_project(self, project: Project | None) -> None:
        if self._destroyed:
            return
        self._stop_memory_monitor()
        self._project = project
        # Rows are about to be replaced — the old "current" reference would
        # point at a detached widget. The dirty-nav guard handles a dirty
        # editor on the first selection of the new list (previous=None).
        self._current_row = None

        if project is None:
            while (row := self._listbox.get_first_child()) is not None:
                self._listbox.remove(row)
            self._files = []
            self._subtitle.set_label("")
            self._preview.get_buffer().set_text("")
            self._content_stack.set_visible_child_name("empty")
            return

        self._content_stack.set_visible_child_name("files")
        self._apply_files(context_files_for(project), keep_selection=False)
        self._start_memory_monitor(project)

    def _apply_files(self, files: list[ContextFile], *, keep_selection: bool) -> None:
        """Update self._files + the subtitle count, then rebuild rows.

        `keep_selection=False` (a project switch) auto-selects the first
        real file — the previous behaviour. `keep_selection=True` (a
        memory-dir refresh) instead keeps whatever row was already open, so
        a live update never yanks the user onto a different file or reloads
        the one they're editing — MemoryEditor's existing external-change
        conflict path is what handles that file being stale underneath.
        """
        self._files = files
        present = sum(1 for f in files if f.exists)
        self._subtitle.set_label(f"{present} file{'s' if present != 1 else ''}")

        keep_path = (
            self._current_row.context.path
            if keep_selection and self._current_row is not None
            else None
        )

        while (row := self._listbox.get_first_child()) is not None:
            self._listbox.remove(row)

        groups = [
            ("CLAUDE.md", ["global-claude-md", "project-claude-md"]),
            ("Memory", ["memory-index", "memory"]),
        ]
        keep_row = None
        first_row = None
        for group_title, kinds in groups:
            files_in_group = [f for f in self._files if f.kind in kinds]
            if not files_in_group:
                continue
            self._listbox.append(_GroupHeader(group_title))
            for f in files_in_group:
                row = _ContextRow(f)
                self._listbox.append(row)
                if first_row is None and f.exists:
                    first_row = row
                if keep_path is not None and f.path == keep_path:
                    keep_row = row

        if keep_selection:
            self._current_row = keep_row
            self._select_row_silently(keep_row)
        elif first_row is not None:
            self._listbox.select_row(first_row)

    # ── Memory-dir watcher ───────────────────────────────────────────

    def _refresh_files(self) -> None:
        if self._destroyed or self._project is None:
            return
        self._apply_files(context_files_for(self._project), keep_selection=True)

    def _start_memory_monitor(self, project: Project) -> None:
        mem_dir = project.path / "memory"
        if mem_dir.is_dir():
            self._bind_memory_monitor(mem_dir)
            return
        # No memory/ yet for this project. project.path itself always
        # exists (Project objects are only built for directories already on
        # disk), so watch that instead and rebind onto memory/ the moment
        # it appears — otherwise the first memory file Claude ever writes
        # for a brand new project would go unnoticed until the next
        # project switch, which is exactly the gap this feature closes.
        try:
            gfile = Gio.File.new_for_path(str(project.path))
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except Exception as e:
            _log.warning("could not start context memory monitor: %s", e)
            return
        monitor.connect("changed", self._on_memory_ancestor_changed)
        self._memory_monitor = monitor
        self._memory_monitor_dir = None

    def _bind_memory_monitor(self, mem_dir: Path) -> bool:
        """Arm the memory-directory watch. False when it could not be armed."""
        try:
            gfile = Gio.File.new_for_path(str(mem_dir))
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except Exception as e:
            _log.warning("could not start context memory monitor: %s", e)
            return False
        monitor.connect("changed", self._on_memory_changed)
        self._memory_monitor = monitor
        self._memory_monitor_dir = mem_dir
        return True

    def _on_memory_ancestor_changed(self, _monitor, file, _other, _event_type) -> None:
        if self._destroyed or self._project is None:
            return
        # Only "memory/" appearing is ours — everything else that can churn
        # in a project dir (session .jsonl writes, especially) is noise a
        # banner must never fire for.
        try:
            name = file.get_basename() or ""
        except Exception:
            name = ""
        if name != "memory":
            return
        mem_dir = self._project.path / "memory"
        if not mem_dir.is_dir():
            return
        old = self._memory_monitor
        if not self._bind_memory_monitor(mem_dir):
            # Keep watching the parent: dropping it would leave nothing armed.
            return
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        # The directory and its first file can both exist before this callback
        # runs, and the new monitor only reports what happens AFTER it binds —
        # so read the directory once now or the first memory file stays
        # invisible until the next write.
        self._refresh_files()

    def _on_memory_changed(self, _monitor, file, _other, _event_type) -> None:
        if self._destroyed:
            return
        try:
            name = file.get_basename() or ""
        except Exception:
            name = ""
        # Ignore memory_io's own atomic-write temp file — a save (ours or a
        # future backup restore) is not "Claude updated memory".
        if not name or name.endswith(".tmp"):
            return
        # Our own save lands here as an ordinary rename. Claim it once, within
        # a short window, so a self-save is not announced as Claude's work.
        saved_at = self._own_saves.get(name)
        now = time.monotonic()
        if saved_at is not None and now - saved_at <= _OWN_SAVE_GRACE_S:
            del self._own_saves[name]
            self._refresh_files()
            return
        self._own_saves = {
            key: when
            for key, when in self._own_saves.items()
            if now - when <= _OWN_SAVE_GRACE_S
        }
        self._memory_changed_names.add(name)
        if self._memory_debounce_id:
            GLib.source_remove(self._memory_debounce_id)
        self._memory_debounce_id = GLib.timeout_add(
            _MEMORY_DEBOUNCE_MS, self._flush_memory_debounce
        )

    def _flush_memory_debounce(self) -> bool:
        self._memory_debounce_id = 0
        if self._destroyed:
            return False
        names = sorted(self._memory_changed_names)
        self._memory_changed_names = set()
        self._refresh_files()
        if len(names) == 1:
            self._memory_banner.set_title(f"Claude updated memory: {names[0]}")
            self._memory_banner.set_revealed(True)
        elif names:
            self._memory_banner.set_title(f"Claude updated memory: {len(names)} files")
            self._memory_banner.set_revealed(True)
        return False  # one-shot

    def _stop_memory_monitor(self) -> None:
        if self._memory_debounce_id:
            GLib.source_remove(self._memory_debounce_id)
            self._memory_debounce_id = 0
        self._memory_changed_names = set()
        self._memory_banner.set_revealed(False)
        if self._memory_monitor is not None:
            try:
                self._memory_monitor.cancel()
            except Exception:
                pass
            self._memory_monitor = None

    def _on_row_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if self._destroyed or row is None or not isinstance(row, _ContextRow):
            return
        if row is self._current_row:
            return

        # Unsaved memory-file edits? Confirm before the switch destroys them
        # (set_file reloads from disk). Selection is put back where it was
        # until the user decides.
        if (
            self._bottom_stack.get_visible_child_name() == "editor"
            and self._editor.is_dirty()
        ):
            self._select_row_silently(self._current_row)
            self._present_discard_dialog(row)
            return

        self._current_row = row
        self._show_row(row)

    def _show_row(self, row: "_ContextRow") -> None:
        if self._destroyed:
            return
        cf: ContextFile = row.context
        if not cf.exists:
            self._show_preview(f"(file does not exist)\n{cf.path}")
            return
        # Every existing file is editable now. Individual memory files keep
        # the frontmatter form; CLAUDE.md and MEMORY.md have none, so those
        # two kinds load in plain mode (whole file is the body).
        self._editor.set_file(cf.path, plain=cf.kind != "memory")
        self._bottom_stack.set_visible_child_name("editor")

    def _select_row_silently(self, row: Gtk.ListBoxRow | None) -> None:
        """Move the listbox selection without re-firing our handler."""
        self._listbox.handler_block(self._row_sel_handler)
        try:
            if row is not None:
                self._listbox.select_row(row)
            else:
                self._listbox.unselect_all()
        finally:
            self._listbox.handler_unblock(self._row_sel_handler)

    def _present_discard_dialog(self, target_row: "_ContextRow") -> None:
        if self._destroyed:
            return
        root = self.get_root()
        if root is None:
            # Can't prompt — keep the current file rather than lose edits.
            return
        dlg = Adw.AlertDialog.new(
            "Discard unsaved changes?",
            "The memory file you're editing has unsaved changes. "
            "Switching files will lose them.",
        )
        dlg.add_response("keep", "Keep editing")
        dlg.add_response("discard", "Discard changes")
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")
        dlg.set_close_response("keep")

        def on_response(_d, response: str) -> None:
            if self._destroyed or response != "discard":
                return  # selection was already restored; editor untouched
            self._current_row = target_row
            self._select_row_silently(target_row)
            self._show_row(target_row)

        dlg.connect("response", on_response)
        dlg.present(root)

    def _show_preview(self, text: str) -> None:
        if self._destroyed:
            return
        self._bottom_stack.set_visible_child_name("preview")
        self._preview.get_buffer().set_text(text)

    def _on_editor_dirty_changed(self, _ed, _dirty: bool) -> None:
        # Hook for future: prompt before navigation, show indicator in title.
        pass

    def _on_editor_saved(self, _ed, mem) -> None:
        path = getattr(mem, "path", None)
        if path is not None:
            self._own_saves[path.name] = time.monotonic()

        # Row titles are filename stems, which a save can't change — and the
        # full rebuild this used to do auto-selected the FIRST row, yanking
        # the user off the file they had just saved. Nothing to refresh.
        pass


class _GroupHeader(Gtk.ListBoxRow):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.set_selectable(False)
        self.set_activatable(False)
        self.add_css_class("helios-context-group")
        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("caption-heading")
        label.add_css_class("dim-label")
        label.set_margin_top(10)
        label.set_margin_bottom(2)
        label.set_margin_start(16)
        self.set_child(label)


class _ContextRow(Gtk.ListBoxRow):
    def __init__(self, cf: ContextFile) -> None:
        super().__init__()
        self.context = cf
        self.add_css_class("helios-context-row")
        if not cf.exists:
            self.add_css_class("helios-context-missing")
            self.set_selectable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(14)
        box.set_margin_end(12)

        icon_name = {
            "global-claude-md": "emblem-system-symbolic",
            "project-claude-md": "folder-symbolic",
            "memory-index": "view-list-symbolic",
            "memory": "text-x-generic-symbolic",
        }.get(cf.kind, "text-x-generic-symbolic")
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(14)
        icon.add_css_class("dim-label")
        box.append(icon)

        label = Gtk.Label(label=cf.title, xalign=0)
        label.set_ellipsize(3)
        label.set_hexpand(True)
        if not cf.exists:
            label.add_css_class("dim-label")
        box.append(label)

        self.set_child(box)
