"""Runtime dependency version checks.

`gi.require_version("Adw", "1")` only pins the *major* version, so an
incompatible older libadwaita/GTK passes import and then crashes later with an
opaque ``AttributeError: 'gi.repository.Adw' has no attribute 'PreferencesDialog'``
when the UI first touches a newer API. This module checks the actual runtime
versions up front and fails with a clear, actionable message instead.

Minimums are driven by the newest APIs Helios actually uses:
  * GTK 4.10  — Gtk.UriLauncher, Gtk.FileDialog
  * libadwaita 1.5  — Adw.AboutDialog, Adw.PreferencesDialog, Adw.AlertDialog
  * GtkSourceView 5  — the source-view widget family
"""

from __future__ import annotations

from helios.log import get_logger

_log = get_logger("deps")

# (gi namespace, gi version-arg, required (major, minor), human name)
_REQUIREMENTS = [
    ("Gtk", "4.0", (4, 10), "GTK"),
    ("Adw", "1", (1, 5), "libadwaita"),
    ("GtkSource", "5", (5, 0), "GtkSourceView"),
]


def _problem_for(
    label: str,
    required: tuple[int, int],
    installed: tuple[int, int] | None,
    detail: str = "",
) -> str | None:
    """Pure comparison helper (no gi). Returns a problem string or None."""
    min_major, min_minor = required
    if installed is None:
        msg = f"{label} {min_major}.{min_minor}+ is required but is not available"
        return f"{msg} ({detail})." if detail else f"{msg}."
    if installed < (min_major, min_minor):
        return (
            f"{label} {min_major}.{min_minor}+ is required, "
            f"but {installed[0]}.{installed[1]} is installed."
        )
    return None


def check_runtime_versions() -> list[str]:
    """Return a list of human-readable problems; empty if everything is OK."""
    import gi

    problems: list[str] = []
    for namespace, ver_arg, required, label in _REQUIREMENTS:
        installed: tuple[int, int] | None = None
        detail = ""
        try:
            gi.require_version(namespace, ver_arg)
            mod = __import__("gi.repository", fromlist=[namespace])
            ns = getattr(mod, namespace)
            installed = (ns.get_major_version(), ns.get_minor_version())
        except (ValueError, ImportError) as e:
            detail = str(e)
        problem = _problem_for(label, required, installed, detail)
        if problem:
            problems.append(problem)
    return problems


def format_problems(problems: list[str]) -> str:
    return (
        "Helios can't start because some system libraries are too old:\n\n"
        + "\n".join(f"  • {p}" for p in problems)
        + "\n\nPlease upgrade these packages (on Ubuntu/Debian: "
        "gir1.2-gtk-4.0, gir1.2-adw-1, gir1.2-gtksource-5) and try again."
    )


def _show_gui_error(message: str) -> bool:
    """Best-effort native error dialog. Returns True if it managed to show one.

    Uses only GTK (not libadwaita, which may be the thing that's too old). If
    GTK itself is too old for Gtk.AlertDialog, we give up and let the caller
    fall back to stderr."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        if not hasattr(Gtk, "AlertDialog"):
            return False

        app = Gtk.Application(application_id="dev.norvi.Helios.DepsError")

        def _activate(a: Gtk.Application) -> None:
            win = Gtk.ApplicationWindow(application=a)
            win.set_default_size(1, 1)
            dialog = Gtk.AlertDialog()
            dialog.set_modal(True)
            dialog.set_message("Helios can't start")
            dialog.set_detail(message)

            def _done(*_a) -> None:
                a.quit()

            dialog.choose(win, None, _done)

        app.connect("activate", _activate)
        app.run([])
        return True
    except Exception:  # noqa: BLE001 — error path must never itself crash
        return False


def enforce_runtime_versions() -> bool:
    """Check versions; on failure log + print + best-effort GUI dialog.

    Returns True if the runtime is OK (caller proceeds), False if it should
    exit non-zero."""
    problems = check_runtime_versions()
    if not problems:
        return True
    message = format_problems(problems)
    _log.critical("incompatible runtime: %s", "; ".join(problems))
    print(message, file=__import__("sys").stderr)
    _show_gui_error(message)
    return False
