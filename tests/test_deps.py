"""Tests for the runtime version-check logic (GTK-free — gi is never imported)."""

from __future__ import annotations

from helios.deps import _REQUIREMENTS, _problem_for, format_problems


def test_ok_when_installed_meets_minimum():
    assert _problem_for("GTK", (4, 10), (4, 10)) is None
    assert _problem_for("GTK", (4, 10), (4, 22)) is None
    assert _problem_for("libadwaita", (1, 5), (1, 9)) is None


def test_problem_when_too_old():
    msg = _problem_for("libadwaita", (1, 5), (1, 4))
    assert msg is not None
    assert "1.5+ is required" in msg
    assert "1.4 is installed" in msg


def test_problem_when_missing():
    msg = _problem_for("GtkSourceView", (5, 0), None, detail="No module")
    assert msg is not None
    assert "not available" in msg
    assert "No module" in msg


def test_minor_version_boundary():
    # Same major, one minor below the floor -> problem.
    assert _problem_for("GTK", (4, 10), (4, 9)) is not None
    # Older major -> problem even with a high minor.
    assert _problem_for("GTK", (4, 10), (3, 99)) is not None


def test_requirements_table_matches_used_apis():
    """Guards against silently lowering the floor below what the code uses."""
    reqs = {label: required for _, _, required, label in _REQUIREMENTS}
    assert reqs["GTK"] >= (4, 10)  # Gtk.UriLauncher / Gtk.FileDialog
    assert reqs["libadwaita"] >= (1, 5)  # Adw.AboutDialog / PreferencesDialog
    assert reqs["GtkSourceView"][0] == 5


def test_format_problems_is_actionable():
    text = format_problems(["GTK 4.10+ is required, but 4.8 is installed."])
    assert "can't start" in text
    assert "GTK 4.10+" in text
    assert "gir1.2-gtk-4.0" in text  # names the package to upgrade
