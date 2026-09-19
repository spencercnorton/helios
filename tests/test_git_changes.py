"""Real temporary repositories exercise the read-only Changes contract."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from helios.backend import git_changes as changes


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Helios test")
    git(root, "config", "user.email", "helios@example.invalid")
    (root / "tracked.txt").write_text("before\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Initial")
    return root


def test_staged_unstaged_and_untracked_preserve_index_and_worktree(repo):
    path = repo / "tracked.txt"
    path.write_text("staged\n")
    git(repo, "add", "tracked.txt")
    path.write_text("unstaged\n")
    (repo / "new.txt").write_text("new content\n")
    index = (repo / ".git/index").read_bytes()
    snapshot = changes.read_changes(str(repo))
    rows = {row.path: row for row in snapshot.files}
    assert rows["tracked.txt"].status == "MM"
    assert rows["new.txt"].status == "??"
    diff = changes.read_diff(snapshot.root, rows["tracked.txt"])
    assert "Staged\n" in diff and "Unstaged\n" in diff
    assert "-before" in diff and "+staged" in diff and "+unstaged" in diff
    assert "new content" in changes.read_diff(snapshot.root, rows["new.txt"])
    assert (repo / ".git/index").read_bytes() == index
    assert path.read_text() == "unstaged\n"


def test_rename_paths_and_literal_pathspecs(repo):
    tricky = ":(glob)*.txt"
    (repo / tricky).write_text("specific before\n")
    git(repo, "--literal-pathspecs", "add", tricky)
    git(repo, "commit", "-qm", "Add literal filename")
    (repo / tricky).write_text("specific after\n")
    git(repo, "mv", "tracked.txt", "renamed \nfile.txt")
    snapshot = changes.read_changes(str(repo))
    rows = {row.path: row for row in snapshot.files}
    assert rows["renamed \nfile.txt"].original_path == "tracked.txt"
    diff = changes.read_diff(snapshot.root, rows[tricky])
    assert "+specific after" in diff
    assert "rename from" not in diff
    assert "rename from tracked.txt" in changes.read_diff(snapshot.root, rows["renamed \nfile.txt"])


def test_unborn_repo_subdirectory_and_ignored_files(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/new").write_text("hello")
    (tmp_path / ".gitignore").write_text("secret\n")
    (tmp_path / "secret").write_text("ignored")
    git(tmp_path, "add", "nested/new")
    snapshot = changes.read_changes(str(tmp_path / "nested"))
    assert snapshot.root == str(tmp_path)
    assert "secret" not in [row.path for row in snapshot.files]
    staged = next(row for row in snapshot.files if row.path == "nested/new")
    assert "+hello" in changes.read_diff(snapshot.root, staged)


def test_clean_and_non_git_are_distinct(repo, tmp_path):
    assert changes.read_changes(str(repo)).files == ()
    with pytest.raises(changes.ChangesError, match="not a git repository"):
        changes.read_changes(str(tmp_path))


def test_deleted_file_diff_and_conflict_status(repo):
    (repo / "tracked.txt").unlink()
    snapshot = changes.read_changes(str(repo))
    row = snapshot.files[0]
    assert row.status == " D"
    assert "deleted file mode" in changes.read_diff(snapshot.root, row)
    rows, _ = changes.parse_status(b"UU conflicted.txt\0AA added-both.txt\0")
    assert all(row.description == "Conflict" for row in rows)


def test_untracked_binary_large_and_symlink_previews(repo, tmp_path):
    (repo / "binary").write_bytes(b"binary\0content")
    assert "binary" in changes.read_diff(str(repo), changes.ChangedFile("binary", "??"))
    (repo / "large").write_bytes(b"a" * (changes.MAX_OUTPUT + 50))
    assert "truncated" in changes.read_diff(str(repo), changes.ChangedFile("large", "??"))
    outside = tmp_path / "outside"
    outside.write_text("private file contents")
    (repo / "link").symlink_to(outside)
    preview = changes.read_diff(str(repo), changes.ChangedFile("link", "??"))
    assert "Symbolic link" in preview and "private file contents" not in preview
    (repo / "directory-link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(changes.ChangesError):
        changes.read_diff(str(repo), changes.ChangedFile("directory-link/outside", "??"))
    with pytest.raises(changes.ChangesError, match="not inside"):
        changes.read_diff(str(repo), changes.ChangedFile("../outside", "??"))


def test_removed_and_special_files_return_clear_errors(repo):
    with pytest.raises(changes.ChangesError, match="moved or was removed"):
        changes.read_diff(str(repo), changes.ChangedFile("gone", "??"))
    os.mkfifo(repo / "fifo")
    with pytest.raises(changes.ChangesError, match="regular files"):
        changes.read_diff(str(repo), changes.ChangedFile("fifo", "??"))


def test_status_limit_and_partial_rename_are_explicit(monkeypatch):
    monkeypatch.setattr(changes, "MAX_FILES", 2)
    rows, truncated = changes.parse_status(b"?? one\0?? two\0?? three\0")
    assert [row.path for row in rows] == ["one", "two"] and truncated
    rows, truncated = changes.parse_status(b"R  destination\0source", truncated=True)
    assert rows == () and truncated
    rows, _ = changes.parse_status(b"?? ../outside\0?? /absolute\0?? safe\0")
    assert [row.path for row in rows] == ["safe"]


def test_git_env_cannot_redirect_inspection(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    assert changes.read_changes(str(repo)).root == str(repo)


def test_custom_diff_and_textconv_are_not_executed(repo):
    marker = repo / "helper-ran"
    helper = repo / "helper"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\necho bad\n")
    helper.chmod(0o755)
    git(repo, "config", "diff.external", str(helper))
    git(repo, "config", "diff.custom.textconv", str(helper))
    (repo / ".gitattributes").write_text("tracked.txt diff=custom\n")
    (repo / "tracked.txt").write_text("after\n")
    assert "+after" in changes.read_diff(str(repo), changes.ChangedFile("tracked.txt", " M"))
    assert not marker.exists()


def test_subprocess_output_and_time_are_bounded(tmp_path, monkeypatch):
    fake = tmp_path / "git"
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write('x' * 100000)\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    raw, truncated = changes._git(str(tmp_path), "status", limit=1024)
    assert len(raw) == 1024 and truncated
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    monkeypatch.setattr(changes, "GIT_TIMEOUT", 0.1)
    with pytest.raises(changes.ChangesError, match="timed out"):
        changes._git(str(tmp_path), "status")
