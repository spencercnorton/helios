"""The specialty registry: real contracts, and still non-dispatchable.

Two things this guards. First, that every entry is a *complete* contract rather
than a summary — the previous version of this surface was five inert dicts with
no schemas and no verifiers, which is worse than nothing because it advertises
capability that does not exist. Second, that nothing here quietly becomes
dispatchable: the broker's gates are independent, and discovery must never read
as authorization.
"""

from __future__ import annotations

import json

import pytest

from helios.backend import specialty_registry as registry
from helios.backend import specialty_verifiers


class TestContractCompleteness:
    def test_every_tool_has_both_schemas_and_a_real_verifier(self):
        """A contract naming a verifier that does not exist is exactly the
        hollow entry this module replaced."""
        for tool_id in registry.tool_ids():
            contract = registry.get(tool_id)
            assert contract["input_schema"]["type"] == "object"
            assert contract["output_schema"]["type"] == "object"
            verifier = contract["verification"]["verifier"]
            assert callable(getattr(specialty_verifiers, verifier))
            assert contract["verification"]["host_inputs"], (
                f"{tool_id} declares no host-supplied evidence, so its verifier "
                "cannot check anything"
            )

    def test_input_schemas_are_closed(self):
        for tool_id in registry.tool_ids():
            schema = registry.get(tool_id)["input_schema"]
            assert schema["additionalProperties"] is False

    def test_draft_metadata_does_not_claim_benchmark_support(self):
        """No controlled study exists for commit messages or docstrings. The
        registry must say so rather than implying parity."""
        evidence = registry.get("draft_metadata")["parity_evidence"].lower()
        assert "no controlled" in evidence
        assert "pragmatics" in evidence

    def test_contracts_are_json_serializable(self):
        for tool_id in registry.tool_ids():
            json.dumps(registry.get(tool_id))


class TestEvidenceBeforeAnswer:
    """Constrained decoding that commits to an answer before its justification
    measurably degrades reasoning. Field order is a quality parameter here, not
    cosmetics — and a serializer that re-sorts keys would silently undo it."""

    def test_digest_orders_span_and_quote_before_the_claim(self):
        item = registry.get("digest_source")["output_schema"]["properties"]["claims"]["items"]
        assert list(item["properties"]) == ["lines", "quote", "claim"]
        assert item["required"] == ["lines", "quote", "claim"]

    def test_classification_orders_evidence_before_the_label(self):
        item = registry.get("classify_closed")["output_schema"]["properties"]["labels"]["items"]
        assert list(item["properties"]) == ["evidence_spans", "label_id"]
        assert item["required"] == ["evidence_spans", "label_id"]

    def test_localization_orders_explanation_before_locations(self):
        schema = registry.get("localize_code")["output_schema"]
        assert list(schema["properties"]) == ["explanation", "files"]

    def test_classification_orders_the_reason_before_the_abstain_decision(self):
        """`reason` exists solely to justify `abstain`, so it precedes it."""
        schema = registry.get("classify_closed")["output_schema"]
        assert list(schema["properties"]) == ["labels", "reason", "abstain"]

    @pytest.mark.parametrize("tool_id", sorted(registry.tool_ids()))
    def test_no_output_schema_puts_a_conclusion_before_its_evidence(self, tool_id):
        """Locks the ordering for every contract, including ones added later —
        the nested-items tests above would not catch a new verdict-first
        top-level schema."""
        schema = registry.get(tool_id)["output_schema"]
        order = list(schema.get("properties", {}))
        for conclusion, evidence in (
            ("claim", "quote"), ("claim", "lines"),
            ("label_id", "evidence_spans"),
            ("abstain", "reason"),
            ("files", "explanation"),
        ):
            if conclusion in order and evidence in order:
                assert order.index(evidence) < order.index(conclusion), (
                    f"{tool_id}: {conclusion!r} precedes its evidence {evidence!r}"
                )


class TestNonDispatchable:
    def test_no_tool_is_eligible_or_dispatchable(self):
        for tool_id in registry.tool_ids():
            contract = registry.get(tool_id)
            assert contract["eligible"] is False
            assert contract["dispatchable"] is False
            assert contract["stage"] == "shadow"
            assert contract["reason_codes"]

    def test_discovery_rows_are_never_eligible(self):
        for row in registry.summaries():
            assert row["eligible"] is False

    def test_no_contract_leaks_a_model_or_credential(self):
        """Discovery reveals a shape, never a capability or a vendor."""
        for tool_id in registry.tool_ids():
            rendered = json.dumps(registry.get(tool_id)).lower()
            for leak in ("api_key", "sk-or-", "openrouter.ai", "bearer"):
                assert leak not in rendered

    def test_no_contract_names_a_vendor_or_model(self):
        """get_specialty_tool's own description promises provider-neutrality.
        parity_evidence is returned verbatim, so a benchmark baseline naming a
        vendor would quietly break that promise."""
        for tool_id in registry.tool_ids():
            rendered = json.dumps(registry.get(tool_id)).lower()
            for vendor in ("gpt-", "claude", "gemini", "deepseek", "qwen",
                           "openai", "anthropic", "mistral", "llama"):
                assert vendor not in rendered, f"{tool_id} names {vendor!r}"


class TestLookup:
    def test_get_returns_none_for_an_unknown_tool(self):
        assert registry.get("nope") is None
        assert registry.get("") is None

    def test_get_returns_a_copy_callers_cannot_corrupt(self):
        first = registry.get("digest_source")
        first["input_schema"]["properties"].clear()
        assert registry.get("digest_source")["input_schema"]["properties"]

    def test_search_ranks_by_term_overlap(self):
        assert registry.search("classify taxonomy labels")[0]["tool_id"] == "classify_closed"
        assert registry.search("commit message docstring")[0]["tool_id"] == "draft_metadata"
        assert registry.search("which files should I read")[0]["tool_id"] == "localize_code"

    def test_search_is_deterministic_for_an_unmatched_query(self):
        first = registry.search("zzzz nothing matches")
        assert first == registry.search("zzzz nothing matches")
        assert [row["tool_id"] for row in first] == sorted(registry.tool_ids())[: len(first)]

    def test_search_honors_the_limit(self):
        assert len(registry.search("tool", limit=2)) == 2
        assert registry.search("tool", limit=0) == []
