"""Tests for memory_io: frontmatter round-trips, lossy preservation,
external-change detection, and backup rotation."""
from __future__ import annotations

import os

import pytest

from helios.backend import memory_io
from helios.backend.memory_io import (
    BACKUP_RETAIN,
    has_external_changes,
    load,
    load_plain,
    save,
    save_plain,
)

SIMPLE = """---
name: test-slug
description: a one-line summary
metadata:
  type: feedback
---

Body line one.

Body line two.
"""

LISTY = """---
name: listy
topics:
  - alpha
  - beta
metadata:
  type: project
---

Body.
"""

BLOCK_SCALAR = """---
name: blocky
description: |
  multi-line
  description
---

Body.
"""

COMMENTED = """---
# a comment the parser would drop
name: commented
---

Body.
"""

# A hand-written CLAUDE.md whose top line is a `---` divider, not
# frontmatter. load()/save() would mis-split this; load_plain()/save_plain()
# must not.
CLAUDE_MD_LEADING_DASHES = """---
This looks like frontmatter but is just a divider at the top of a
hand-written CLAUDE.md. It must never be parsed as one.
---

# Project notes

Real content follows the divider above.
"""


def _write(tmp_path, text, name="m.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_simple_roundtrip_preserves_fields_and_body(tmp_path):
    p = _write(tmp_path, SIMPLE)
    mem = load(p)
    assert not mem.lossy
    assert mem.name == "test-slug"
    assert mem.description == "a one-line summary"
    assert mem.frontmatter["metadata"]["type"] == "feedback"
    assert "Body line two." in mem.body

    save(mem, backup=False)
    again = load(p)
    assert again.name == mem.name
    assert again.description == mem.description
    assert again.frontmatter == mem.frontmatter
    assert again.body == mem.body


def test_non_lossy_save_is_textually_idempotent(tmp_path):
    p = _write(tmp_path, SIMPLE)
    mem = load(p)
    save(mem, backup=False)
    first = p.read_text(encoding="utf-8")
    save(load(p), backup=False)
    assert p.read_text(encoding="utf-8") == first


def test_list_frontmatter_detected_lossy_and_preserved(tmp_path):
    p = _write(tmp_path, LISTY)
    mem = load(p)
    assert mem.lossy
    # The dict parser drops the list — the raw text must keep it.
    assert "- alpha" in mem.raw_frontmatter

    mem.body = "Edited body."
    save(mem, backup=False)
    text = p.read_text(encoding="utf-8")
    assert "- alpha" in text and "- beta" in text  # header verbatim
    assert "Edited body." in text
    assert load(p).lossy  # still flagged on reload


def test_block_scalar_detected_lossy(tmp_path):
    mem = load(_write(tmp_path, BLOCK_SCALAR))
    assert mem.lossy


def test_comment_detected_lossy(tmp_path):
    mem = load(_write(tmp_path, COMMENTED))
    assert mem.lossy


def test_no_frontmatter_roundtrip(tmp_path):
    p = _write(tmp_path, "Just a body.\nNo header.\n")
    mem = load(p)
    assert not mem.lossy and mem.frontmatter == {}
    save(mem, backup=False)
    assert p.read_text(encoding="utf-8") == "Just a body.\nNo header.\n"


def test_external_change_detected_and_cleared_by_save(tmp_path):
    p = _write(tmp_path, SIMPLE)
    mem = load(p)
    assert not has_external_changes(mem)
    # Simulate another tool touching the file well past mtime tolerance.
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert has_external_changes(mem)
    save(mem, backup=False)  # save refreshes read_mtime
    assert not has_external_changes(mem)


def test_backup_rotation_keeps_retain_count(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_io, "_BACKUPS_ROOT", tmp_path / "backups")
    p = _write(tmp_path, SIMPLE)
    mem = load(p)
    for i in range(BACKUP_RETAIN + 3):
        mem.body = f"rev {i}"
        save(mem, backup=True)
    backups = memory_io.list_backups(p)
    assert 0 < len(backups) <= BACKUP_RETAIN


# ── Plain files: CLAUDE.md / MEMORY.md (no frontmatter, ever) ─────────────


def test_plain_roundtrip_never_parses_a_leading_dashes_block(tmp_path):
    p = _write(tmp_path, CLAUDE_MD_LEADING_DASHES, name="CLAUDE.md")
    mem = load_plain(p)
    # Never split: the leading `---...---` is body text, not frontmatter.
    assert mem.frontmatter == {}
    assert not mem.lossy
    assert mem.body == CLAUDE_MD_LEADING_DASHES

    save_plain(mem, backup=False)
    assert p.read_text(encoding="utf-8") == CLAUDE_MD_LEADING_DASHES
    assert load_plain(p).body == CLAUDE_MD_LEADING_DASHES


def test_plain_roundtrip_ordinary_body(tmp_path):
    text = "# Global CLAUDE.md\n\nSome instructions.\n"
    p = _write(tmp_path, text, name="CLAUDE.md")
    mem = load_plain(p)
    assert mem.body == text
    save_plain(mem, backup=False)
    assert p.read_text(encoding="utf-8") == text


def test_context_backup_rotation_lands_under_context_root(tmp_path, monkeypatch):
    ctx_root = tmp_path / "backups" / "context"
    monkeypatch.setattr(memory_io, "_CONTEXT_BACKUPS_ROOT", ctx_root)
    p = _write(tmp_path, "# CLAUDE.md\n\nGlobal instructions.\n", name="CLAUDE.md")
    mem = load_plain(p)
    for i in range(BACKUP_RETAIN + 3):
        mem.body = f"rev {i}\n"
        save_plain(mem, backup=True)
    backups = memory_io.list_backups(p, plain=True)
    assert 0 < len(backups) <= BACKUP_RETAIN
    assert all(ctx_root in b.parents for b in backups)


def test_context_backup_buckets_do_not_collide_across_same_stem_files(tmp_path, monkeypatch):
    """Global CLAUDE.md and a project's CLAUDE.md share a stem ("CLAUDE")
    but must never share a backup bucket — otherwise saving one would
    rotate out the other's history."""
    monkeypatch.setattr(memory_io, "_CONTEXT_BACKUPS_ROOT", tmp_path / "backups" / "context")
    global_claude = _write(tmp_path, "global instructions\n", name="CLAUDE.md")
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    project_claude = _write(proj_dir, "project instructions\n", name="CLAUDE.md")

    save_plain(load_plain(global_claude), backup=True)
    save_plain(load_plain(project_claude), backup=True)

    assert len(memory_io.list_backups(global_claude, plain=True)) == 1
    assert len(memory_io.list_backups(project_claude, plain=True)) == 1
    # And never mixed into memory_io's memory-file bucket for the same path.
    assert memory_io.list_backups(global_claude, plain=False) == []


def test_plain_external_change_detected_and_cleared_by_save(tmp_path):
    p = _write(tmp_path, "# CLAUDE.md\n\nSome notes.\n", name="CLAUDE.md")
    mem = load_plain(p)
    assert not has_external_changes(mem)
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert has_external_changes(mem)
    save_plain(mem, backup=False)  # save refreshes read_mtime
    assert not has_external_changes(mem)


# --- GPT cross-check fixes (2026-09-03) ---------------------------------------


def test_plain_save_writes_through_a_symlink_and_keeps_it(tmp_path):
    real = tmp_path / "dotfiles" / "CLAUDE.md"
    real.parent.mkdir()
    real.write_text("# real\n", encoding="utf-8")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(real)

    mem = memory_io.load_plain(link)
    mem.body = "# edited\n"
    memory_io.save_plain(mem, backup=False)

    assert link.is_symlink(), "the link itself must survive a save"
    assert real.read_text(encoding="utf-8") == "# edited\n"
    assert not (tmp_path / "CLAUDE.md.tmp").exists()


def test_plain_files_round_trip_crlf_and_trailing_newlines_verbatim(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(b"# title\r\n\r\ntext\r\n\r\n\r\n")
    mem = memory_io.load_plain(path)
    assert mem.body == "# title\r\n\r\ntext\r\n\r\n\r\n"
    memory_io.save_plain(mem, backup=False)
    assert path.read_bytes() == b"# title\r\n\r\ntext\r\n\r\n\r\n"


def test_a_non_utf8_plain_file_is_shown_but_never_rewritten(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(b"ok \xff\xfe bytes\n")
    mem = memory_io.load_plain(path)
    assert mem.lossy is True
    assert "ok" in mem.body
    with pytest.raises(ValueError):
        memory_io.save_plain(mem, backup=False)
    assert path.read_bytes() == b"ok \xff\xfe bytes\n"


def test_context_backup_buckets_are_injective_for_sibling_projects(tmp_path):
    a = tmp_path / "foo" / "bar" / "CLAUDE.md"
    b = tmp_path / "foo-bar" / "CLAUDE.md"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    assert memory_io._context_backup_dir(a) != memory_io._context_backup_dir(b)
    assert memory_io._context_backup_dir(a).parent.name.startswith("bar-")


def test_a_hostile_tmp_symlink_cannot_be_clobbered_by_a_save(tmp_path):
    """A review finding: the temp name was `<target>.tmp`, which an untrusted
    checkout can pre-place as a symlink to another writable file. Opening it
    "w" followed the link and truncated the victim before the rename."""
    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS\n", encoding="utf-8")
    target = tmp_path / "CLAUDE.md"
    target.write_text("# before\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md.tmp").symlink_to(victim)

    mem = memory_io.load_plain(target)
    mem.body = "# after\n"
    memory_io.save_plain(mem, backup=False)

    assert victim.read_text(encoding="utf-8") == "PRECIOUS\n", "the decoy was written through"
    assert target.read_text(encoding="utf-8") == "# after\n"
    assert not target.is_symlink()
    # No unique temp file is left behind.
    assert [p.name for p in tmp_path.glob("*.tmp*") if not p.is_symlink()] == []


def test_a_save_keeps_the_files_existing_permissions(tmp_path):
    """A review finding: the replacement inherited the umask, so a 0600 context file
    silently became 0644."""
    import stat as _stat

    target = tmp_path / "CLAUDE.md"
    target.write_text("# secret\n", encoding="utf-8")
    target.chmod(0o600)

    mem = memory_io.load_plain(target)
    mem.body = "# still secret\n"
    memory_io.save_plain(mem, backup=False)

    assert _stat.S_IMODE(target.stat().st_mode) == 0o600

    # A brand-new file is not forced tighter than an ordinary create.
    fresh = tmp_path / "NEW.md"
    new_mem = memory_io.load_plain(fresh)
    new_mem.body = "# hello\n"
    memory_io.save_plain(new_mem, backup=False)
    # The oracle is what an ordinary create produces under the live umask —
    # not a helper that read the umask by mutating it.
    reference = fresh.parent / "reference-create"
    reference.touch()
    assert _stat.S_IMODE(fresh.stat().st_mode) == _stat.S_IMODE(reference.stat().st_mode)


def test_a_save_never_touches_the_process_umask(tmp_path, monkeypatch):
    """A review finding (the same class as openrouter_tools, fixed together):
    reading the umask by mutating it races every other thread's file creation."""
    import os as _os

    def _boom(*_a):
        raise AssertionError("os.umask must not be called from a memory save")

    import stat as _stat

    monkeypatch.setattr(_os, "umask", _boom)
    target = tmp_path / "CLAUDE.md"
    target.write_text("# hi\n")
    mem = memory_io.load_plain(target)
    mem.body = "# changed\n"
    memory_io.save_plain(mem, backup=False)
    assert target.read_text() == "# changed\n"
    # And a brand-new file gets an ordinary create's mode, umask applied by the kernel.
    fresh = tmp_path / "MEMORY.md"
    fresh_mem = memory_io.load_plain(target)
    fresh_mem.path = fresh
    memory_io.save_plain(fresh_mem, backup=False)
    reference = tmp_path / "reference-create"
    reference.touch()
    assert _stat.S_IMODE(fresh.stat().st_mode) == _stat.S_IMODE(reference.stat().st_mode)


def test_a_private_context_file_is_never_exposed_mid_save(tmp_path, monkeypatch):
    """A review finding (the same class as openrouter_tools, fixed
    together): the temp file was created at 0666 & ~umask regardless of the
    destination, so a 0600 CLAUDE.md had its contents in a 0644 temp for the
    length of the write."""
    import os as _os
    import stat as _stat
    import unittest.mock as _m

    target = tmp_path / "CLAUDE.md"
    target.write_text("# secret\n")
    _os.chmod(target, 0o600)
    mem = memory_io.load_plain(target)
    mem.body = "# still secret\n"

    seen: list[int] = []
    real_open = _os.open

    def spy(path, flags, mode=0o777, **kw):
        if ".CLAUDE.md." in str(path):
            seen.append(mode)
        return real_open(path, flags, mode, **kw)

    with _m.patch.object(_os, "open", spy):
        memory_io.save_plain(mem, backup=False)

    assert seen == [0o600], seen
    assert _stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_new_context_file_still_uses_the_umask_path(tmp_path):
    import os as _os
    import stat as _stat
    import unittest.mock as _m

    seed = tmp_path / "seed.md"
    seed.write_text("# x\n")
    mem = memory_io.load_plain(seed)
    mem.path = tmp_path / "MEMORY.md"

    seen: list[int] = []
    real_open = _os.open

    def spy(path, flags, mode=0o777, **kw):
        if ".MEMORY.md." in str(path):
            seen.append(mode)
        return real_open(path, flags, mode, **kw)

    with _m.patch.object(_os, "open", spy):
        memory_io.save_plain(mem, backup=False)

    assert seen == [0o666], seen
    reference = tmp_path / "reference-create"
    reference.touch()
    assert _stat.S_IMODE(mem.path.stat().st_mode) == _stat.S_IMODE(
        reference.stat().st_mode
    )
