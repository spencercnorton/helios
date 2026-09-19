"""A rewind must not race the agent it is undoing.

Both write the same files. Whichever lands second wins, so a restore during a
live turn produces a state that is neither the checkpoint nor the turn. These
drive `MainWindow`'s unbound methods against minimal stand-ins, matching the
convention in test_model_catalog_refresh.py.
"""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.main_window import MainWindow  # noqa: E402


def _window(*, busy: bool, in_flight: bool = False):
    toasts: list[str] = []
    fake = types.SimpleNamespace(
        _destroyed=False,
        _driver=types.SimpleNamespace(is_busy=busy, session_id="s1"),
        _restore_in_flight=in_flight,
        _toast=lambda msg, timeout=None: toasts.append(msg),
        _agent_is_writing=MainWindow._agent_is_writing,
    )
    return fake, toasts


def test_a_busy_agent_blocks_the_restore():
    fake, toasts = _window(busy=True)
    started: list = []
    fake._restore_in_flight = False

    MainWindow._on_restore_requested(fake, None, object(), ["a.py"])

    assert not started
    assert fake._restore_in_flight is False
    assert "writing files" in toasts[0]


def test_a_second_restore_is_refused_while_one_is_running():
    fake, toasts = _window(busy=False, in_flight=True)
    MainWindow._on_restore_requested(fake, None, object(), ["a.py"])
    assert "already running" in toasts[0]


def test_the_in_flight_flag_is_cleared_even_when_restore_fails():
    """Otherwise the action wedges for the rest of the session."""
    fake, toasts = _window(busy=False, in_flight=True)
    MainWindow._report_restore(fake, None, "git exploded")
    assert fake._restore_in_flight is False
    assert "Restore failed" in toasts[0]
    assert "git exploded" in toasts[0]


def test_a_failure_is_reported_rather_than_dying_silently():
    fake, toasts = _window(busy=False, in_flight=True)
    MainWindow._report_restore(fake, None, "")
    assert toasts, "a failed restore produced no user-visible result"
    assert "not fully changed" in toasts[0]


def test_an_idle_agent_does_not_block():
    fake, _toasts = _window(busy=False)
    assert MainWindow._agent_is_writing(fake._driver) is False


def test_a_missing_driver_is_not_treated_as_writing():
    assert MainWindow._agent_is_writing(None) is False


# ── the other direction: a restore must also block a turn ─────────────────


def _send_window(*, restore_in_flight: bool):
    toasts: list[str] = []
    kept: list[str] = []
    fake = types.SimpleNamespace(
        _destroyed=False,
        _driver=types.SimpleNamespace(is_busy=False, session_id="s1"),
        _restore_in_flight=restore_in_flight,
        _toast=lambda msg, timeout=None: toasts.append(msg),
        _restore_blocked_execution_send=lambda text: kept.append(text),
        _execution_change_pending_for=lambda _drv: False,
    )
    return fake, toasts, kept


def test_a_running_restore_blocks_a_new_turn(monkeypatch):
    """Exclusion has to hold in BOTH directions.

    Refusing a restore while a turn runs is only half of it: if a turn can
    start while `checkpoints.restore()` is still moving files, the same mixed
    worktree appears with the writes in the opposite order.
    """
    from helios import main_window as mw

    scheduled: list = []
    monkeypatch.setattr(mw.GLib, "idle_add", lambda fn, *a: scheduled.append((fn, a)))
    fake, toasts, _kept = _send_window(restore_in_flight=True)

    MainWindow._on_composer_send(fake, None, "please do the thing")

    assert toasts and "restore is still running" in toasts[0]
    # The message is preserved, not silently dropped.
    assert scheduled and scheduled[0][1] == ("please do the thing",)


def test_an_idle_window_with_no_restore_proceeds_past_the_gate(monkeypatch):
    """The gate must not block ordinary sends."""
    from helios import main_window as mw

    monkeypatch.setattr(mw.GLib, "idle_add", lambda fn, *a: None)
    fake, toasts, _kept = _send_window(restore_in_flight=False)
    # Past the two early-return gates it will fail on the parts of the window
    # this stand-in does not model — which is exactly the point: it got past
    # them rather than returning at the restore check.
    with pytest.raises(AttributeError):
        MainWindow._on_composer_send(fake, None, "hello")
    assert not [t for t in toasts if "restore is still running" in t]
