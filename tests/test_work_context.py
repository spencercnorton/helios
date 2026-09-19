from helios.backend.work_context import (
    CollaborationUpdate,
    build_packet,
    strip_work_envelope,
    wrap_user_prompt,
)
from helios.backend.session_goals import GoalState, wrap_user_prompt as wrap_goal
from helios.backend.transcript import turn_from_record


def test_packet_contains_every_supplied_ledger_update():
    packet = build_packet(
        "work-1",
        [
            CollaborationUpdate(1, "user.message", "anthropic", "Build the API"),
            CollaborationUpdate(2, "participant.contribution", "anthropic", "Added tests"),
            CollaborationUpdate(3, "decision.recorded", "", "Use SQLite"),
            CollaborationUpdate(4, "participant.contribution", "openai", "My own note"),
        ],
    )

    assert "Build the API" in packet.text
    assert "Added tests" in packet.text
    assert "Use SQLite" in packet.text
    assert "My own note" in packet.text
    assert packet.through_seq == 4


def test_provider_output_is_rendered_for_replacement_native_thread():
    packet = build_packet(
        "work-1",
        [CollaborationUpdate(7, "participant.contribution", "anthropic", "done")],
    )

    assert "done" in packet.text
    assert packet.through_seq == 7


def test_packet_is_bounded_and_covers_a_contiguous_prefix():
    packet = build_packet(
        "work-1",
        [
            CollaborationUpdate(1, "user.message", "anthropic", "old " * 300),
            CollaborationUpdate(2, "participant.contribution", "anthropic", "new result"),
        ],
        max_chars=500,
    )

    assert len(packet.text) <= 500
    assert "payload elided" in packet.text
    assert "new result" in packet.text
    assert packet.through_seq == 2


def test_event_count_cap_never_advances_past_omitted_update():
    packet = build_packet(
        "work-1",
        [
            CollaborationUpdate(4, "user.message", "anthropic", "one"),
            CollaborationUpdate(5, "user.message", "anthropic", "two"),
        ],
        max_updates=1,
    )

    assert "one" in packet.text
    assert "two" not in packet.text
    assert packet.through_seq == 4


def test_wrapper_round_trips_original_user_text():
    packet = build_packet(
        "work-1",
        [CollaborationUpdate(1, "decision.recorded", "", "Use WAL")],
    )
    wrapped = wrap_user_prompt("continue", packet)

    assert "Work: work-1" in wrapped
    assert strip_work_envelope(wrapped) == "continue"


def test_unrecognized_or_incomplete_text_is_left_unchanged():
    assert strip_work_envelope("ordinary") == "ordinary"
    partial = "--- BEGIN HELIOS WORK CONTEXT ---\nmissing end"
    assert strip_work_envelope(partial) == partial


def test_transcript_strips_goal_and_work_wrappers_in_either_nesting_order():
    packet = build_packet(
        "work-1",
        [CollaborationUpdate(1, "decision.recorded", "", "Use WAL")],
    )
    goal = GoalState("Ship it")
    goal_outside = wrap_goal(wrap_user_prompt("continue", packet), goal)
    work_outside = wrap_user_prompt(wrap_goal("continue", goal), packet)

    for wire_text in (goal_outside, work_outside):
        turn = turn_from_record(
            {
                "type": "user",
                "message": {"role": "user", "content": wire_text},
            }
        )
        assert turn is not None
        assert turn.text == "continue"


def test_accepted_state_renders_inside_envelope_even_without_updates():
    packet = build_packet(
        "work-1",
        [],
        accepted_state="Objective: none (cleared)",
    )

    assert "authoritative; supersedes older updates" in packet.text
    assert "Objective: none (cleared)" in packet.text
    assert packet.through_seq == 0
    assert strip_work_envelope(wrap_user_prompt("next", packet)) == "next"
