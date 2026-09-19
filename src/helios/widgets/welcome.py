from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


def build_welcome_view() -> Gtk.Widget:
    """Shown when no session is selected. Mirrors GNOME's empty-state HIG."""
    page = Adw.StatusPage()
    page.set_icon_name("network-workgroup-symbolic")
    page.set_title("Helios")
    page.set_description(
        "All your Helios sessions - this machine and the shared pool.\n"
        "Pick a session on the left, or press Ctrl+N for a new chat."
    )
    return page
