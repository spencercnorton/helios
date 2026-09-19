"""Appearance / color-scheme preference (GTK-free core).

The GTK glue that maps these values onto Adw.ColorScheme and applies them to
the default style manager lives in helios.app; this module stays import-safe
for the GTK-free CI test lane.
"""

from __future__ import annotations

UI_STATE_KEY = "color_scheme"

SCHEMES = ("auto", "light", "dark")

# (label, value) — order is the combo-row order.
SCHEME_CHOICES = [
    ("Automatic (follow system)", "auto"),
    ("Light", "light"),
    ("Dark", "dark"),
]


def normalize_scheme(value: object) -> str:
    """Coerce any stored/incoming value to a known scheme; default 'auto'."""
    return value if value in SCHEMES else "auto"


#: Translucent sessions sidebar. Off by default — it only looks intentional
#: with a compositor blur behind it (Blur My Shell's Applications component,
#: whitelisting the `dev.norvi.Helios` window AND `dynamic-opacity` off, which
#: otherwise hides the blur on the focused window), and over a busy wallpaper
#: with no blur it costs readability for nothing.
GLASS_UI_STATE_KEY = "glass_sidebar"


def normalize_glass(value: object) -> bool:
    """Coerce any stored/incoming value to a bool; default False.

    Deliberately strict rather than truthy: a stored ``"false"`` from a
    hand-edited ui_state.json is a non-empty string and would otherwise enable
    the feature. Only real booleans and the two JSON spellings count.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False
