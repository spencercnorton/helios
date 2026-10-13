"""Find a working `muse` binary (Muse Code, Meta).

Lookup order mirrors :mod:`helios.backend.claude_binary`, trimmed to the
install shapes Muse actually ships:

  1. $HELIOS_MUSE_BINARY env var (explicit user override)
  2. `muse` on PATH (if executable)
  3. `~/.local/bin/muse` — GUI launches get a session environment without
     `~/.local/bin` on PATH, so step 2 misses it exactly when Helios is
     started the normal way.
  4. `/opt/homebrew/bin/muse` and `/usr/local/bin/muse` (package managers)
  5. Raise MuseBinaryNotFound

Helios never installs or bundles the CLI: like Claude and Codex, the user
installs it and signs in with it; Helios only detects and drives it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from helios.backend.process.env_scrub import MUSE_AUTH_ENV, scrubbed_child_env


class MuseBinaryNotFound(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MuseBinary:
    path: Path
    source: str  # "env" | "PATH" | "local-bin" | "package"

    def __str__(self) -> str:
        return f"{self.path} (via {self.source})"


def _is_runnable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def find_muse_binary() -> MuseBinary:
    env = os.environ.get("HELIOS_MUSE_BINARY")
    if env:
        candidate = Path(env).expanduser()
        if _is_runnable(candidate):
            return MuseBinary(path=candidate, source="env")

    on_path = shutil.which("muse")
    if on_path:
        candidate = Path(on_path).resolve()
        if _is_runnable(candidate):
            return MuseBinary(path=candidate, source="PATH")

    local_bin = Path.home() / ".local" / "bin" / "muse"
    if _is_runnable(local_bin):
        return MuseBinary(path=local_bin.resolve(), source="local-bin")

    for candidate in (Path("/opt/homebrew/bin/muse"), Path("/usr/local/bin/muse")):
        if _is_runnable(candidate):
            return MuseBinary(path=candidate, source="package")

    raise MuseBinaryNotFound(
        "Could not find a working `muse` binary. Install Muse Code, "
        "or set $HELIOS_MUSE_BINARY to its path."
    )


_version_cache: dict[tuple[str, int], str] = {}
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def muse_version(binary: MuseBinary | None = None) -> str:
    """Best-effort `muse --version` (e.g. "1.4.6"); "" on any failure."""
    try:
        resolved = binary or find_muse_binary()
        key = (str(resolved.path), int(resolved.path.stat().st_mtime))
    except (MuseBinaryNotFound, OSError):
        return ""
    if key in _version_cache:
        return _version_cache[key]
    version = ""
    try:
        proc = subprocess.run(
            [str(resolved.path), "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
            env=scrubbed_child_env(keep=MUSE_AUTH_ENV),
        )
        match = _VERSION_RE.search(proc.stdout or "")
        version = match.group(1) if match else ""
    except (OSError, subprocess.TimeoutExpired):
        version = ""
    _version_cache[key] = version
    return version
