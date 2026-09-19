from __future__ import annotations

import json

import pytest

from helios.backend.router_policy import (
    CANARY_PROFILE,
    TaskSpecError,
    compile_messages,
    decide_route,
    normalize_task_spec,
    validate_result_content,
)


def _task(**overrides):
    value = {
        "contract_version": 1,
        "objective": "Classify each supplied line.",
        "task_kind": "classify",
        "deliverable": {"description": "Labels", "format": "json"},
        "context_refs": [{"kind": "inline", "ref": "one\ntwo"}],
        "constraints": ["Use only A or B."],
        "capability_hints": {
            "workspace": "none",
            "network": False,
            "tools": [],
            "multimodal": False,
            "minimum_context_tokens": 0,
        },
        "quality_tier": "quick",
        "independence": "same_family_ok",
        "wait_mode": "bounded",
    }
    value.update(overrides)
    return value


def test_canary_route_requires_explicit_enable_and_health():
    task = normalize_task_spec(_task())
    assert decide_route(
        task,
        enabled=False,
        profile_ready=True,
        profile_promoted=False,
        endpoint_verified=False,
        trusted_context=False,
    ).reason_codes == (
        "ROUTING_DISABLED",
    )
    assert decide_route(
        task,
        enabled=True,
        profile_ready=False,
        profile_promoted=False,
        endpoint_verified=False,
        trusted_context=False,
    ).reason_codes == (
        "NO_ELIGIBLE_PROFILE",
    )
    decision = decide_route(
        task,
        enabled=True,
        profile_ready=True,
        profile_promoted=False,
        endpoint_verified=False,
        trusted_context=False,
    )
    assert decision.disposition == "shadow_candidate"
    assert decision.reason_codes == (
        "TRUSTED_CONTEXT_COMPILER_REQUIRED",
        "PAIRED_QUALITY_EVAL_REQUIRED",
        "ENDPOINT_VARIANT_PIN_REQUIRED",
    )
    decision = decide_route(
        task,
        enabled=True,
        profile_ready=True,
        profile_promoted=True,
        endpoint_verified=True,
        trusted_context=True,
    )
    assert decision.disposition == "delegate"
    assert decision.profile == CANARY_PROFILE


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"task_kind": "review"}, "TASK_KIND_UNQUALIFIED"),
        ({"quality_tier": "critic"}, "QUALITY_TIER_UNQUALIFIED"),
        ({"independence": "different_vendor"}, "INDEPENDENCE_UNQUALIFIED"),
        ({"wait_mode": "async"}, "ASYNC_SUPERVISOR_NOT_AVAILABLE"),
        (
            {"context_refs": [{"kind": "workspace_path", "ref": "/repo"}]},
            "CONTEXT_COMPILER_REQUIRED",
        ),
        (
            {
                "capability_hints": {
                    "workspace": "none",
                    "network": True,
                    "tools": [],
                    "multimodal": False,
                    "minimum_context_tokens": 0,
                }
            },
            "SEARCH_PIPELINE_NOT_AVAILABLE",
        ),
    ],
)
def test_unqualified_work_stays_primary(patch, reason):
    decision = decide_route(
        normalize_task_spec(_task(**patch)),
        enabled=True,
        profile_ready=True,
        profile_promoted=True,
        endpoint_verified=True,
        trusted_context=True,
    )
    assert decision.disposition == "retain_primary"
    assert decision.reason_codes == (reason,)


def test_task_contract_rejects_unknown_fields_and_secrets():
    with pytest.raises(TaskSpecError, match="unknown task fields"):
        normalize_task_spec(_task(model_id="not caller controlled"))
    with pytest.raises(TaskSpecError) as caught:
        normalize_task_spec(
            _task(context_refs=[{"kind": "inline", "ref": "API_KEY=" + "sk-secretsecret123456"}])
        )
    assert caught.value.code == "SECRETS_SCOPE"


def test_prompt_marks_context_untrusted_and_json_output_is_validated():
    task = normalize_task_spec(_task())
    system, user = compile_messages(task)
    assert "untrusted data" in system
    assert "valid JSON" in system
    assert json.loads(user)["context"] == ["one\ntwo"]
    assert validate_result_content(task, '{"labels":["A","B"]}') == [
        "non_empty",
        "valid_json",
    ]
    with pytest.raises(TaskSpecError) as caught:
        validate_result_content(task, "not json")
    assert caught.value.code == "OUTPUT_VALIDATION_FAILED"


def test_output_schema_remains_primary_until_strict_validator_lands():
    task = normalize_task_spec(
        _task(
            deliverable={
                "description": "Labels",
                "format": "json",
                "output_schema": {"type": "object"},
            }
        )
    )
    decision = decide_route(
        task,
        enabled=True,
        profile_ready=True,
        profile_promoted=True,
        endpoint_verified=True,
        trusted_context=True,
    )
    assert decision.reason_codes == ("STRICT_SCHEMA_NOT_AVAILABLE",)


def test_minimum_context_requirement_and_nonstandard_json_fail_closed():
    task = normalize_task_spec(
        _task(
            capability_hints={
                "workspace": "none",
                "network": False,
                "tools": [],
                "multimodal": False,
                "minimum_context_tokens": 300_000,
            }
        )
    )
    decision = decide_route(
        task,
        enabled=True,
        profile_ready=True,
        profile_promoted=True,
        endpoint_verified=True,
        trusted_context=True,
    )
    assert decision.reason_codes == ("CONTEXT_CAPACITY_UNQUALIFIED",)

    with pytest.raises(TaskSpecError) as caught:
        validate_result_content(normalize_task_spec(_task()), '{"value":NaN}')
    assert caught.value.code == "OUTPUT_VALIDATION_FAILED"


def test_task_contract_requires_explicit_capability_and_independence_fields():
    missing_capability = _task()
    missing_capability.pop("capability_hints")
    with pytest.raises(TaskSpecError, match="missing task fields"):
        normalize_task_spec(missing_capability)

    partial_capability = _task()
    partial_capability["capability_hints"] = {"workspace": "none"}
    with pytest.raises(TaskSpecError, match="declare every capability"):
        normalize_task_spec(partial_capability)
