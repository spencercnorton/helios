"""Bounded, read-only worktree inspection. Call only from a worker thread.

These are current repository changes, including edits made outside Helios;
they are not an attribution of changes to the selected conversation.
"""

from __future__ import annotations

import os
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_FILES = 250
MAX_OUTPUT = 256 * 1024
GIT_TIMEOUT = 8.0


class ChangesError(Exception):
    """Inspection failed without changing the repository."""


def display_text(text: str) -> str:
    """Git filenames may contain undecodable bytes or control characters."""
    return text.encode("utf-8", "replace").decode("utf-8").replace("\x00", "�")


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    original_path: str = ""

    @property
    def label(self) -> str:
        path = display_text(self.path).replace("\n", " ↵ ").replace("\r", " ")
        return path

    @property
    def description(self) -> str:
        if self.status == "??":
            return "Untracked"
        if "U" in self.status or self.status in {"AA", "DD"}:
            return "Conflict"
        parts = []
        if self.status[0] != " ":
            parts.append("Staged")
        if self.status[1] != " ":
            parts.append("Unstaged")
        return " · ".join(parts)


@dataclass(frozen=True)
class ChangesSnapshot:
    root: str
    files: tuple[ChangedFile, ...]
    truncated: bool = False


def _git(cwd: str, *args: str, limit: int = MAX_OUTPUT) -> tuple[bytes, bool]:
    # Do not inherit another repository/index, run a pager, invoke diff/text
    # conversion helpers, or refresh the real index during this inspection.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    command = [
        "git", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false",
        "-c", "color.ui=false", *args,
    ]
    try:
        proc = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise ChangesError(f"Cannot inspect this folder: {exc}") from exc
    output, errors = bytearray(), bytearray()
    truncated = False
    deadline = time.monotonic() + GIT_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, output)
            selector.register(proc.stderr, selectors.EVENT_READ, errors)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ChangesError("Git inspection timed out. Try refreshing again.")
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 16 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = key.data
                    cap = limit if target is output else 4096
                    target.extend(chunk[:max(0, cap - len(target))])
                    if len(target) >= cap:
                        truncated = True
                        proc.kill()
                        break
                if truncated:
                    break
        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        if truncated and len(errors) >= 4096:
            raise ChangesError(errors.decode("utf-8", "replace").strip()[:500])
        if proc.returncode and not truncated:
            message = errors.decode("utf-8", "replace").strip()
            raise ChangesError(message[:500] or "Git could not inspect this folder.")
        return bytes(output), truncated
    except subprocess.TimeoutExpired as exc:
        raise ChangesError("Git inspection timed out. Try refreshing again.") from exc
    finally:
        # Git may have spawned another Git process for a submodule. A timed
        # out/capped inspection must not leave descendants holding pipes open.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.poll() is None:
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def _safe_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    return bool(path) and not parsed.is_absolute() and ".." not in parsed.parts


def parse_status(raw: bytes, *, truncated: bool = False) -> tuple[tuple[ChangedFile, ...], bool]:
    """Parse porcelain v1 -z, where a rename is destination NUL source NUL."""
    records = raw.split(b"\0")
    # Only complete records: a size-limited final filename must not be used.
    records.pop()
    files = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" ":
            continue
        status = record[:2].decode("ascii", "replace")
        path = os.fsdecode(record[3:])
        original = ""
        if "R" in status or "C" in status:
            if index >= len(records):
                truncated = True
                break
            original = os.fsdecode(records[index])
            index += 1
        if not _safe_path(path) or (original and not _safe_path(original)):
            continue
        if len(files) >= MAX_FILES:
            truncated = True
            break
        files.append(ChangedFile(path, status, original))
    return tuple(files), truncated


def read_changes(cwd: str) -> ChangesSnapshot:
    root_raw, truncated = _git(cwd, "rev-parse", "--show-toplevel", limit=16384)
    if truncated:
        raise ChangesError("Repository path exceeds the inspection limit.")
    root = os.fsdecode(root_raw.removesuffix(b"\n"))
    raw, truncated = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    files, truncated = parse_status(raw, truncated=truncated)
    return ChangesSnapshot(root, files, truncated)


def _new_file(root: str, path: str) -> tuple[bytes, bool]:
    # Resolve each path component without following symlinks. A new symlink
    # should never make the preview read a file outside the selected worktree.
    parts = PurePosixPath(path).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return os.fsencode("Symbolic link → " + os.readlink(parts[-1], dir_fd=fd)), False
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ChangesError("Preview is available only for regular files and symbolic links.")
            with os.fdopen(file_fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_OUTPUT + 1)
            return data[:MAX_OUTPUT], len(data) > MAX_OUTPUT
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)


def read_diff(root: str, change: ChangedFile) -> str:
    if not _safe_path(change.path) or (change.original_path and not _safe_path(change.original_path)):
        raise ChangesError("The selected path is not inside the repository.")
    try:
        if change.status == "??":
            raw, truncated = _new_file(root, change.path)
            if b"\0" in raw:
                return "Untracked binary file — no text preview."
            text = "Untracked file\n\n" + raw.decode("utf-8", "replace")
        else:
            paths = [change.path]
            if change.original_path:
                paths.append(change.original_path)
            sections = []
            truncated = False
            for title, options in (("Staged", ["--cached"]), ("Unstaged", [])):
                raw, clipped = _git(
                    root, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
                    *options, "--", *paths,
                )
                truncated |= clipped
                if raw:
                    sections.append(title + "\n\n" + raw.decode("utf-8", "replace"))
            text = "\n\n".join(sections) or "No text diff remains. Refresh to update the file list."
        if truncated:
            text += "\n\n… Preview truncated. Inspect this file in your editor for the full change."
        return display_text(text)
    except OSError as exc:
        raise ChangesError(f"Cannot read this file. Refresh if it moved or was removed: {exc}") from exc
