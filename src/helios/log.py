"""Centralized logging for Helios.

Replaces the scattered `print(..., flush=True)` debugging with a real logger so
output is timestamped, level-filtered, and easy to silence or capture. Levels:

    HELIOS_DEBUG=1            -> DEBUG (everything, incl. driver wire tracing)
    HELIOS_LOG_LEVEL=WARNING  -> explicit level override
    (default)                 -> INFO

All Helios modules log under the `helios.*` namespace; call
`get_logger(__name__-ish)` and use `.debug()/.info()/.warning()/.exception()`.

The log directory is resolved at setup time via :func:`helios.paths.state_dir`
so that setting ``HELIOS_STATE_DIR`` before importing (or between test runs)
redirects all file output to a different tree without touching real ``~/.helios``.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from helios.paths import state_dir

_configured = False
_excepthook_installed = False

_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUPS = 3


def log_file_path() -> Path:
    """Path of the rotating log file (for surfacing in Settings/About).

    Reflects the *current* ``HELIOS_STATE_DIR`` so that the value returned
    after a test fixture redirects the directory matches the file that was
    actually written to.
    """
    return state_dir() / "logs" / "helios.log"


def _file_handler() -> logging.Handler | None:
    """A rotating file handler, or None if the log dir can't be created
    (read-only ~/.helios) — logging must never break startup.

    The log directory is resolved at call time so that ``HELIOS_STATE_DIR``
    changes (e.g. between test fixtures) take effect on the next setup().
    """
    log_dir = state_dir() / "logs"
    log_file = log_dir / "helios.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(log_dir, 0o700)
        except OSError:
            pass
        handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        return handler
    except OSError:
        return None


def setup() -> None:
    """Configure the root `helios` logger exactly once (idempotent)."""
    global _configured
    if _configured:
        return
    if os.environ.get("HELIOS_DEBUG"):
        level = logging.DEBUG
    else:
        level = getattr(
            logging, os.environ.get("HELIOS_LOG_LEVEL", "INFO").upper(), logging.INFO
        )
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger("helios")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(stderr_handler)
    file_handler = _file_handler()
    if file_handler is not None:
        root.addHandler(file_handler)
    root.propagate = False
    _configured = True


def _reset() -> None:
    """Tear down the current log configuration so the next :func:`setup` call
    re-reads ``HELIOS_STATE_DIR`` and opens a fresh handler.

    **For test use only.**  Never call this in production code.
    """
    global _configured, _excepthook_installed
    root = logging.getLogger("helios")
    for h in root.handlers[:]:
        h.close()
    root.handlers.clear()
    _configured = False
    _excepthook_installed = False


def install_excepthook() -> None:
    """Route otherwise-uncaught exceptions (main thread and worker threads) to
    the log file, so a crash leaves a diagnosable record instead of vanishing.

    Idempotent. Call once at startup, after/around setup()."""
    global _excepthook_installed
    if _excepthook_installed:
        return
    setup()
    log = logging.getLogger("helios")

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _hook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread is not None else "?"
        log.critical(
            "Uncaught exception in thread %s",
            name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook
    _excepthook_installed = True


def get_logger(name: str) -> logging.Logger:
    """Return a `helios.<name>` logger, configuring logging on first use."""
    setup()
    return logging.getLogger(f"helios.{name}")
