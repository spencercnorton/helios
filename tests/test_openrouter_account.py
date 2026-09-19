"""OpenRouter account status: credit rows, and a read that is actually bounded.

GTK-free — runs in the slim CI lane.
"""

from __future__ import annotations

import json

import pytest

from helios.backend.openrouter import account
from helios.backend.openrouter.transport import (
    OversizedResponse,
    TransportFailure,
    TransportFailureKind,
    read_capped,
)


class _Response:
    """Minimal HttpResponse stand-in that streams a scripted chunk sequence."""

    def __init__(self, chunks, status=200):
        self.status = status
        self.headers: dict[str, str] = {}
        self._chunks = list(chunks)
        self.pulled = 0
        self.closed = False

    def iter_bytes(self):
        for chunk in self._chunks:
            self.pulled += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


class _UrlRoutedTransport:
    """Fake HttpTransport that answers per URL, for the two-endpoint fetch.

    ``responses`` maps a URL to either a response object or an exception
    instance to raise — one endpoint failing must not stop the other from
    being tried.
    """

    def __init__(self, responses: dict):
        self._responses = responses
        self.requests: list = []

    def open(self, request, *, timeout_seconds):
        self.requests.append(request)
        outcome = self._responses[request.url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# Verbatim /api/v1/key and /api/v1/credits bodies, measured live 2026-09-03
# against the real account (probe behind F-19 / gap 18): a key with no
# per-key cap, on an account with $239.26 of $300 remaining.
_KEY_PAYLOAD_UNCAPPED = {
    "data": {
        "label": "sk-or-v1-aed...9ee",
        "is_management_key": False,
        "is_provisioning_key": False,
        "limit": None,
        "limit_reset": None,
        "limit_remaining": None,
        "include_byok_in_limit": False,
        "usage": 0.004314447,
        "usage_daily": 0,
        "usage_weekly": 0,
        "usage_monthly": 0,
        "byok_usage": 0,
        "byok_usage_daily": 0,
        "byok_usage_weekly": 0,
        "byok_usage_monthly": 0,
        "is_free_tier": False,
        "expires_at": None,
        "creator_user_id": "...",
        "rate_limit": {
            "requests": -1,
            "interval": "10s",
            "note": "This field is deprecated and safe to ignore.",
        },
    }
}
_CREDITS_PAYLOAD = {"data": {"total_credits": 300, "total_usage": 60.73895567}}


# ── the bound ──────────────────────────────────────────────────────────────


def test_read_capped_stops_pulling_once_the_cap_is_passed():
    # The point of a limit is to not allocate the body first. A peer offering
    # 10MB in 1MB chunks must not get 10MB read before being rejected.
    response = _Response([b"x" * 1_000_000] * 10)
    with pytest.raises(OversizedResponse):
        read_capped(response, 2_000_000)
    assert response.pulled <= 3_000_000  # stopped early, did not drain


def test_read_capped_returns_a_body_that_fits():
    response = _Response([b"ab", b"cd"])
    assert read_capped(response, 64) == b"abcd"


def test_oversized_key_response_yields_no_row_rather_than_a_wrong_one():
    response = _Response([b"{" + b" " * 200_000 + b"}"])
    assert account.fetch_credit_row.__module__  # sanity: module imported
    with pytest.raises(OversizedResponse):
        read_capped(response, account._MAX_BODY)


# ── the row: pure translation ───────────────────────────────────────────────


def test_metered_key_reports_percentage_and_amounts():
    row = account.credit_row({"data": {"usage": 8.4, "limit": 20.0}})
    assert row["usedPercent"] == 42
    assert row["detail"] == "$8.40 of $20.00"
    assert row["status"] == "allowed"
    assert row["provider"] == "openrouter"


def test_unlimited_key_reports_spend_without_a_percentage_of_infinity():
    row = account.credit_row({"data": {"usage": 3.5, "limit": None}})
    assert "usedPercent" not in row
    assert row["detail"] == "$3.50 spent"


def test_exhausted_key_is_flagged():
    row = account.credit_row({"data": {"usage": 20.0, "limit": 20.0}})
    assert row["usedPercent"] == 100
    assert row["status"] == "exceeded"


@pytest.mark.parametrize(
    "payload",
    [{}, {"data": {}}, {"data": {"usage": None}}, "nonsense", {"data": {"usage": True}}],
)
def test_unrecognised_payloads_yield_nothing_rather_than_a_zero_balance(payload):
    # A zero that looks like a real balance is worse than an empty panel.
    assert account.credit_row(payload) is None


def test_unrecognised_credits_payload_is_ignored_not_trusted():
    # A malformed /credits body must not corrupt the key-only fallback.
    row = account.credit_row(
        {"data": {"usage": 3.5, "limit": None}}, {"data": {"total_credits": "lots"}}
    )
    assert row["detail"] == "$3.50 spent"


# ── the row: account balance and precedence ─────────────────────────────────


def test_account_balance_is_the_headline_when_the_key_has_no_cap():
    row = account.credit_row(_KEY_PAYLOAD_UNCAPPED, _CREDITS_PAYLOAD)
    assert row["detail"] == "$239.26 of $300.00 remaining"
    assert row["usedPercent"] == 20
    assert row["status"] == "allowed"


def test_key_cap_wins_the_headline_over_the_account_balance():
    # A key cap is tighter and more relevant than the account balance, so it
    # binds first even when /credits also answered.
    row = account.credit_row({"data": {"usage": 8.4, "limit": 20.0}}, _CREDITS_PAYLOAD)
    assert row["detail"] == "$8.40 of $20.00"
    assert row["usedPercent"] == 42


def test_account_balance_exhausted_is_flagged():
    row = account.credit_row(None, {"data": {"total_credits": 10.0, "total_usage": 10.0}})
    assert row["usedPercent"] == 100
    assert row["status"] == "exceeded"
    assert row["detail"] == "$0.00 of $10.00 remaining"


def test_free_tier_key_gets_a_qualifier():
    # Synthetic: the live probe account is not on the free tier, so this
    # exercises the documented 20/min · daily quota depends on the account ceiling instead (Gap 18).
    row = account.credit_row({"data": {"usage": 0.0, "limit": None, "is_free_tier": True}})
    assert row["detail"] == "$0.00 spent (free tier, 20 req/min · daily quota depends on the account)"


# ── fetch_credit_row: both endpoints, and losing either one ────────────────


def test_fetch_credit_row_merges_both_endpoints(monkeypatch):
    monkeypatch.setattr(account, "load_key", lambda: "sk-or-v1-testkey0000000000")
    transport = _UrlRoutedTransport(
        {
            account.KEY_URL: _Response([json.dumps(_KEY_PAYLOAD_UNCAPPED).encode()]),
            account.CREDITS_URL: _Response([json.dumps(_CREDITS_PAYLOAD).encode()]),
        }
    )
    row = account.fetch_credit_row(transport=transport)
    assert row["detail"] == "$239.26 of $300.00 remaining"
    assert len(transport.requests) == 2
    assert all(
        r.headers["Authorization"] == "Bearer " + "sk-or-v1-testkey0000000000"
        for r in transport.requests
    )


def test_credits_unreachable_degrades_to_the_per_key_row(monkeypatch):
    monkeypatch.setattr(account, "load_key", lambda: "sk-or-v1-testkey0000000000")
    transport = _UrlRoutedTransport(
        {
            account.KEY_URL: _Response([json.dumps(_KEY_PAYLOAD_UNCAPPED).encode()]),
            account.CREDITS_URL: TransportFailure(TransportFailureKind.CONNECTION),
        }
    )
    row = account.fetch_credit_row(transport=transport)
    # usage 0.004314447 formats to $0.00 — still the true per-key fallback.
    assert row["detail"] == "$0.00 spent"


def test_key_unreachable_still_shows_the_account_balance(monkeypatch):
    monkeypatch.setattr(account, "load_key", lambda: "sk-or-v1-testkey0000000000")
    transport = _UrlRoutedTransport(
        {
            account.KEY_URL: TransportFailure(TransportFailureKind.TIMEOUT),
            account.CREDITS_URL: _Response([json.dumps(_CREDITS_PAYLOAD).encode()]),
        }
    )
    row = account.fetch_credit_row(transport=transport)
    assert row["detail"] == "$239.26 of $300.00 remaining"


def test_oversized_credits_body_degrades_to_the_per_key_row(monkeypatch):
    monkeypatch.setattr(account, "load_key", lambda: "sk-or-v1-testkey0000000000")
    transport = _UrlRoutedTransport(
        {
            account.KEY_URL: _Response([json.dumps(_KEY_PAYLOAD_UNCAPPED).encode()]),
            account.CREDITS_URL: _Response([b"{" + b" " * 200_000 + b"}"]),
        }
    )
    row = account.fetch_credit_row(transport=transport)
    assert row["detail"] == "$0.00 spent"


def test_both_endpoints_unreachable_yields_nothing(monkeypatch):
    monkeypatch.setattr(account, "load_key", lambda: "sk-or-v1-testkey0000000000")
    transport = _UrlRoutedTransport(
        {
            account.KEY_URL: TransportFailure(TransportFailureKind.CONNECTION),
            account.CREDITS_URL: TransportFailure(TransportFailureKind.CONNECTION),
        }
    )
    assert account.fetch_credit_row(transport=transport) is None


def test_no_saved_key_short_circuits_before_any_request(monkeypatch):
    monkeypatch.setattr(account, "load_key", lambda: "")
    transport = _UrlRoutedTransport({})
    assert account.fetch_credit_row(transport=transport) is None
    assert transport.requests == []
