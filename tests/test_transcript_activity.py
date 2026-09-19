"""`activity_items` — pairing a tool_result with the call that produced it.

GTK-free on purpose: this is the ordering logic the transcript's per-call cards
are built from, and it has to be exercised on the `python:3.13-slim` `tests`
lane. The seven `test_flatten_*` cases this file replaces lived in
tests/test_message_bubble.py, which `pytest.importorskip("gi")`s at module
scope — so on slim CI they never ran at all.
"""

from __future__ import annotations

from helios.backend.transcript import ToolResult, ToolUse, Turn, activity_items


def _turn(uses: list[ToolUse], results: list[ToolResult]) -> Turn:
    return Turn(role="assistant", tool_uses=uses, tool_results=results)


def _make_turn(n_uses: int, n_results: int) -> Turn:
    """n_uses calls with ids u0.., n_results results answering u0.. in order."""
    return _turn(
        [ToolUse(name=f"tool_{i}", input={"i": i}, id=f"u{i}") for i in range(n_uses)],
        [ToolResult(tool_use_id=f"u{j}", content=f"result {j}") for j in range(n_results)],
    )


def test_empty_turn_has_no_activity() -> None:
    assert activity_items(Turn(role="assistant")) == []


def test_result_follows_its_own_call_and_carries_its_name() -> None:
    tu = ToolUse(name="Bash", input={"command": "ls"}, id="t1")
    tr = ToolResult(tool_use_id="t1", content="ok")

    assert activity_items(_turn([tu], [tr])) == [
        ("tool_use", tu, "Bash"),
        ("tool_result", tr, "Bash"),
    ]


def test_results_arriving_reversed_still_follow_their_own_call() -> None:
    """The transcript order of results is not the order of the calls."""
    first = ToolUse(name="Bash", input={"command": "one"}, id="t1")
    second = ToolUse(name="Read", input={"file_path": "a.py"}, id="t2")
    late = ToolResult(tool_use_id="t1", content="one done")
    early = ToolResult(tool_use_id="t2", content="two done")

    assert activity_items(_turn([first, second], [early, late])) == [
        ("tool_use", first, "Bash"),
        ("tool_result", late, "Bash"),
        ("tool_use", second, "Read"),
        ("tool_result", early, "Read"),
    ]


def test_orphan_result_lands_last_with_no_name() -> None:
    tu = ToolUse(name="Bash", input={}, id="t1")
    paired = ToolResult(tool_use_id="t1", content="mine")
    orphan = ToolResult(tool_use_id="nobody", content="whose?")

    items = activity_items(_turn([tu], [orphan, paired]))

    assert items[-1] == ("tool_result", orphan, "")


def test_result_without_an_id_is_never_paired() -> None:
    tu = ToolUse(name="Bash", input={}, id="t1")
    anonymous = ToolResult(tool_use_id="", content="from nowhere")

    items = activity_items(_turn([tu], [anonymous]))

    assert items == [("tool_use", tu, "Bash"), ("tool_result", anonymous, "")]


def test_uses_only_keep_their_order() -> None:
    turn = _make_turn(n_uses=3, n_results=0)
    items = activity_items(turn)

    assert [kind for kind, _obj, _name in items] == ["tool_use"] * 3
    assert [obj for _kind, obj, _name in items] == turn.tool_uses


def test_results_only_keep_their_order() -> None:
    turn = _make_turn(n_uses=0, n_results=4)
    items = activity_items(turn)

    assert [kind for kind, _obj, _name in items] == ["tool_result"] * 4
    assert [obj for _kind, obj, _name in items] == turn.tool_results
    assert all(name == "" for _kind, _obj, name in items)


def test_single_use_and_single_result() -> None:
    assert len(activity_items(_make_turn(n_uses=1, n_results=0))) == 1
    assert len(activity_items(_make_turn(n_uses=0, n_results=1))) == 1


def test_every_object_appears_exactly_once() -> None:
    """The regression that matters: no drops, no duplicates.

    30 calls, 25 answered plus 5 results nothing asked for — every one of the
    60 objects has to come out, once.
    """
    turn = _make_turn(n_uses=30, n_results=25)
    turn.tool_results.extend(
        ToolResult(tool_use_id=f"gone{i}", content=str(i)) for i in range(5)
    )

    items = activity_items(turn)
    seen = [id(obj) for _kind, obj, _name in items]

    assert len(items) == 60
    assert sorted(seen) == sorted(
        id(obj) for obj in (*turn.tool_uses, *turn.tool_results)
    )


def test_one_id_answered_twice_keeps_both_results() -> None:
    """A reused id gives both results to the first call (the documented
    ceiling), but neither is dropped."""
    tu = ToolUse(name="Bash", input={}, id="t1")
    first = ToolResult(tool_use_id="t1", content="a")
    second = ToolResult(tool_use_id="t1", content="b")

    items = activity_items(_turn([tu], [first, second]))

    assert items == [
        ("tool_use", tu, "Bash"),
        ("tool_result", first, "Bash"),
        ("tool_result", second, "Bash"),
    ]
