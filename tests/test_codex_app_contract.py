import pytest
from helios.backend.process import codex_app_contract as contract
from helios.backend.process.codex_app_contract import (
    approval_question,
    build_review_settings_params,
    build_review_start_params,
    build_thread_params,
    build_thread_fork_params,
    build_turn_start_params,
    build_turn_steer_params,
    humanize_window_mins,
    interaction_response,
    rate_limit_rows,
)
from helios.backend.codex_context import CODEX_DEVELOPER_INSTRUCTIONS
from helios.backend.project_perms import PROTECTED_HOME_CWD


@pytest.mark.parametrize("builder", [build_thread_params, build_turn_start_params])
def test_contract_rejects_unselected_full_access_even_if_profile_regresses(
    builder,
    monkeypatch,
):
    monkeypatch.setattr(
        contract,
        "codex_permission_profile",
        lambda _mode: ("never", "danger-full-access", "user", True),
    )
    kwargs = {
        "cwd": "/repo",
        "model": "gpt-5.6",
        "permission_mode": "default",
    }
    if builder is build_turn_start_params:
        kwargs.update(thread_id="thread-1", text="do it")
    with pytest.raises(ValueError, match="danger-full-access requires explicit Bypass"):
        builder(**kwargs)


def test_thread_params_are_schema_exact_for_start_and_resume():
    start = build_thread_params(cwd="/repo", model="gpt-5.6", permission_mode="auto")
    assert start == {
        "cwd": "/repo",
        "model": "gpt-5.6",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandbox": "workspace-write",
        "personality": "pragmatic",
        "config": {
            "suppress_unstable_features_warning": True,
            "features": {"default_mode_request_user_input": True},
            "tools": {"update_plan": {"enabled": True}},
            "agents": {"enabled": False},
        },
        "ephemeral": False,
        "serviceName": "helios",
        "threadSource": "helios",
    }
    assert "dynamicTools" not in start
    resume = build_thread_params(
        cwd="/repo",
        model="gpt-5.6",
        permission_mode="default",
        thread_id="thread-1",
    )
    assert resume["threadId"] == "thread-1"
    assert resume["approvalPolicy"] == "on-request"
    assert resume["sandbox"] == "workspace-write"
    assert "ephemeral" not in resume
    assert "dynamicTools" not in resume


def test_turn_start_and_steer_use_v2_text_input():
    assert build_turn_start_params(
        thread_id="th",
        text="hello",
        permission_mode="default",
        cwd="/repo/./src/..",
        model="gpt-5.6",
        effort="high",
    ) == {
        "threadId": "th",
        "input": [{"type": "text", "text": "hello"}],
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandboxPolicy": {
            "type": "workspaceWrite",
            "writableRoots": ["/repo"],
            "networkAccess": False,
        },
        "model": "gpt-5.6",
        "effort": "high",
        "collaborationMode": {
            "mode": "default",
            "settings": {
                "model": "gpt-5.6",
                "reasoning_effort": "high",
                "developer_instructions": None,
            },
        },
        "additionalContext": {
            "helios-policy": {
                "kind": "application",
                "value": CODEX_DEVELOPER_INSTRUCTIONS,
            }
        },
    }
    assert (
        build_turn_steer_params(
            thread_id="th", turn_id="turn", text="also check tests"
        )["expectedTurnId"]
        == "turn"
    )


def test_native_review_is_inline_and_read_only_before_model_dispatch() -> None:
    assert build_review_settings_params(
        thread_id="thread-1",
        model="gpt-5.6",
        effort="high",
    ) == {
        "threadId": "thread-1",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
        "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        "model": "gpt-5.6",
        "effort": "high",
        "collaborationMode": {
            "mode": "default",
            "settings": {
                "model": "gpt-5.6",
                "reasoning_effort": "high",
                "developer_instructions": None,
            },
        },
    }
    assert build_review_start_params(thread_id="thread-1") == {
        "threadId": "thread-1",
        "delivery": "inline",
        "target": {"type": "uncommittedChanges"},
    }
    assert build_review_start_params(
        thread_id="thread-1",
        instructions="  focus on races  ",
    )["target"] == {
        "type": "custom",
        "instructions": "focus on races",
    }


def test_native_fork_is_persisted_fresh_and_never_danger_full_access() -> None:
    params = build_thread_fork_params(
        thread_id="source-thread",
        cwd="/repo/./src/..",
        permission_mode="auto",
        model="gpt-5.6",
        before_turn_id="turn-7",
    )

    assert params == {
        "threadId": "source-thread",
        "cwd": "/repo",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandbox": "workspace-write",
        "config": {
            "suppress_unstable_features_warning": True,
            "features": {"default_mode_request_user_input": True},
            "tools": {"update_plan": {"enabled": True}},
            "agents": {"enabled": False},
        },
        "ephemeral": False,
        "threadSource": "helios",
        "model": "gpt-5.6",
        "beforeTurnId": "turn-7",
    }
    assert "deferGoalContinuation" not in params

    original = contract.codex_permission_profile
    try:
        contract.codex_permission_profile = lambda _mode: (
            "never",
            "danger-full-access",
            "user",
            False,
        )
        with pytest.raises(ValueError, match="danger-full-access requires explicit Bypass"):
            build_thread_fork_params(
                thread_id="source-thread",
                cwd="/repo",
                permission_mode="auto",
            )
    finally:
        contract.codex_permission_profile = original


@pytest.mark.parametrize(
    ("mode", "approval", "reviewer", "sandbox"),
    [
        (
            "bypassPermissions",
            "never",
            "user",
            {"type": "dangerFullAccess"},
        ),
        (
            "plan",
            "never",
            "user",
            {"type": "readOnly", "networkAccess": False},
        ),
        (
            # Auto is one of the two modes that carry egress, so the model can
            # fetch without raising an approval per network command.
            "auto",
            "on-request",
            "user",
            {
                "type": "workspaceWrite",
                "writableRoots": ["/repo"],
                "networkAccess": True,
            },
        ),
        (
            "default",
            "on-request",
            "user",
            {
                "type": "workspaceWrite",
                "writableRoots": ["/repo"],
                "networkAccess": False,
            },
        ),
    ],
)
def test_turn_start_carries_current_permission_profile(
    mode, approval, reviewer, sandbox
):
    params = build_turn_start_params(
        thread_id="th",
        text="continue",
        permission_mode=mode,
        cwd="/repo",
    )

    assert params["approvalPolicy"] == approval
    assert params["approvalsReviewer"] == reviewer
    assert params["sandboxPolicy"] == sandbox


def test_home_is_read_only_even_when_caller_requests_auto():
    home = PROTECTED_HOME_CWD

    thread = build_thread_params(
        cwd=home,
        model="gpt-5.6",
        permission_mode="auto",
    )
    turn = build_turn_start_params(
        thread_id="th",
        text="inspect only",
        permission_mode="auto",
        cwd=home,
    )

    assert thread["approvalPolicy"] == "never"
    assert thread["sandbox"] == "read-only"
    assert turn["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}


def test_native_plan_is_a_workflow_and_forces_read_only_without_replacing_policy():
    params = build_turn_start_params(
        thread_id="th",
        text="inspect and propose a plan",
        permission_mode="auto",
        workflow_mode="plan",
        cwd="/repo",
        model="gpt-5.6",
        effort="high",
    )

    assert params["approvalPolicy"] == "never"
    assert params["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": False,
    }
    assert params["collaborationMode"] == {
        "mode": "plan",
        "settings": {
            "model": "gpt-5.6",
            "reasoning_effort": "high",
            "developer_instructions": None,
        },
    }
    assert params["additionalContext"]["helios-policy"] == {
        "kind": "application",
        "value": CODEX_DEVELOPER_INSTRUCTIONS,
    }


def test_approval_prompt_and_responses_fail_closed_on_dismiss():
    payload = approval_question(
        "item/commandExecution/requestApproval",
        {"command": "make test", "cwd": "/repo", "reason": "verify"},
    )
    assert payload["allowOther"] is False
    assert payload["requireExplicitChoice"] is True
    assert "make test" in payload["questions"][0]["question"]
    assert interaction_response(
        "item/commandExecution/requestApproval", "Approve for session"
    ) == {"decision": "acceptForSession"}
    assert interaction_response("item/fileChange/requestApproval", None) == {
        "decision": "decline"
    }


def test_user_input_response_wraps_exact_answer_map():
    answers = {"language": {"answers": ["Python"]}}
    assert interaction_response("item/tool/requestUserInput", answers) == {
        "answers": answers
    }


def test_permission_grant_response_preserves_requested_profile_and_scope():
    requested = {"network": {"enabled": True}}
    assert interaction_response(
        "item/permissions/requestApproval",
        "Approve for session",
        {"permissions": requested},
    ) == {"permissions": requested, "scope": "session"}
    assert interaction_response(
        "item/permissions/requestApproval",
        None,
        {"permissions": requested},
    ) == {"permissions": {}, "scope": "turn"}


def test_permission_prompt_discloses_requested_grants_without_cancel_label():
    payload = approval_question(
        "item/permissions/requestApproval",
        {
            "cwd": "/repo",
            "reason": "Download build metadata",
            "permissions": {
                "network": {"enabled": True},
                "fileSystem": {
                    "entries": [
                        {
                            "access": "write",
                            "path": {"type": "path", "path": "/outside/output"},
                        },
                        {
                            "access": "read",
                            "path": {
                                "type": "special",
                                "value": {"kind": "unknown", "path": "/mounted/data"},
                            },
                        },
                    ]
                },
            },
        },
    )

    question = payload["questions"][0]
    assert "Network: enabled" in question["question"]
    assert "Write: /outside/output" in question["question"]
    assert "Read: unknown: /mounted/data" in question["question"]
    assert "Download build metadata" in question["question"]
    labels = [option["label"] for option in question["options"]]
    assert labels == ["Approve once", "Approve for session", "Decline"]


def test_rate_limit_snapshot_flattens_without_losing_percent():
    rows = rate_limit_rows(
        {
            "limitId": "codex",
            "primary": {
                "usedPercent": 72,
                "windowDurationMins": 300,
                "resetsAt": 123,
            },
            "secondary": {"usedPercent": 9},
        }
    )
    assert rows[0]["rateLimitType"] == "codex_primary"
    assert rows[0]["usedPercent"] == 72
    assert rows[1]["rateLimitType"] == "codex_secondary"


def test_rate_limit_rows_label_windows_from_duration_not_limit_id():
    """The meter must never render the opaque backend limit id.

    `limitId` is a nullable free-form string in the 0.149.1 schema
    (v2/AccountRateLimitsUpdatedNotification.json), so a label table keyed on
    it shows `codex_primary` the moment OpenAI renames a limit.
    """

    rows = rate_limit_rows(
        {
            "limitId": "gpt-5-codex-max-2026",
            "limitName": "ChatGPT Pro",
            "primary": {"usedPercent": 72, "windowDurationMins": 300},
            "secondary": {"usedPercent": 9, "windowDurationMins": 10080},
        }
    )
    assert rows[0]["label"] == "ChatGPT Pro · 5-hour usage"
    assert rows[1]["label"] == "ChatGPT Pro · Weekly usage"
    # The routing key stays the id — only the rendered label is humanized.
    assert rows[0]["rateLimitType"] == "gpt-5-codex-max-2026_primary"


def test_rate_limit_rows_label_falls_back_when_cli_sends_neither_field():
    rows = rate_limit_rows({"primary": {"usedPercent": 5}})
    assert rows[0]["label"] == "Codex primary window"


def test_humanize_window_mins_covers_the_units_the_schema_allows():
    assert humanize_window_mins(60) == "Hourly usage"
    assert humanize_window_mins(300) == "5-hour usage"
    assert humanize_window_mins(1440) == "Daily usage"
    assert humanize_window_mins(4320) == "3-day usage"
    assert humanize_window_mins(10080) == "Weekly usage"
    assert humanize_window_mins(20160) == "2-week usage"
    assert humanize_window_mins(15) == "15-minute usage"
    assert humanize_window_mins(None) == ""
    assert humanize_window_mins(0) == ""
    assert humanize_window_mins("nonsense") == ""


@pytest.mark.parametrize("thread_id", ["", "existing-thread"])
def test_bypass_start_and_resume_preserve_execution_controls(thread_id):
    params = build_thread_params(
        cwd="/repo", model="gpt-6-astra", permission_mode="bypassPermissions",
        thread_id=thread_id,
    )
    assert params["sandbox"] == "danger-full-access"
    assert params["approvalPolicy"] == "never"
    assert params["config"]["agents"] == {"enabled": False}
    assert params["config"]["tools"]["update_plan"]["enabled"] is True


def test_bypass_fork_uses_selected_permissions():
    params = build_thread_fork_params(
        thread_id="source-thread", cwd="/repo", permission_mode="bypassPermissions",
    )
    assert params["sandbox"] == "danger-full-access"
    assert params["approvalPolicy"] == "never"
    assert params["config"]["agents"] == {"enabled": False}


@pytest.mark.parametrize("cwd,workflow", [(PROTECTED_HOME_CWD, "default"), ("/repo", "plan")])
def test_home_and_plan_still_narrow_selected_bypass(cwd, workflow):
    common = dict(cwd=cwd, model="gpt-6-astra", permission_mode="bypassPermissions", workflow_mode=workflow)
    thread = build_thread_params(**common)
    turn = build_turn_start_params(thread_id="thread", text="continue", **common)
    assert thread["sandbox"] == "read-only"
    assert turn["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn["collaborationMode"]["mode"] == workflow
    assert turn["approvalPolicy"] == "never"
