"""Instruction files must never redirect reads to an unselected secret path."""
from __future__ import annotations

import errno
import os

import pytest

from helios.backend.provider_instructions import InstructionContextError, compile_instructions


SECRET = "synthetic-private-key-outside-workspace"


@pytest.fixture
def layout(tmp_path):
    repo, global_dir, outside = (tmp_path / name for name in ("repository", "helios-state", "outside"))
    for folder in (repo, global_dir, outside):
        folder.mkdir()
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("repository guidance")
    (global_dir / "AGENTS.md").write_text("global guidance")
    for name in ("AGENTS.md", "AGENTS.override.md", "private-key"):
        (outside / name).write_text(SECRET)
    return repo, global_dir, outside


def source_for(layout, scope):
    repo, global_dir, _outside = layout
    if scope == "global":
        return global_dir / "AGENTS.md"
    if scope == "override":
        result = repo / "AGENTS.override.md"
        result.write_text("override guidance")
        return result
    return repo / "AGENTS.md"


def compile_layout(layout):
    repo, global_dir, _outside = layout
    return compile_instructions(str(repo), global_path=global_dir / "AGENTS.md")


@pytest.mark.parametrize("scope", ["repository", "global", "override"])
@pytest.mark.parametrize("dangling", [False, True])
def test_linked_source_is_rejected_even_if_optional_or_dangling(layout, scope, dangling):
    source = source_for(layout, scope)
    source.unlink()
    source.symlink_to(layout[2] / ("missing-private-key" if dangling else "private-key"))
    with pytest.raises(InstructionContextError, match="symlink"):
        compile_layout(layout)


def test_even_inside_workspace_instruction_symlinks_are_rejected(layout):
    repo, _global_dir, _outside = layout
    target = repo / "ordinary-policy.md"
    target.write_text("in-repository policy")
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(target)
    with pytest.raises(InstructionContextError, match="symlink"):
        compile_layout(layout)


@pytest.mark.parametrize("scope", ["repository", "global", "override"])
@pytest.mark.parametrize("phase", ["before-open", "after-open"])
def test_final_component_replacement_cannot_redirect_an_instruction_read(layout, monkeypatch, scope, phase):
    source = source_for(layout, scope)
    original = source.read_text()
    parent_identity = source.parent.stat().st_ino
    real_open = os.open
    replaced = False

    def race(path, flags, *args, **kwargs):
        nonlocal replaced
        directory_fd = kwargs.get("dir_fd")
        matching = (not replaced and path == source.name and directory_fd is not None
                    and os.fstat(directory_fd).st_ino == parent_identity)
        if matching:
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
            replaced = True
            if phase == "after-open":
                fd = real_open(path, flags, *args, **kwargs)
            source.unlink()
            source.symlink_to(layout[2] / "private-key")
            if phase == "after-open":
                return fd
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    if phase == "before-open":
        with pytest.raises(InstructionContextError, match="symlink"):
            compile_layout(layout)
    else:
        bundle = compile_layout(layout)
        assert original in bundle.text
        assert SECRET not in bundle.text
    assert replaced


@pytest.mark.parametrize("scope", ["repository", "global"])
@pytest.mark.parametrize("phase", ["before-open", "after-open"])
def test_ancestor_replacement_cannot_redirect_a_directory_relative_read(layout, monkeypatch, scope, phase):
    directory = layout[0 if scope == "repository" else 1]
    parked = directory.with_name(directory.name + "-original")
    real_open = os.open
    replaced = False

    def race(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and path == directory.name and kwargs.get("dir_fd") is not None:
            assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
            replaced = True
            if phase == "after-open":
                fd = real_open(path, flags, *args, **kwargs)
            directory.rename(parked)
            directory.symlink_to(layout[2], target_is_directory=True)
            if phase == "after-open":
                return fd
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    if phase == "before-open":
        with pytest.raises(InstructionContextError, match="symlink"):
            compile_layout(layout)
    else:
        bundle = compile_layout(layout)
        assert "repository guidance" in bundle.text and "global guidance" in bundle.text
        assert SECRET not in bundle.text
    assert replaced


def test_nested_ancestor_link_is_not_hidden_by_a_regular_final_directory(layout):
    repo, global_dir, outside = layout
    (outside / "src").mkdir()
    (outside / "src" / "AGENTS.md").write_text(SECRET)
    linked = repo / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstructionContextError, match="symlink"):
        compile_instructions(str(linked / "src"), global_path=global_dir / "AGENTS.md")


def test_default_global_directory_has_the_same_no_follow_policy(layout, monkeypatch):
    repo, _global_dir, outside = layout
    state = repo.parent / "linked-state"
    state.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HELIOS_STATE_DIR", str(state))
    with pytest.raises(InstructionContextError, match="symlink"):
        compile_instructions(str(repo))


@pytest.mark.parametrize("scope", ["repository", "global", "override"])
@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_special_instruction_sources_are_rejected_before_any_read(layout, monkeypatch, scope, kind):
    source = source_for(layout, scope)
    source.unlink()
    if kind == "fifo":
        os.mkfifo(source)
    else:
        source.mkdir()
    original_open = os.open
    checked = False

    def assert_nonblocking(path, flags, *args, **kwargs):
        nonlocal checked
        if path == source.name and kwargs.get("dir_fd") is not None:
            assert flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW
            checked = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", assert_nonblocking)
    with pytest.raises(InstructionContextError, match="regular file|unreadable"):
        compile_layout(layout)
    assert checked


def test_all_open_directory_and_file_descriptors_close_on_failure(layout, monkeypatch):
    (layout[0] / "AGENTS.md").write_bytes(b"\xff")
    original_open = os.open
    opened = []

    def record(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", record)
    with pytest.raises(InstructionContextError, match="unreadable"):
        compile_layout(layout)
    assert opened
    for fd in opened:
        with pytest.raises(OSError) as error:
            os.fstat(fd)
        assert error.value.errno == errno.EBADF


def test_missing_optional_sources_and_global_ancestors_are_harmless(tmp_path):
    repo = tmp_path / "repository"
    repo.mkdir()
    bundle = compile_instructions(str(repo), global_path=tmp_path / "absent" / "state" / "AGENTS.md")
    assert bundle.sources == () and bundle.text == ""


def test_empty_override_suppresses_unused_regular_or_linked_agents(layout):
    repo, global_dir, outside = layout
    (repo / "AGENTS.override.md").write_text("")
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(outside / "private-key")
    bundle = compile_instructions(str(repo), global_path=global_dir / "absent.md")
    assert len(bundle.sources) == 1
    assert bundle.sources[0]["path"] == str(repo / "AGENTS.override.md")
    assert bundle.sources[0]["bytes"] == 0
    assert SECRET not in bundle.text


def test_worktree_git_file_preserves_boundary_and_root_to_cwd_order(tmp_path):
    repo = tmp_path / "worktree"
    child = repo / "src"
    child.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("unrelated ancestor")
    (repo / ".git").write_text("gitdir: /outside/admin")
    (repo / "AGENTS.md").write_text("root rules")
    (child / "AGENTS.md").write_text("child rules")
    bundle = compile_instructions(str(child), global_path=tmp_path / "missing.md")
    assert "unrelated ancestor" not in bundle.text
    assert [row["path"] for row in bundle.sources] == [str(repo / "AGENTS.md"), str(child / "AGENTS.md")]


def test_same_file_is_not_read_or_charged_twice(layout):
    repo, _global_dir, _outside = layout
    size = (repo / "AGENTS.md").stat().st_size
    bundle = compile_instructions(str(repo), global_path=repo / "AGENTS.md", max_bytes=size)
    assert len(bundle.sources) == 1 and bundle.sources[0]["bytes"] == size


def test_parent_traversal_does_not_erase_a_symlink_before_validation(layout):
    repo, global_dir, outside = layout
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InstructionContextError, match="symlink"):
        compile_instructions(str(repo / "linked" / ".."), global_path=global_dir / "AGENTS.md")


def test_plain_parent_traversal_uses_the_pinned_parent(layout):
    repo, global_dir, _outside = layout
    (repo / "src").mkdir()
    bundle = compile_instructions(str(repo / "src" / ".."), global_path=global_dir / "AGENTS.md")
    assert bundle.sources[-1]["path"] == str(repo / "AGENTS.md")


def test_required_no_follow_support_is_not_silently_disabled(layout, monkeypatch):
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(InstructionContextError, match="cannot safely"):
        compile_layout(layout)
