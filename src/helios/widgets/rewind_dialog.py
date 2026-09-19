"""Pick a checkpoint, see exactly what reverting does, then confirm.

Two steps on purpose. A rewind overwrites files on disk, including any edits
the user made by hand since — so the second step lists every affected path
with what will happen to it, and lets individual paths be unticked. Nothing is
written until Restore is pressed.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from helios.backend.checkpoints import Change, Checkpoint
from helios.widgets._motion import BASE_MS

_STATUS_TEXT = {
    "modified": "was edited — will be reverted",
    "added": "was created — will be deleted",
    "deleted": "was deleted — will be recreated",
}


def humanize_age(created_at: int, now: int | None = None) -> str:
    seconds = max(0, (int(time.time()) if now is None else now) - created_at)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} min ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    days = seconds // 86400
    return f"{days}d ago"


class RewindDialog(Adw.Dialog):
    """Signals:
    restore-requested(object, object) -- (Checkpoint, list[str] of paths)
    """

    __gsignals__ = {
        "restore-requested": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
    }

    def __init__(
        self,
        checkpoints: list[Checkpoint],
        *,
        load_changes,
    ) -> None:
        """``load_changes(checkpoint, on_ready)`` must be ASYNCHRONOUS.

        Computing a change list takes a fresh worktree snapshot and several
        git invocations; doing that on the GTK thread froze the whole window
        for as long as git took, which on a big repo or a slow filesystem is
        seconds to tens of seconds. The dialog shows a loading state and waits
        for ``on_ready(changes)``.
        """
        super().__init__()
        self.set_title("Rewind files")
        self.set_content_width(620)
        self.set_content_height(560)
        self._load_changes = load_changes
        self._checkpoints = list(checkpoints)
        self._selected: Checkpoint | None = None
        self._checks: dict[str, Gtk.CheckButton] = {}
        self._pending_token = 0
        self._destroyed = False
        self.connect("closed", self._on_closed)

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self._stack.set_transition_duration(BASE_MS)
        self._stack.add_named(self._build_list_page(), "list")

        toolbar = Adw.ToolbarView()
        self._header = Adw.HeaderBar()
        toolbar.add_top_bar(self._header)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

    # ── page 1: which checkpoint ──────────────────────────────────────

    def _build_list_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(14)
        page.set_margin_bottom(14)
        page.set_margin_start(14)
        page.set_margin_end(14)

        if not self._checkpoints:
            empty = Adw.StatusPage()
            empty.set_icon_name("document-open-recent-symbolic")
            empty.set_title("No checkpoints yet")
            empty.set_description(
                "A checkpoint is taken before each message you send, in "
                "sessions whose folder is a git repository."
            )
            page.append(empty)
            return page

        intro = Gtk.Label(
            label=(
                "Restore this session's files to how they were before one of "
                "your messages. Nothing is changed until you confirm."
            ),
            xalign=0,
        )
        intro.add_css_class("caption")
        intro.add_css_class("dim-label")
        intro.set_wrap(True)
        page.append(intro)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for point in self._checkpoints:
            listbox.append(self._checkpoint_row(point))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(listbox)
        page.append(scroller)
        return page

    def _on_closed(self, *_args) -> None:
        # A worker may still be mid-git when the dialog goes away; its
        # callback must not touch destroyed widgets.
        self._destroyed = True

    def _checkpoint_row(self, point: Checkpoint) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(point.label or "(no message)")
        row.set_subtitle(f"before this message · {humanize_age(point.created_at)}")
        row.set_activatable(True)
        arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        arrow.add_css_class("dim-label")
        row.add_suffix(arrow)
        row.connect("activated", lambda *_a, p=point: self._show_changes(p))
        return row

    # ── page 2: what exactly changes ──────────────────────────────────

    def _show_changes(self, point: Checkpoint) -> None:
        self._selected = point
        self._pending_token += 1
        token = self._pending_token
        self._swap_changes_page(self._build_loading_page())
        self._load_changes(
            point, lambda changes: self._changes_ready(token, point, changes)
        )

    def _changes_ready(self, token: int, point: Checkpoint, changes) -> bool:
        # Ignore a result the user has already navigated away from — clicking
        # two checkpoints quickly must not render the first one's answer.
        if self._destroyed or token != self._pending_token:
            return False
        self._swap_changes_page(self._build_changes_page(point, list(changes)))
        return False

    def _swap_changes_page(self, page: Gtk.Widget) -> None:
        existing = self._stack.get_child_by_name("changes")
        if existing is not None:
            self._stack.remove(existing)
        self._stack.add_named(page, "changes")
        self._stack.set_visible_child_name("changes")

    def _build_loading_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_valign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner()
        spinner.set_size_request(28, 28)
        spinner.start()
        page.append(spinner)
        label = Gtk.Label(label="Comparing your files with this checkpoint…")
        label.add_css_class("dim-label")
        page.append(label)
        return page

    def _build_changes_page(
        self, point: Checkpoint, changes: list[Change]
    ) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(14)
        page.set_margin_bottom(14)
        page.set_margin_start(14)
        page.set_margin_end(14)

        back = Gtk.Button(label="Back")
        back.add_css_class("flat")
        back.set_halign(Gtk.Align.START)
        back.connect("clicked", lambda *_a: self._stack.set_visible_child_name("list"))
        page.append(back)

        heading = Gtk.Label(
            label=f"Restore to before “{point.label or 'this message'}”", xalign=0
        )
        heading.add_css_class("heading")
        heading.set_wrap(True)
        page.append(heading)

        self._checks = {}
        if not changes:
            note = Gtk.Label(
                label="Nothing has changed on disk since this checkpoint.",
                xalign=0,
            )
            note.add_css_class("dim-label")
            page.append(note)
            page.append(self._button_row(enabled=False))
            return page

        warn = Gtk.Label(
            label=(
                "This overwrites the files on disk, including any edits you "
                "made yourself since. Untick anything you want to keep."
            ),
            xalign=0,
        )
        warn.add_css_class("caption")
        warn.add_css_class("warning")
        warn.set_wrap(True)
        page.append(warn)

        if point.ignored:
            # State the boundary on the page where the decision is made, not
            # only on the list page: these files were git-ignored when the
            # snapshot was taken, so they are outside it entirely — a rewind
            # will neither revert nor delete them.
            scope = Gtk.Label(
                label=(
                    f"{len(point.ignored)} git-ignored path"
                    f"{'s' if len(point.ignored) != 1 else ''} were outside "
                    "this checkpoint and will be left untouched."
                ),
                xalign=0,
            )
            scope.add_css_class("caption")
            scope.add_css_class("dim-label")
            scope.set_wrap(True)
            scope.set_tooltip_text("\n".join(point.ignored[:40]))
            page.append(scope)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for change in changes:
            row = Adw.ActionRow()
            row.set_title(change.path)
            row.set_subtitle(_STATUS_TEXT.get(change.status, change.status))
            check = Gtk.CheckButton()
            check.set_active(True)
            check.set_valign(Gtk.Align.CENTER)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            self._checks[change.path] = check
            listbox.append(row)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(listbox)
        page.append(scroller)
        page.append(self._button_row(enabled=True))
        return page

    def _button_row(self, *, enabled: bool) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_a: self.close())
        row.append(cancel)
        restore = Gtk.Button(label="Restore")
        restore.add_css_class("destructive-action")
        restore.set_sensitive(enabled)
        restore.connect("clicked", self._on_restore)
        row.append(restore)
        return row

    def selected_paths(self) -> list[str]:
        return sorted(p for p, c in self._checks.items() if c.get_active())

    def _on_restore(self, _button: Gtk.Button) -> None:
        if self._selected is None:
            return
        paths = self.selected_paths()
        if not paths:
            self.close()
            return
        self.emit("restore-requested", self._selected, paths)
        self.close()
