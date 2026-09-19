from __future__ import annotations

import json
import stat
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from helios.backend.openrouter import (
    EndpointRef,
    InferenceResult,
    RouteReceipt,
    UsageReceipt,
)
from helios.backend.router_policy import CANARY_PROFILE
from helios.router_service import (
    BrokerCommandError,
    RouterBroker,
    _prepare_socket_directory,
)


def _task():
    return {
        "contract_version": 1,
        "objective": "Summarize the supplied note.",
        "task_kind": "summarize",
        "deliverable": {"description": "Short summary", "format": "text"},
        "context_refs": [{"kind": "inline", "ref": "A bounded note."}],
        "constraints": [],
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


def _result(request_id):
    return InferenceResult(
        request_id=request_id,
        content="A short bounded summary.",
        finish_reason="stop",
        native_finish_reason=None,
        route=RouteReceipt(
            profile=CANARY_PROFILE,
            catalog_snapshot="snapshot",
            requested_model="nvidia/nemotron-3.5-lightning:free",
            actual_model="nvidia/nemotron-3.5-lightning:free",
            actual_endpoint_model="nvidia/nemotron-3.5-lightning:free",
            endpoint=EndpointRef("nvidia", "nvfp4", ("Nvidia", "NVIDIA")),
            actual_provider="Nvidia",
            endpoint_confirmed=True,
            generation_id="gen-1",
            gateway_attempts=1,
            router_strategy="direct",
            router_attempt=1,
            router_region=None,
            pipeline_stages=(),
            request_digest="sha256:request",
            started_at="2026-07-24T00:00:00Z",
            completed_at="2026-07-24T00:00:01Z",
            require_parameters=True,
            data_collection="deny",
            zdr=True,
            provider_fallbacks=False,
            cross_model_fallback=False,
            tools_enabled=False,
            response_cache_enabled=False,
            context_compression_enabled=False,
        ),
        usage=UsageReceipt(10, 5, 15, None, None, None, Decimal("0"), True),
    )


class FakeGateway:
    def __init__(self):
        self.requests = []

    def infer(self, request, *, api_key, cancellation=None):
        assert api_key == "fixture-key-sixteen"
        assert cancellation is not None
        self.requests.append(request)
        return _result(request.request_id)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "helios.router_service._check_credential",
        lambda _key: {"status": "healthy", "account_tier": "free"},
    )
    gateway = FakeGateway()
    value = RouterBroker(
        api_key="fixture-key-sixteen",
        state_dir=tmp_path,
        gateway=gateway,
    )
    return value, gateway


@pytest.fixture
def promoted_broker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "helios.router_service._check_credential",
        lambda _key: {"status": "healthy", "account_tier": "free"},
    )
    gateway = FakeGateway()
    value = RouterBroker(
        api_key="fixture-key-sixteen",
        state_dir=tmp_path,
        gateway=gateway,
        promoted_profiles=frozenset({CANARY_PROFILE}),
        verified_endpoints=frozenset({CANARY_PROFILE}),
        context_authorizer=lambda _binding, _task_value: True,
    )
    return value, gateway


def _binding():
    return {
        "kind": "codex",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "call_id": "call-1",
    }


def test_socket_directory_is_owned_by_client_group(tmp_path, monkeypatch):
    socket_path = tmp_path / "runtime" / "router.sock"
    chown_calls = []
    monkeypatch.setattr(
        "helios.router_service.grp.getgrnam",
        lambda name: (
            SimpleNamespace(gr_gid=4242) if name == "desktop" else None
        ),
    )
    monkeypatch.setattr(
        "helios.router_service.os.chown",
        lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)),
    )

    group_id = _prepare_socket_directory(socket_path, "desktop")

    assert group_id == 4242
    assert chown_calls == [(socket_path.parent, -1, 4242)]
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o750


def test_broker_defaults_off_and_persists_explicit_enable(broker):
    value, _gateway = broker
    assert value.status()["enabled"] is False
    status = value.set_enabled(True)
    assert status["enabled"] is True
    assert status["profiles"][0]["dispatchable"] is False
    assert status["profiles"][0]["stage"] == "manual_evaluation"
    assert "PAIRED_QUALITY_EVAL_REQUIRED" in status["profiles"][0]["reason_codes"]
    assert status["control_policy_id"] == "shadow-preview.v3"


@pytest.mark.parametrize("old_policy", ["shadow-preview.v1", "shadow-preview.v2", "future-active.v1"])
def test_preview_state_cannot_cross_control_policy_schema(tmp_path, monkeypatch, old_policy):
    monkeypatch.setattr(
        "helios.router_service._check_credential",
        lambda _key: {"status": "healthy", "account_tier": "free"},
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "control_policy_id": old_policy,
                "enabled": True,
                "policy_epoch": 99,
            }
        )
    )

    value = RouterBroker(
        api_key="fixture-key-sixteen",
        state_dir=tmp_path,
        gateway=FakeGateway(),
    )

    assert value.status()["enabled"] is False
    assert value.status()["policy_epoch"] == 0


def test_delegate_is_fail_closed_until_enabled(broker):
    value, gateway = broker
    with pytest.raises(BrokerCommandError) as caught:
        value.handle(
            {
                "protocol_version": 1,
                "method": "delegate_task",
                "params": _task(),
                "binding": _binding(),
            }
        )
    assert caught.value.code == "ROUTING_DISABLED"
    assert gateway.requests == []


def test_enabled_preview_cannot_dispatch_without_host_and_evaluation_gates(broker):
    value, gateway = broker
    value.set_enabled(True)
    with pytest.raises(BrokerCommandError) as caught:
        value.handle(
            {
                "protocol_version": 1,
                "method": "delegate_task",
                "params": _task(),
                "binding": _binding(),
            }
        )
    assert caught.value.code == "TRUSTED_CONTEXT_COMPILER_REQUIRED"
    assert gateway.requests == []


def test_delegate_returns_receipt_and_is_idempotent_in_process(promoted_broker):
    value, gateway = promoted_broker
    value.set_enabled(True)
    request = {
        "protocol_version": 1,
        "method": "delegate_task",
        "params": _task(),
        "binding": _binding(),
    }
    first = value.handle(request)
    second = value.handle(request)

    assert first == second
    assert len(gateway.requests) == 1
    assert first["accepted"] is True
    assert first["result"]["summary"] == "A short bounded summary."
    assert first["receipt_ref"].startswith("receipt:sha256:")
    receipt_text = value._receipt_path.read_text()
    assert "A bounded note" not in receipt_text
    assert "A short bounded summary" not in receipt_text
    assert "fixture-api-key" not in receipt_text
    ledger_bytes = value._ledger_path.read_bytes()
    assert b"A bounded note" not in ledger_bytes
    assert b"A short bounded summary" not in ledger_bytes
    assert b"fixture-api-key" not in ledger_bytes


def test_durable_acceptance_prevents_replay_after_restart(
    promoted_broker,
    monkeypatch,
):
    value, gateway = promoted_broker
    value.set_enabled(True)
    request = {
        "protocol_version": 1,
        "method": "delegate_task",
        "params": _task(),
        "binding": _binding(),
    }
    value.handle(request)
    assert len(gateway.requests) == 1

    replacement_gateway = FakeGateway()
    restarted = RouterBroker(
        api_key="fixture-key-sixteen",
        state_dir=value._state_dir,
        gateway=replacement_gateway,
        promoted_profiles=frozenset({CANARY_PROFILE}),
        verified_endpoints=frozenset({CANARY_PROFILE}),
        context_authorizer=lambda _binding, _task_value: True,
    )
    with pytest.raises(BrokerCommandError) as caught:
        restarted.handle(request)
    assert caught.value.code == "IDEMPOTENT_REPLAY_UNAVAILABLE"
    assert replacement_gateway.requests == []


def test_native_call_identity_cannot_be_reused_with_different_task(promoted_broker):
    value, gateway = promoted_broker
    value.set_enabled(True)
    base = {
        "protocol_version": 1,
        "method": "delegate_task",
        "params": _task(),
        "binding": _binding(),
    }
    value.handle(base)
    changed = {**base, "params": {**_task(), "objective": "Different objective."}}

    with pytest.raises(BrokerCommandError) as caught:
        value.handle(changed)

    assert caught.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(gateway.requests) == 1


def test_disable_is_immediate_and_does_not_recheck_credential(broker, monkeypatch):
    value, _gateway = broker
    value.set_enabled(True)

    def unexpected_health(_key):
        raise AssertionError("disable must not call the network")

    monkeypatch.setattr("helios.router_service._check_credential", unexpected_health)
    status = value.set_enabled(False)

    assert status["enabled"] is False
    assert status["automatic_dispatch"] is False


def test_repeated_enable_does_not_repeat_credential_probe(broker, monkeypatch):
    value, _gateway = broker
    probes = 0

    def count_health(_key):
        nonlocal probes
        probes += 1
        return {"status": "healthy", "account_tier": "free"}

    monkeypatch.setattr("helios.router_service._check_credential", count_health)
    value.set_enabled(True)
    value.set_enabled(True)

    # The broker's fresh startup health is reused for a transition immediately
    # after launch, and an already-on preview never forces another probe.
    assert probes == 0


def test_binding_must_be_native_and_exact(broker):
    value, _gateway = broker
    with pytest.raises(BrokerCommandError) as caught:
        value.handle(
            {
                "protocol_version": 1,
                "method": "routing_status",
                "params": {"contract_version": 1},
                "binding": {"kind": "codex", "thread_id": "thread-1"},
            }
        )
    assert caught.value.code == "INVALID_BINDING"


def test_specialty_search_never_exposes_raw_models(broker):
    value, _gateway = broker
    result = value.handle(
        {
            "protocol_version": 1,
            "method": "search_specialty_tools",
            "params": {"contract_version": 1, "query": "research evidence"},
            "binding": _binding(),
        }
    )
    assert len(result["matches"]) <= 5
    rendered = str(result).lower()
    assert "inclusionai/" not in rendered
    assert "deepseek/" not in rendered


def test_get_specialty_tool_returns_a_complete_contract(broker):
    """Discovery is useless without the exact shape: a caller cannot honor an
    output contract it has only seen summarized."""
    value, _gateway = broker
    result = value.handle(
        {
            "protocol_version": 1,
            "method": "get_specialty_tool",
            "params": {"contract_version": 1, "tool_id": "digest_source"},
            "binding": _binding(),
        }
    )
    tool = result["tool"]
    assert tool["tool_id"] == "digest_source"
    assert tool["input_schema"]["additionalProperties"] is False
    assert tool["output_schema"]["type"] == "object"
    assert tool["verification"]["verifier"] == "verify_digest"
    # Discovery reveals a shape, never a capability.
    assert tool["eligible"] is False
    assert tool["dispatchable"] is False
    rendered = str(result).lower()
    assert "inclusionai/" not in rendered
    assert "deepseek/" not in rendered
    assert "api_key" not in rendered


def test_get_specialty_tool_rejects_an_unknown_id(broker):
    value, _gateway = broker
    with pytest.raises(BrokerCommandError) as caught:
        value.handle(
            {
                "protocol_version": 1,
                "method": "get_specialty_tool",
                "params": {"contract_version": 1, "tool_id": "does_not_exist"},
                "binding": _binding(),
            }
        )
    assert caught.value.code == "NOT_FOUND"


def test_get_specialty_tool_requires_a_binding(broker):
    """It is a model-facing tool, so it carries the same caller-binding
    requirement as every other one — discovery is not an unauthenticated
    side door into the broker."""
    value, _gateway = broker
    with pytest.raises(BrokerCommandError) as caught:
        value.handle(
            {
                "protocol_version": 1,
                "method": "get_specialty_tool",
                "params": {"contract_version": 1, "tool_id": "digest_source"},
                "binding": {"kind": "codex", "thread_id": "thread-1"},
            }
        )
    assert caught.value.code == "INVALID_BINDING"


def test_specialty_discovery_does_not_make_anything_dispatchable(broker):
    """The registry landing must not move the broker's gates. Every promotion
    gate is independent of tool discovery and must still be closed."""
    value, _gateway = broker
    status = value.status(refresh_health=False)
    assert status["automatic_dispatch"] is False
    assert status["execution_mode"] == "shadow_evaluation"
    assert all(not row["dispatchable"] for row in status["profiles"])


class TestEndpointVerificationRefresh:
    """Endpoint verification used to run once, in ``main()``.

    Two failures were unreachable from a running service. A pinned model can be
    retired upstream — ``free_canary.ling3_novita`` was, when OpenRouter dropped
    the ``:free`` variant — and because ``verify_endpoint`` is fail-closed, a
    catalog that was merely unreachable at boot shut the gate until somebody
    restarted the unit. Both rendered as the same reason code a held profile
    shows, so ``status()`` could not tell them apart.
    """

    def _broker(self, tmp_path, monkeypatch, verifier):
        monkeypatch.setattr(
            "helios.router_service._check_credential",
            lambda _key: {"status": "healthy", "account_tier": "free"},
        )
        return RouterBroker(
            api_key="fixture-key-sixteen",
            state_dir=tmp_path,
            gateway=FakeGateway(),
            endpoint_verifier=verifier,
        )

    def test_without_a_verifier_the_set_is_frozen_at_construction(
        self, tmp_path, monkeypatch
    ):
        """The path every other test relies on: no verifier, no refresh."""
        broker = self._broker(tmp_path, monkeypatch, None)
        broker._verified_endpoints = frozenset({CANARY_PROFILE})
        broker._endpoints_checked_at -= 10_000
        broker._refresh_endpoints_if_stale()
        assert broker._verified_endpoints == frozenset({CANARY_PROFILE})

    def test_a_pin_recovers_without_a_restart(self, tmp_path, monkeypatch):
        """The catalog was unreachable at boot; the next refresh confirms it."""
        # The constructor's default is the empty set — exactly what `main()`
        # hands over when the boot-time sweep could not reach the catalog.
        broker = self._broker(
            tmp_path, monkeypatch, lambda: frozenset({CANARY_PROFILE})
        )
        assert CANARY_PROFILE not in broker._verified_endpoints
        assert "ENDPOINT_VARIANT_PIN_REQUIRED" in broker.status()["profiles"][0][
            "reason_codes"
        ]

        broker._endpoints_checked_at -= 10_000
        status = broker.status()
        assert CANARY_PROFILE in broker._verified_endpoints
        assert "ENDPOINT_VARIANT_PIN_REQUIRED" not in status["profiles"][0][
            "reason_codes"
        ]

    def test_a_retired_pin_is_dropped_on_refresh(self, tmp_path, monkeypatch):
        """The mirror image: verified at boot, retired upstream afterwards."""
        broker = self._broker(tmp_path, monkeypatch, lambda: frozenset())
        broker._verified_endpoints = frozenset({CANARY_PROFILE})
        broker._endpoints_checked_at -= 10_000
        assert "ENDPOINT_VARIANT_PIN_REQUIRED" in broker.status()["profiles"][0][
            "reason_codes"
        ]

    def test_a_failing_verifier_fails_closed(self, tmp_path, monkeypatch):
        """Unverifiable is unverified — never 'keep the last good answer'."""

        def explode():
            raise RuntimeError("catalog unreachable")

        broker = self._broker(tmp_path, monkeypatch, explode)
        broker._verified_endpoints = frozenset({CANARY_PROFILE})
        broker._endpoints_checked_at -= 10_000
        broker._refresh_endpoints_if_stale()
        assert broker._verified_endpoints == frozenset()

    def test_a_fresh_answer_is_not_re_fetched(self, tmp_path, monkeypatch):
        """Bounded like the credential check: one catalog sweep per window."""
        calls = []

        def counting():
            calls.append(1)
            return frozenset({CANARY_PROFILE})

        broker = self._broker(tmp_path, monkeypatch, counting)
        calls.clear()
        broker.status()
        broker.status()
        assert calls == []

        broker._endpoints_checked_at -= 10_000
        broker.status()
        assert len(calls) == 1
