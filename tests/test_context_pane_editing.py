"""Real-GTK contracts for ContextPane's editable CLAUDE.md/MEMORY.md and its
memory-directory watcher (parity plan Phase 2C).

Needs real GTK + a display like the other widget suites (test_mission_pane.py
is the closest prior art: a pane owning a debounced Gio.FileMonitor), so this
is skipped on the GTK-free CI image and any headless box.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from helios.backend import memory_io  # noqa: E402
from helios.backend.projects import Project  # noqa: E402
from helios.widgets.context_pane import ContextPane, _ContextRow  # noqa: E402


@pytest.fixture
def make_pane():
    """Construct ContextPanes and guarantee teardown even when a test's
    assertions fail partway through — a ContextPane owns a debounce timer
    and a Gio.FileMonitor, and a leaked one segfaults the next test in the
    same process (see docs/gtk4-gotchas.md #9)."""
    panes: list[ContextPane] = []

    def _make() -> ContextPane:
        pane = ContextPane()
        panes.append(pane)
        return pane

    yield _make

    errors = []
    for pane in panes:
        try:
            pane.shutdown()
        except Exception as exc:  # noqa: BLE001 — re-raised after all cleanup
            errors.append(exc)
    if errors:
        raise AssertionError(f"pane.shutdown() raised during teardown: {errors!r}")


def _make_project(tmp_path: Path) -> Project:
    """A project whose cwd (for CLAUDE.md) and .claude project dir (for
    memory/) are two distinct real temp directories — same shape as the
    real `~/.claude/projects/<encoded>/` layout, just relocated."""
    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    claude_project_dir = tmp_path / "claude-project"
    claude_project_dir.mkdir()
    return Project(dirname="-cwd", cwd=str(cwd_dir), path=claude_project_dir)


def _row_for_kind(pane: ContextPane, kind: str) -> _ContextRow | None:
    child = pane._listbox.get_first_child()
    while child is not None:
        if isinstance(child, _ContextRow) and child.context.kind == kind:
            return child
        child = child.get_next_sibling()
    return None


def _buffer_text(editor) -> str:
    start, end = editor._buffer.get_bounds()
    return editor._buffer.get_text(start, end, True)


def _pump(predicate, timeout: float = 5.0) -> bool:
    """Spin the default main context until `predicate` holds (or we give
    up) — the Gio.FileMonitor "changed" signal and the debounce timeout
    both deliver through it, not synchronously."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        ctx.iteration(False)
        time.sleep(0.01)
    return predicate()


def test_opening_claude_md_shows_editor_without_frontmatter_form(tmp_path, make_pane):
    project = _make_project(tmp_path)
    claude_md = Path(project.cwd) / "CLAUDE.md"
    claude_md.write_text("# Project notes\n\nHello.\n", encoding="utf-8")

    pane = make_pane()
    pane.set_project(project)
    row = _row_for_kind(pane, "project-claude-md")
    assert row is not None
    pane._listbox.select_row(row)

    assert pane._bottom_stack.get_visible_child_name() == "editor"
    editor = pane._editor
    assert editor._plain is True
    assert editor._form.get_visible() is False
    assert _buffer_text(editor) == "# Project notes\n\nHello.\n"


def test_saving_plain_file_writes_atomically_and_creates_a_backup(
    tmp_path, make_pane, monkeypatch
):
    monkeypatch.setattr(memory_io, "_CONTEXT_BACKUPS_ROOT", tmp_path / "ctx-backups")

    project = _make_project(tmp_path)
    claude_md = Path(project.cwd) / "CLAUDE.md"
    claude_md.write_text("original\n", encoding="utf-8")

    pane = make_pane()
    pane.set_project(project)
    pane._listbox.select_row(_row_for_kind(pane, "project-claude-md"))

    editor = pane._editor
    editor._buffer.set_text("edited body\n")
    assert editor.is_dirty()

    editor.commit()

    assert not editor.is_dirty()
    assert claude_md.read_text(encoding="utf-8") == "edited body\n"
    # Atomic write leaves no .tmp sibling behind.
    assert not claude_md.with_suffix(claude_md.suffix + ".tmp").exists()
    backups = memory_io.list_backups(claude_md, plain=True)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original\n"


def test_memory_dir_change_shows_banner_and_refreshes_list(tmp_path, make_pane):
    project = _make_project(tmp_path)
    mem_dir = project.path / "memory"
    mem_dir.mkdir()

    pane = make_pane()
    pane.set_project(project)
    assert pane._memory_monitor is not None
    assert _row_for_kind(pane, "memory") is None

    (mem_dir / "new-note.md").write_text(
        "---\nname: new-note\n---\n\nBody.\n", encoding="utf-8"
    )

    assert _pump(lambda: pane._memory_banner.get_revealed())
    assert "new-note.md" in pane._memory_banner.get_title()
    assert _row_for_kind(pane, "memory") is not None


def test_memory_dir_change_does_not_reload_a_dirty_open_editor(tmp_path, make_pane):
    project = _make_project(tmp_path)
    mem_dir = project.path / "memory"
    mem_dir.mkdir()
    open_file = mem_dir / "open.md"
    open_file.write_text("---\nname: open\n---\n\nOriginal body.\n", encoding="utf-8")

    pane = make_pane()
    pane.set_project(project)
    pane._listbox.select_row(_row_for_kind(pane, "memory"))
    assert pane._bottom_stack.get_visible_child_name() == "editor"

    pane._editor._buffer.set_text("Unsaved edit.")
    assert pane._editor.is_dirty()

    # The SAME file that's open gets modified externally (e.g. Claude
    # rewrote it mid-session).
    open_file.write_text(
        "---\nname: open\n---\n\nExternally changed.\n", encoding="utf-8"
    )
    assert _pump(lambda: pane._memory_banner.get_revealed())

    # The dirty editor is untouched — still the unsaved edit, not reloaded.
    # MemoryEditor's existing has_external_changes()/conflict-banner path is
    # what surfaces this instead, and only when the user next tries to save.
    assert _buffer_text(pane._editor) == "Unsaved edit."
    assert pane._editor.is_dirty()
    assert memory_io.has_external_changes(pane._editor._mem)


def test_shutdown_cancels_the_memory_monitor(tmp_path, make_pane):
    project = _make_project(tmp_path)
    (project.path / "memory").mkdir()

    pane = make_pane()
    pane.set_project(project)
    assert pane._memory_monitor is not None

    pane.shutdown()

    assert pane._memory_monitor is None
    assert pane._memory_debounce_id == 0
    assert pane._destroyed is True


def test_memory_monitor_rebinds_once_memory_dir_is_created_later(tmp_path, make_pane):
    """A brand new project has no memory/ yet. _start_memory_monitor falls
    back to watching project.path (which always exists) so the *first*
    memory file Claude ever writes is still noticed without a project
    switch — this is the gap the feature exists to close, so the fallback
    itself needs a check, not just the common already-exists path above."""
    project = _make_project(tmp_path)
    assert not (project.path / "memory").is_dir()

    pane = make_pane()
    pane.set_project(project)
    assert pane._memory_monitor is not None

    (project.path / "memory").mkdir()
    # Let the ancestor-watch observe "memory/" appearing and rebind onto it
    # directly before writing into it — real Claude tool calls are never
    # this fast back-to-back, but the rebind is what's under test here.
    # Wait for the EXACT directory, not merely "not None": the ancestor
    # fallback leaves it None today, but an exact predicate cannot be
    # satisfied by a future refactor that records the fallback's own path.
    assert _pump(
        lambda: pane._memory_monitor_dir is not None
        and Path(pane._memory_monitor_dir).resolve() == (project.path / "memory").resolve()
    ), "monitor rebound onto memory/"

    (project.path / "memory" / "first-note.md").write_text(
        "---\nname: first-note\n---\n\nBody.\n", encoding="utf-8"
    )

    assert _pump(lambda: pane._memory_banner.get_revealed())
    assert "first-note.md" in pane._memory_banner.get_title()


def test_a_self_save_is_not_announced_as_claude_updating_memory(tmp_path, make_pane):
    """A review finding: the monitor sees MemoryEditor's own atomic rename exactly
    like an external write, so saving in the UI showed "Claude updated
    memory". The pane now claims its own save once, within a short window."""
    import types

    from gi.repository import Gio

    project = _make_project(tmp_path)
    pane = make_pane()
    pane.set_project(project)
    mem_dir = project.path / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    target = mem_dir / "note.md"
    target.write_text("# before\n", encoding="utf-8")

    pane._on_editor_saved(None, types.SimpleNamespace(path=target))
    pane._on_memory_changed(None, Gio.File.new_for_path(str(target)), None, None)

    assert pane._memory_changed_names == set(), "our own save is not a Claude write"
    assert not pane._memory_banner.get_revealed()

    # A write we did not make still announces.
    pane._on_memory_changed(
        None, Gio.File.new_for_path(str(mem_dir / "other.md")), None, None
    )
    assert "other.md" in pane._memory_changed_names
