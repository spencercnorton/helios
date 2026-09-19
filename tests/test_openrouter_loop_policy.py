"""Continuation is evidence-aware, bounded, and never task completion."""

from helios.backend.openrouter.loop_policy import ToolRoundPolicy


def observe(policy, output="evidence", *, name="Read", arguments="{}", error=False):
    policy.observe([(name, arguments, output, error)])


def test_new_evidence_extends_but_cannot_cross_hard_limit():
    policy = ToolRoundPolicy(2, 5)
    for index in range(5):
        assert policy.pause_reason() == ""
        observe(policy, f"new evidence {index}")
    assert policy.limit == 5
    assert policy.pause_reason() == "tool_round_limit"


def test_repeated_identical_rounds_pause_early():
    policy = ToolRoundPolicy(25, 100)
    for _ in range(3):
        assert policy.pause_reason() == ""
        observe(policy)
    assert policy.pause_reason() == "tool_stalled"


def test_unique_commands_returning_old_evidence_do_not_buy_more_rounds():
    policy = ToolRoundPolicy(8, 100)
    for index in range(8):
        observe(policy, name="Bash", arguments=f'{{"command":"echo unchanged #{index}"}}')
    assert policy.pause_reason() == "tool_round_limit"


def test_failure_or_plan_churn_cannot_extend():
    for name, error in [("Read", True), ("update_plan", False), ("checkpoint_context", False)]:
        policy = ToolRoundPolicy(2, 100)
        observe(policy, "one", name=name, error=error)
        observe(policy, "two", name=name, error=error)
        assert policy.pause_reason() == "tool_round_limit"


def test_changed_write_payload_counts_even_when_receipt_is_unchanged():
    policy = ToolRoundPolicy(8, 100)
    for index in range(8):
        observe(policy, "Written", name="Write", arguments=f'{{"content":"{index}"}}')
    assert policy.pause_reason() == ""
    assert policy.limit == 16


def test_new_output_after_identical_round_resets_stall():
    policy = ToolRoundPolicy(25, 100)
    observe(policy)
    observe(policy)
    observe(policy, "changed")
    assert policy.pause_reason() == ""
