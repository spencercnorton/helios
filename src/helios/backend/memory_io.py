"""Read / write Helios memory files (markdown with YAML-ish frontmatter).

Memory files live at `~/.claude/projects/<encoded-cwd>/memory/<name>.md` and
follow this shape:

    ---
    name: kebab-case-slug
    description: one-line summary used for cross-reference matching
    metadata:
      type: user | feedback | project | reference
    ---

    Body text. Standard Markdown.

The frontmatter is delimited by lines containing exactly `---`. Body is
everything after the closing `---`. This module:

  * parses both halves robustly (claude is generally well-behaved but we
    tolerate missing/empty frontmatter, trailing whitespace, etc.)
  * writes atomically (tmp + rename) so a save can't half-finish
  * keeps the last `BACKUP_RETAIN` versions in
    `~/.helios/backups/memory/<encoded-cwd>/<name>.<utc>.md`

`load_plain()`/`save_plain()` are the same machinery for CLAUDE.md and
MEMORY.md, which carry no frontmatter at all: the whole file is the body,
and a leading `---` line (a divider, not a header) must never be parsed as
one. Their backups live under `~/.helios/backups/context/` instead, so they
never share a bucket with a memory file's rotation.

No GTK imports — pure stdlib.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BACKUPS_ROOT = Path.home() / ".helios" / "backups" / "memory"
_CONTEXT_BACKUPS_ROOT = Path.home() / ".helios" / "backups" / "context"
BACKUP_RETAIN = 5  # keep this many historical versions per file


# ── Parse / serialize ────────────────────────────────────────────────────


@dataclass(slots=True)
class MemoryFile:
    """A parsed memory file: frontmatter as a dict + body string.

    Path is provided for context (writes refer back to it). For files that
    don't exist yet, `path` can be set after parse.

    `lossy` is True when the frontmatter contains constructs our simple
    parser can't represent (lists, deep nesting, block scalars, comments).
    For those files the original frontmatter text is kept verbatim in
    `raw_frontmatter` and `to_text()` re-emits it unchanged — editing the
    body can never silently destroy frontmatter we didn't understand.
    """

    path: Path
    name: str = ""
    description: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    # Mtime captured at read time so a save can detect mid-edit external
    # modification. 0.0 if never read from disk.
    read_mtime: float = 0.0
    # Verbatim text between the `---` delimiters as read from disk, and
    # whether it holds anything the dict round-trip would drop.
    raw_frontmatter: str = ""
    lossy: bool = False

    def to_text(self) -> str:
        """Re-serialize the file. Frontmatter first (if any), then body."""
        if self.lossy and self.raw_frontmatter:
            # Preserve not-fully-parsed frontmatter byte-for-byte; only the
            # body is editable for these files (the editor enforces it).
            head = "\n".join(["---", self.raw_frontmatter, "---", ""])
            body = self.body.rstrip("\n")
            return head + ("\n" + body if body else "") + "\n"
        if not self.frontmatter:
            return self.body.rstrip("\n") + "\n"
        lines = ["---"]
        lines.extend(_render_frontmatter(self.frontmatter))
        lines.append("---")
        lines.append("")
        body = self.body.rstrip("\n")
        if body:
            lines.append(body)
        return "\n".join(lines) + "\n"


_FRONTMATTER_DELIM = "---"


def _split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    """Split into (frontmatter_lines, body). frontmatter_lines is None when
    the text has no well-delimited frontmatter block."""
    if not text.startswith(_FRONTMATTER_DELIM):
        return None, text

    lines = text.split("\n")
    if lines[0].strip() != _FRONTMATTER_DELIM:
        return None, text

    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            close_idx = i
            break
    if close_idx == -1:
        # No closing delimiter — treat the whole file as body, no frontmatter.
        return None, text

    fm_lines = lines[1:close_idx]
    body_lines = lines[close_idx + 1 :]
    # Strip a single leading blank line if present.
    if body_lines and body_lines[0] == "":
        body_lines = body_lines[1:]
    return fm_lines, "\n".join(body_lines)


def parse_text(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown-with-frontmatter blob into (frontmatter, body).

    Returns `({}, text)` if the file has no frontmatter (i.e. doesn't begin
    with a `---` line). The frontmatter parser is intentionally simple:
    supports flat `key: value` pairs and one level of `metadata:` nesting
    with `  key: value` indented children. That covers everything claude
    writes; we don't pull in PyYAML. Anything beyond that slice is flagged
    by `_detect_lossy` and preserved verbatim through `MemoryFile.lossy`.
    """
    fm_lines, body = _split_frontmatter(text)
    if fm_lines is None:
        return {}, body
    return _parse_frontmatter(fm_lines), body


def _detect_lossy(fm_lines: list[str]) -> bool:
    """True when the frontmatter holds anything `_parse_frontmatter` would
    drop or misread on a dict round-trip: list items, nesting deeper than
    one level, block scalars (`key: |`), comments, or any line that isn't a
    plain `key: value` / one-level child. Mirrors the parser's acceptance
    exactly — keep the two in sync."""
    i = 0
    n = len(fm_lines)
    while i < n:
        line = fm_lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", line)
        if not m:
            return True  # comment, list item, stray indent at top level
        value = m.group(2)
        if value in ("|", ">", "|-", "|+", ">-", ">+"):
            return True  # block scalar — parser would read it as a string
        # (Indented continuation lines under a block scalar also fail the
        # top-level regex below on the next iteration, so multi-line values
        # are caught even without this explicit check.)
        if not value and i + 1 < n and fm_lines[i + 1].startswith("  "):
            i += 1
            while i < n and (fm_lines[i].startswith("  ") or not fm_lines[i].strip()):
                child = fm_lines[i]
                if child.strip():
                    cm = re.match(r"^\s+([A-Za-z_][\w-]*):\s*(.*?)\s*$", child)
                    if not cm:
                        return True  # list item under the nested block
                    if not cm.group(2):
                        return True  # nesting deeper than one level
                i += 1
            continue
        i += 1
    return False


def _parse_frontmatter(lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*$", line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2)
        # Nested block — empty value, then indented children.
        if not value and i + 1 < len(lines) and lines[i + 1].startswith("  "):
            nested: dict[str, Any] = {}
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                child = lines[i]
                if not child.strip():
                    i += 1
                    continue
                cm = re.match(r"^\s+([A-Za-z_][\w-]*):\s*(.*?)\s*$", child)
                if cm:
                    nested[cm.group(1)] = _coerce(cm.group(2))
                i += 1
            out[key] = nested
            continue
        out[key] = _coerce(value)
        i += 1
    return out


def _coerce(s: str) -> Any:
    """Best-effort YAML-ish scalar coercion for frontmatter values."""
    s = s.strip()
    if not s:
        return ""
    # Strip surrounding quotes.
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    lower = s.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _render_frontmatter(d: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(f"{k}:")
            for ck, cv in v.items():
                out.append(f"  {ck}: {_render_scalar(cv)}")
        else:
            out.append(f"{k}: {_render_scalar(v)}")
    return out


def _render_scalar(v: Any) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    if isinstance(v, str):
        # Quote if the value has structural chars or leading/trailing space.
        if not v:
            return '""'
        # Be a bit conservative; quote any value containing : # or starting
        # with one of YAML's special chars.
        if any(c in v for c in (":", "#", "@", "&", "*", "!", "|", ">", "'", '"')):
            escaped = v.replace('"', '\\"')
            return f'"{escaped}"'
        if v != v.strip():
            return f'"{v}"'
        return v
    return str(v)


# ── Read / write ─────────────────────────────────────────────────────────


def load(path: Path) -> MemoryFile:
    """Load and parse a memory file. Missing file → empty MemoryFile."""
    if not path.is_file():
        return MemoryFile(path=path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return MemoryFile(path=path)
    fm_lines, body = _split_frontmatter(text)
    fm = _parse_frontmatter(fm_lines) if fm_lines is not None else {}
    lossy = _detect_lossy(fm_lines) if fm_lines is not None else False
    name = ""
    description = ""
    if isinstance(fm.get("name"), str):
        name = fm["name"]
    if isinstance(fm.get("description"), str):
        description = fm["description"]
    return MemoryFile(
        path=path,
        name=name,
        description=description,
        frontmatter=fm,
        body=body,
        read_mtime=mtime,
        raw_frontmatter="\n".join(fm_lines) if fm_lines is not None else "",
        lossy=lossy,
    )


def load_plain(path: Path) -> MemoryFile:
    """Load a plain markdown file (CLAUDE.md, MEMORY.md) as body-only.

    Unlike load(), this never calls _split_frontmatter — even a body that
    happens to start with a `---` line (a divider, not a header) is kept
    exactly as read. These files carry no Helios-authored frontmatter, so a
    false-positive parse would silently hide the first lines of the file
    inside a "frontmatter" block the editor never shows.
    """
    if not path.is_file():
        return MemoryFile(path=path)
    try:
        raw = path.read_bytes()
        mtime = path.stat().st_mtime
    except OSError:
        return MemoryFile(path=path)
    # Byte-faithful: line endings are kept (no universal-newline translation)
    # and a file that is not valid UTF-8 is shown with replacement characters
    # but marked lossy, so save_plain() refuses to rewrite it — the editor
    # must never turn someone's CLAUDE.md into a different file.
    try:
        text = raw.decode("utf-8")
        lossy = False
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        lossy = True
    return MemoryFile(path=path, body=text, read_mtime=mtime, lossy=lossy)


def _atomic_write(mem: MemoryFile, text: str) -> None:
    path = mem.path
    # Write THROUGH a symlink, never over it: loading followed the link, and
    # rename-over-the-link would silently turn a dotfiles-managed CLAUDE.md
    # into a plain copy (GPT cross-check, 2026-09-03).
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    # A PREDICTABLE temp name is a symlink-clobbering vector: a project-level
    # CLAUDE.md.tmp can already exist in an untrusted checkout as a symlink to
    # another writable file, and opening it "w" would follow and truncate that
    # file before the rename. O_CREAT|O_EXCL on a unique
    # name can neither collide nor follow. Opened at 0o666 so the kernel
    # applies the real umask to a brand-new file: reading the umask back to do
    # that by hand meant calling os.umask twice, and the umask is
    # process-global under worker threads (a review finding — the same
    # class in openrouter_tools; fixed together).
    # The destination's mode is read BEFORE the temp file is created: a 0600
    # CLAUDE.md must not have its contents sit in a 0644 temp file even
    # briefly (a review finding, the same class as openrouter_tools).
    try:
        existing: int | None = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        existing = None
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        str(tmp),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600 if existing is not None else 0o666,
    )
    try:
        # newline="" writes the text as-is (a CRLF body stays CRLF); fsync
        # before the rename so a crash cannot leave the rename pointing at
        # unflushed data.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Carry the existing file's permissions across the replace. Without
        # this a 0600 CLAUDE.md silently became 0644. A brand-new file keeps
        # the umask-derived mode the kernel already gave the temp file — what
        # an ordinary create would have produced, not a tighter one.
        if existing is not None:
            os.chmod(tmp, existing)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    # Refresh read_mtime so subsequent external-change checks work.
    try:
        mem.read_mtime = path.stat().st_mtime
    except OSError:
        pass


def save(mem: MemoryFile, *, backup: bool = True) -> None:
    """Atomically write a memory file. By default rotates a backup first."""
    if backup and mem.path.is_file():
        _write_backup(mem.path, _backup_dir(mem.path))
    _atomic_write(mem, mem.to_text())


def save_plain(mem: MemoryFile, *, backup: bool = True) -> None:
    """Atomically write a plain context file (CLAUDE.md, MEMORY.md).

    Same tmp+rename/mtime-refresh machinery as save() — only the backup
    bucket differs, so a project's CLAUDE.md backups never land next to a
    memory file's rotation (see _context_backup_dir).
    """
    if mem.lossy:
        raise ValueError(
            f"{mem.path.name} is not valid UTF-8; Helios will not rewrite it. "
            "Edit it with another tool."
        )
    if backup and mem.path.is_file():
        _write_backup(mem.path, _context_backup_dir(mem.path))
    # Verbatim: no trailing-newline normalisation for a file Helios does not own.
    _atomic_write(mem, mem.body)


def has_external_changes(mem: MemoryFile) -> bool:
    """Return True if the file's on-disk mtime moved past what we read.

    Used to warn the user before clobbering an edit made via another tool.
    Returns False if the file doesn't exist (e.g. brand-new memory).
    """
    if mem.read_mtime == 0.0:
        return False
    try:
        current = mem.path.stat().st_mtime
    except OSError:
        return False
    # Tolerance: filesystem mtime granularity is ~1ms on ext4.
    return current > mem.read_mtime + 0.001


# ── Backup rotation ──────────────────────────────────────────────────────


def _backup_dir(memory_path: Path) -> Path:
    """Backups go to ~/.helios/backups/memory/<encoded-grandparent>/<file>/.

    The encoding handles per-project memory dirs and the global memory
    dir cleanly — we use the encoded cwd dirname as the bucket, falling
    back to "global" for paths outside a project structure.
    """
    parts = memory_path.parts
    # Find ".claude/projects/<encoded>/memory" in the path.
    try:
        i = parts.index("projects")
        bucket = parts[i + 1] if i + 1 < len(parts) else "global"
    except ValueError:
        bucket = "global"
    return _BACKUPS_ROOT / bucket / memory_path.stem


def _context_backup_dir(path: Path) -> Path:
    """Backups go to ~/.helios/backups/context/<encoded-parent>/<stem>/.

    A plain context file's *parent* is what varies (the global CLAUDE.md,
    every project's CLAUDE.md, and every project's MEMORY.md all share a
    stem of "CLAUDE" or "MEMORY"), so — unlike memory files, which share one
    per-project directory and bucket on the encoded project dirname —
    bucket on the full encoded parent path instead. That keeps every one of
    them collision-free without needing project.py's own encoding helper.
    """
    # Injective on purpose: "foo/bar" and "foo-bar" encoded with "/"->"-" would
    # share a bucket and rotate each other's history away. Name + digest keeps
    # it readable and collision-free.
    parent = path.parent.resolve()
    digest = hashlib.sha1(str(parent).encode("utf-8", "surrogateescape")).hexdigest()[:12]
    bucket = f"{parent.name or 'root'}-{digest}"
    return _CONTEXT_BACKUPS_ROOT / bucket / path.stem


def _write_backup(path: Path, bdir: Path) -> None:
    """Copy `path` into `bdir` with a UTC timestamp, then rotate."""
    bdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(bdir.parent.parent, 0o700)
    except OSError:
        pass
    # Microsecond precision — saves within a single second would otherwise
    # collide on the timestamp and silently overwrite each other (caught
    # in the unit tests on 2026-05-29).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = bdir / f"{stamp}{path.suffix}"
    try:
        shutil.copy2(path, backup_path)
    except OSError:
        return
    # Keep only the most recent BACKUP_RETAIN entries.
    existing = sorted(bdir.glob(f"*{path.suffix}"), reverse=True)
    for stale in existing[BACKUP_RETAIN:]:
        try:
            stale.unlink()
        except OSError:
            pass


def list_backups(memory_path: Path, *, plain: bool = False) -> list[Path]:
    """Return the available backups for a file, newest first.

    `plain=True` looks under backups/context/ instead of backups/memory/ —
    pass it for CLAUDE.md / MEMORY.md, matching save_plain().
    """
    bdir = _context_backup_dir(memory_path) if plain else _backup_dir(memory_path)
    if not bdir.is_dir():
        return []
    return sorted(bdir.glob(f"*{memory_path.suffix}"), reverse=True)
