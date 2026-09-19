"""Tests for the logging / crash-handler setup (GTK-free)."""

from __future__ import annotations

import logging
import sys
import threading

import pytest

import helios.log as L


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Redirect the log file to *tmp_path* and force a clean (re)configure.

    The autouse ``isolate_state_dir`` fixture in conftest.py sets
    ``HELIOS_STATE_DIR`` to ``tmp_path`` and configures logging under it, so we
    reset to an unconfigured state here (tests in this module drive ``setup()``
    themselves) and restore the broader logging state (handlers, level,
    exception hooks) afterwards so that subsequent tests see a clean slate.
    """
    log_dir = tmp_path / "logs"

    root = logging.getLogger("helios")
    # conftest leaves logging configured under tmp_path; these tests exercise
    # setup() from a clean slate, so tear it down first.
    L._reset()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_sys = sys.excepthook
    saved_thread = threading.excepthook
    try:
        yield log_dir
    finally:
        for h in root.handlers[:]:
            h.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        sys.excepthook = saved_sys
        threading.excepthook = saved_thread
        L._reset()


def test_setup_writes_to_log_file(isolated_log):
    L.setup()
    L.get_logger("test").warning("written-to-file")
    for h in logging.getLogger("helios").handlers:
        h.flush()
    log_file = isolated_log / "helios.log"
    assert log_file.is_file()
    assert "written-to-file" in log_file.read_text(encoding="utf-8")


def test_log_dir_is_owner_only(isolated_log):
    L.setup()
    assert oct(isolated_log.stat().st_mode)[-3:] == "700"


def test_install_excepthook_sets_hooks_and_is_idempotent(isolated_log):
    L.install_excepthook()
    assert sys.excepthook is not sys.__excepthook__
    assert threading.excepthook.__name__ == "_thread_hook"
    first = sys.excepthook
    L.install_excepthook()  # second call is a no-op
    assert sys.excepthook is first


def test_excepthook_logs_traceback(isolated_log):
    L.install_excepthook()
    try:
        raise ValueError("kaboom")
    except ValueError:
        sys.excepthook(*sys.exc_info())
    for h in logging.getLogger("helios").handlers:
        h.flush()
    text = (isolated_log / "helios.log").read_text(encoding="utf-8")
    assert "Uncaught exception" in text
    assert "kaboom" in text
    assert "Traceback" in text


def test_keyboardinterrupt_is_delegated_not_logged(isolated_log, monkeypatch):
    L.install_excepthook()
    called = {}
    monkeypatch.setattr(
        sys, "__excepthook__", lambda *a: called.setdefault("delegated", True)
    )
    try:
        raise KeyboardInterrupt()
    except KeyboardInterrupt:
        sys.excepthook(*sys.exc_info())
    assert called.get("delegated") is True


def test_setup_survives_unwritable_log_dir(isolated_log, monkeypatch):
    """A read-only ~/.helios must not break startup logging — the stderr
    handler still works even if the file handler can't be created."""
    monkeypatch.setattr(L, "_file_handler", lambda: None)
    L.setup()  # must not raise
    handlers = logging.getLogger("helios").handlers
    assert any(isinstance(h, logging.StreamHandler) for h in handlers)
    # No file handler was added.
    assert not any(
        isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers
    )
