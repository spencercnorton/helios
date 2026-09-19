"""Shared pytest configuration for the Helios test suite.

Three responsibilities:

0. **Import-time state quarantine** — redirect ``HOME`` (and
   ``HELIOS_STATE_DIR`` / ``CLAUDE_HOME``) to a throwaway directory *before
   anything under ``helios`` is imported*.  This is the load-bearing guard, and
   it exists because the ``isolate_state_dir`` fixture below cannot possibly
   protect a module that computes ``Path.home() / ".helios" / ...`` at **import**
   time: by the time any fixture runs, the constant is already bound to the real
   path.  Nine modules did exactly that, and on 2026-07-28 a plain
   ``python3 -m pytest`` on the developer's workstation overwrote the running
   app's ``~/.helios/openrouter-models.json``, cutting its live model catalog
   from 367 entries to 1.  ``test_state_quarantine.py`` fails if a new module
   reintroduces the pattern.

1. **sys.path bootstrap** — add ``src/`` to ``sys.path`` so that ``import
   helios`` works without an editable install (``pip install -e .``).

2. **State-directory isolation** — the ``isolate_state_dir`` fixture
   (function-scoped, autouse) redirects every piece of Helios file I/O that
   goes through :func:`helios.paths.state_dir` to a fresh temporary directory
   for each test.  This keeps the real ``~/.helios`` untouched during test runs.

   Concretely the fixture:
   * sets ``HELIOS_STATE_DIR`` to ``tmp_path`` via ``monkeypatch.setenv``;
   * sets ``HOME`` to ``tmp_path`` as a belt-and-suspenders guard for any call
     that still resolves ``Path.home()`` directly;
   * calls ``helios.log._reset()`` so the next :func:`helios.log.setup` (or
     :func:`helios.log.get_logger`) creates a fresh ``RotatingFileHandler``
     under the temporary directory rather than recycling the one pointed at the
     real log file;
   * calls ``helios.backend.session_providers.reload()`` so the provider cache
     is dropped and the next read goes to the temporary directory.

   All of these side-effects are automatically reversed by ``monkeypatch`` and
   by the ``finally`` block in the fixture body after each test.
"""

import os
import sys
import tempfile
from pathlib import Path

# ── Import-time quarantine (must run before ANY helios import) ─────────────
# Deliberately at module scope, above the sys.path bootstrap, so it is in
# effect for every subsequent import in the whole session. Not cleaned up: the
# process exits at the end of the run, and leaving it lets a failed run be
# inspected. tempfile puts it under TMPDIR, which the OS reaps.
REAL_HOME = Path(os.path.expanduser("~")).resolve()
QUARANTINE = Path(tempfile.mkdtemp(prefix="helios-tests-home-"))
os.environ["HOME"] = str(QUARANTINE)
os.environ["USERPROFILE"] = str(QUARANTINE)  # Path.home() reads this on Windows
os.environ["HELIOS_STATE_DIR"] = str(QUARANTINE / ".helios")
os.environ["CLAUDE_HOME"] = str(QUARANTINE / ".claude")
(QUARANTINE / ".helios").mkdir(parents=True, exist_ok=True)
(QUARANTINE / ".claude").mkdir(parents=True, exist_ok=True)

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest  # noqa: E402 — must come after sys.path fixup


@pytest.fixture(autouse=True)
def isolate_state_dir(tmp_path, monkeypatch):
    """Redirect all Helios state I/O to a per-test temp directory.

    Sets ``HELIOS_STATE_DIR`` (and ``HOME`` as a fallback) to *tmp_path*,
    then resets the log and session-provider caches so they re-initialise
    against the temporary tree.  Everything is automatically restored after
    the test body returns.
    """
    # Point Helios state at the per-test temp directory.
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
    # Belt-and-suspenders: any Path.home() call (not yet routed through
    # state_dir()) will also land under tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))

    # Import lazily so that earlier sys.path mutation is visible.
    import helios.log as _log
    import helios.backend.conversation_perms as _cp
    import helios.backend.project_perms as _pp
    import helios.backend.session_providers as _sp

    # Re-initialise logging under the new temp directory. Reset clears the
    # real-dir handler that get_logger() attached at import time (before this
    # fixture ran), then setup() deterministically re-attaches a handler under
    # HELIOS_STATE_DIR=tmp_path — so logging during the test lands in the temp
    # tree, never the real ~/.helios/logs/helios.log. (Lazy re-setup via the
    # next get_logger() call was racy: cached loggers don't re-trigger it.)
    _log._reset()
    _log.setup()

    # Drop the session-provider cache so the next read resolves _path() fresh.
    _cp.reload()
    _pp.reload_legacy_permissions()
    _sp.reload()

    yield tmp_path

    # Tear down logging handlers pointing at tmp_path before pytest removes it,
    # then restore the "unconfigured" state so the next test can set up cleanly.
    _log._reset()
    _cp.reload()
    _pp.reload_legacy_permissions()
    _sp.reload()
