"""Keyboard-first launcher for provider-native agent commands."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GObject, Gtk  # noqa: E402

from helios.backend.agent_commands import AgentCommand


class CommandPalette(Adw.Window):
    """Show the selected provider's current command capabilities."""

    __gsignals__ = {
        "activated": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(
        self,
        parent: Gtk.Window,
        commands: tuple[AgentCommand, ...] | list[AgentCommand],
    ) -> None:
        super().__init__()
        self._owner = parent
        self._commands = tuple(commands)
        self._rows: list[_CommandRow] = []
        self.set_title("Agent commands")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 430)

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
        self._entry.set_placeholder_text("Type a command…")
        self._entry.connect("search-changed", self._on_search_changed)
        self._entry.connect("activate", lambda *_: self._activate_selected())
        box.append(self._entry)

        self._status = Gtk.Label(xalign=0)
        self._status.add_css_class("caption")
        self._status.add_css_class("dim-label")
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

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

        self._rebuild("")

    def focus_entry(self) -> None:
        self._entry.grab_focus()

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        self._rebuild(self._entry.get_text())

    def _rebuild(self, query: str) -> None:
        while (row := self._list.get_first_child()) is not None:
            self._list.remove(row)
        self._rows = []

        needle = str(query or "").strip().casefold()
        matches = [
            command
            for command in self._commands
            if not needle or needle in command.searchable_text
        ]
        for command in matches:
            row = _CommandRow(command)
            self._rows.append(row)
            self._list.append(row)

        enabled = [row for row in self._rows if row.command.enabled]
        if enabled:
            self._list.select_row(enabled[0])
        if not matches:
            self._status.set_label("No commands match.")
        else:
            available = sum(command.enabled for command in matches)
            if available == len(matches):
                status = f"{available} available"
            elif available:
                status = f"{available} available · unavailable actions include a reason"
            else:
                status = "Unavailable actions include a reason"
            self._status.set_label(status)

    def _on_row_activated(
        self,
        _list: Gtk.ListBox,
        row: Gtk.ListBoxRow,
    ) -> None:
        self._activate_row(row)

    def _activate_selected(self) -> None:
        self._activate_row(self._list.get_selected_row())

    def _activate_row(self, row: Gtk.ListBoxRow | None) -> None:
        command = getattr(row, "command", None)
        if not isinstance(command, AgentCommand) or not command.enabled:
            return
        self.emit("activated", command.command_id)
        self.close()

    def _on_key(self, _ctrl, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


class _CommandRow(Gtk.ListBoxRow):
    def __init__(self, command: AgentCommand) -> None:
        super().__init__()
        self.command = command
        self.set_activatable(command.enabled)
        self.set_selectable(command.enabled)
        if not command.enabled:
            self.add_css_class("dim-label")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_margin_top(9)
        box.set_margin_bottom(9)
        box.set_margin_start(11)
        box.set_margin_end(11)

        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label(label=command.title, xalign=0)
        title.set_hexpand(True)
        heading.append(title)
        invocation = Gtk.Label(label=command.invocation, xalign=1)
        invocation.add_css_class("caption")
        invocation.add_css_class("dim-label")
        heading.append(invocation)
        box.append(heading)

        description = Gtk.Label(label=command.description, xalign=0)
        description.set_wrap(True)
        description.add_css_class("caption")
        box.append(description)

        detail = command.source
        if command.unavailable_reason:
            detail += f" · {command.unavailable_reason}"
        source = Gtk.Label(label=detail, xalign=0)
        source.set_wrap(True)
        source.add_css_class("caption")
        source.add_css_class("dim-label")
        box.append(source)
        self.set_child(box)
