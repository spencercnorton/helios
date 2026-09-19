"""Per-endpoint route selection, pinning, and prompt caching.

A model slug is not a deployment. These tests care about the three things that
follow from that: the endpoint's own context budget (not the model-level
maximum), a pin that stays put for the session, and cache markup sent only
where the endpoint actually needs it — sending a content-parts array to an
endpoint that only accepts a plain string is a 400.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from helios.backend.openrouter import chat as or_chat
from helios.backend.openrouter import routes as or_routes


def endpoint(**over):
    """One endpoint, shaped like the live API actually returns them.

    Copied from a real GET /models/deepseek/deepseek-v4-flash/endpoints
    response, not from the docs. There is **no** `provider_slug` field — the
    pinnable identifier lives in `tag` ("deepinfra/fp4"), with `provider_name`
    for display. An invented fixture is how the empty-pin bug got past a green
    suite: every test passed while the pin was never sent.
    """
    base = {
        "name": "Parasail | deepseek/deepseek-v4-flash-20260423",
        "provider_name": "Parasail",
        "tag": "parasail/fp8",
        "model_id": "vendor/model",
        "context_length": 131_072,
        "max_completion_tokens": 8_192,
        "max_prompt_tokens": None,
        "quantization": "fp8",
        "status": 0,
        "uptime_last_30m": 99.9,
        "uptime_last_1d": 99.2,
        "supports_implicit_caching": False,
        "supported_parameters": ["tools", "tool_choice", "temperature", "max_tokens"],
        "pricing": {"prompt": "0.0000001", "completion": "0.0000002",
                    "input_cache_read": "0.00000001"},
    }
    base.update(over)
    return base


def choose(*endpoints):
    return or_routes._choose("vendor/model", list(endpoints))


class TestSelection:
    def test_prefers_an_endpoint_that_supports_tools(self):
        """A coding agent without tools is useless, and a cheaper endpoint that
        silently ignores `tools` is the worst outcome — it answers in prose."""
        no_tools = endpoint(tag="cheap", supported_parameters=["temperature"],
                            pricing={"prompt": "0.00000001"})
        with_tools = endpoint(tag="tooled")
        assert choose(no_tools, with_tools).provider_slug == "tooled"

    def test_prefers_a_caching_endpoint_over_a_marginally_cheaper_one(self):
        plain = endpoint(tag="plain", pricing={"prompt": "0.00000009"})
        caching = endpoint(tag="caching", supports_implicit_caching=True,
                           pricing={"prompt": "0.0000001", "input_cache_read": "0.000000001"})
        assert choose(plain, caching).provider_slug == "caching"

    def test_falls_back_to_price_when_all_else_is_equal(self):
        a = endpoint(tag="dear", pricing={"prompt": "0.000001"})
        b = endpoint(tag="cheap", pricing={"prompt": "0.0000001"})
        assert choose(a, b).provider_slug == "cheap"

    def test_skips_a_degraded_endpoint(self):
        degraded = endpoint(tag="degraded", status=-2,
                            pricing={"prompt": "0.00000001"})
        healthy = endpoint(tag="healthy")
        assert choose(degraded, healthy).provider_slug == "healthy"

    def test_skips_an_endpoint_below_the_uptime_floor(self):
        flaky = endpoint(tag="flaky", uptime_last_30m=40.0,
                         pricing={"prompt": "0.00000001"})
        assert choose(flaky, endpoint(tag="solid")).provider_slug == "solid"

    def test_a_missing_uptime_is_unknown_not_unhealthy(self):
        """The field is frequently absent. Treating null as zero would demote
        every endpoint that simply does not report it."""
        quiet = endpoint(tag="quiet", uptime_last_30m=None,
                         pricing={"prompt": "0.00000001"})
        assert choose(quiet, endpoint(tag="loud")).provider_slug == "quiet"

    def test_pins_a_degraded_endpoint_rather_than_going_unpinned(self):
        """If everything is degraded, an unpinned request would swap providers
        mid-session anyway — a stable degraded pin is the better failure."""
        only = endpoint(tag="sole", status=-2)
        assert choose(only).provider_slug == "sole"

    def test_no_endpoints_yields_no_route(self):
        assert choose() is None


class TestProviderSlug:
    """The bug a green unit suite missed: the live payload has no
    `provider_slug`, so the pin resolved to "" and was silently never sent."""

    def test_slug_comes_from_the_tag_prefix(self):
        assert choose(endpoint(tag="deepinfra/fp4")).provider_slug == "deepinfra"
        assert choose(endpoint(tag="deepseek")).provider_slug == "deepseek"

    def test_provider_name_is_the_display_name_not_the_slug(self):
        route = choose(endpoint(tag="deep-infra/fp4", provider_name="DeepInfra"))
        assert route.provider_slug == "deep-infra"
        assert route.provider_name == "DeepInfra"

    def test_falls_back_to_a_hyphenated_provider_name(self):
        route = choose(endpoint(tag=None, provider_name="Io Net"))
        assert route.provider_slug == "io-net"

    def test_an_unpinnable_endpoint_yields_no_route(self):
        """Better unpinned than pinned to the empty string, which is a silent
        no-op that looks like a working pin."""
        assert choose(endpoint(tag=None, provider_name=None)) is None


class TestBudget:
    def test_a_modest_output_ceiling_is_reserved_whole(self):
        """The endpoint's own maximum still clamps the reserve when it is
        smaller than the allowance, so this can never promise more output than
        the endpoint will produce."""
        route = choose(endpoint(context_length=131_072, max_completion_tokens=8_192))
        assert route.completion_reserve == 8_192
        assert route.prompt_budget == 131_072 - 8_192

    def test_prompt_budget_without_an_output_ceiling(self):
        route = choose(endpoint(context_length=64_000, max_completion_tokens=0))
        assert route.completion_reserve == 8_000  # ctx // 8
        assert route.prompt_budget == 56_000

    def test_a_huge_advertised_output_does_not_eat_the_window(self):
        """Measured 2026-09-03: `moonshotai/kimi-k3` is served by an endpoint
        advertising a 943,718-token maximum output on a 1,048,576 window.
        Reserving that left 10% of the window for the conversation, so
        compaction began at roughly 7% fill and never stopped."""
        route = choose(
            endpoint(context_length=1_048_576, max_completion_tokens=943_718)
        )
        assert route.completion_reserve == 131_072  # ctx // 8, not the ceiling
        assert route.prompt_budget == 917_504
        assert route.prompt_budget / route.context_length > 0.85

    def test_the_reserve_has_a_floor(self):
        """An eighth of a small window is not a usable reply allowance."""
        route = choose(endpoint(context_length=32_000, max_completion_tokens=0))
        assert route.completion_reserve == 4_096  # the floor, not ctx // 8
        assert route.prompt_budget == 27_904

    def test_the_floor_never_swallows_a_small_window(self):
        """Found by cross-model review, 2026-09-03. Without the half-window
        cap, a 4,095-token endpoint reserved all 4,095 for the reply: prompt
        budget zero, and `max_tokens` equal to the whole context on every
        request, so the model was permanently unusable through this driver.
        `openai/gpt-3.5-turbo-0613` is exactly that shape and declares tools,
        so it is reachable from the picker."""
        route = choose(endpoint(context_length=4_095, max_completion_tokens=0))
        assert route.completion_reserve == 2_047
        assert route.prompt_budget == 2_048
        assert 0 < route.completion_reserve < route.context_length

    @pytest.mark.parametrize("context_length", [1, 2, 100, 2_000, 4_095, 4_096, 8_191])
    def test_no_window_size_yields_a_zero_prompt_budget(self, context_length):
        route = choose(endpoint(context_length=context_length, max_completion_tokens=0))
        assert route.completion_reserve < context_length or context_length < 2
        assert route.prompt_budget > 0 or context_length < 2

    def test_unknown_context_length_yields_no_budget(self):
        route = choose(endpoint(context_length=None))
        assert route.context_length == 0
        assert route.completion_reserve == 0
        assert route.prompt_budget == 0


class TestCachingMode:
    def test_implicit_caching_is_taken_from_the_endpoint(self):
        route = choose(endpoint(supports_implicit_caching=True))
        assert route.implicit_caching is True
        assert route.caches_prompt is True

    @pytest.mark.parametrize("tag,explicit", [
        ("anthropic", True), ("google-vertex", True), ("qwen/fp8", True),
        ("parasail/fp8", False), ("deepinfra/fp4", False),
    ])
    def test_explicit_caching_is_vendor_derived(self, tag, explicit):
        assert choose(endpoint(tag=tag)).explicit_caching is explicit


class TestCacheFileCycle:
    def test_route_round_trips_through_the_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        payload = json.dumps({"data": {"endpoints": [endpoint()]}}).encode()

        class FakeResponse:
            status = 200
            def iter_bytes(self):
                yield payload
            def close(self):
                pass

        class FakeTransport:
            calls = 0
            def open(self, request, timeout_seconds=0):
                FakeTransport.calls += 1
                return FakeResponse()

        transport = FakeTransport()
        first = or_routes.route_for("vendor/model", transport=transport)
        assert first.provider_slug == "parasail"
        assert FakeTransport.calls == 1
        # Second call must be served from cache, not refetched.
        second = or_routes.route_for("vendor/model", transport=transport)
        assert second == first
        assert FakeTransport.calls == 1

    def test_a_stale_cache_entry_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        or_routes._write_cache({
            "vendor/model": {"fetched_at": time.time() - 99_999, "route": {}}
        })
        assert or_routes.cached_route("vendor/model") is None

    def test_a_cache_written_by_another_version_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        or_routes._write_cache({
            "vendor/model": {"fetched_at": time.time(), "route": {"unexpected": 1}}
        })
        assert or_routes.cached_route("vendor/model") is None

    def test_discovery_failure_is_not_fatal(self, tmp_path, monkeypatch):
        """An unroutable model must still be usable — unpinned, as before."""
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        with patch.object(or_routes, "_fetch_endpoints",
                          side_effect=or_routes.RouteError("http-404")):
            assert or_routes.route_for("vendor/model") is None

    def test_a_model_without_a_slash_is_never_routed(self):
        assert or_routes.route_for("not-an-openrouter-model") is None


class TestRequestBody:
    def _body(self, **kw):
        return json.loads(or_chat._build_body(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            model="vendor/model", tools=(), **kw))

    def test_pin_is_sent_when_a_route_resolved(self):
        provider = self._body(provider_only="parasail")["provider"]
        assert provider["only"] == ["parasail"]
        assert provider["order"] == ["parasail"]

    def test_no_pin_without_a_route(self):
        provider = self._body()["provider"]
        assert "only" not in provider
        assert provider["data_collection"] == "deny"

    def test_cache_control_marks_only_the_system_prefix(self):
        messages = self._body(cache_prefix=True)["messages"]
        assert messages[0]["content"] == [{
            "type": "text", "text": "sys",
            "cache_control": {"type": "ephemeral"},
        }]
        # The growing conversation is untouched: a breakpoint that moves
        # invalidates more than it saves.
        assert messages[1] == {"role": "user", "content": "hi"}

    def test_no_cache_control_by_default(self):
        """Sending a content-parts array to an endpoint that only accepts a
        plain string is a 400, so markup is opt-in per route."""
        assert self._body()["messages"][0]["content"] == "sys"

    def test_cache_control_does_not_mutate_the_caller_list(self):
        original = [{"role": "system", "content": "sys"}]
        or_chat._build_body(original, model="v/m", tools=(), cache_prefix=True)
        assert original[0]["content"] == "sys"

    def test_cache_prefix_is_a_no_op_without_a_system_message(self):
        body = json.loads(or_chat._build_body(
            [{"role": "user", "content": "hi"}], model="v/m", tools=(), cache_prefix=True))
        assert body["messages"] == [{"role": "user", "content": "hi"}]


class TestPolicyExclusion:
    """The endpoints API does not publish per-endpoint data policy, so an
    endpoint that `data_collection: "deny"` rejects is only discoverable by
    being told. Observed live: the deepseek endpoint was rejected and the
    session re-routed to alibaba and succeeded.
    """

    def test_excluded_provider_is_not_chosen_again(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        or_routes.exclude("vendor/model", "deepseek")
        chosen = or_routes._choose(
            "vendor/model",
            [endpoint(tag="deepseek"), endpoint(tag="alibaba")],
            excluded=or_routes.excluded_for("vendor/model"),
        )
        assert chosen.provider_slug == "alibaba"

    def test_exclusion_survives_a_cache_reread(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        or_routes.exclude("vendor/model", "deepseek")
        or_routes.exclude("vendor/model", "baidu")
        assert or_routes.excluded_for("vendor/model") == frozenset({"deepseek", "baidu"})

    def test_exclusion_invalidates_the_cached_choice(self, tmp_path, monkeypatch):
        """Otherwise the next lookup returns the very endpoint just rejected."""
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        or_routes._write_cache({"vendor/model": {
            "fetched_at": time.time(),
            "route": {"model": "vendor/model", "provider_slug": "deepseek",
                      "provider_name": "DeepSeek", "context_length": 1,
                      "max_completion_tokens": 0, "quantization": "",
                      "supports_tools": True, "supports_reasoning": False,
                      "implicit_caching": False,
                      "explicit_caching": False, "input_price": 0.0,
                      "cache_read_price": 0.0},
        }})
        assert or_routes.cached_route("vendor/model") is not None
        or_routes.exclude("vendor/model", "deepseek")
        assert or_routes.cached_route("vendor/model") is None

    def test_excluding_nothing_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        or_routes.exclude("", "deepseek")
        or_routes.exclude("vendor/model", "")
        assert or_routes.excluded_for("vendor/model") == frozenset()


class TestPolicyErrorClassification:
    @pytest.mark.parametrize("message", [
        "No endpoints found matching your data policy (Paid model training).",
        "No endpoints found that support tool use.",
    ])
    def test_policy_rejection_is_not_model_unavailable(self, message):
        """It must not look like "this model is gone" — the model exists and
        the caller can recover by re-routing."""
        err = or_chat._classify(404, None, message)
        assert err.kind is or_chat.ChatErrorKind.NO_ELIGIBLE_ENDPOINT

    def test_a_genuine_404_is_still_model_unavailable(self):
        err = or_chat._classify(404, None, "No such model")
        assert err.kind is or_chat.ChatErrorKind.MODEL_UNAVAILABLE


class TestNeverBlocksSessionStart:
    """`route_for` runs inside OpenRouterDriver.start(). Anything escaping it
    stops a session from starting — strictly worse than the unpinned request it
    is supposed to degrade to. So it is total, and the layers beneath it treat
    the payload as untyped."""

    @pytest.mark.parametrize("broken", [
        {"tag": "x", "supported_parameters": 7, "pricing": "nope"},
        {"tag": "y", "supported_parameters": None, "pricing": None},
        {"tag": "z", "pricing": {"prompt": {"nested": "object"}}},
        {"tag": "w", "supported_parameters": [None, 3, "tools"]},
        {"tag": "v", "context_length": "lots", "max_completion_tokens": []},
    ])
    def test_choose_survives_an_evolved_payload(self, broken):
        result = or_routes._choose("vendor/model", [broken])
        assert result is None or isinstance(result, or_routes.Route)

    def test_a_non_utf8_cache_is_a_miss_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        path = tmp_path / "openrouter-routes.json"
        path.write_bytes(b"\xff\xfe not utf-8 at all")
        assert or_routes.cached_route("vendor/model") is None

    def test_a_truncated_cache_is_a_miss_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        (tmp_path / "openrouter-routes.json").write_text('{"vendor/model": {"fet')
        assert or_routes.cached_route("vendor/model") is None

    def test_an_unexpected_exception_degrades_to_unpinned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
        with patch.object(or_routes, "_fetch_endpoints",
                          side_effect=RuntimeError("payload shape changed")):
            assert or_routes.route_for("vendor/model") is None

    def test_a_mid_response_transport_failure_becomes_a_route_error(self, tmp_path, monkeypatch):
        from helios.backend.openrouter.transport import TransportFailure, TransportFailureKind

        monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))

        class Dying:
            status = 200
            def iter_bytes(self):
                raise TransportFailure(TransportFailureKind.TIMEOUT)
                yield b""  # pragma: no cover
            def close(self):
                pass

        class Transport:
            def open(self, request, timeout_seconds=0):
                return Dying()

        with pytest.raises(or_routes.RouteError):
            or_routes._fetch_endpoints("vendor/model", transport=Transport())
        # and the public entry point still degrades rather than raising
        assert or_routes.route_for("vendor/model", transport=Transport()) is None


class TestReasoningCapability:
    def test_reasoning_support_comes_from_the_endpoint(self):
        with_reasoning = endpoint(
            supported_parameters=["tools", "tool_choice", "reasoning"])
        assert choose(with_reasoning).supports_reasoning is True
        assert choose(endpoint()).supports_reasoning is False

    def test_the_portable_parameter_is_checked_not_the_openai_one(self):
        """`reasoning` is declared by far more endpoints than the
        OpenAI-specific `reasoning_effort`."""
        only_openai = endpoint(
            supported_parameters=["tools", "tool_choice", "reasoning_effort"])
        assert choose(only_openai).supports_reasoning is False


class TestEffortMapping:
    @pytest.mark.parametrize("key,expected", [
        ("off", {"enabled": False}),
        ("low", {"effort": "low"}),
        ("medium", {"effort": "medium"}),
        ("high", {"effort": "high"}),
        # OpenRouter exposes only low/medium/high, so the keys above high map
        # down rather than being silently dropped.
        ("xhigh", {"effort": "high"}),
        ("ultracode", {"effort": "high"}),
    ])
    def test_every_helios_effort_key_maps(self, key, expected):
        assert or_chat.reasoning_for_effort(key) == expected

    def test_an_unknown_key_maps_to_nothing(self):
        assert or_chat.reasoning_for_effort("bogus") is None
        assert or_chat.reasoning_for_effort("") is None

    def test_reasoning_is_omitted_from_the_body_when_unset(self):
        body = json.loads(or_chat._build_body(
            [{"role": "user", "content": "x"}], model="v/m", tools=()))
        assert "reasoning" not in body

    def test_reasoning_is_sent_when_set(self):
        body = json.loads(or_chat._build_body(
            [{"role": "user", "content": "x"}], model="v/m", tools=(),
            reasoning=or_chat.reasoning_for_effort("medium")))
        assert body["reasoning"] == {"effort": "medium"}
