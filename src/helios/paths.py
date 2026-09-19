"""Centralised state-directory resolution for Helios.

All modules that need a path under `~/.helios/` should resolve the base
directory via :func:`state_dir` rather than computing ``Path.home() /
".helios"`` inline.  This makes it trivial to redirect I/O in tests (and in
any other sandboxed context) by setting the environment variable
``HELIOS_STATE_DIR``:

    HELIOS_STATE_DIR=/tmp/mytest python -m pytest

When the variable is **not** set the function returns ``~/.helios``, so
production behaviour is completely unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Return the root Helios state directory.

    Reads ``HELIOS_STATE_DIR`` from the environment on every call so that
    tests can change the variable between fixtures without stale references.
    Falls back to ``~/.helios`` when the variable is absent or empty.
    """
    override = os.environ.get("HELIOS_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".helios"
