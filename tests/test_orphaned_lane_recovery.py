"""Startup must free the lanes a Helios that died mid-turn still holds.

The store-level release is covered in `test_work_store.py`. What this file
covers is that *the window actually calls it*: the bug was never that the
release was wrong, it was that nothing ever ran one, so a chat orphaned by a
restart refused every later message for good.
"""

from __future__ import annotations

import types

import pytest

from helios.backend.work_store import ExecutionAdmissionError, WorkStore

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios.main_window import MainWindow  # noqa: E402


def _bricked_chat(tmp_path):
    """A Work whose turn was dispatched and whose Helios then died."""

    store = WorkStore(tmp_path / "work" / "work.db")
    work = store.create_work(objective="orphaned", cwd=str(tmp_path))
    participant = store.bind_participant(work.work_id, "anthropic")
    attempt = store.start_execution_attempt(
        work.work_id,
        participant.participant_id,
        provider="anthropic",
        expected_participant_generation=participant.generation,
        workspace_root=str(tmp_path),
    )
    store.record_execution_dispatch(
        attempt.attempt_id,
        wire_prompt_text="the turn that never came back",
        provider_request_key=attempt.attempt_id,
    )

    def send_again():
        return store.start_execution_attempt(
            work.work_id,
            participant.participant_id,
            provider="anthropic",
            expected_participant_generation=participant.generation,
            workspace_root=str(tmp_path),
        )

    return store, attempt, send_again


def test_startup_reclaims_the_lane_so_the_chat_answers_again(tmp_path):
    store, orphan, send_again = _bricked_chat(tmp_path)
    with pytest.raises(ExecutionAdmissionError, match="already executing"):
        send_again()
    window = types.SimpleNamespace(
        _work_coordinator=types.SimpleNamespace(
            reclaim_orphaned_execution_attempts=(
                store.reclaim_orphaned_execution_attempts
            )
        )
    )

    MainWindow._reclaim_orphaned_execution_lanes(window)

    assert store.get_execution_attempt(orphan.attempt_id).status == "aborted"
    assert send_again().status == "running"


def test_startup_survives_a_store_that_cannot_be_swept(tmp_path):
    """Recovery is best-effort: a failed sweep must not stop Helios opening."""

    def boom():
        raise RuntimeError("work.db is locked")

    window = types.SimpleNamespace(
        _work_coordinator=types.SimpleNamespace(
            reclaim_orphaned_execution_attempts=boom
        )
    )

    MainWindow._reclaim_orphaned_execution_lanes(window)  # must not raise


def test_startup_without_the_work_graph_is_a_no_op():
    MainWindow._reclaim_orphaned_execution_lanes(types.SimpleNamespace())
