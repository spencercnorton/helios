from helios.backend import session_goals as sg


def test_item_status_checked_is_completed():
    assert sg.item_status_for(checked=True, original=sg.PLAN_PENDING) == sg.PLAN_COMPLETED
    assert sg.item_status_for(checked=True, original=sg.PLAN_IN_PROGRESS) == sg.PLAN_COMPLETED


def test_item_status_unchecked_preserves_agent_state():
    assert sg.item_status_for(checked=False, original=sg.PLAN_IN_PROGRESS) == sg.PLAN_IN_PROGRESS
    assert sg.item_status_for(checked=False, original=sg.PLAN_BLOCKED) == sg.PLAN_BLOCKED


def test_item_status_unchecked_otherwise_pending():
    assert sg.item_status_for(checked=False, original=sg.PLAN_COMPLETED) == sg.PLAN_PENDING
    assert sg.item_status_for(checked=False, original=sg.PLAN_PENDING) == sg.PLAN_PENDING
    assert sg.item_status_for(checked=False, original="garbage") == sg.PLAN_PENDING
