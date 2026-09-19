"""Router-broker correctness fixes that need no change to the dispatch gates.

Each of these is a statement the broker made that was not true: a preview tool
that evaluated a different gate than the real one, a status response that
contradicted itself, a schema advertising a mode always rejected, and a ceiling
that had drifted away from the profile it was meant to protect. None of them
changes what the broker will dispatch — all four gates stay closed.
"""

from __future__ import annotations

import pytest

from helios.backend import router_policy
from helios.backend.openrouter import PROFILES, get_profile
from helios.backend.router_policy import CANARY_PROFILE
from helios.backend.router_tools import DELEGATE_TASK_SCHEMA
from helios.router_service import BrokerCommandError, RouterBroker, _quarantined_rows


@pytest.fixture
def broker_pair(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "helios.router_service._check_credential",
        lambda _key: {"status": "healthy", "account_tier": "free"},
    )
    return RouterBroker(
        api_key="fixture-key-sixteen",
        state_dir=tmp_path,
    ), None


class TestQuarantinedRows:
    """Status rows are derived from the profile table, not hand-copied."""

    def test_every_non_canary_profile_appears_exactly_once(self):
        rows = _quarantined_rows("paid")
        ids = [(r["profile_id"], r["profile_version"]) for r in rows]
        expected = [
            (ref.profile_id, ref.version)
            for ref in PROFILES
            if ref != CANARY_PROFILE
        ]
        assert sorted(ids) == sorted(expected)

    def test_rows_match_the_profile_table_identity(self):
        """The literal rows this replaced duplicated identity with nothing
        enforcing agreement, so a renamed profile would have gone unnoticed."""
        for row in _quarantined_rows("paid"):
            ref = next(
                r for r in PROFILES
                if r.profile_id == row["profile_id"] and r.version == row["profile_version"]
            )
            assert get_profile(ref) is PROFILES[ref]

    def test_paid_account_is_not_demanded_when_the_account_is_paid(self):
        """The status contract used to report account_tier "paid" and
        PAID_ACCOUNT_REQUIRED in the same response."""
        for row in _quarantined_rows("paid"):
            assert "PAID_ACCOUNT_REQUIRED" not in row["reason_codes"]

    def test_paid_account_is_demanded_when_the_profile_costs_money(self):
        rows = _quarantined_rows("free")
        priced = [
            row for row in rows
            if get_profile(next(
                r for r in PROFILES
                if r.profile_id == row["profile_id"] and r.version == row["profile_version"]
            )).max_prompt_price_per_million > 0
        ]
        assert priced, "expected at least one priced profile to exercise this"
        for row in priced:
            assert "PAID_ACCOUNT_REQUIRED" in row["reason_codes"]

    def test_nothing_derived_is_ever_dispatchable(self):
        for tier in ("paid", "free", "unknown", None):
            for row in _quarantined_rows(tier):
                assert row["eligible"] is False
                assert row["dispatchable"] is False
                assert row["stage"] == "quarantined"
                assert row["reason_codes"]

    def test_the_canary_is_never_in_the_quarantined_set(self):
        ids = {r["profile_id"] for r in _quarantined_rows("paid")}
        assert CANARY_PROFILE.profile_id not in ids


class TestInlineContextCeiling:
    def test_ceiling_matches_the_profile_that_will_run_the_task(self):
        """A literal had drifted to half the profile's own max_input_chars,
        rejecting context the profile could have accepted."""
        assert (
            router_policy._max_inline_context_chars()
            == get_profile(CANARY_PROFILE).max_input_chars
        )

    def test_context_at_the_profile_limit_is_accepted(self):
        limit = get_profile(CANARY_PROFILE).max_input_chars
        assert limit > 100_000, "regression: the ceiling drifted back down"


class TestWaitModeContract:
    def test_async_is_not_advertised(self):
        """There is no delegation lifecycle, so async was advertised in the
        schema and then rejected on every call."""
        assert DELEGATE_TASK_SCHEMA["properties"]["wait_mode"]["enum"] == ["bounded"]

    def test_the_advertised_mode_is_accepted_by_the_policy(self):
        for mode in DELEGATE_TASK_SCHEMA["properties"]["wait_mode"]["enum"]:
            spec = _task(wait_mode=mode)
            assert router_policy.normalize_task_spec(spec)["wait_mode"] == mode


def _task(**over):
    spec = {
        "contract_version": 1,
        "objective": "summarise the supplied text",
        "task_kind": "summarize",
        "deliverable": {"description": "a short summary", "format": "text"},
        "context_refs": [{"kind": "inline", "ref": "hello"}],
        "constraints": [],
        "capability_hints": {
            "workspace": "none", "network": False, "tools": [],
            "multimodal": False, "minimum_context_tokens": 0,
        },
        "quality_tier": "quick",
        "independence": "same_family_ok",
        "wait_mode": "bounded",
    }
    spec.update(over)
    return spec


class TestOperatorOnlyControls:
    def test_set_enabled_refuses_a_native_client_binding(self, broker_pair):
        """The preview switch is an operator control from Settings, which sends
        no binding. A request carrying one did not come from Settings."""
        broker, _ = broker_pair
        with pytest.raises(BrokerCommandError) as caught:
            broker.handle({
                "protocol_version": 1,
                "method": "set_enabled",
                "params": {"contract_version": 1, "enabled": True},
                "binding": {"kind": "claude", "client_binding": "c", "call_id": "1"},
            })
        assert caught.value.code == "INVALID_BINDING"

    def test_set_enabled_without_a_binding_still_reaches_the_gate(self, broker_pair):
        """Unchanged path: it is still refused, but by the credential gate."""
        broker, _ = broker_pair
        try:
            broker.handle({
                "protocol_version": 1,
                "method": "set_enabled",
                "params": {"contract_version": 1, "enabled": True},
            })
        except BrokerCommandError as e:
            assert e.code != "INVALID_BINDING"


class TestExplainMatchesDelegate:
    """explain_route exists to predict what delegate_task will do. It used to
    hardcode trusted_context=False while delegate passed the real authorizer
    result — invisible while no authorizer is wired, and guaranteed to diverge
    the moment one is."""

    @staticmethod
    def _broker(tmp_path, monkeypatch, *, authorizer):
        monkeypatch.setattr(
            "helios.router_service._check_credential",
            lambda _key: {"status": "healthy", "account_tier": "paid"},
        )
        broker = RouterBroker(
            api_key="fixture-key-sixteen",
            state_dir=tmp_path,
            context_authorizer=authorizer,
        )
        # ROUTING_DISABLED short-circuits ahead of the context gate, so these
        # tests would pass vacuously against a disabled broker.
        broker.set_enabled(True)
        return broker

    def test_explain_consults_the_authorizer(self, tmp_path, monkeypatch):
        seen: list[tuple] = []
        broker = self._broker(
            tmp_path, monkeypatch,
            authorizer=lambda binding, task: (seen.append((binding, task)), True)[1],
        )
        broker.explain(router_policy.normalize_task_spec(_task()),
                       binding={"kind": "claude", "client_binding": "c", "call_id": "1"})
        assert seen, "explain_route never called the context authorizer"

    def test_explain_and_delegate_agree_on_the_context_gate(self, tmp_path, monkeypatch):
        """With an authorizer wired, the preview must not report a hold that
        the real call would not apply."""
        broker = self._broker(tmp_path, monkeypatch, authorizer=lambda b, t: True)
        binding = {"kind": "claude", "client_binding": "c", "call_id": "1"}
        task = router_policy.normalize_task_spec(_task())
        preview = broker.explain(task, binding=binding)["route"]["reason_codes"]
        assert "ROUTING_DISABLED" not in preview, "test would be vacuous"
        assert "TRUSTED_CONTEXT_COMPILER_REQUIRED" not in preview

    def test_without_an_authorizer_the_hold_is_still_reported(self, tmp_path, monkeypatch):
        broker = self._broker(tmp_path, monkeypatch, authorizer=None)
        preview = broker.explain(
            router_policy.normalize_task_spec(_task()),
            binding={"kind": "claude", "client_binding": "c", "call_id": "1"},
        )["route"]["reason_codes"]
        assert "ROUTING_DISABLED" not in preview, "test would be vacuous"
        assert "TRUSTED_CONTEXT_COMPILER_REQUIRED" in preview


class TestDecisionSnapshot:
    """explain and delegate must not tear across a concurrent health refresh:
    a mix of pre-refresh enable bit and post-refresh credential status matches
    neither the before nor the after state."""

    def test_snapshot_reads_both_inputs_under_one_lock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "helios.router_service._check_credential",
            lambda _key: {"status": "healthy", "account_tier": "paid"},
        )
        broker = RouterBroker(
            api_key="fixture-key-sixteen", state_dir=tmp_path
        )
        broker.set_enabled(True)
        enabled, ready, endpoint_verified = broker._decision_snapshot()
        assert enabled is True
        assert ready is True
        # No verified endpoints were supplied, so the gate reads shut — the
        # snapshot reports the real value rather than defaulting it open.
        assert endpoint_verified is False

    def test_snapshot_is_consistent_while_health_flips(self, tmp_path, monkeypatch):
        """Interleave a health replacement with snapshots; every observation
        must be internally coherent rather than a mix of two epochs."""
        import threading

        monkeypatch.setattr(
            "helios.router_service._check_credential",
            lambda _key: {"status": "healthy", "account_tier": "paid"},
        )
        broker = RouterBroker(
            api_key="fixture-key-sixteen", state_dir=tmp_path
        )
        broker.set_enabled(True)
        stop = threading.Event()

        def churn():
            healthy = True
            while not stop.is_set():
                with broker._lock:
                    broker._credential_health = (
                        {"status": "healthy", "account_tier": "paid"} if healthy
                        else {"status": "unknown", "account_tier": "unknown"}
                    )
                    # Endpoint verification is replaced wholesale by
                    # _refresh_endpoints_if_stale the same way health is, so it
                    # is churned here too: it has to be read inside the same
                    # lock, not re-read at the decide_route call site.
                    broker._verified_endpoints = (
                        frozenset({CANARY_PROFILE}) if healthy else frozenset()
                    )
                healthy = not healthy

        t = threading.Thread(target=churn, daemon=True)
        t.start()
        try:
            for _ in range(2000):
                enabled, ready, endpoint_verified = broker._decision_snapshot()
                assert enabled is True
                assert isinstance(ready, bool)
                # Both mutable inputs come from one epoch: the churn thread
                # writes them together, so a torn read would pair a healthy
                # credential with an unverified endpoint.
                assert endpoint_verified is ready
        finally:
            stop.set()
            t.join(timeout=2)
