"""Tests for AskUserQuestion dialog serialization in MainWindow.

We bind the real `_on_question_asked` / `_pump_questions` methods to a tiny
fake `self` and monkeypatch `present_question` so no actual dialog is shown.
This needs gi only to import main_window (no display), so it's skipped on the
GTK-free CI image.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.main_window import MainWindow  # noqa: E402


class FakeDrv:
    def __init__(self, sid: str, running: bool = True) -> None:
        self.session_id = sid
        self._running = running
        self.answers: list[tuple[str, str | None]] = []

    @property
    def is_running(self) -> bool:
        return self._running

    def answer_question(self, tool_use_id: str, text: str | None) -> None:
        self.answers.append((tool_use_id, text))


@pytest.fixture
def win(monkeypatch):
    captured: list[tuple] = []

    class FakeDialog:
        closed = False

        def close(self):
            self.closed = True

    def fake_present(parent, payload, on_answer, on_dismiss):
        dialog = FakeDialog()
        captured.append((payload, on_answer, on_dismiss, dialog))
        return dialog

    monkeypatch.setattr(
        "helios.widgets.question_dialog.present_question", fake_present
    )

    f = types.SimpleNamespace()
    f._destroyed = False
    f._question_active = False
    f._question_active_key = None
    f._question_dialog = None
    f._question_queue = []
    f._answered_question_ids = {}
    f._selected: list[str] = []
    f._pending_sets: list[set] = []
    f._notified: list[str] = []
    f._notified_question_sids: set = set()
    f._notify_pending_question = lambda drv: f._notified.append(drv.session_id)
    f._sessions = types.SimpleNamespace(
        select_session=lambda sid: f._selected.append(sid),
        set_pending_questions=lambda ids: f._pending_sets.append(set(ids)),
    )
    f._drv_is_current = lambda drv: True
    f._on_question_asked = types.MethodType(MainWindow._on_question_asked, f)
    f._pump_questions = types.MethodType(MainWindow._pump_questions, f)
    f._sync_pending_question_indicators = types.MethodType(
        MainWindow._sync_pending_question_indicators, f
    )
    f._on_interaction_resolved = types.MethodType(
        MainWindow._on_interaction_resolved, f
    )
    f._resolve_questions_for_driver = types.MethodType(
        MainWindow._resolve_questions_for_driver, f
    )
    f.captured = captured
    return f


def test_only_one_dialog_at_a_time(win):
    d1, d2 = FakeDrv("s1"), FakeDrv("s2")
    win._on_question_asked(d1, {"q": 1}, "t1")
    win._on_question_asked(d2, {"q": 2}, "t2")
    # Only the first is presented; the second waits in the queue.
    assert len(win.captured) == 1
    assert win._question_active is True
    assert len(win._question_queue) == 1

    # Answer the first → the second is presented next.
    win.captured[0][1]("answer-1")  # on_answer
    assert d1.answers == [("t1", "answer-1")]
    assert len(win.captured) == 2
    assert len(win._question_queue) == 0

    # Dismiss the second → driver gets a None answer, queue idle.
    win.captured[1][2]()  # on_dismiss
    assert d2.answers == [("t2", None)]
    assert win._question_active is False


def test_dead_driver_question_is_skipped(win):
    dead = FakeDrv("s-dead", running=False)
    win._on_question_asked(dead, {"q": 1}, "t-dead")
    # Nothing presented; no answer attempted on a dead driver.
    assert win.captured == []
    assert win._question_active is False
    assert dead.answers == []


def test_duplicate_tool_use_id_ignored(win):
    d = FakeDrv("s1")
    win._on_question_asked(d, {"q": 1}, "dup")
    win._on_question_asked(d, {"q": 1}, "dup")
    assert len(win.captured) == 1
    assert len(win._question_queue) == 0


def test_no_dialog_after_destroyed(win):
    win._destroyed = True
    win._on_question_asked(FakeDrv("s1"), {"q": 1}, "t1")
    assert win.captured == []  # pump is a no-op once destroyed


def test_background_question_stays_queued_with_indicator(win):
    # A question from a NON-visible session must not steal focus or pop a modal:
    # it stays queued and its row is flagged "needs you".
    win._drv_is_current = lambda drv: False
    d = FakeDrv("bg-session")
    win._on_question_asked(d, {"q": 1}, "t1")
    assert win.captured == []              # no modal
    assert win._selected == []             # no forced session switch
    assert win._question_active is False
    assert len(win._question_queue) == 1   # still waiting
    assert win._pending_sets[-1] == {"bg-session"}  # dot set
    assert win._notified == ["bg-session"]  # + desktop notification (filter-proof)


def test_current_session_question_does_not_notify(win):
    # The visible session's question presents inline — no desktop notification.
    d = FakeDrv("cur")
    win._drv_is_current = lambda drv: True
    win._on_question_asked(d, {"q": 1}, "t1")
    assert len(win.captured) == 1
    assert win._notified == []


def test_background_question_presents_when_session_becomes_current(win):
    # Deferred while backgrounded, then presented once the user switches to it
    # (simulated by _drv_is_current flipping true + a pump, as _on_session_selected does).
    d = FakeDrv("bg")
    win._drv_is_current = lambda drv: False
    win._on_question_asked(d, {"q": 1}, "t1")
    assert win.captured == []
    win._drv_is_current = lambda drv: True
    win._pump_questions()
    assert len(win.captured) == 1
    assert win._question_active_key == (d, "t1")
    assert win._pending_sets[-1] == set()  # its dot clears once shown


def test_resolving_queued_background_question_clears_dot(win):
    # A background question resolved elsewhere (App Server) must clear its dot
    # even though it was never the active dialog.
    cur, bg = FakeDrv("cur"), FakeDrv("bg")
    win._drv_is_current = lambda drv: drv is cur
    win._on_question_asked(cur, {"q": 1}, "cur")  # presents (active)
    win._on_question_asked(bg, {"q": 2}, "bg")    # queued -> dot {bg}
    assert win._pending_sets[-1] == {"bg"}
    win._on_interaction_resolved(bg, "bg")
    assert win._question_queue == []
    assert win._pending_sets[-1] == set()         # stale dot cleared
    assert win._question_active_key == (cur, "cur")  # active untouched


def test_departing_driver_with_only_queued_clears_dot(win):
    # A driver that exits holding only a QUEUED question (no active dialog)
    # must still clear its 'needs you' dot.
    cur, bg = FakeDrv("cur"), FakeDrv("bg")
    win._drv_is_current = lambda drv: drv is cur
    win._on_question_asked(cur, {"q": 1}, "cur")  # active
    win._on_question_asked(bg, {"q": 2}, "bg")    # queued -> {bg}
    assert win._pending_sets[-1] == {"bg"}
    win._resolve_questions_for_driver(bg)
    assert win._pending_sets[-1] == set()
    assert bg.answers == [("bg", None)]           # failed closed
    assert win._question_active_key == (cur, "cur")


def test_stale_question_notification_is_withdrawn(win):
    # Once a notified session's question leaves the queue, its desktop
    # notification must be withdrawn so it doesn't outlive the question.
    withdrawn: list[str] = []
    win.get_application = lambda: types.SimpleNamespace(
        withdraw_notification=lambda key: withdrawn.append(key)
    )
    win._notified_question_sids = {"bg"}  # a notification was sent earlier
    win._question_queue = []              # ...but the question is gone now
    win._sync_pending_question_indicators()
    assert withdrawn == ["helios-question-bg"]
    assert win._notified_question_sids == set()


def test_indicator_sync_is_noop_after_destroy(win):
    # Late driver signals after close must not touch the finalizing UI.
    win._question_queue.append((FakeDrv("s1"), {"q": 1}, "t1"))
    before = len(win._pending_sets)
    win._destroyed = True
    win._sync_pending_question_indicators()
    assert len(win._pending_sets) == before  # session list left untouched


def test_current_question_not_blocked_by_background_head(win):
    # A background question sitting at the HEAD of the queue must not
    # head-of-line-block the visible session's question behind it.
    bg, cur = FakeDrv("bg"), FakeDrv("cur")
    win._drv_is_current = lambda drv: drv is cur
    win._on_question_asked(bg, {"q": 1}, "bg")    # queued, not visible
    win._on_question_asked(cur, {"q": 2}, "cur")  # visible → presents now
    assert len(win.captured) == 1
    assert win._question_active_key == (cur, "cur")
    assert len(win._question_queue) == 1          # bg still waiting
    assert win._pending_sets[-1] == {"bg"}


def test_server_resolved_closes_active_and_removes_queued(win):
    first, second = FakeDrv("s1"), FakeDrv("s2")
    win._on_question_asked(first, {"q": 1}, "active")
    win._on_question_asked(second, {"q": 2}, "queued")

    dialog = win.captured[0][3]
    win._on_interaction_resolved(second, "queued")
    assert win._question_queue == []
    assert dialog.closed is False

    win._on_interaction_resolved(first, "active")
    assert dialog.closed is True


def test_late_close_callback_cannot_clear_next_active_dialog(win):
    first, second = FakeDrv("s1"), FakeDrv("s2")
    win._on_question_asked(first, {"q": 1}, "first")
    win._on_question_asked(second, {"q": 2}, "second")
    old_dismiss = win.captured[0][2]

    win._on_interaction_resolved(first, "first")
    assert win._question_active_key == (second, "second")
    old_dismiss()

    assert first.answers == []
    assert win._question_active_key == (second, "second")
    assert win._question_dialog is win.captured[1][3]


def test_synchronous_empty_payload_does_not_overwrite_next_dialog(win, monkeypatch):
    class FakeDialog:
        pass

    presented: list[FakeDialog] = []

    def synchronous_present(_parent, payload, _on_answer, on_dismiss):
        if payload.get("empty"):
            on_dismiss()
            return None
        dialog = FakeDialog()
        presented.append(dialog)
        return dialog

    monkeypatch.setattr(
        "helios.widgets.question_dialog.present_question",
        synchronous_present,
    )
    first, second = FakeDrv("s1"), FakeDrv("s2")
    win._question_queue.extend(
        [
            (first, {"empty": True}, "empty"),
            (second, {"questions": ["valid"]}, "valid"),
        ]
    )

    win._pump_questions()

    assert first.answers == [("empty", None)]
    assert win._question_active_key == (second, "valid")
    assert win._question_dialog is presented[0]


def test_departing_driver_releases_active_and_queued_questions(win):
    departing = FakeDrv("departing")
    survivor = FakeDrv("survivor")
    win._on_question_asked(departing, {"q": 1}, "active")
    win._on_question_asked(departing, {"q": 2}, "queued-departing")
    win._on_question_asked(survivor, {"q": 3}, "queued-survivor")
    active_dialog = win.captured[0][3]

    win._resolve_questions_for_driver(departing)

    assert active_dialog.closed is True
    assert departing.answers == [
        ("queued-departing", None),
        ("active", None),
    ]
    assert win._question_active_key == (survivor, "queued-survivor")
    assert len(win.captured) == 2


def test_a_cancelled_prompt_closes_its_dialog_and_can_be_reissued(win):
    """control_cancel_request (v0.88.0): the active dialog closes without an
    answer, a queued copy leaves the queue, and the dedup memory forgets the
    id so the CLI can ask again under the same tool_use_id."""
    import types as _types

    from helios.main_window import MainWindow as _MW

    win._on_question_cancelled = _types.MethodType(_MW._on_question_cancelled, win)
    d1, d2 = FakeDrv("s1"), FakeDrv("s2")
    win._on_question_asked(d1, {"q": 1}, "t1")
    win._on_question_asked(d2, {"q": 2}, "t2")
    dialog = win.captured[0][3]

    win._on_question_cancelled(d1, "t1")

    assert dialog.closed is True
    assert d1.answers == [], "a withdrawn request is never answered"
    # The next queued question is presented in its place.
    assert len(win.captured) == 2
    assert win._question_active_key == (d2, "t2")

    win._on_question_cancelled(d2, "t2")
    assert win._question_active is False
    assert win._question_queue == []

    # Reissued under the same id → presented again, not dropped as a duplicate.
    win._on_question_asked(d1, {"q": 3}, "t1")
    assert len(win.captured) == 3
