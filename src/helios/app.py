from __future__ import annotations

import importlib.resources

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from helios import APP_ID, APP_NAME, __version__
from helios.backend import appearance, ui_state
from helios.resources.icons import LAUNCH_ICON_NAME


_COLOR_SCHEMES = {
    "auto": Adw.ColorScheme.DEFAULT,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
}


def register_app_icons(display) -> bool:
    """Register Helios's unpacked package icon directory with this display."""

    if display is None:
        return False
    try:
        icon_dir = importlib.resources.files("helios.resources.icons")
        if not icon_dir.is_dir():
            return False
    except (FileNotFoundError, ModuleNotFoundError):
        return False
    theme = Gtk.IconTheme.get_for_display(display)
    path = str(icon_dir)
    if path not in theme.get_search_path():
        theme.add_search_path(path)
    return theme.has_icon(LAUNCH_ICON_NAME)


def apply_color_scheme(value: str) -> None:
    """Apply a saved appearance preference to the default style manager."""
    scheme = _COLOR_SCHEMES[appearance.normalize_scheme(value)]
    Adw.StyleManager.get_default().set_color_scheme(scheme)


#: Window class the translucent-sidebar rules in helios.css hang off.
GLASS_CSS_CLASS = "helios-glass"


def apply_glass(window: Gtk.Widget, value: object) -> bool:
    """Toggle the translucent-sidebar class on `window`. Returns the state set.

    Split out from the widget so the preference can be flipped from the
    settings dialog without rebuilding anything, and so the mapping from stored
    value to class is testable on its own.
    """
    on = appearance.normalize_glass(value)
    if on:
        window.add_css_class(GLASS_CSS_CLASS)
    else:
        window.remove_css_class(GLASS_CSS_CLASS)
    return on


class HeliosApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect("activate", self._on_activate)
        self._setup_actions()
        self._scheme_watch_id = 0

    # --- lifecycle ---

    def _on_activate(self, _app) -> None:
        self._apply_saved_scheme()
        self._watch_portal_color_scheme()
        self._apply_style()

        window = self.props.active_window
        if window is None:
            from helios.main_window import MainWindow

            window = MainWindow(self)
        apply_glass(window, ui_state.store().get(appearance.GLASS_UI_STATE_KEY, False))
        window.present()
        # Belt-and-suspenders: re-assert once after the window is up and the
        # portal has had a beat to connect, in case the first apply landed
        # before it was ready (see _watch_portal_color_scheme).
        GLib.timeout_add(1500, self._reapply_saved_scheme_once)

    def _apply_saved_scheme(self) -> None:
        apply_color_scheme(ui_state.store().get(appearance.UI_STATE_KEY, "auto"))

    def _reapply_saved_scheme_once(self) -> bool:
        self._apply_saved_scheme()
        return False  # one-shot

    def _watch_portal_color_scheme(self) -> None:
        """xdg-desktop-portal can be slow or absent at login (it times out on
        this box), so the color scheme set at activate can land *before* the
        portal is ready — the app then comes up light and, for the 'auto'
        (follow-system) setting, never re-establishes the system-follow.
        Re-assert our saved preference whenever portal color-scheme support
        (re)appears, so the theme self-corrects once the portal is up."""
        if self._scheme_watch_id:
            return
        style_manager = Adw.StyleManager.get_default()
        self._scheme_watch_id = style_manager.connect(
            "notify::system-supports-color-schemes",
            lambda *_: self._apply_saved_scheme(),
        )

    # --- styling ---

    def _apply_style(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        register_app_icons(display)
        provider = Gtk.CssProvider()
        try:
            css = importlib.resources.files("helios.resources.style").joinpath(
                "helios.css"
            ).read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError):
            css = ""
        if css:
            provider.load_from_string(css)
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    # --- actions ---

    def _setup_actions(self) -> None:
        quit_act = Gio.SimpleAction.new("quit", None)
        quit_act.connect("activate", lambda *_: self.quit())
        self.add_action(quit_act)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

        about_act = Gio.SimpleAction.new("about", None)
        about_act.connect("activate", self._show_about)
        self.add_action(about_act)

    def _show_about(self, *_args) -> None:
        window = self.props.active_window
        about = Adw.AboutDialog()
        about.set_application_name(APP_NAME)
        about.set_application_icon(APP_ID)
        about.set_developer_name("Spencer Norton")
        about.set_version(__version__)
        about.set_comments("Native Ubuntu GUI for Claude Code and OpenAI Codex")
        about.set_license_type(Gtk.License.MIT_X11)
        about.set_copyright("© 2026 Spencer Norton")
        about.set_website("https://github.com/spencercnorton/helios")
        about.set_issue_url("https://github.com/spencercnorton/helios/issues")
        if window is not None:
            about.present(window)
