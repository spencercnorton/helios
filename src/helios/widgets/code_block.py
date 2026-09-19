"""Syntax-highlighted code block via GtkSourceView5.

Auto-resolves the system dark/light theme so blocks blend in with the OS.
"""

from __future__ import annotations

import weakref

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GtkSource", "5")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk, GtkSource  # noqa: E402


# Common language aliases users write in fences vs the canonical GtkSource ID.
_ALIAS = {
    "bash": "sh",
    "shell": "sh",
    "zsh": "sh",
    "fish": "sh",
    "javascript": "js",
    "ts": "typescript",
    "tsx": "jsx",
    "py": "python",
    "rb": "ruby",
    "rs": "rust",
    "go": "go",
    "dockerfile": "docker",
    "Dockerfile": "docker",
    "yml": "yaml",
    "md": "markdown",
    "h": "c",
    "hpp": "cpp",
    "c++": "cpp",
    "c#": "c-sharp",
    "cs": "c-sharp",
    "jsonc": "json",
    "html5": "html",
}


_lang_mgr = GtkSource.LanguageManager.get_default()
_scheme_mgr = GtkSource.StyleSchemeManager.get_default()


def _resolve_language(hint: str) -> GtkSource.Language | None:
    if not hint:
        return None
    raw = hint.strip().split()[0].lower()
    aliased = _ALIAS.get(raw, raw)
    return _lang_mgr.get_language(aliased)


def _current_scheme() -> GtkSource.StyleScheme | None:
    sm = Adw.StyleManager.get_default()
    is_dark = sm.get_dark()
    name = "Adwaita-dark" if is_dark else "Adwaita"
    return _scheme_mgr.get_scheme(name)


# ── Centralized theme tracking ──────────────────────────────────────────
# Every CodeBlock used to connect its own `notify::dark` handler against the
# long-lived `Adw.StyleManager`, with a closure holding `self`. GTK4's
# `destroy` signal isn't reliably fired for widgets removed via
# `parent.remove()` (which the streaming bubble does on every flush), so
# those connections leaked.
#
# Now there is ONE module-level handler. Each `CodeBlock` registers itself
# in a `WeakSet`; when the widget is collected the registry auto-drops the
# reference and the next theme flip simply skips it.

_THEME_SUBSCRIBERS: weakref.WeakSet[CodeBlock] = weakref.WeakSet()
_THEME_LISTENER_INSTALLED = False


def _on_theme_changed(*_args) -> None:
    scheme = _current_scheme()
    if scheme is None:
        return
    # Snapshot to a list — items can be GC'd mid-iteration.
    for cb in list(_THEME_SUBSCRIBERS):
        try:
            cb._buf.set_style_scheme(scheme)
        except Exception:
            pass


def _ensure_theme_listener() -> None:
    global _THEME_LISTENER_INSTALLED
    if _THEME_LISTENER_INSTALLED:
        return
    sm = Adw.StyleManager.get_default()
    sm.connect("notify::dark", _on_theme_changed)
    _THEME_LISTENER_INSTALLED = True


class CodeBlock(Gtk.Box):
    """A fenced code block with language header, copy button, and syntax highlighting."""

    def __init__(self, text: str, language_hint: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("helios-codeblock")
        self._text = text

        # Header strip.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.add_css_class("helios-codeblock-header")
        header.set_margin_top(4)
        header.set_margin_bottom(2)
        header.set_margin_start(10)
        header.set_margin_end(6)

        lang = _resolve_language(language_hint)
        label_text = lang.get_name() if lang else (language_hint or "code")
        label = Gtk.Label(label=label_text, xalign=0)
        label.add_css_class("caption")
        label.add_css_class("dim-label")
        label.set_hexpand(True)
        header.append(label)

        copy_btn = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        copy_btn.set_tooltip_text("Copy code")
        copy_btn.add_css_class("flat")
        copy_btn.add_css_class("circular")
        copy_btn.connect("clicked", self._on_copy)
        header.append(copy_btn)

        self.append(header)

        # Source buffer + view.
        buf = GtkSource.Buffer.new(None)
        if lang is not None:
            buf.set_language(lang)
        buf.set_highlight_syntax(True)
        scheme = _current_scheme()
        if scheme is not None:
            buf.set_style_scheme(scheme)
        buf.set_text(text)

        view = GtkSource.View.new_with_buffer(buf)
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.NONE)  # horizontal scroll for code
        view.set_show_line_numbers(False)
        view.set_top_margin(6)
        view.set_bottom_margin(8)
        view.set_left_margin(10)
        view.set_right_margin(10)
        view.add_css_class("helios-codeview")
        self._view = view
        self._buf = buf

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(view)
        self.append(scroller)

        # Re-style when the system dark/light changes. No per-instance
        # signal connection — the module-level listener handles every
        # CodeBlock through a WeakSet (see top of file).
        _ensure_theme_listener()
        _THEME_SUBSCRIBERS.add(self)

    def _on_copy(self, _btn: Gtk.Button) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        clip = display.get_clipboard()
        clip.set(self._text)
