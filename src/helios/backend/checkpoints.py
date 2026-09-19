"""Undo for agent edits — snapshot the worktree before each turn.

Claude Code checkpoints every change and rewinds with `/rewind`; Cursor
restores per message. Helios shipped Bypass in the permission list with no
undo behind it at all, so an agent edit was final and `git` was the only
recovery — assuming the work happened to be committed.

**How the snapshot is taken.** Not by copying files: by writing a git tree
through a *throwaway index file*.

    GIT_INDEX_FILE=<tmp> git read-tree HEAD    # seed from the current commit
    GIT_INDEX_FILE=<tmp> git add -A            # stage the worktree as it is
    GIT_INDEX_FILE=<tmp> git write-tree        # -> tree sha

The user's real `.git/index` is never read or written, nothing is stashed,
nothing is committed, and no branch or ref moves. The resulting objects are
dangling until git's own gc reaps them, which is exactly the lifetime we
want. Restoring is `git restore --source=<tree> --worktree`, which touches
the worktree only and leaves the index alone.

**What it deliberately does not cover.** `git add -A` honours `.gitignore`,
so ignored files are not captured — which is the right call (nobody wants
`node_modules` in an undo buffer) but means an agent edit to an ignored file
(a `.env`, a build artifact) cannot be rewound. `checkpoint_gaps` reports
this so the UI can say so rather than implying total coverage.

Sessions whose cwd is not inside a git worktree get no checkpoints at all.
That is a real limitation, not a silent one: `available()` is false and the
UI says why.

GTK-free; every call shells out to git and belongs on a worker thread.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from helios.log import get_logger
from helios.paths import state_dir

_log = get_logger("checkpoints")

# Per session. Old entries fall off the end; the git objects they referenced
# become unreachable and gc away on git's normal schedule.
MAX_PER_SESSION = 50

# Cap on the recorded ignored-path list. With `--directory` a normal repo
# produces a handful of entries; the cap only bites on pathological trees,
# where `_predates` remains as the backstop.
MAX_IGNORED_RECORDED = 5000

_GIT_TIMEOUT = 30.0


def _store_path() -> Path:
    return state_dir() / "checkpoints.json"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One pre-turn snapshot of a session's working tree."""

    tree: str          # git tree sha — the snapshot itself
    created_at: int    # unix seconds
    label: str         # the message that was about to be sent, truncated
    cwd: str
    head: str = ""     # HEAD at capture time, for context in the UI
    # Paths git ignored AT CAPTURE TIME, so absent from `tree`. Entries ending
    # in "/" are whole directories. Recorded because ignore rules are not
    # stable: the agent can edit .gitignore, at which point a file that
    # predates the checkpoint starts looking newly created. Load-bearing for
    # `restore` — see `covers`.
    ignored: tuple[str, ...] = ()
    # True when `ignored` hit MAX_IGNORED_RECORDED and is therefore NOT a
    # complete picture of what the snapshot failed to capture. An incomplete
    # list must never be treated as authoritative — see `may_delete`.
    ignored_truncated: bool = False

    def as_dict(self) -> dict:
        return {
            "tree": self.tree,
            "created_at": self.created_at,
            "label": self.label,
            "cwd": self.cwd,
            "head": self.head,
            "ignored": list(self.ignored),
            "ignored_truncated": self.ignored_truncated,
        }

    @property
    def may_delete(self) -> bool:
        """Whether this checkpoint knows enough to classify a path as new.

        False when the ignored listing was truncated: any path missing from
        the tree might be a pre-existing ignored file that simply did not fit
        in the record, and `_predates` cannot tell — modifying a file updates
        its mtime. With incomplete coverage the honest answer is to remove
        nothing and say so.
        """
        return not self.ignored_truncated

    def covers(self, path: str) -> bool:
        """False when ``path`` was git-ignored when this snapshot was taken.

        Such a path is not in the tree, so the checkpoint can neither restore
        nor meaningfully delete it — it is outside the snapshot's coverage and
        must be left strictly alone.
        """
        for entry in self.ignored:
            if entry.endswith("/"):
                if path == entry.rstrip("/") or path.startswith(entry):
                    return False
            elif path == entry:
                return False
        return True

    @staticmethod
    def from_dict(raw: object) -> "Checkpoint | None":
        if not isinstance(raw, dict):
            return None
        tree = str(raw.get("tree") or "")
        cwd = str(raw.get("cwd") or "")
        if not tree or not cwd:
            return None
        try:
            created_at = int(raw.get("created_at") or 0)
        except (TypeError, ValueError):
            return None
        raw_ignored = raw.get("ignored")
        ignored = (
            tuple(str(x) for x in raw_ignored if isinstance(x, str))
            if isinstance(raw_ignored, list)
            else ()
        )
        return Checkpoint(
            tree=tree,
            created_at=created_at,
            label=str(raw.get("label") or ""),
            cwd=cwd,
            head=str(raw.get("head") or ""),
            ignored=ignored,
            ignored_truncated=bool(raw.get("ignored_truncated")),
        )


@dataclass(frozen=True, slots=True)
class Change:
    """One path that differs between a checkpoint and the worktree now."""

    status: str  # "modified" | "added" | "deleted"
    path: str

    @property
    def restores_to(self) -> str:
        """What restoring this checkpoint would do to the path, in words."""
        return {
            "modified": "revert",
            "added": "delete",
            "deleted": "recreate",
        }.get(self.status, "restore")


@dataclass
class RestoreResult:
    restored: list[str] = field(default_factory=list)
    # Paths moved OUT of the worktree into `trash_dir` — never unlinked. See
    # `_trash` for why this is a move rather than a delete.
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # Deletions refused outright, because the checkpoint did not cover the
    # path or it predates the checkpoint. Reported, never silent.
    kept: list[str] = field(default_factory=list)
    # Where `removed` files went, so the UI can point at it.
    trash_dir: str = ""


class GitError(RuntimeError):
    """A git invocation failed. Message is git's stderr, trimmed."""


def _git(cwd: str | Path, *args: str, env_extra: dict | None = None) -> str:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            # --literal-pathspecs everywhere: a repository may legally contain
            # a tracked file named `:(glob)**`, and passing that as a pathspec
            # — even after `--` — makes git match everything it globs, so
            # restoring one ticked file could overwrite files the user
            # explicitly unticked. Applied at the single call site rather than
            # per-command so a future one cannot forget it.
            ["git", "--literal-pathspecs", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(str(e)) from e
    if proc.returncode != 0:
        raise GitError((proc.stderr or "git failed").strip()[:500])
    return proc.stdout


def repo_root(cwd: str | Path) -> Path | None:
    """The git worktree containing ``cwd``, or None."""
    try:
        out = _git(cwd, "rev-parse", "--show-toplevel").strip()
    except GitError:
        return None
    return Path(out) if out else None


def available(cwd: str | Path) -> bool:
    return repo_root(cwd) is not None


def snapshot(cwd: str | Path, label: str = "") -> Checkpoint | None:
    """Capture the worktree as a git tree. None when cwd is not a repo.

    Never mutates the user's index, refs, or worktree.
    """
    root = repo_root(cwd)
    if root is None:
        return None
    handle, index_path = tempfile.mkstemp(prefix="helios-ckpt-index-")
    os.close(handle)
    # read-tree/add want to CREATE this file; an existing empty one is not a
    # valid index and git rejects it.
    os.unlink(index_path)
    env = {"GIT_INDEX_FILE": index_path}
    try:
        try:
            head = _git(root, "rev-parse", "HEAD").strip()
        except GitError:
            head = ""  # unborn branch: a repo with no commits yet is fine
        if head:
            _git(root, "read-tree", head, env_extra=env)
        _git(root, "add", "-A", env_extra=env)
        tree = _git(root, "write-tree", env_extra=env).strip()
    except GitError as e:
        _log.warning("checkpoint snapshot failed in %s: %s", root, e)
        return None
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass
    if not tree:
        return None
    gaps = checkpoint_gaps(root)
    return Checkpoint(
        tree=tree,
        created_at=int(time.time()),
        label=_short_label(label),
        cwd=str(root),
        head=head,
        ignored=tuple(gaps[:MAX_IGNORED_RECORDED]),
        ignored_truncated=len(gaps) > MAX_IGNORED_RECORDED,
    )


def changes_since(checkpoint: Checkpoint) -> list[Change]:
    """What restoring this checkpoint would change, relative to now.

    Compares the checkpoint tree against a *freshly taken* tree of the current
    worktree, rather than using `git diff <tree>`. That matters: `git diff`
    only considers paths git already tracks, so a file the agent created would
    not appear at all — the single most important thing to be able to undo.
    Tree-to-tree is symmetric and sees it.
    """
    now = snapshot(checkpoint.cwd)
    if now is None:
        return []
    if now.tree == checkpoint.tree:
        return []
    try:
        out = _git(
            checkpoint.cwd,
            "diff-tree",
            "-r",
            "-M",
            "--name-status",
            "-z",
            checkpoint.tree,
            now.tree,
        )
    except GitError as e:
        _log.warning("checkpoint diff failed: %s", e)
        return []
    # Paths the checkpoint never covered (git-ignored when it was taken) are
    # neither revertible nor safe to delete. Offering them in the dialog would
    # be offering a lie — and one the user could tick.
    return [c for c in _parse_name_status(out) if checkpoint.covers(c.path)]


def checkpoint_gaps(cwd: str | Path) -> list[str]:
    """Ignored-but-present paths a checkpoint cannot capture or restore.

    Surfaced so the UI can name the limitation instead of implying that a
    rewind returns the directory to exactly its former state.
    """
    try:
        out = _git(
            cwd,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",  # collapse node_modules/ to one entry, not 40k
            "-z",
        )
    except GitError:
        return []
    return [p for p in out.split("\0") if p]


def restore(checkpoint: Checkpoint, paths: list[str]) -> RestoreResult:
    """Put ``paths`` back the way the checkpoint has them.

    Paths added since the checkpoint are removed (the tree has no blob to
    restore); everything else is written from the tree. Only the worktree is
    touched — the index and HEAD are left exactly as they were.
    """
    result = RestoreResult()
    if not paths:
        return result
    # Anything the checkpoint never covered is refused up front, before it can
    # be handed to `git restore` (which would fail: the tree has no such blob)
    # or to unlink (which would destroy it). `changes_since` already filters
    # these out, so reaching here means a caller asked explicitly.
    wanted = {p for p in paths if checkpoint.covers(p)}
    result.kept.extend(sorted(set(paths) - wanted))
    if not wanted:
        return result
    by_status = {c.path: c.status for c in changes_since(checkpoint)}
    to_delete = sorted(p for p in wanted if by_status.get(p) == "added")
    to_write = sorted(p for p in wanted if by_status.get(p) != "added")

    if to_write:
        try:
            _git(
                checkpoint.cwd,
                "restore",
                "--source",
                checkpoint.tree,
                "--worktree",
                "--",
                *to_write,
            )
            result.restored.extend(to_write)
        except GitError as e:
            _log.warning("checkpoint restore failed: %s", e)
            result.failed.extend(to_write)

    trash_root = _trash_root(checkpoint)
    for path in to_delete:
        target = Path(checkpoint.cwd) / path
        if (
            not checkpoint.may_delete
            or not checkpoint.covers(path)
            or _predates(target, checkpoint.created_at)
        ):
            # Three independent reasons to refuse, because none alone suffices.
            #
            # `may_delete` — the ignored listing was truncated, so `covers`
            # cannot be trusted for anything missing from the tree.
            #
            # `covers` — the path was git-ignored when the snapshot was taken,
            # so it is absent from the tree. If the agent later edits
            # .gitignore, the file starts looking newly created and would be
            # reaped. mtime alone does NOT catch this: an ignored .env that
            # the agent also *modified* carries a post-checkpoint mtime.
            #
            # `_predates` — a backstop for anything the recorded list missed
            # (it is capped), and for files that were simply already there.
            #
            # Either way: refuse, and say so.
            result.kept.append(path)
            continue
        try:
            _trash(target, trash_root / path)
            result.removed.append(path)
            result.trash_dir = str(trash_root)
        except OSError as e:
            _log.warning("could not move %s aside: %s", target, e)
            result.failed.append(path)
    return result


def _trash_root(checkpoint: Checkpoint) -> Path:
    """A fresh recovery directory for one restore operation."""
    stamp = f"{checkpoint.tree[:12]}-{int(time.time())}"
    return state_dir() / "checkpoint-trash" / stamp


def _trash(source: Path, destination: Path) -> None:
    """Move a file out of the worktree instead of deleting it.

    Every guard in this module is a *classification*: is this path one the
    agent created, or one that was already here? Classification can be wrong —
    a renamed ignored file, an ignored path beyond the recorded cap, a clock
    skew — and when it is wrong the old behaviour destroyed a file that
    predated the turn, unrecoverably, because ignored files are not in the
    snapshot tree and cannot be restored from it.

    So the destructive step is not destructive: undoing a turn moves files
    aside. A wrong guess costs the user a `mv` instead of their data.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    # shutil.move, not os.replace: ~/.helios and the repository are frequently
    # on different filesystems, where rename(2) fails with EXDEV.
    shutil.move(str(source), str(destination))


def _predates(path: Path, created_at: int) -> bool:
    """True when ``path`` looks older than the checkpoint.

    ``created_at`` is whole seconds, so a file written at 10:00:00.400 and a
    checkpoint stamped 10:00:00 compare as "newer" even though the file came
    first. The one-second grace absorbs that truncation: within the same
    second we cannot tell the order, so we assume the file was already there.

    Errs toward True throughout — an unreadable stat also means "do not
    delete". Wrong in the safe direction: the cost is a leftover file, not a
    lost one.
    """
    try:
        return path.stat().st_mtime < created_at + 1
    except OSError:
        return True


# ── persistence ────────────────────────────────────────────────────────────


def load(session_id: str) -> list[Checkpoint]:
    """Checkpoints for one session, newest first."""
    if not session_id:
        return []
    rows = _read_all().get(session_id)
    if not isinstance(rows, list):
        return []
    out = [c for c in (Checkpoint.from_dict(r) for r in rows) if c is not None]
    # File order is oldest-first (record appends). Reverse before the stable
    # sort so two checkpoints taken in the same second — easy when a turn is
    # short — still come back newest-first rather than in write order.
    out.reverse()
    out.sort(key=lambda c: c.created_at, reverse=True)
    return out


def record(session_id: str, checkpoint: Checkpoint) -> None:
    """Append a checkpoint, keeping only the most recent MAX_PER_SESSION."""
    if not session_id or checkpoint is None:
        return
    data = _read_all()
    rows = data.get(session_id)
    rows = list(rows) if isinstance(rows, list) else []
    rows.append(checkpoint.as_dict())
    # Trim from the front: oldest first in file order, newest kept.
    data[session_id] = rows[-MAX_PER_SESSION:]
    _write_all(data)


def forget(session_id: str) -> None:
    data = _read_all()
    if data.pop(session_id, None) is not None:
        _write_all(data)


def _read_all() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(data: dict) -> None:
    path = _store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as e:
        _log.warning("could not save checkpoints: %s", e)


# ── helpers ────────────────────────────────────────────────────────────────

_STATUS_WORDS = {"M": "modified", "A": "added", "D": "deleted"}


def _parse_name_status(raw: str) -> list[Change]:
    """Parse `git diff --name-status -z` output.

    NUL-separated and *alternating*: status, path, status, path… Renames carry
    two paths, which we split into a delete plus an add so every entry names
    exactly one file the restore will touch.
    """
    fields = [f for f in raw.split("\0") if f]
    out: list[Change] = []
    i = 0
    while i < len(fields):
        code = fields[i]
        letter = code[:1]
        if letter in ("R", "C") and i + 2 < len(fields):
            old, new = fields[i + 1], fields[i + 2]
            out.append(Change("deleted", old))
            out.append(Change("added", new))
            i += 3
            continue
        if i + 1 >= len(fields):
            break
        path = fields[i + 1]
        out.append(Change(_STATUS_WORDS.get(letter, "modified"), path))
        i += 2
    out.sort(key=lambda c: c.path)
    return out


def _short_label(text: str, limit: int = 90) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
