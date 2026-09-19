"""`definition_of_done` becomes writable and does something.

The field has existed in the schema since v2 with **no writer anywhere**, which
is why `contract_epoch = 0` on all 141 live Works. Stage 1 gives it a writer and
puts the stopping condition in the goal envelope for every Work, single or
tandem — that is what makes filling it in worth doing.

GTK-free: runs in the slim CI lane.
"""

from __future__ import annotations

import pytest

from helios.backend import session_goals
from helios.backend.session_goals import GoalState
from helios.backend.work_coordinator import WorkCoordinator
from helios.backend.work_store import WorkStore


# --- envelope ------------------------------------------------------------


def _goal(objective="Ship the thing", dod="") -> GoalState:
    return GoalState(objective=objective, definition_of_done=dod)


def test_an_empty_definition_of_done_leaves_the_envelope_byte_identical() -> None:
    """The whole risk case: nothing that works today may change shape."""

    with_field = session_goals.wrap_user_prompt("hi", _goal(dod=""))

    assert "Done when:" not in with_field
    # The pre-stage-1 closing instruction, verbatim.
    assert (
        "- When the objective is complete, say so clearly and summarize validation.\n"
        in with_field
    )


def test_a_stated_condition_reaches_the_model() -> None:
    text = session_goals.wrap_user_prompt("hi", _goal(dod="ruff check . is clean"))

    assert "Done when: ruff check . is clean" in text


def test_a_stated_condition_replaces_the_self_referential_instruction() -> None:
    """"When the objective is complete, say so" left the model to invent its own
    completion test. With a condition, the instruction can name it."""

    text = session_goals.wrap_user_prompt("hi", _goal(dod="the failing test passes"))

    assert '- Stop when "Done when" is satisfied' in text
    assert "Do not widen scope" in text
    assert "say so clearly and summarize validation" not in text


def test_the_user_message_still_arrives_intact() -> None:
    text = session_goals.wrap_user_prompt("do the thing", _goal(dod="tests pass"))

    assert session_goals.strip_goal_envelope(text) == "do the thing"


# --- persistence ---------------------------------------------------------


def test_the_field_round_trips_through_session_goals_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_goals, "_PATH", tmp_path / "session-goals.json")
    monkeypatch.setattr(session_goals, "_cache", None)

    session_goals.set_goal("s1", _goal(dod="ruff is clean"))
    monkeypatch.setattr(session_goals, "_cache", None)  # force a reload from disk

    loaded = session_goals.get_goal("s1")
    assert loaded is not None
    assert loaded.definition_of_done == "ruff is clean"


def test_a_secret_in_the_condition_is_scrubbed(tmp_path, monkeypatch) -> None:
    """Scrubbed like the objective — it reaches the model and the store."""

    monkeypatch.setattr(session_goals, "_PATH", tmp_path / "session-goals.json")
    monkeypatch.setattr(session_goals, "_cache", None)

    # A shape the scrubber recognizes that is NOT key-shaped. A literal
    # `sk-ant-...` here tripped gitleaks in the secret_scan job — the scanner
    # doing its job. Do not reintroduce a realistic key pattern in a fixture.
    # (Separately noted: the scrubber does NOT match GitLab `glpat-` PATs —
    # that gap predates this change and is not stage 1's to fix.)
    goal = _goal(dod="deploy with password=hunter2")
    session_goals.set_goal("s2", goal)

    loaded = session_goals.get_goal("s2")
    assert loaded is not None
    assert "hunter2" not in loaded.definition_of_done
    assert "REDACTED" in loaded.definition_of_done


# --- write-through to works.definition_of_done ---------------------------


def _store(tmp_path) -> WorkStore:
    return WorkStore(tmp_path / "work.db")


def test_record_goal_writes_through_to_the_work(tmp_path) -> None:
    """The first writer this column has ever had."""

    store = _store(tmp_path)
    coord = WorkCoordinator(store)
    work = coord.ensure_work(cwd="/repo", lead_provider="anthropic")

    coord.record_goal(
        work_id=work.work_id,
        provider="anthropic",
        goal=_goal(objective="Fix the bug", dod="the failing test passes"),
    )

    reloaded = store.get_work(work.work_id)
    assert reloaded is not None
    assert reloaded.definition_of_done == "the failing test passes"
    assert reloaded.objective == "Fix the bug"


def test_a_single_work_with_both_fields_is_still_not_contract_accepted(tmp_path) -> None:
    """The 2026-08-03 containment fix must survive stage 1 untouched.

    A filled-in contract must NOT switch the cross-provider packet on by
    accident — `mode` is still `single`, and deriving `tandem` is stage 3.
    """

    store = _store(tmp_path)
    coord = WorkCoordinator(store)
    work = coord.ensure_work(cwd="/repo", lead_provider="anthropic")
    coord.bind_participant(work.work_id, "anthropic")
    coord.record_goal(
        work_id=work.work_id,
        provider="anthropic",
        goal=_goal(objective="Fix the bug", dod="the failing test passes"),
    )

    reloaded = store.get_work(work.work_id)
    assert reloaded.mode == "single"
    snapshot = store.snapshot_collaboration_prompt(work.work_id, "anthropic", limit=20)
    assert snapshot.contract_accepted is False


def test_prepare_prompt_still_sends_no_collaboration_packet(tmp_path) -> None:
    """Stage 1 changes the envelope, never the packet."""

    store = _store(tmp_path)
    coord = WorkCoordinator(store)
    work = coord.ensure_work(cwd="/repo", lead_provider="anthropic")
    coord.bind_participant(work.work_id, "anthropic")
    goal = _goal(objective="Fix the bug", dod="the failing test passes")
    coord.record_goal(work_id=work.work_id, provider="anthropic", goal=goal)

    wire = coord.prepare_prompt(
        work_id=work.work_id, provider="anthropic", text="go", goal=goal
    )

    wire_text = wire if isinstance(wire, str) else wire.text
    assert "Done when: the failing test passes" in wire_text
    # The collaboration ledger delta is gated on contract_accepted, which is
    # still False. Its marker must be absent.
    assert "HELIOS-WORK-PACKET" not in wire_text


@pytest.mark.parametrize("dod", ["", "   "])
def test_a_blank_condition_writes_nothing_and_changes_nothing(tmp_path, dod) -> None:
    store = _store(tmp_path)
    coord = WorkCoordinator(store)
    work = coord.ensure_work(cwd="/repo", lead_provider="anthropic")

    coord.record_goal(
        work_id=work.work_id, provider="anthropic", goal=_goal(dod=dod)
    )

    reloaded = store.get_work(work.work_id)
    assert reloaded.definition_of_done == ""


def test_a_goal_edit_without_a_condition_never_clears_an_existing_one(tmp_path) -> None:
    """Regression: blanking it silently revokes contract acceptance.

    Several callers build a GoalState without the field (native goal
    reconciliation, the legacy session-keyed path). If any of them could clear
    it, a tandem Work would lose its contract as a side effect of an unrelated
    goal edit.
    """

    store = _store(tmp_path)
    coord = WorkCoordinator(store)
    work = coord.ensure_work(cwd="/repo", lead_provider="anthropic")
    coord.record_goal(
        work_id=work.work_id,
        provider="anthropic",
        goal=_goal(objective="Fix the bug", dod="the failing test passes"),
    )

    # An edit that only changes the objective, carrying no condition.
    coord.record_goal(
        work_id=work.work_id,
        provider="anthropic",
        goal=_goal(objective="Fix the other bug", dod=""),
    )

    reloaded = store.get_work(work.work_id)
    assert reloaded.definition_of_done == "the failing test passes"
    assert reloaded.objective == "Fix the other bug"
