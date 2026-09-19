"""Provider-neutral repository changes, with bounded background Git reads."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GtkSource", "5")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, GtkSource, Pango

from helios.backend.git_changes import ChangesSnapshot, display_text, read_changes, read_diff
from helios.backend.latest_worker import LatestTaskRunner


class ChangesPane(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("helios-changes-pane")
        for edge in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{edge}")(12)
        self._closed = False
        self._cwd = ""
        self._revision = 0
        self._diff_revision = 0
        self._snapshot: ChangesSnapshot | None = None
        self._selected_path = ""
        # Pin the callbacks; only delivery touches GLib, never GTK off-thread.
        self._status_ready = self._apply_status
        self._diff_ready = self._apply_diff
        self._status_runner = LatestTaskRunner(
            work=lambda request: read_changes(request[1]),
            deliver=lambda request, result: GLib.idle_add(self._status_ready, request, result),
            name="helios-changes-status",
        )
        self._diff_runner = LatestTaskRunner(
            work=lambda request: read_diff(request[2], request[3]),
            deliver=lambda request, result: GLib.idle_add(self._diff_ready, request, result),
            name="helios-changes-diff",
        )
        header = Gtk.Box(spacing=8)
        title = Gtk.Label(label="Repository changes", xalign=0, hexpand=True)
        title.add_css_class("heading")
        header.append(title)
        self._refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._refresh.set_tooltip_text("Refresh repository changes")
        self._refresh.connect("clicked", lambda *_: self.refresh())
        header.append(self._refresh)
        self.append(header)
        scope = Gtk.Label(
            label="Current staged, unstaged and untracked files from all sessions.",
            xalign=0, wrap=True,
        )
        scope.add_css_class("caption")
        scope.add_css_class("dim-label")
        self.append(scope)
        self._location = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self._location.add_css_class("caption")
        self.append(self._location)
        self._status = Gtk.Label(xalign=0, wrap=True, label="Select a local project to inspect its changes.")
        self._status.add_css_class("dim-label")
        self.append(self._status)
        self._files = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._files.add_css_class("boxed-list")
        self._files.connect("row-selected", self._on_file_selected)
        file_scroll = Gtk.ScrolledWindow()
        file_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        file_scroll.set_min_content_height(100)
        file_scroll.set_max_content_height(190)
        file_scroll.set_propagate_natural_height(True)
        file_scroll.set_child(self._files)
        self._file_scroll = file_scroll
        self.append(file_scroll)
        self._file_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self._file_title.add_css_class("heading")
        self.append(self._file_title)
        self._buffer = GtkSource.Buffer()
        self._buffer.set_language(GtkSource.LanguageManager.get_default().get_language("diff"))
        self._style_manager = Adw.StyleManager.get_default()
        self._theme_handler = self._style_manager.connect("notify::dark", self._apply_theme)
        self._apply_theme()
        self._view = GtkSource.View.new_with_buffer(self._buffer)
        self._view.set_editable(False)
        self._view.set_monospace(True)
        self._view.set_wrap_mode(Gtk.WrapMode.NONE)
        self._view.set_left_margin(10)
        self._view.set_right_margin(10)
        self._view.set_top_margin(10)
        self._view.set_bottom_margin(10)
        self._view.set_tooltip_text("Selected file diff — read only")
        self._view.update_property([Gtk.AccessibleProperty.LABEL], ["Selected file diff, read only"])
        self._diff_scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._diff_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._diff_scroll.add_css_class("card")
        self._diff_scroll.set_child(self._view)
        self.append(self._diff_scroll)
        self._refresh.set_sensitive(False)
        self._show_files(False)

    @property
    def cwd(self) -> str:
        return self._cwd

    def _apply_theme(self, *_args) -> None:
        name = "Adwaita-dark" if self._style_manager.get_dark() else "Adwaita"
        scheme = GtkSource.StyleSchemeManager.get_default().get_scheme(name)
        if scheme is not None:
            self._buffer.set_style_scheme(scheme)

    def set_project(self, project) -> None:
        cwd = str(project.cwd) if project is not None and not project.read_only else ""
        if cwd == self._cwd:
            if cwd:
                self.refresh()
            return
        self._cwd = cwd
        self._revision += 1
        self._diff_revision += 1
        self._snapshot = None
        self._selected_path = ""
        self._clear_rows()
        self._buffer.set_text("")
        self._location.set_label(display_text(cwd))
        self._location.set_tooltip_text(display_text(cwd))
        self._refresh.set_sensitive(bool(cwd))
        self._show_files(False)
        if cwd:
            self.refresh()
        else:
            self._status.set_label("Select a local project to inspect its changes.")

    def refresh(self) -> None:
        if self._closed or not self._cwd:
            return
        self._revision += 1
        self._diff_revision += 1
        self._status.set_label("Refreshing changes…")
        # Preserve the selected path, but never show its old diff as current.
        self._buffer.set_text("Refreshing changes…")
        self._files.set_sensitive(False)
        self._status_runner.submit((self._revision, self._cwd))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._style_manager.disconnect(self._theme_handler)
        self._revision += 1
        self._diff_revision += 1
        self._status_runner.shutdown(join=False)
        self._diff_runner.shutdown(join=False)

    def _show_files(self, show: bool) -> None:
        self._file_scroll.set_visible(show)
        self._file_title.set_visible(show)
        self._diff_scroll.set_visible(show)

    def _clear_rows(self) -> None:
        while (row := self._files.get_first_child()) is not None:
            self._files.remove(row)

    def _apply_status(self, request, result) -> bool:
        if self._closed or request != (self._revision, self._cwd):
            return False
        self._snapshot = None
        self._clear_rows()
        self._files.set_sensitive(True)
        if isinstance(result, Exception):
            self._snapshot = None
            message = display_text(str(result))
            if "not a git repository" in message.lower():
                message = "This folder is not a Git repository."
            self._status.set_label(message)
            self._show_files(False)
            return False
        self._snapshot = result
        self._location.set_label(display_text(result.root))
        self._location.set_tooltip_text(display_text(result.root))
        count = len(result.files)
        self._status.set_label(
            f"Showing first {count} files — list truncated." if result.truncated
            else (f"{count} changed file{'s' if count != 1 else ''}" if count else "Working tree is clean.")
        )
        self._show_files(bool(count))
        selected = None
        for change in result.files:
            row = Gtk.ListBoxRow()
            row.change = change
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            for edge in ("top", "bottom", "start", "end"):
                getattr(box, f"set_margin_{edge}")(8)
            name = Gtk.Label(label=change.label, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
            box.append(name)
            status = Gtk.Label(label=f"{change.status}  {change.description}", xalign=0)
            status.add_css_class("caption")
            status.add_css_class("dim-label")
            box.append(status)
            row.set_child(box)
            row.set_tooltip_text(change.label)
            self._files.append(row)
            if change.path == self._selected_path:
                selected = row
        self._files.select_row(selected or self._files.get_row_at_index(0))
        return False

    def _on_file_selected(self, _list, row) -> None:
        if row is None or self._snapshot is None:
            return
        self._diff_revision += 1
        change = row.change
        self._selected_path = change.path
        self._file_title.set_label(change.label)
        self._file_title.set_tooltip_text(change.label)
        languages = GtkSource.LanguageManager.get_default()
        untracked = change.status == "??"
        # An untracked file is its original content, not a patch: Markdown
        # bullets and leading '-' in source must not look like deletions.
        self._buffer.set_language(
            languages.guess_language(display_text(change.path), None) if untracked
            else languages.get_language("diff")
        )
        content_name = "Selected file preview" if untracked else "Selected file diff"
        self._view.set_tooltip_text(f"{content_name} — read only")
        self._view.update_property(
            [Gtk.AccessibleProperty.LABEL], [f"{content_name}, read only"]
        )
        self._buffer.set_text("Loading preview…" if untracked else "Loading diff…")
        self._diff_runner.submit((self._revision, self._diff_revision, self._snapshot.root, change))

    def _apply_diff(self, request, result) -> bool:
        if self._closed or request[:2] != (self._revision, self._diff_revision):
            return False
        self._buffer.set_text(display_text(str(result)))
        self._diff_scroll.get_vadjustment().set_value(0)
        self._diff_scroll.get_hadjustment().set_value(0)
        return False
