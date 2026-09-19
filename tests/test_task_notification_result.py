"""A background subagent finishing must not close someone else's turn.

Measured: one user message produced THREE root `result` records
as background subagents finished. Each carries `origin.kind ==
"task-notification"` and NO `parent_tool_use_id` — it IS the root — so the
`if child_id:` guard did not catch it and it fell through to the root-terminal
path: confirm delivery, flip `_busy`, book terminal accounting, drain the
queue. After a queue flush the attempt that gets closed is the NEXT user
message, still in flight.

The wake-up is a real turn with real content, so the `result` signal is kept
for background sessions — `_on_turn_result` is the only thing that tells the
user a background Work finished. It is gated on `_busy` because that same
signal re-enables the composer and clears the activity strip for the current
driver.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402


def _driver() -> ClaudeCliDriver:
    drv = ClaudeCliDriver.__new__(ClaudeCliDriver)
    ClaudeCliDriver.__init__(drv, cwd="/tmp")
    return drv


def _wake_up(**extra) -> dict:
    rec = {
        "type": "result",
        "subtype": "success",
        "origin": {"kind": "task-notification"},
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    rec.update(extra)
    return rec


def _real_terminal() -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def test_a_wake_up_does_not_close_the_open_attempt() -> None:
    drv = _driver()
    closed: list = []
    drv._finish_execution_with_evidence = lambda ev: (closed.append(ev), True)[1]
    drv._confirm_uncertain_delivery = lambda: closed.append("confirmed")
    drv._busy = True

    drv._dispatch_record(_wake_up())

    assert closed == [], "a task-notification wake-up closed an attempt"
    assert drv._busy is True, "it flipped _busy for a turn it does not own"


def test_a_wake_up_is_silent_while_a_real_turn_is_in_flight() -> None:
    """Emitting would re-enable the composer and clear the activity strip on
    the current driver, reporting a live turn as finished."""

    drv = _driver()
    drv._busy = True
    seen: list = []
    drv.connect("result", lambda _d, obj: seen.append(obj))

    drv._dispatch_record(_wake_up())

    assert seen == []


def test_an_idle_wake_up_still_reports_the_background_work_finished() -> None:
    """The only signal a background session finished. Dropping it entirely
    would silence that, which is worse than the double-close."""

    drv = _driver()
    drv._busy = False
    seen: list = []
    drv.connect("result", lambda _d, obj: seen.append(obj))

    drv._dispatch_record(_wake_up())

    assert len(seen) == 1


def test_a_wake_up_held_while_busy_is_delivered_when_the_turn_ends() -> None:
    """Caught in review: gating on `_busy` silently DROPPED the notification.
    A background Work finishing during a foreground turn would never be
    reported. Held, not discarded."""

    drv = _driver()
    drv._busy = True
    drv._confirm_uncertain_delivery = lambda: None
    drv._finish_execution_with_evidence = lambda ev: True  # truthy = persisted
    drv._flush_user_queue = lambda: None
    seen: list = []
    drv.connect("result", lambda _d, obj: seen.append(obj))

    drv._dispatch_record(_wake_up())
    assert seen == [], "emitted during the live turn"

    drv._dispatch_record(_real_terminal())

    assert len(seen) == 2, "the held wake-up was never delivered"
    # The live turn reports first, then the background completion.
    assert seen[0].get("origin") is None
    assert seen[1]["origin"]["kind"] == "task-notification"


def test_held_wake_ups_are_delivered_once_not_replayed() -> None:
    drv = _driver()
    drv._busy = True
    drv._confirm_uncertain_delivery = lambda: None
    drv._finish_execution_with_evidence = lambda ev: True  # truthy = persisted
    drv._flush_user_queue = lambda: None
    seen: list = []
    drv.connect("result", lambda _d, obj: seen.append(obj))

    drv._dispatch_record(_wake_up())
    drv._dispatch_record(_real_terminal())
    drv._busy = True
    drv._dispatch_record(_real_terminal())

    kinds = [o.get("origin", {}).get("kind") if o.get("origin") else None for o in seen]
    assert kinds.count("task-notification") == 1


def test_a_genuine_root_terminal_still_closes_normally() -> None:
    """The guard must not swallow the ordinary case."""

    drv = _driver()
    closed: list = []
    drv._finish_execution_with_evidence = lambda ev: (closed.append(ev), True)[1]
    drv._confirm_uncertain_delivery = lambda: closed.append("confirmed")
    drv._busy = True

    drv._dispatch_record(_real_terminal())

    assert "confirmed" in closed, "an ordinary root terminal stopped closing"
    assert drv._busy is False


def test_a_child_terminal_is_still_routed_to_the_child() -> None:
    drv = _driver()
    noted: list = []
    drv._note_child_terminal = lambda cid, sub: noted.append((cid, sub))

    drv._dispatch_record(_real_terminal() | {"parent_tool_use_id": "tu-9"})

    assert noted == [("tu-9", "success")]


def test_a_result_without_an_origin_is_unaffected() -> None:
    """Only `origin.kind == task-notification` is special-cased; a malformed
    or absent origin must take the ordinary path."""

    drv = _driver()
    drv._busy = True
    drv._confirm_uncertain_delivery = lambda: None
    drv._finish_execution_with_evidence = lambda ev: True  # truthy = persisted

    drv._dispatch_record(_real_terminal() | {"origin": "not-a-dict"})

    assert drv._busy is False


# --- held results must not survive an abnormal turn end ---------------------


def test_an_abnormal_terminal_discards_held_wake_ups_without_emitting() -> None:
    """This path WITHHOLDS the real terminal and fences the driver. Emitting a
    held wake-up through the ordinary `result` signal would re-enable the
    composer and clear the activity strip — undoing the blocked state, and
    doing it with the only `result` the failed turn ever produced. Clear it.

    It must also not stay attached: a held record drained by some later,
    unrelated turn is the cross-turn confusion this guard exists to prevent."""

    drv = _driver()
    drv._busy = True
    drv._confirm_uncertain_delivery = lambda: None
    drv._finish_execution_with_evidence = lambda ev: False  # persistence failed
    drv.end_input = lambda: None
    seen: list = []
    drv.connect("result", lambda _d, obj: seen.append(obj))
    drv.connect("error", lambda _d, _m: None)

    drv._dispatch_record(_wake_up())
    drv._dispatch_record(_real_terminal())

    assert seen == [], "a held wake-up posed as the failed turn's completion"
    assert drv._deferred_task_results == [], "held results leaked past the turn"


def test_process_death_discards_held_wake_ups() -> None:
    """The session is over; emitting a completion against a driver being torn
    down would report the wrong thing. Drop, do not deliver."""

    drv = _driver()
    drv._busy = True
    drv._dispatch_record(_wake_up())
    assert drv._deferred_task_results, "precondition: something is held"

    seen: list = []
    drv.connect("result", lambda _d, obj: seen.append(obj))
    drv._deferred_task_results.clear()  # what the exit handler does

    assert seen == []
    assert drv._deferred_task_results == []
