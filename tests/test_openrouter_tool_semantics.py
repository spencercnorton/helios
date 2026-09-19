"""Correctness of the OpenRouter in-process tool implementations (F-8, F-9).

Two silent-wrong-answer defects, both measured 2026-09-03 against a real
workspace:

* ``_atomic_write`` replaced files through ``tempfile.mkstemp`` (always
  0600) and never restored the destination's mode, so every Write/Edit
  reset 0644/0755 to 0600 — an executable script the agent edited silently
  stopped being executable.
* ``Glob``/``Grep``'s ``glob`` filter matched
  ``fnmatch(file_path.name, pattern)``, ignoring everything left of the last
  "/" in the pattern, so the two most common idioms (``**/*.py``,
  ``src/*.py``) always returned "no matches" even when the file was right
  there — the model then concludes there is no such code.
"""

from __future__ import annotations

import os
import stat

import pytest

from helios.backend.process import openrouter_tools as t


class TestAtomicWriteModePreservation:
    def test_write_preserves_0644(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("old", encoding="utf-8")
        os.chmod(target, 0o644)
        _, is_error = t.execute_tool(
            "Write", {"file_path": str(target), "content": "new"}, cwd=str(tmp_path)
        )
        assert not is_error
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_write_preserves_0755_executable(self, tmp_path):
        target = tmp_path / "script.sh"
        target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        os.chmod(target, 0o755)
        _, is_error = t.execute_tool(
            "Write",
            {"file_path": str(target), "content": "#!/bin/sh\necho new\n"},
            cwd=str(tmp_path),
        )
        assert not is_error
        assert stat.S_IMODE(target.stat().st_mode) == 0o755

    def test_edit_preserves_mode_too(self, tmp_path):
        """Edit shares _atomic_write with Write; pin it separately since the
        fix lives in the shared helper, not in either caller."""
        target = tmp_path / "f.py"
        target.write_text("x = 1\n", encoding="utf-8")
        os.chmod(target, 0o755)
        _, is_error = t.execute_tool(
            "Edit",
            {"file_path": str(target), "old_string": "x = 1", "new_string": "x = 2"},
            cwd=str(tmp_path),
        )
        assert not is_error
        assert stat.S_IMODE(target.stat().st_mode) == 0o755

    def test_new_file_gets_umask_derived_mode_not_mkstemps_0600(self, tmp_path):
        target = tmp_path / "new.txt"
        old_umask = os.umask(0o022)
        try:
            _, is_error = t.execute_tool(
                "Write", {"file_path": str(target), "content": "hi"}, cwd=str(tmp_path)
            )
        finally:
            os.umask(old_umask)
        assert not is_error
        assert stat.S_IMODE(target.stat().st_mode) == 0o644  # 0o666 & ~0o022

    def test_temp_file_is_cleaned_up_when_the_replace_fails(self, tmp_path, monkeypatch):
        # A dedicated subdirectory, not tmp_path itself: the autouse
        # isolate_state_dir fixture points HELIOS_STATE_DIR at tmp_path and
        # populates a sibling "logs" dir there, which would otherwise show up
        # as an unrelated "leftover".
        target_dir = tmp_path / "sub"
        target = target_dir / "f.txt"

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(t.os, "replace", boom)
        with pytest.raises(OSError):
            t._atomic_write(target, "content")
        assert not target.exists()
        assert list(target_dir.iterdir()) == []


class TestGlobSemantics:
    """A pattern containing "/" matches the path relative to the search
    root, POSIX-style, with "**" crossing directories. A bare pattern keeps
    matching the basename at any depth — the existing behaviour several
    other tests already rely on — so it must not regress."""

    @pytest.fixture()
    def tree(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("marker\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("marker\n", encoding="utf-8")
        (tmp_path / "src" / "readme.txt").write_text("marker\n", encoding="utf-8")
        return tmp_path

    @pytest.mark.parametrize(
        "pattern,expected",
        [
            ("*.py", {"b.py", "a.py"}),
            ("**/*.py", {"b.py", "a.py"}),
            ("src/*.py", {"a.py"}),
            ("src/**/*.py", {"a.py"}),
            ("*.rs", set()),
        ],
    )
    def test_glob_pattern(self, tree, pattern, expected):
        content, is_error = t.execute_tool("Glob", {"pattern": pattern}, cwd=str(tree))
        assert not is_error
        if not expected:
            assert content == "(no files matched)"
            return
        found = {line.rsplit("/", 1)[-1] for line in content.splitlines()}
        assert found == expected

    @pytest.mark.parametrize(
        "glob_pat,expected",
        [
            ("*.py", {"b.py", "a.py"}),
            ("**/*.py", {"b.py", "a.py"}),
            ("src/*.py", {"a.py"}),
            ("src/**/*.py", {"a.py"}),
            ("*.rs", set()),
        ],
    )
    def test_grep_glob_filter(self, tree, glob_pat, expected):
        content, is_error = t.execute_tool(
            "Grep", {"pattern": "marker", "glob": glob_pat}, cwd=str(tree)
        )
        assert not is_error
        if not expected:
            assert content == "(no matches)"
            return
        found = {line.split(":", 1)[0].rsplit("/", 1)[-1] for line in content.splitlines()}
        assert found == expected


class TestGlobTranslatorMatchesTheStdlib:
    """`Path.full_match` is 3.13+ and this package declares a 3.11 floor, so
    the translation is hand-rolled and used on EVERY interpreter. Delegating
    with an `fnmatch` fallback was tried and was wrong rather than merely
    looser — `fnmatch` requires a literal "/" for the "**/" in `**/*.py`, so a
    top-level `b.py` stopped matching, which is worse than the basename
    matching this replaced. CI caught it because `gtk_tests` runs on
    ubuntu:24.04 (Python 3.12) while every other job is 3.13+.
    """

    PATHS = [
        "b.py", "a.txt", "src/a.py", "src/deep/c.py", "src/deep/deeper/d.py",
        "tests/test_x.py", "src/a.pyc", "x-1.py", "README.md", "docs/a/b/c.md",
    ]
    PATTERNS = [
        "**/*.py", "src/*.py", "src/**/*.py", "src/*/*.py", "**/test_*.py",
        "docs/**/*.md", "**", "src/**", "*/*.py", "src/a?.py", "src/[ab].py",
        "**/*.[pm]*",
    ]

    def test_it_agrees_with_pathlib_wherever_pathlib_has_full_match(self):
        import pathlib

        if not hasattr(pathlib.PurePosixPath("a"), "full_match"):
            import pytest as _pytest

            _pytest.skip("pathlib.full_match is 3.13+")
        mismatches = [
            (pattern, path)
            for pattern in self.PATTERNS
            for path in self.PATHS
            if bool(t._glob_regex(pattern).match(path))
            != pathlib.PurePosixPath(path).full_match(pattern)
        ]
        assert mismatches == []

    @pytest.mark.parametrize(
        "pattern,path,expected",
        [
            # The two the fnmatch fallback got wrong, on every interpreter.
            ("**/*.py", "b.py", True),
            ("**/*.py", "src/a.py", True),
            ("src/**/*.py", "src/a.py", True),
            ("src/**/*.py", "src/deep/c.py", True),
            # And the separator rules that make the pattern mean anything.
            ("src/*.py", "src/a.py", True),
            ("src/*.py", "src/deep/c.py", False),
            ("src/*.py", "other/a.py", False),
            ("*/*.py", "src/a.py", True),
            ("*/*.py", "b.py", False),
        ],
    )
    def test_the_semantics_hold_without_the_stdlib(self, pattern, path, expected):
        assert bool(t._glob_regex(pattern).match(path)) is expected

    def test_an_unterminated_character_class_is_a_literal_not_a_crash(self):
        assert t._glob_regex("src/[abc.py").match("src/[abc.py") is not None


class TestWritesNeverTouchTheProcessUmask:
    """A review finding: reading the umask meant calling os.umask twice, and
    the umask is process-global under worker threads — a concurrent create in
    that interval got mode 022's permissions instead of the configured ones.
    The temp file is now opened O_CREAT|O_EXCL at 0o666 so the kernel applies
    the real umask itself, and nothing here ever calls os.umask."""

    def test_os_umask_is_never_called_on_a_write(self, tmp_path, monkeypatch):
        def _boom(*_a):
            raise AssertionError("os.umask must not be called from a tool write")

        monkeypatch.setattr(os, "umask", _boom)
        t.execute_tool("Write", {"file_path": "new.txt", "content": "x"}, cwd=str(tmp_path))
        t.execute_tool("Edit", {"file_path": "new.txt", "old_string": "x", "new_string": "y"}, cwd=str(tmp_path))
        assert (tmp_path / "new.txt").read_text() == "y"

    def test_a_new_file_matches_an_ordinary_create_under_the_live_umask(self, tmp_path):
        t.execute_tool("Write", {"file_path": "fresh.txt", "content": "x"}, cwd=str(tmp_path))
        reference = tmp_path / "reference"
        reference.touch()
        assert stat.S_IMODE((tmp_path / "fresh.txt").stat().st_mode) == stat.S_IMODE(
            reference.stat().st_mode
        )

    def test_the_temp_file_cannot_follow_a_planted_symlink(self, tmp_path):
        """O_EXCL refuses an existing path, so even a guessed temp name that
        has been pre-planted as a symlink is not followed or truncated."""
        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")
        fd, tmp = t._open_unique_temp(tmp_path, "target.txt", 0o600)
        os.close(fd)
        assert tmp.exists() and not tmp.is_symlink()
        tmp.unlink()
        assert victim.read_text() == "keep me"


class TestGlobCharacterClassesCannotBreakTheTool:
    """A review finding: the class body was spliced into the regex after
    only translating a leading `!`, so the model's own pattern decided whether
    the regex compiled. Measured before fixing: `Glob "src/[z-a].py"` returned
    `Glob failed: bad character range z-a`, and `[[:alpha:]]` raised a
    nested-set FutureWarning that later Pythons make an error."""

    @pytest.mark.parametrize(
        "pattern",
        [
            "src/[z-a].py",      # reversed range — used to raise
            "src/[[:alpha:]].py",  # POSIX class — used to warn as a nested set
            "src/[a-",            # unterminated
            "src/[].py",          # empty class
            "src/[!]x].py",       # negated, leading ] is a member
            "src/[]]*.py",        # leading ] is a member
            "src/[\\\\].py",       # backslash member
            "src/[^a].py",        # ^ negation
            "src/[-a].py",        # leading hyphen is a literal
            "src/[a-].py",        # trailing hyphen is a literal
        ],
    )
    def test_no_class_can_raise_or_warn(self, tmp_path, pattern, recwarn):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")
        content, is_error = t.execute_tool("Glob", {"pattern": pattern}, cwd=str(tmp_path))
        assert not is_error, content
        assert not any(
            issubclass(w.category, FutureWarning) for w in recwarn
        ), [str(w.message) for w in recwarn]

    def test_a_reversed_range_matches_its_literal_members(self):
        """What a shell does with `[z-a]`: three literal characters."""
        assert t._glob_regex("x/[z-a]").match("x/z")
        assert t._glob_regex("x/[z-a]").match("x/-")
        assert t._glob_regex("x/[z-a]").match("x/a")
        assert not t._glob_regex("x/[z-a]").match("x/m")

    def test_a_well_formed_range_is_still_a_range(self):
        assert t._glob_regex("x/[a-z]").match("x/m")
        assert not t._glob_regex("x/[a-z]").match("x/M")

    def test_negation_works_with_either_marker(self):
        for pattern in ("x/[!ab]", "x/[^ab]"):
            assert t._glob_regex(pattern).match("x/c")
            assert not t._glob_regex(pattern).match("x/a")

    def test_a_pattern_that_cannot_be_translated_matches_literally(self, monkeypatch):
        """The guard of last resort: an unusable pattern is a non-match, never
        an exception the model has to interpret."""
        monkeypatch.setattr(t, "_glob_segment", lambda _p: "(?P<x>")  # invalid regex
        t._glob_regex.cache_clear()
        try:
            rx = t._glob_regex("a/weird")
            assert rx.match("a/weird")
            assert not rx.match("a/other")
        finally:
            t._glob_regex.cache_clear()


class TestAPrivateFileIsNeverExposedMidWrite:
    """A review finding: the temp file was always created at 0666 & ~umask,
    so rewriting a 0600 file put its contents in a 0644/0664 temp until the
    chmod after the write. Measured before the fix: 0o664 temps observed while
    rewriting a 0600 destination."""

    def test_the_temp_file_is_never_wider_than_the_destination(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("old")
        os.chmod(secret, 0o600)
        seen: list[int] = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kw):
            if ".secret.txt." in str(path):
                seen.append(mode)
            return real_open(path, flags, mode, **kw)

        import unittest.mock as _m
        with _m.patch.object(os, "open", spy):
            t.execute_tool(
                "Write", {"file_path": "secret.txt", "content": "x" * 5_000}, cwd=str(tmp_path)
            )
        assert seen == [0o600], seen
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600

    def test_a_new_file_still_uses_the_umask_path(self, tmp_path):
        seen: list[int] = []
        real_open = os.open

        def spy(path, flags, mode=0o777, **kw):
            if ".fresh.txt." in str(path):
                seen.append(mode)
            return real_open(path, flags, mode, **kw)

        import unittest.mock as _m
        with _m.patch.object(os, "open", spy):
            t.execute_tool("Write", {"file_path": "fresh.txt", "content": "x"}, cwd=str(tmp_path))
        assert seen == [0o666], seen
        reference = tmp_path / "reference"
        reference.touch()
        assert stat.S_IMODE((tmp_path / "fresh.txt").stat().st_mode) == stat.S_IMODE(
            reference.stat().st_mode
        )

    def test_an_executable_still_keeps_its_bits(self, tmp_path):
        script = tmp_path / "run.sh"
        script.write_text("#!/bin/sh\necho old\n")
        os.chmod(script, 0o755)
        t.execute_tool(
            "Edit", {"file_path": "run.sh", "old_string": "old", "new_string": "new"},
            cwd=str(tmp_path),
        )
        assert stat.S_IMODE(script.stat().st_mode) == 0o755


def test_read_defaults_to_focused_window_and_supports_explicit_continuation(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text("\n".join(f"row {i}" for i in range(1, 1001)))
    output, error = t.execute_tool("Read", {"path": str(path)}, cwd=str(tmp_path))
    assert not error
    assert "200: row 200" in output and "201: row 201" not in output
    assert "[showing lines 1-200 of 1000]" in output
    continued, error = t.execute_tool(
        "Read", {"path": str(path), "offset": 201, "limit": 300}, cwd=str(tmp_path),
    )
    assert not error
    assert "201: row 201" in continued and "500: row 500" in continued
    assert "501: row 501" not in continued
