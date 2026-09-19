"""The composer's `/` completion list.

Same row vocabulary as the Ctrl+Shift+P palette (`command_palette.py`) but
inline and non-modal: it never takes focus, so the user keeps typing in the
TextView while it filters. All key handling lives in the Composer, which
forwards Up/Down/Tab/Enter/Esc here rather than letting the popover grab.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk, Pango  # noqa: E402

from helios.backend.slash_commands import SlashCommand


class SlashPopover(Gtk.Popover):
    """Filtered `/command` rows anchored to the composer.

    Emits 'accepted' when a row is chosen; the highlighted row is the one to
    insert, and a click highlights before it accepts so there is one path.
    """

    __gsignals__ = {
        "accepted": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, anchor: Gtk.Widget) -> None:
        super().__init__()
        self.set_parent(anchor)
        self.set_position(Gtk.PositionType.TOP)
        self.set_has_arrow(False)
        self.set_halign(Gtk.Align.START)
        # Typing must continue in the TextView while this is up, so the
        # popover neither autohides nor accepts focus.
        self.set_autohide(False)
        self.set_can_focus(False)
        self.add_css_class("helios-slash-popover")

        self._commands: tuple[SlashCommand, ...] = ()
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list.set_can_focus(False)
        self._list.connect("row-activated", self._on_row_activated)
        self._list.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Slash commands"],
        )
        self.set_child(self._list)

    def set_commands(self, commands: tuple[SlashCommand, ...]) -> None:
        """Replace the visible rows, highlighting the first."""

        self._commands = tuple(commands)
        while (row := self._list.get_first_child()) is not None:
            self._list.remove(row)
        for command in self._commands:
            self._list.append(_SlashRow(command))
        self._highlight(0)

    def move_highlight(self, delta: int) -> None:
        """Step the highlight, wrapping at both ends."""

        if not self._commands:
            return
        row = self._list.get_selected_row()
        index = 0 if row is None else row.get_index()
        self._highlight((index + delta) % len(self._commands))

    def highlighted_command(self) -> SlashCommand | None:
        return getattr(self._list.get_selected_row(), "command", None)

    def _highlight(self, index: int) -> None:
        row = self._list.get_row_at_index(index)
        if row is not None:
            self._list.select_row(row)

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self._list.select_row(row)
        self.emit("accepted")


class _SlashRow(Gtk.ListBoxRow):
    def __init__(self, command: SlashCommand) -> None:
        super().__init__()
        self.command = command
        self.set_can_focus(False)  # keyboard focus stays in the composer

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(8)
        box.set_margin_end(8)

        name = Gtk.Label(label=f"/{command.name}", xalign=0)
        name.add_css_class("heading")
        box.append(name)

        if command.argument_hint:
            hint = Gtk.Label(label=command.argument_hint, xalign=0)
            hint.add_css_class("caption")
            hint.add_css_class("monospace")
            hint.add_css_class("dim-label")
            box.append(hint)

        description = Gtk.Label(label=command.description, xalign=0)
        description.set_hexpand(True)
        description.set_ellipsize(Pango.EllipsizeMode.END)
        description.add_css_class("caption")
        description.add_css_class("dim-label")
        box.append(description)

        if command.source:
            source = Gtk.Label(label=command.source, xalign=1)
            source.add_css_class("caption")
            source.add_css_class("helios-slash-source")
            box.append(source)

        self.set_child(box)
        self.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [
                f"{command.name} — {command.description}"
                if command.description
                else command.name
            ],
        )
