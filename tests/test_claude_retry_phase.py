"""Claude's silent stalls get a name.

`system/api_retry` and `system/status` were dropped by `_dispatch_record`, and
both mark the two windows in which **nothing else arrives on the wire**: a
failing API request that will be retried, and an auto-compaction between turns.
With them dropped, the activity strip held whatever state it last inferred from
a stream block, so a five-attempt retry storm and a dead subprocess looked
identical.

Wire shapes are the installed CLI's own, not guesses. `claude 2.1.245` carries
a zod schema for `api_retry`:

    {type:"system", subtype:"api_retry", attempt:int, max_retries:int,
     retry_delay_ms:int, error_status:int|null, error:…, uuid, session_id}

with `error_status` documented null for connection errors, and a probe run of
`claude --print --output-format stream-json --verbose --include-partial-messages`
produced exactly one live `{"type":"system","subtype":"status",
"status":"requesting","uuid":…,"session_id":…}` per API request.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402
from helios.widgets.activity_indicator import (  # noqa: E402
    STATE_COMPACTING,
    STATE_IDLE,
    STATE_REVIEWING,
    STATE_RETRYING,
    STATE_THINKING,
    native_activity_state,
)


def _driver() -> ClaudeCliDriver:
    drv = ClaudeCliDriver.__new__(ClaudeCliDriver)
    ClaudeCliDriver.__init__(drv, cwd="/tmp")
    return drv


def _seen(record: dict) -> list[dict]:
    drv = _driver()
    out: list[dict] = []
    drv.connect("activity-updated", lambda _d, payload: out.append(payload))
    drv._dispatch_record(record)
    return out


# ── the driver stops dropping the two records ──────────────────────────────


def test_api_retry_reaches_the_window_with_the_cli_s_own_fields() -> None:
    seen = _seen(
        {
            "type": "system",
            "subtype": "api_retry",
            "attempt": 2,
            "max_retries": 5,
            "retry_delay_ms": 3000,
            "error_status": 529,
            "session_id": "s",
        }
    )
    assert seen == [
        {
            "category": "retry",
            "attempt": 2,
            "max_retries": 5,
            "retry_delay_ms": 3000,
            "error_status": 529,
        }
    ]


def test_status_reaches_the_window_as_a_phase() -> None:
    assert _seen(
        {"type": "system", "subtype": "status", "status": "compacting"}
    ) == [{"category": "phase", "phase": "compacting"}]


def test_a_status_record_with_no_status_is_not_emitted() -> None:
    assert _seen({"type": "system", "subtype": "status"}) == []


def test_unrelated_system_subtypes_still_emit_nothing() -> None:
    assert _seen({"type": "system", "subtype": "thinking_tokens"}) == []


# ── the strip renders them honestly ────────────────────────────────────────


def test_retry_renders_attempt_cause_and_countdown() -> None:
    state, detail = native_activity_state(
        {
            "category": "retry",
            "attempt": 2,
            "max_retries": 5,
            "retry_delay_ms": 3000,
            "error_status": 529,
        }
    )
    assert state == STATE_RETRYING
    assert detail == "attempt 2 of 5 · HTTP 529 · next try in 3s"


def test_a_null_error_status_says_connection_error_not_nothing() -> None:
    """The CLI sends error_status: null for timeouts — the case where a bare
    "Retrying" is least informative."""

    _, detail = native_activity_state(
        {
            "category": "retry",
            "attempt": 1,
            "max_retries": 5,
            "retry_delay_ms": 1000,
            "error_status": None,
        }
    )
    assert detail == "attempt 1 of 5 · connection error · next try in 1s"


def test_compacting_gets_its_own_state_and_requesting_reads_as_thinking() -> None:
    assert native_activity_state({"category": "phase", "phase": "compacting"}) == (
        STATE_COMPACTING,
        "",
    )
    assert native_activity_state({"category": "phase", "phase": "requesting"}) == (
        STATE_THINKING,
        "",
    )
    assert native_activity_state({"category": "phase", "phase": "idle"}) == (
        STATE_IDLE,
        "",
    )
    assert native_activity_state({"category": "phase", "phase": "reviewing"}) == (
        STATE_REVIEWING,
        "",
    )


def test_an_unknown_future_phase_never_renders_a_raw_enum_name() -> None:
    assert native_activity_state({"category": "phase", "phase": "warp_drive"}) == (
        STATE_THINKING,
        "",
    )


def test_the_strip_paints_the_retry_icon_amber_and_takes_it_back() -> None:
    """The amber is the whole point: a retry must not look like tool work."""

    from helios.widgets.activity_indicator import (
        STATE_BASH,
        ActivityIndicator,
    )

    strip = ActivityIndicator()
    icon = strip._icon_stack.get_child_by_name(STATE_RETRYING)
    assert icon is not None

    strip.set_activity(STATE_RETRYING, "attempt 2 of 5")
    assert icon.has_css_class("helios-activity-warn")

    strip.set_activity(STATE_BASH, "ls")
    assert not icon.has_css_class("helios-activity-warn")
    strip.shutdown()
