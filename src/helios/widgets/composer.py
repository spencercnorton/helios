"""The bottom-of-transcript input area. Multi-line, Ctrl+Enter to send.

The input never locks while a turn is running — submitting mid-turn emits
`send` like any other submit, and MainWindow queues it (visible in the
transcript) for auto-send when the turn completes. Mirrors the Claude Code
desktop app. Only read-only (pooled remote) sessions disable typing."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from helios.backend.slash_commands import (
    SlashCommand,
    completion_text,
    match_commands,
)
from helios.resources.icons import LAUNCH_ICON_NAME
from helios.widgets.slash_popover import SlashPopover

# One compact motion token for the send acknowledgement. Keep the matching
# GTK CSS declaration at the same duration; GTK CSS cannot consume Python
# constants or custom properties.
SEND_ACCEPTANCE_ACK_MS = 160


def _set_send_accessibility(button, *, queued: bool) -> None:
    """Give the icon-only control an explicit action and keyboard hint."""

    label = "Queue message" if queued else "Send message"
    description = (
        "Queue this message to send after the current response. "
        "Keyboard shortcut: Control or Command plus Enter."
        if queued
        else "Send this message. Keyboard shortcut: Control or Command plus Enter."
    )
    button.update_property(
        [
            Gtk.AccessibleProperty.LABEL,
            Gtk.AccessibleProperty.DESCRIPTION,
        ],
        [label, description],
    )


class Composer(Gtk.Box):
    """Emits 'send' (str) when the user submits, 'stop' when they hit Stop
    mid-turn."""

    __gsignals__ = {
        "send": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "stop": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "commands-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("helios-composer-wrapper")
        self.set_margin_start(16)
        self.set_margin_end(16)
        self.set_margin_top(8)
        self.set_margin_bottom(16)

        # Above the card, so it is never hidden by buffer state the way the
        # placeholder is (the placeholder disappears the moment you type).
        self._ro_banner = Adw.Banner.new("")
        self._ro_banner.set_revealed(False)
        self.append(self._ro_banner)

        card = Gtk.Frame()
        card.add_css_class("helios-composer-card")
        card.set_hexpand(True)
        self.append(card)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(8)
        row.set_margin_end(8)
        card.set_child(row)

        # Multi-line TextView in a scroller (capped at ~6 rows then scrolls).
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_min_content_height(40)
        scroller.set_max_content_height(180)
        scroller.set_propagate_natural_height(True)

        self._textview = Gtk.TextView()
        self._textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._textview.set_top_margin(6)
        self._textview.set_bottom_margin(6)
        self._textview.set_left_margin(6)
        self._textview.set_right_margin(6)
        self._textview.add_css_class("helios-composer-text")
        scroller.set_child(self._textview)

        # Placeholder, overlaid (GTK4 has no built-in placeholder for TextView).
        self._assistant_label = "Claude"
        self._placeholder = Gtk.Label(label=self._default_placeholder_text())
        self._placeholder.set_xalign(0)
        self._placeholder.add_css_class("helios-composer-placeholder")
        self._placeholder.add_css_class("dim-label")
        self._placeholder.set_halign(Gtk.Align.START)
        self._placeholder.set_valign(Gtk.Align.START)
        self._placeholder.set_margin_top(12)
        self._placeholder.set_margin_start(14)
        self._placeholder.set_can_target(False)

        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_child(scroller)
        overlay.add_overlay(self._placeholder)
        row.append(overlay)

        # Send / Stop buttons. Idle: just Send. Busy: Stop, plus Send next to
        # it once there's text to queue.
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.set_valign(Gtk.Align.END)

        # The launch glyph is packaged and registered by HeliosApplication,
        # so its silhouette never changes with the host icon theme.
        self._send_btn = Gtk.Button.new_from_icon_name(LAUNCH_ICON_NAME)
        self._send_btn.set_tooltip_text("Send message (Ctrl+Enter)")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.add_css_class("circular")
        self._send_btn.add_css_class("helios-send-button")
        self._send_btn.set_size_request(36, 36)
        self._send_btn.connect("clicked", self._on_send_clicked)
        _set_send_accessibility(self._send_btn, queued=False)
        self._send_acceptance_ack_id = 0
        self._shutdown = False

        # Keep a fixed far-right allocation even while Send itself is hidden.
        # The acceptance glyph can therefore appear without shifting the Stop
        # containment target laterally.
        self._send_slot = Gtk.Overlay()
        self._send_slot.set_size_request(36, 36)
        self._send_slot.set_child(self._send_btn)

        # Spinner shown while generation is in progress.
        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(20, 20)
        self._spinner.set_visible(False)
        self._spinner.set_valign(Gtk.Align.END)
        self._spinner.set_margin_end(2)
        self._spinner.set_margin_bottom(8)
        row.append(self._spinner)

        self._stop_btn = Gtk.Button.new_from_icon_name("media-playback-stop-symbolic")
        self._stop_btn.set_tooltip_text("Stop generation (Esc) — queued messages return here")
        self._stop_btn.add_css_class("destructive-action")
        self._stop_btn.add_css_class("circular")
        self._stop_btn.set_size_request(36, 36)
        self._stop_btn.set_visible(False)
        self._stop_btn.connect("clicked", lambda *_: self.emit("stop"))

        # Stop sits left of Send so Send keeps its far-right home position.
        btn_box.append(self._stop_btn)
        btn_box.append(self._send_slot)
        row.append(btn_box)

        # Leading command and attachment controls. Commands open a
        # capability-driven launcher; they never insert pretend protocol
        # commands into the user's prompt.
        self._commands_btn = Gtk.Button.new_from_icon_name("system-run-symbolic")
        self._commands_btn.set_tooltip_text("Agent commands (Ctrl+Shift+P)")
        self._commands_btn.add_css_class("flat")
        self._commands_btn.add_css_class("circular")
        self._commands_btn.set_size_request(36, 36)
        self._commands_btn.set_valign(Gtk.Align.END)
        self._commands_btn.connect(
            "clicked",
            lambda *_: self.emit("commands-requested"),
        )
        self._commands_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            ["Open agent commands"],
        )
        row.prepend(self._commands_btn)

        # "+" inserts @path references the backend reads. Drag-and-drop of
        # files onto the composer does the same.
        self._attach_btn = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self._attach_btn.set_tooltip_text("Attach files (inserted as @path)")
        self._attach_btn.add_css_class("flat")
        self._attach_btn.add_css_class("circular")
        self._attach_btn.set_size_request(36, 36)
        self._attach_btn.set_valign(Gtk.Align.END)
        self._attach_btn.connect("clicked", self._on_attach_clicked)
        self._attach_dialog: Gtk.FileDialog | None = None  # pin across async
        row.prepend(self._attach_btn)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        # State fields BEFORE the first _on_buffer_changed call below — it
        # reaches _update_send_enabled, which reads _busy/_read_only. (The
        # old ordering only survived by short-circuit: the empty-buffer check
        # came first in the expression.)
        self._busy = False
        self._read_only = False
        # Slash completion. Empty until MainWindow supplies the provider's
        # inventory, so with none set the popover never appears at all.
        self._slash_commands: tuple[SlashCommand, ...] = ()
        self._slash = SlashPopover(self._textview)
        self._slash.connect("accepted", lambda *_: self._accept_slash())
        # Track placeholder visibility + send-button state (enabled whenever
        # there's text — mid-turn submits queue rather than being blocked).
        buf = self._textview.get_buffer()
        buf.connect("changed", self._on_buffer_changed)
        # Arrow keys move the cursor without changing the buffer, and leaving
        # the first token has to close the popover too.
        buf.connect("notify::cursor-position", lambda *_: self._update_slash())
        self._on_buffer_changed(buf)

        # Ctrl+Enter handler.
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self._textview.add_controller(key_ctrl)

    # --- public ---

    def set_busy(self, busy: bool) -> None:
        """Reflect turn-in-flight state: show Stop + spinner, hint that
        submits will queue. Typing stays enabled — only read-only locks it."""
        self._busy = busy
        self._stop_btn.set_visible(busy)
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._textview.set_editable(not self._read_only)
        self._refresh_placeholder()
        self._update_send_enabled()

    def set_assistant_label(self, label: str) -> None:
        self._assistant_label = label or "Assistant"
        self._refresh_placeholder()

    def set_read_only(self, read_only: bool, note: str = "") -> None:
        """View-only mode for remote (pooled) sessions: lock input + send and
        say why in a banner. `note` overrides the banner text."""
        self._read_only = read_only
        self._ro_banner.set_title(note or "Read-only session.")
        self._ro_banner.set_revealed(read_only)
        self._textview.set_editable(not read_only)
        self._attach_btn.set_sensitive(not read_only)
        self._commands_btn.set_sensitive(not read_only)
        self._refresh_placeholder()
        self._update_send_enabled()

    def set_slash_commands(self, commands: tuple[SlashCommand, ...]) -> None:
        """Supply the inventory the `/` popover completes against."""
        self._slash_commands = tuple(commands)
        self._update_slash()

    @property
    def slash_commands(self) -> tuple[SlashCommand, ...]:
        return self._slash_commands

    def grab_input_focus(self) -> None:
        self._textview.grab_focus()

    def clear(self) -> None:
        self._textview.get_buffer().set_text("")
        self._close_slash()

    def set_text(self, text: str) -> None:
        """Refill the input (e.g. queued messages handed back after a Stop)."""
        self._textview.get_buffer().set_text(text)
        self._close_slash()

    def current_text(self) -> str:
        buf = self._textview.get_buffer()
        start, end = buf.get_bounds()
        return buf.get_text(start, end, True)

    def acknowledge_accepted(self) -> None:
        """Acknowledge one causally accepted send, never a button click.

        MainWindow owns the provider/ledger boundary and calls this only after
        its durable acceptance path succeeds. Rejected, pending, and uncertain
        deliveries deliberately have no Composer entry point.
        """

        if self._shutdown:
            return
        if self._send_acceptance_ack_id:
            try:
                GLib.source_remove(self._send_acceptance_ack_id)
            except Exception:
                pass
            self._send_acceptance_ack_id = 0
        # Removing first restarts the one-shot keyframe if two provider ACKs
        # arrive unusually close together.
        self._send_btn.remove_css_class("helios-send-accepted")
        self._send_btn.add_css_class("helios-send-accepted")
        # Busy composers normally hide Send until there is another message to
        # queue. Keep the launch glyph visible for this short provider ACK so
        # the semantic feedback is not animating an invisible widget.
        self._send_btn.set_visible(True)
        self._send_acceptance_ack_id = GLib.timeout_add(
            SEND_ACCEPTANCE_ACK_MS,
            self._finish_acceptance_ack,
        )

    def shutdown(self) -> None:
        """Own and cancel the one-shot acknowledgement source idempotently."""

        if self._shutdown:
            return
        self._shutdown = True
        source_id, self._send_acceptance_ack_id = (
            self._send_acceptance_ack_id,
            0,
        )
        if source_id:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
        self._send_btn.remove_css_class("helios-send-accepted")
        # A popover is its own native surface: leaving it parented to a
        # disposed TextView is the accumulation bug in gtk4-gotchas §5.
        self._close_slash()
        self._slash.unparent()

    @staticmethod
    def _attach_token(path: str) -> str:
        # Quote paths with whitespace so the whole path stays ONE @-token
        # (common dirs like "My Project"/"Screenshots" would otherwise split).
        # Escape backslashes/quotes first so a crafted filename can't terminate
        # the quote early and inject extra @refs into the prompt.
        needs_quote = any(ch.isspace() for ch in path) or '"' in path or "\\" in path
        if not needs_quote:
            return f"@{path}"
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'@"{escaped}"'

    def add_attachments(self, paths: list[str]) -> None:
        """Insert `@<abspath>` references for the given files into the input so
        the backend can pick them up. Claude expands @-paths; for GPT they ride
        as plain text. ponytail: text @-refs only (no real image content-blocks);
        the path stays visible/editable, upgrade if it proves insufficient."""
        if self._read_only:
            return
        tokens = [self._attach_token(p) for p in paths if p]
        if not tokens:
            return
        buf = self._textview.get_buffer()
        existing = self.current_text()
        # Keep a single space between the previous text and the refs.
        prefix = "" if (not existing or existing.endswith((" ", "\n"))) else " "
        buf.insert(buf.get_end_iter(), prefix + " ".join(tokens) + " ")
        self._textview.grab_focus()

    def _on_attach_clicked(self, *_) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Attach files")
        self._attach_dialog = dialog  # pin: async result callback else GC'd
        dialog.open_multiple(self.get_root(), None, self._on_files_chosen)

    def _on_files_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return  # cancelled or failed — nothing to attach
        paths = []
        if files is not None:
            for i in range(files.get_n_items()):
                gfile = files.get_item(i)
                path = gfile.get_path() if gfile is not None else None
                if path:
                    paths.append(path)
        self.add_attachments(paths)

    def _on_drop(self, _target, value, _x, _y) -> bool:
        if self._read_only:
            return False
        paths = []
        try:
            for gfile in value.get_files():
                path = gfile.get_path()
                if path:
                    paths.append(path)
        except Exception:
            return False
        if not paths:
            return False
        self.add_attachments(paths)
        return True

    # --- internals ---

    def _refresh_placeholder(self) -> None:
        if self._busy:
            self._placeholder.set_label(
                f"{self._assistant_label} is working - type to queue your next message (Ctrl+Enter)"
            )
        else:
            self._placeholder.set_label(self._default_placeholder_text())

    def _default_placeholder_text(self) -> str:
        return (
            f"Send a message to {self._assistant_label}...  "
            "(Ctrl+Enter to send, Enter for newline)"
        )

    def _on_buffer_changed(self, buf: Gtk.TextBuffer) -> None:
        empty = buf.get_char_count() == 0
        self._placeholder.set_visible(empty)
        self._update_send_enabled()
        self._update_slash()

    # --- slash completion ---

    def _slash_prefix(self) -> str | None:
        """The text typed after a leading `/`, or None when the popover has no
        business being open: no leading slash, or a cursor that has left the
        first token."""
        first_line = self.current_text().split("\n", 1)[0]
        if not first_line.startswith("/"):
            return None
        token = first_line[1:].split(" ", 1)[0]
        buf = self._textview.get_buffer()
        cursor = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        if not 1 <= cursor <= len(token) + 1:
            return None
        return token

    def _update_slash(self) -> None:
        """Filter, show or hide the popover for the buffer's current state."""
        if not self._slash_commands or self._shutdown:
            self._close_slash()
            return
        typed = self._slash_prefix()
        matches = () if typed is None else match_commands(self._slash_commands, typed)
        if not matches:
            self._close_slash()
            return
        self._slash.set_commands(matches)
        if not self._slash.get_visible():
            self._slash.popup()

    def _close_slash(self) -> None:
        if self._slash.get_visible():
            self._slash.popdown()

    def _accept_slash(self) -> None:
        """Replace the first token with the highlighted command, keeping the
        rest of the buffer and leaving the cursor after the inserted space."""
        command = self._slash.highlighted_command()
        if command is None:
            return
        text = completion_text(command)
        first_line = self.current_text().split("\n", 1)[0]
        end = len(first_line.split(" ", 1)[0])
        # completion_text brings its own trailing space; swallow the one that
        # is already there rather than doubling it (as add_attachments does).
        if first_line[end : end + 1] == " ":
            end += 1
        buf = self._textview.get_buffer()
        buf.delete(buf.get_start_iter(), buf.get_iter_at_offset(end))
        buf.insert(buf.get_start_iter(), text)
        buf.place_cursor(buf.get_iter_at_offset(len(text)))
        self._close_slash()

    def _update_send_enabled(self) -> None:
        has_text = bool(self.current_text().strip())
        self._send_btn.set_sensitive(has_text and not self._read_only)
        # While busy, Send doubles as "queue": hidden until there's text so
        # the resting busy state stays a single Stop button.
        self._send_btn.set_visible(
            not self._busy or has_text or bool(self._send_acceptance_ack_id)
        )
        self._send_btn.set_tooltip_text(
            "Queue message (Ctrl+Enter)"
            if self._busy
            else "Send message (Ctrl+Enter)"
        )
        _set_send_accessibility(self._send_btn, queued=self._busy)

    def _finish_acceptance_ack(self) -> bool:
        self._send_acceptance_ack_id = 0
        if self._shutdown:
            return GLib.SOURCE_REMOVE
        self._send_btn.remove_css_class("helios-send-accepted")
        self._update_send_enabled()
        return GLib.SOURCE_REMOVE

    def _on_send_clicked(self, *_) -> None:
        self._submit()

    def _on_key_pressed(self, _ctrl, keyval, _keycode, state) -> bool:
        is_enter = keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter)
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        meta = bool(state & Gdk.ModifierType.META_MASK)
        if is_enter and (ctrl or meta):
            self._submit()
            return True
        if self._slash.get_visible() and not (ctrl or meta):
            return self._slash_key(keyval, is_enter)
        return False  # plain Enter inserts a newline

    def _slash_key(self, keyval, is_enter: bool) -> bool:
        """Navigation keys belong to the open popover — notably plain Enter,
        which completes instead of inserting a newline or sending."""
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._slash.move_highlight(1)
        elif keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._slash.move_highlight(-1)
        elif is_enter or keyval in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab, Gdk.KEY_ISO_Left_Tab):
            self._accept_slash()
        elif keyval == Gdk.KEY_Escape:
            self._close_slash()
        else:
            return False
        return True

    def _submit(self) -> None:
        text = self.current_text().strip()
        if not text or self._read_only:
            return
        # Mid-turn submits are allowed: MainWindow queues them for auto-send
        # when the current turn completes.
        self.emit("send", text)
        self.clear()
