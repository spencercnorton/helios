"""Operator prompt files in $XDG_CONFIG_HOME/helios/system-prompt.d/."""

from __future__ import annotations

from helios.backend.codex_context import load_extra_system_prompt


def test_absent_directory_adds_nothing(tmp_path):
    assert load_extra_system_prompt(str(tmp_path)) == ""


def test_markdown_files_join_in_name_order(tmp_path):
    d = tmp_path / "helios" / "system-prompt.d"
    d.mkdir(parents=True)
    (d / "20-style.md").write_text("Write short commit messages.\n", encoding="utf-8")
    (d / "10-tracker.md").write_text("  Record progress on {the} ticket.  \n", encoding="utf-8")
    (d / "15-empty.md").write_text("\n", encoding="utf-8")
    (d / "30-notes.txt").write_text("not a prompt", encoding="utf-8")
    (d / "40-dir.md").mkdir()  # unreadable as a file: skipped, not fatal

    assert load_extra_system_prompt(str(tmp_path)) == (
        "Record progress on {the} ticket.\n\nWrite short commit messages."
    )


def test_xdg_config_home_is_the_default_root(tmp_path, monkeypatch):
    d = tmp_path / "helios" / "system-prompt.d"
    d.mkdir(parents=True)
    (d / "a.md").write_text("site policy", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert load_extra_system_prompt() == "site policy"


def test_every_provider_gets_it(tmp_path, monkeypatch):
    import importlib
    from pathlib import Path

    import helios
    from helios.backend import codex_context

    d = tmp_path / "helios" / "system-prompt.d"
    d.mkdir(parents=True)
    (d / "a.md").write_text("site policy", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    try:
        assert importlib.reload(codex_context).CODEX_DEVELOPER_INSTRUCTIONS.endswith(" site policy")
    finally:
        monkeypatch.undo()
        importlib.reload(codex_context)

    # The Claude and OpenRouter drivers import GTK, so they are checked at the
    # source level to keep this test in the GTK-free lane.
    process = Path(helios.__file__).parent / "backend" / "process"
    assert 'argv += ["--append-system-prompt", EXTRA_SYSTEM_PROMPT]' in (process / "cli_driver.py").read_text()
    assert "EXTRA_SYSTEM_PROMPT.replace(" in (process / "openrouter_driver.py").read_text()
