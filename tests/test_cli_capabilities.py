"""Helios keeps what the CLI tells it about itself.

The `initialize` control_request was already being SENT; it was sent with no
callback, so `_handle_control_response` dropped the payload — and that payload
is the CLI stating `supportsEffort` / `supportedEffortLevels` per model. In its
absence Helios showed a fixed six-stop slider and guessed which models have a
reasoning axis at all.

The window-authority tests pin the fix for a subtler problem: TWO sources were
writing `_ctx_window`. `modelUsage[...].contextWindow` is the model's hard cap
(1,000,000 on sonnet) and `get_context_usage.maxTokens` is the usable budget
before autocompaction (967,000 = cap minus a 33,000 buffer). Letting both write
one field made the gauge mean one thing on turn 1 and another from turn 2.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402

INITIALIZE_PAYLOAD = {
    "models": [
        {
            "value": "default",
            "resolvedModel": "claude-opus-5[1m]",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        },
        {
            "value": "claude-haiku-4-5-20251001",
            "resolvedModel": "claude-haiku-4-5-20251001",
            "supportsEffort": True,
            "supportedEffortLevels": ["low", "medium"],
        },
        {"value": "no-effort-model", "resolvedModel": "no-effort-model",
         "supportsEffort": False},
    ],
    "account": {"subscriptionType": "Claude Max"},
    "output_style": "default",
}


def _driver(model: str = "") -> ClaudeCliDriver:
    drv = ClaudeCliDriver.__new__(ClaudeCliDriver)
    ClaudeCliDriver.__init__(drv, cwd="/tmp", model=model)
    return drv


def test_the_initialize_payload_is_kept_not_discarded() -> None:
    drv = _driver()
    seen: list = []
    drv.connect("capabilities-updated", lambda _d, p: seen.append(p))

    drv._cli_models = INITIALIZE_PAYLOAD["models"]
    drv._cli_capabilities = INITIALIZE_PAYLOAD
    drv.emit("capabilities-updated", INITIALIZE_PAYLOAD)

    assert seen and seen[0]["account"]["subscriptionType"] == "Claude Max"


def test_effort_levels_come_from_the_selected_model() -> None:
    drv = _driver(model="claude-haiku-4-5-20251001")
    drv._cli_models = INITIALIZE_PAYLOAD["models"]

    assert drv.supported_effort_levels() == ["low", "medium"]


def test_effort_levels_are_empty_when_the_cli_never_answered() -> None:
    """Empty must mean "fall back to the constants", never "no effort"."""

    assert _driver(model="sonnet").supported_effort_levels() == []


def test_a_model_marked_unsupported_never_inherits_another_models_levels() -> None:
    """Caught in review, and this test previously PINNED THE BUG. The first
    cut fell back to the first effort-capable entry, so a model explicitly
    marked `supportsEffort: false` was handed opus's five stops. Offering
    capabilities a model does not have is worse than not knowing."""

    drv = _driver(model="no-effort-model")
    drv._cli_models = INITIALIZE_PAYLOAD["models"]

    assert drv.supported_effort_levels() == []


def test_an_unlisted_model_gets_no_statement_not_a_neighbours_levels() -> None:
    """An alias or id the CLI did not mention must fall back to the caller's
    constants, not to whichever entry happened to be first."""

    drv = _driver(model="some-model-the-cli-never-listed")
    drv._cli_models = INITIALIZE_PAYLOAD["models"]

    assert drv.supported_effort_levels() == []


def test_context_alias_uses_cli_resolved_model_for_effort_capabilities() -> None:
    drv = _driver(model="fable[1m]")
    drv._cli_models = [{
        "value": "fable",
        "resolvedModel": "claude-fable-5-1",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
    }]
    assert drv.supported_effort_levels() == []
    # The real system/init record provides the resolved model; no family
    # heuristic is needed and the context-qualified requested id is retained.
    drv._dispatch_record({"type": "system", "subtype": "init", "model": "claude-fable-5-1"})
    assert drv.supported_effort_levels() == ["low", "medium", "high", "xhigh", "max"]


# --- window authority -------------------------------------------------------


def _usage_payload(max_tokens: int = 967000) -> dict:
    return {
        "totalTokens": 40609,
        "maxTokens": max_tokens,
        "categories": [{"name": "System prompt", "tokens": 8807}],
    }


def test_get_context_usage_becomes_the_authority(monkeypatch) -> None:
    drv = _driver()
    sent: list = []
    monkeypatch.setattr(drv, "_send_control_request",
                        lambda req, cb=None: sent.append((req, cb)) or True)

    drv.request_context_usage()
    req, cb = sent[-1]
    assert req["subtype"] == "get_context_usage"

    cb(True, "", _usage_payload())

    assert drv._ctx_window == 967000
    assert drv._ctx_window_authoritative is True


def test_model_usage_must_not_overwrite_the_authoritative_window(monkeypatch) -> None:
    """The bug this prevents: a meter reading 967,000 on turn 1 and
    1,000,000 from turn 2, because both sources wrote the same field."""

    drv = _driver()
    monkeypatch.setattr(drv, "_send_control_request",
                        lambda req, cb=None: cb(True, "", _usage_payload()) or True)
    drv.request_context_usage()
    assert drv._ctx_window == 967000

    # Now a turn ends and modelUsage reports the HARD cap.
    monkeypatch.setattr(drv, "_main_model_context", lambda obj: (40609, 1_000_000))
    monkeypatch.setattr(drv, "_confirm_uncertain_delivery", lambda: None)
    monkeypatch.setattr(drv, "_finish_execution_with_evidence", lambda ev: True)
    monkeypatch.setattr(drv, "_flush_user_queue", lambda: None)
    seen: list = []
    drv.connect("usage-updated", lambda _d, used, total: seen.append((used, total)))

    drv._dispatch_record({"type": "result", "subtype": "success"})

    assert drv._ctx_window == 967000, "the hard cap overwrote the usable budget"
    assert seen[-1][1] == 967000, "the meter's denominator changed mid-session"


def test_model_usage_still_fills_in_when_the_frame_never_answered(monkeypatch) -> None:
    """Without get_context_usage — an older CLI — the cap is better than
    nothing and must still be used."""

    drv = _driver()
    monkeypatch.setattr(drv, "_main_model_context", lambda obj: (40609, 1_000_000))
    monkeypatch.setattr(drv, "_confirm_uncertain_delivery", lambda: None)
    monkeypatch.setattr(drv, "_finish_execution_with_evidence", lambda ev: True)
    monkeypatch.setattr(drv, "_flush_user_queue", lambda: None)
    monkeypatch.setattr(drv, "request_context_usage", lambda: None)
    seen: list = []
    drv.connect("usage-updated", lambda _d, used, total: seen.append((used, total)))

    drv._dispatch_record({"type": "result", "subtype": "success"})

    assert drv._ctx_window == 1_000_000
    assert seen[-1] == (40609, 1_000_000)


def test_a_failed_usage_frame_changes_nothing(monkeypatch) -> None:
    drv = _driver()
    monkeypatch.setattr(drv, "_send_control_request",
                        lambda req, cb=None: cb(False, "nope", {}) or True)

    drv.request_context_usage()

    assert drv._ctx_window == 0
    assert drv._ctx_window_authoritative is False
