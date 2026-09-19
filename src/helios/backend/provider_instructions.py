"""Bounded, provider-owned instruction loading for the in-process agent.

Claude/Codex retain their native loaders. OpenRouter loads its own global file
and AGENTS hierarchy; it never borrows CLAUDE.md or optional Claude memory.
"""
from __future__ import annotations

import hashlib
import os
import stat
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from helios.paths import state_dir

MAX_INSTRUCTION_BYTES = 32 * 1024


class InstructionContextError(ValueError):
    pass


@dataclass(frozen=True)
class InstructionBundle:
    text: str
    sources: tuple[dict, ...]


def _directory_chain(path: Path, stack: ExitStack, *, optional: bool = False) -> list[tuple[Path, int]] | None:
    """Pin each directory without following any path component's symlink.

    O_NOFOLLOW protects only the final component of an open. Walking one name
    at a time relative to the previous descriptor protects ancestors too, and
    keeps later source opens in those same directories if names are replaced.
    Do not replace this with resolve()/lstat() followed by an absolute open.
    """
    path = path.absolute()  # Prefix cwd only; do not resolve links or collapse ..
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(path.anchor, flags)
        stack.callback(os.close, fd)
        chain = [(Path(path.anchor), fd)]
        for name in path.parts[1:]:
            if name == "..":
                # Earlier components were already opened without following
                # links. Reuse the pinned parent, not a new pathname lookup.
                if len(chain) > 1:
                    chain.pop()
                continue
            parent, parent_fd = chain[-1]
            fd = os.open(name, flags, dir_fd=parent_fd)
            stack.callback(os.close, fd)
            chain.append((parent / name, fd))
        return chain
    except FileNotFoundError as error:
        if optional:
            return None
        raise InstructionContextError(f"Instruction directory is missing: {path}") from error
    except OSError as error:
        raise InstructionContextError(
            f"Instruction directory is unreadable or contains a symlink: {path}"
        ) from error


def compile_instructions(cwd: str, *, provider: str = "openrouter",
                         global_path: Path | None = None,
                         max_bytes: int = MAX_INSTRUCTION_BYTES) -> InstructionBundle:
    if provider != "openrouter":
        raise InstructionContextError("This provider owns its native instruction loader")
    if max_bytes < 1 or max_bytes > MAX_INSTRUCTION_BYTES:
        raise InstructionContextError("Invalid instruction byte budget")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise InstructionContextError("This platform cannot safely open instruction sources")
    chunks, sources, seen = [], [], set()
    remaining = max_bytes

    def load(folder: Path, directory_fd: int, name: str) -> bool:
        """Return presence separately from loading, preserving empty overrides."""
        nonlocal remaining
        candidate = folder / name
        try:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=directory_fd)
            except FileNotFoundError:
                return False
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise InstructionContextError(f"Instruction source is not a regular file: {candidate}")
                identity = (info.st_dev, info.st_ino)
                if identity in seen:
                    return True
                data = stream.read(remaining + 1)
            if len(data) > remaining:
                raise InstructionContextError(f"Required AGENTS instructions exceed {max_bytes:,} bytes; shorten the source before sending")
            text = data.decode("utf-8")
        except (OSError, UnicodeError, RuntimeError) as error:
            raise InstructionContextError(
                f"Required instruction source is unreadable or symlinked: {candidate}"
            ) from error
        remaining -= len(data)
        seen.add(identity)
        digest = hashlib.sha256(data).hexdigest()
        sources.append({"path": str(candidate), "resolved_path": str(candidate), "sha256": digest,
                        "bytes": len(data), "mtime_ns": info.st_mtime_ns, "loaded_at": time.time(),
                        "provider": provider, "precedence": len(sources)})
        chunks.append(f"Instruction source: {candidate}\nSHA-256: {digest}\n{text}")
        return True

    with ExitStack() as stack:
        directories = _directory_chain(Path(cwd), stack)
        # Nearest git root, including .git files in worktrees. Inspect only
        # marker presence in pinned directories; never read/follow its target.
        # Outside a repo only cwd is eligible, not unrelated ancestor policy.
        root_index = len(directories) - 1
        for index in range(len(directories) - 1, -1, -1):
            try:
                os.stat(".git", dir_fd=directories[index][1], follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise InstructionContextError("Could not establish the instruction workspace boundary") from error
            root_index = index
            break
        global_file = Path(global_path or state_dir() / "AGENTS.md")
        global_directories = _directory_chain(global_file.parent, stack, optional=True)
        if global_directories is not None:
            load(*global_directories[-1], global_file.name)
        for folder, directory_fd in directories[root_index:]:
            if not load(folder, directory_fd, "AGENTS.override.md"):
                load(folder, directory_fd, "AGENTS.md")

    preface = ("OpenRouter native instructions, ordered global to repository to working directory. "
               "More specific files refine broader guidance. These instructions do not widen "
               "the user's requested scope or Helios tool permissions.\n\n")
    return InstructionBundle(preface + "\n\n".join(chunks) if chunks else "", tuple(sources))
