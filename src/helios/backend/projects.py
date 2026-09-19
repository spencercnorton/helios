"""Project + session discovery from ~/.claude/projects/.

Claude Code stores one directory per working-directory it has been invoked from.
The directory name is the absolute cwd with '/' replaced by '-'.

    /home/alice             -> -home-alice
    /srv/icloud/kleos       -> -srv-icloud-kleos

Inside each project dir lives one JSONL file per session (<uuid>.jsonl) plus
sometimes a directory with the same uuid for auxiliary state, and a `memory/`
subdir we read elsewhere.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from helios.backend.transcript import (
    session_has_content,
    session_is_descendant_only,
    strip_injected_context,
)


CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_HOME / "projects"

# Where archive_stale_throwaways() moves dead throwaway projects. A plain
# `mv` back into projects/ restores one — nothing is ever deleted.
ARCHIVE_DIR = CLAUDE_HOME / "projects-archive"

# A throwaway project with no activity for this long is presumed dead.
STALE_THROWAWAY_AGE_S = 14 * 86400

# How old a no-content sidecar may be before the display filter hides it.
# Kept short: the real guard against hiding an active chat is its live-driver
# id (see drop_empty_sessions); this grace only covers the sub-second window
# before a freshly-spawned driver registers that id. (Was 60s, which let ghost
# rows flash into the sidebar for a full minute on start/resume.)
_SIDECAR_DISPLAY_GRACE_S = 15

# A no-content sidecar younger than this is left on disk by the startup sweep
# (sweep_orphan_sidecar_sessions) so a session mid-first-turn isn't lost.
_SIDECAR_SWEEP_GRACE_S = 10 * 60

# Shared cross-machine session pool on iCloud. Each host pushes its own
# ~/.claude/projects/ into a subdir named after the host (workstation/, laptop/...).
# We surface OTHER hosts' subdirs as read-only "remote" projects so Helios can
# browse the MacBook's sessions (and vice-versa). See
# /srv/icloud/claude-kleos-mac/claude-session-pool/README.md.
POOL_ROOT = Path(
    os.environ.get(
        "HELIOS_SESSION_POOL",
        "/srv/icloud/claude-kleos-mac/claude-session-pool",
    )
)


def _self_pool_host() -> str:
    """This machine's pool subdir name — skipped during remote discovery so
    we don't show our own pushed copy as a duplicate 'remote' project."""
    return socket.gethostname().split(".")[0].lower()


# The cwd every Helios chat defaults to. Sessions launched from here dominate
# (~70% as of 2026-06-11), so the UI treats it as the unmarked case — rows
# only grow a cwd chip when they point somewhere else.
HOME_CWD = str(Path.home())


def is_throwaway_cwd(cwd: str) -> bool:
    """One-shot agent working directories: anything under /tmp, or the
    `_tmp_<session>_<repo>` clone dirs the multi-session MR workflow mandates.
    These produce single-transcript projects that are dead on arrival — the
    session list hides them behind the Temporary filter instead of letting
    each one pin a permanent row."""
    if cwd == "/tmp" or cwd.startswith("/tmp/"):
        return True
    return any(part.startswith("_tmp_") for part in cwd.split("/"))


def decode_project_dirname(name: str) -> str:
    """`-home-alice` -> `/home/alice`.

    The encoding is lossy: a real dash in a directory name collides with
    the separator. We pick the right interpretation by trying every
    partition of dashes-into-slashes and returning the one that exists on
    disk. If nothing exists, fall back to the most-slashes interpretation
    (matches Claude Code's own behavior for fresh cwds).
    """
    if not name.startswith("-"):
        return name
    tokens = name.lstrip("-").split("-")
    if not tokens:
        return name

    # Walk dashes left-to-right, keeping each token attached either to the
    # previous segment (real dash) or starting a new segment (slash). This
    # is 2^(n-1) candidates but we short-circuit on the first interpretation
    # that exists on disk. Generated lazily so we don't allocate the full
    # candidate list up front — for a 16-token name that would be 32k strings
    # before any stat() runs.
    best_default = "/" + "/".join(tokens)

    def candidates(idx: int, current: list[str]):
        if idx == len(tokens):
            yield "/" + "/".join(current)
            return
        # Option A: start a new segment with this token. (Slashes-first ordering.)
        yield from candidates(idx + 1, current + [tokens[idx]])
        # Option B: attach to previous segment with a literal dash.
        if current:
            joined = current[:-1] + [current[-1] + "-" + tokens[idx]]
            yield from candidates(idx + 1, joined)

    # Cap exponential blow-up: if too many tokens, just trust the
    # slashes-only decode. 16 tokens = 32k candidates is the practical
    # ceiling for stat() spam.
    if len(tokens) > 16:
        return best_default

    for cand in candidates(1, [tokens[0]]):
        if Path(cand).exists():
            return cand
    return best_default


def encode_project_dirname(cwd: str) -> str:
    """`/home/alice` -> `-home-alice`."""
    return cwd.replace("/", "-")


@dataclass(slots=True)
class Session:
    project: "Project"
    session_id: str
    path: Path
    mtime: float
    size: int
    title: str = ""  # filled in lazily
    first_message_loaded: bool = False

    @property
    def mtime_dt(self) -> datetime:
        return datetime.fromtimestamp(self.mtime, tz=timezone.utc).astimezone()

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        return f"Session {self.session_id[:8]}"

    def ensure_title(self) -> str:
        """Cheaply pull the first user message text as the title."""
        if self.first_message_loaded:
            return self.display_title
        self.first_message_loaded = True
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "user":
                        continue
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    # Work/Goal sessions open with an injected Helios context
                    # envelope; strip it so the title is the real request, the
                    # same way transcript.iter_transcript already does.
                    text = strip_injected_context(_first_text(content)).strip()
                    if text:
                        # Trim — sidebar titles get one line.
                        title = " ".join(text.split())
                        if len(title) > 80:
                            title = title[:77] + "..."
                        self.title = title
                        break
        except OSError:
            pass
        return self.display_title


@dataclass(slots=True)
class Project:
    dirname: str  # the encoded form, as it appears on disk
    cwd: str  # the decoded path
    path: Path
    last_modified: float = 0.0
    sessions: list[Session] | None = None
    origin: str = "local"  # "local", or a remote host name (e.g. "macbook")
    read_only: bool = False  # remote pool projects can't be resumed/edited here

    @property
    def display_name(self) -> str:
        # Show the last 2 segments of the path: ".../parent/leaf" style is
        # what GNOME apps tend to do for file paths.
        parts = [p for p in self.cwd.split("/") if p]
        if not parts:
            return self.cwd or "(root)"
        if len(parts) == 1:
            return "/" + parts[0]
        return "/".join(parts[-2:])

    @property
    def subtitle(self) -> str:
        return self.cwd

    def load_sessions(self, *, refresh: bool = False) -> list[Session]:
        """Read every *.jsonl in the project dir as a Session.

        Results are cached on the instance. Pass `refresh=True` after
        anything that mutates the on-disk session set (delete, new session
        created by a fresh driver) — otherwise the cache will silently
        re-serve the stale list and e.g. deleted rows will reappear in the
        sidebar.
        """
        if not refresh and self.sessions is not None:
            return self.sessions
        sessions: list[Session] = []
        try:
            for entry in self.path.iterdir():
                if entry.is_file() and entry.suffix == ".jsonl":
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    sessions.append(
                        Session(
                            project=self,
                            session_id=entry.stem,
                            path=entry,
                            mtime=st.st_mtime,
                            size=st.st_size,
                        )
                    )
        except OSError:
            pass
        sessions.sort(key=lambda s: s.mtime, reverse=True)
        self.sessions = sessions
        return sessions

    def invalidate_sessions(self) -> None:
        """Drop the cached session list. Next `load_sessions()` re-reads disk."""
        self.sessions = None


def _cwd_from_transcript(path: Path) -> str:
    """Read the authoritative working directory from a transcript.

    Every Claude Code record carries a `cwd` field, so the transcript is the
    ground truth — far more reliable than reverse-engineering it from the
    dash-encoded dir name (which is lossy: a literal '-' in a path component is
    indistinguishable from the '/' separator). Returns "" if no record yields a
    cwd (we only need to read until the first one)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"cwd"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        pass
    return ""


def _scan_project_dir(
    entry: Path, *, origin: str, read_only: bool
) -> Project | None:
    """Build a Project from one ~/.claude/projects-style dir, or None if it
    holds no transcripts / should be skipped."""
    if not entry.is_dir():
        return None
    # Skip Helios's own title-generation scratch dir — internal, not a project.
    if "titlegen" in entry.name:
        return None
    # Most recent session mtime = "when was this project last used." Track the
    # newest transcript so we can read its cwd straight from the file.
    latest = 0.0
    latest_path: Path | None = None
    try:
        for f in entry.iterdir():
            if f.suffix == ".jsonl":
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if m > latest:
                    latest = m
                    latest_path = f
    except OSError:
        return None
    if latest == 0.0:
        # No transcripts -> skip; otherwise we'd list every cwd Claude touched.
        return None
    # Prefer the cwd recorded inside the transcript; fall back to decoding the
    # dir name only when the file has no cwd field (older/truncated logs).
    cwd = _cwd_from_transcript(latest_path) if latest_path else ""
    if not cwd:
        cwd = decode_project_dirname(entry.name)
    return Project(
        dirname=entry.name,
        cwd=cwd,
        path=entry,
        last_modified=latest,
        origin=origin,
        read_only=read_only,
    )


def sweep_empty_project_dirs() -> int:
    """Remove completely-empty `~/.claude/projects/<enc>` directories.

    `add_project` (and Claude Code itself) can create a project dir that never
    receives a session — e.g. the user picked a folder, then closed without
    sending. Such a dir lists nothing (discover skips zero-transcript dirs) yet
    lingers on disk forever. We only rmdir dirs that are *entirely* empty (no
    files, no subdirs), so this can never delete a real project's data.
    Returns the count removed. Safe to call on startup."""
    if not PROJECTS_DIR.exists():
        return 0
    removed = 0
    try:
        entries = list(PROJECTS_DIR.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            if any(entry.iterdir()):
                continue  # not empty — leave it alone
            entry.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def sweep_orphan_sidecar_sessions(
    live_ids: set[str] | None = None,
    *,
    now: float | None = None,
) -> int:
    """Move sidecar-only .jsonl files from LOCAL project dirs to the archive.

    A "sidecar-only" session holds no real user/assistant turns — the only
    content is metadata records such as ``{"type":"ai-title","aiTitle":"..."}``.
    These are created by the Claude CLI as a byproduct of title generation and
    never receive conversation content; they clog the sidebar as ghost rows.

    A file is moved only when ALL of the following hold:

      * it is in a LOCAL (non-pool, non-read-only) project dir
      * ``session_has_content`` returns False
      * mtime is older than ``_SIDECAR_SWEEP_GRACE_S`` (10 min)
        so a session that is mid-first-turn isn't swept away
      * its session id is NOT in ``live_ids`` (extra safety for the
        grace-window edge case — belt-and-suspenders with the mtime check)

    Files are MOVED (not deleted) to
    ``~/.helios/session-archive/<project-dirname>/`` so they can be recovered.
    The aux dir (same stem, no suffix) is moved alongside when present.
    ``session_providers.forget`` is called to drop any provider index entry.

    Returns the count moved.  Safe to call on startup; each file is wrapped in
    its own try/except so one bad path doesn't abort the rest."""
    from helios.backend import session_providers  # lazy to avoid startup cycles

    now_ts = time.time() if now is None else now
    live: set[str] = live_ids if live_ids is not None else set()
    moved = 0

    if not PROJECTS_DIR.exists():
        return 0

    try:
        project_entries = list(PROJECTS_DIR.iterdir())
    except OSError:
        return 0

    for proj_dir in project_entries:
        if not proj_dir.is_dir():
            continue
        # Skip internal titlegen scratch dir.
        if "titlegen" in proj_dir.name:
            continue
        try:
            jsonl_files = [f for f in proj_dir.iterdir() if f.is_file() and f.suffix == ".jsonl"]
        except OSError:
            continue

        for f in jsonl_files:
            try:
                st = f.stat()
            except OSError:
                continue
            age = now_ts - st.st_mtime
            if age < _SIDECAR_SWEEP_GRACE_S:
                continue
            session_id = f.stem
            if session_id in live:
                continue
            if session_has_content(f):
                continue
            # Move the sidecar to the archive.
            try:
                dest_dir = HELIOS_ARCHIVE_ROOT / proj_dir.name
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = _unique_sidecar_path(dest_dir / f.name)
                shutil.move(str(f), str(dest))
                # Also move the aux dir if present.
                aux = f.with_suffix("")
                if aux.is_dir():
                    shutil.move(str(aux), str(_unique_sidecar_path(dest_dir / aux.name)))
                session_providers.forget(session_id)
                moved += 1
            except OSError:
                continue
    return moved


# Archive destination for sidecar orphans — mirrors session_archiver.ARCHIVE_ROOT
# but under a different subdirectory name so the two sweeps don't collide.
HELIOS_ARCHIVE_ROOT = Path.home() / ".helios" / "session-archive"


def _unique_sidecar_path(path: Path) -> Path:
    """Return ``path`` if it doesn't exist, else ``path.stem.N.suffix``."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        cand = path.with_name(f"{stem}.{i}{suffix}")
        if not cand.exists():
            return cand
    raise OSError(f"too many archive collisions for {path}")


def archive_stale_throwaways(*, now: float | None = None) -> int:
    """Move dead throwaway projects out of `~/.claude/projects/`.

    The Temporary filter already hides these rows, but every startup scan
    still pays for them and the dir accretes one-shot agent debris forever.
    A project is archived (moved to `~/.claude/projects-archive/`, never
    deleted) only when ALL of these hold:

      * its cwd is a throwaway (`is_throwaway_cwd`)
      * it holds at most one transcript — multi-session throwaways may be
        real debugging history someone wants to revisit
      * last activity is older than `STALE_THROWAWAY_AGE_S` (14 days)
      * the cwd itself no longer exists on disk — a live dir means the
        work may still be in flight

    Local projects only; pool copies belong to their origin host. Returns
    the count moved. Safe to call on startup."""
    now_ts = time.time() if now is None else now
    moved = 0
    for proj in discover_projects(include_pool=False):
        if not is_throwaway_cwd(proj.cwd):
            continue
        if len(proj.load_sessions()) > 1:
            continue
        if now_ts - proj.last_modified < STALE_THROWAWAY_AGE_S:
            continue
        if Path(proj.cwd).exists():
            continue
        dest = ARCHIVE_DIR / proj.dirname
        try:
            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            n = 1
            while dest.exists():
                # Same dirname archived before (cwd reused across months) —
                # keep both copies rather than merging histories.
                dest = ARCHIVE_DIR / f"{proj.dirname}.{n}"
                n += 1
            shutil.move(str(proj.path), str(dest))
            moved += 1
        except OSError:
            continue
    return moved


def discover_pool_projects() -> list[Project]:
    """Read-only projects from OTHER hosts in the shared iCloud pool.

    Returns [] when the pool is absent (mount down / not set up), so callers
    can always include it safely. Our own host subdir is skipped — it's just a
    backup copy of the local projects we already list."""
    if not POOL_ROOT.exists():
        return []
    self_host = _self_pool_host()
    projects: list[Project] = []
    try:
        host_dirs = sorted(POOL_ROOT.iterdir())
    except OSError:
        return []
    for host_dir in host_dirs:
        if not host_dir.is_dir() or host_dir.name.lower() == self_host:
            continue
        try:
            entries = list(host_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            proj = _scan_project_dir(entry, origin=host_dir.name, read_only=True)
            if proj is not None:
                projects.append(proj)
    projects.sort(key=lambda p: p.last_modified, reverse=True)
    return projects


def discover_projects(*, include_pool: bool = False) -> list[Project]:
    """Scan ~/.claude/projects/ for local projects, newest first.

    When `include_pool` is set, append read-only projects from other hosts in
    the shared iCloud pool (local projects stay on top)."""
    locals_: list[Project] = []
    if PROJECTS_DIR.exists():
        for entry in PROJECTS_DIR.iterdir():
            proj = _scan_project_dir(entry, origin="local", read_only=False)
            if proj is not None:
                locals_.append(proj)
        locals_.sort(key=lambda p: p.last_modified, reverse=True)
    if not include_pool:
        return locals_
    return locals_ + discover_pool_projects()


def discover_local_sessions() -> list[Session]:
    """Every local session across every project dir, merged newest-first.

    This is the unified-list view: the project dir stops being a navigation
    level and becomes per-session metadata (`session.project.cwd`). Fast —
    local disk only; pool sessions come from `discover_pool_sessions()` so
    callers can keep the slow CIFS scan off the main thread.

    Returns EVERY local session — the sidebar hides sidecar-only "ghost" rows
    via `drop_empty_sessions()` (which needs live-driver ids the display layer
    owns), and the archiver wants the full list. Keeping this a pure lister
    means the two callers can't accidentally filter each other's view."""
    sessions: list[Session] = []
    for proj in discover_projects(include_pool=False):
        sessions.extend(proj.load_sessions())
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


def drop_empty_sessions(
    sessions: list[Session],
    *,
    live_ids: set[str],
    now: float,
    grace_s: float = _SIDECAR_DISPLAY_GRACE_S,
) -> list[Session]:
    """Hide sidecar-only ghost sessions (no real user/assistant turns — an
    `ai-title`/`last-prompt` stub the CLI left) from a session list.

    Descendant-only transcripts are always excluded: native agents belong in
    the in-conversation Agent Dock, never beside primary sessions.

    Otherwise a session is KEPT when ANY of these hold, so a real or active chat can
    never wink out:
      * its id is in ``live_ids`` (Helios is driving it right now), or
      * it was modified within ``grace_s`` (a just-created session may not have
        its first turn on disk yet), or
      * it actually has content (`session_has_content`).

    Pure and GTK-free so it is unit-testable. The sidebar passes the live ids
    it already tracks; the short grace is the belt-and-suspenders for the brief
    window before a new driver registers its id."""
    out: list[Session] = []
    for s in sessions:
        if session_is_descendant_only(s.path):
            continue
        if (
            s.session_id in live_ids
            or (now - s.mtime) < grace_s
            or session_has_content(s.path)
        ):
            out.append(s)
    return out


def discover_pool_sessions() -> list[Session]:
    """Every other-host session in the shared iCloud pool, newest-first.

    SLOW — each stat and descendant proof goes over the soft CIFS mount. Call
    off-thread. Descendant-only transcripts are removed here, before the pool
    result reaches ``SessionList._apply_pool_sessions`` on GTK; uncertain,
    unreadable, and mixed root/descendant files remain visible."""
    sessions: list[Session] = []
    for proj in discover_pool_projects():
        sessions.extend(proj.load_sessions())
    sessions = [
        session
        for session in sessions
        if not session_is_descendant_only(session.path)
    ]
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions


def ensure_local_project(cwd: str) -> Project:
    """Get-or-create the local Project for `cwd`.

    Creates `~/.claude/projects/<encoded>/` up front (the context/memory
    editor needs somewhere to write) — same contract Claude Code applies: the
    project only becomes "real" once a session writes a transcript; until
    then the dir is empty and the startup sweep can reclaim it. May raise
    OSError if the dir can't be created."""
    cwd = str(Path(cwd).expanduser())
    dirname = encode_project_dirname(cwd)
    proj_dir = PROJECTS_DIR / dirname
    proj_dir.mkdir(parents=True, exist_ok=True)
    existing = _scan_project_dir(proj_dir, origin="local", read_only=False)
    if existing is not None:
        # Transcripts already there — but trust the caller's cwd over the
        # transcript-derived one only when they disagree on encoding, not
        # content (the transcript cwd is authoritative).
        return existing
    return Project(
        dirname=dirname,
        cwd=cwd,
        path=proj_dir,
        last_modified=0.0,
        origin="local",
        read_only=False,
    )


def _first_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text") or ""
                if t.strip():
                    return t
    return ""
