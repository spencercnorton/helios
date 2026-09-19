"""OpenRouter account status for the usage panel.

Unlike Claude and Codex, OpenRouter has no rolling usage window — it has a
prepaid credit balance (and, on a free key, a rate ceiling). ``GET
/api/v1/key`` describes the *key* Helios holds; ``GET /api/v1/credits``
describes the *account* behind it, and they can disagree. Measured live
2026-09-03 against the real account: the key had no per-key cap
(``limit: null``), so the panel said "$0.00 spent" — technically true of
that key, but the account sat at $239.26 of $300.00 remaining, which is the
number a user actually recognises as their balance. Neither call is
expensive, so both are made and the row survives losing either one:

* A per-key spending cap (``limit``), when set, binds before the account
  balance does — it is the tighter, key-scoped ceiling, and it is what
  actually stops this key once hit — so it wins the headline figure and the
  percentage whenever both are present.
* Otherwise the account balance (``total_credits - total_usage``) is the
  headline, since that is the number that actually determines whether the
  next request works.

# ponytail: usage_daily/usage_weekly/usage_monthly and byok_usage are read
# by nothing here — they would feed a rolling-window rate tracker, which is
# explicitly out of scope for this pass (gap 18 asks for the balance and
# is_free_tier, not a tracker). limit_remaining is skipped too: on every
# payload seen it is algebraically limit - usage, so deriving it ourselves
# avoids trusting a second nullable field that means the same thing.
# Upgrade path: if OpenRouter's semantics for limit_remaining ever diverge
# from limit - usage, read it directly instead of deriving it.

GTK-free; the caller runs this on a worker thread.
"""

from __future__ import annotations

import json

from helios.backend.openrouter.key import load_key
from helios.backend.openrouter.transport import (
    HttpRequest,
    HttpTransport,
    OversizedResponse,
    TransportFailure,
    UrlLibTransport,
    read_capped,
)
from helios.log import get_logger

_log = get_logger("openrouter-account")

KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
_TIMEOUT = 10.0
_MAX_BODY = 64 * 1024

# OpenRouter's documented ceiling for a free-tier key on ":free" models —
# not tracked here (see the ponytail note above), just surfaced.
# The per-minute ceiling is fixed; the DAILY one is not — OpenRouter grants
# 1,000/day once an account has bought $10 of credit, so a flat "50/day"
# is wrong for exactly the users who paid to leave it behind (
# round 11). State only what is true of every free-tier key.
_FREE_TIER_NOTE = "free tier, 20 req/min · daily quota depends on the account"


def fetch_credit_row(*, transport: HttpTransport | None = None) -> dict | None:
    """Return a toolbar rate-limit row for the saved key, or None.

    None means "nothing trustworthy to show" — no key, no network, or a
    response we do not recognise on *both* endpoints. The panel keeps its
    explanatory empty state rather than rendering a zero that looks like a
    real balance.
    """
    api_key = load_key()
    if not api_key:
        return None
    client = transport or UrlLibTransport()
    key_payload = _fetch_json(client, KEY_URL, api_key, "key status")
    credits_payload = _fetch_json(client, CREDITS_URL, api_key, "credits")
    if key_payload is None and credits_payload is None:
        return None
    return credit_row(key_payload, credits_payload)


def _fetch_json(client: HttpTransport, url: str, api_key: str, name: str) -> dict | None:
    """GET one OpenRouter JSON endpoint, capped and defensive.

    None means this endpoint failed or its body was not JSON we trust — the
    caller degrades to whatever the other endpoint returned rather than
    losing the whole row over one side of two.
    """
    request = HttpRequest(
        method="GET",
        url=url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        body=b"",
    )
    try:
        response = client.open(request, timeout_seconds=_TIMEOUT)
    except TransportFailure as e:
        _log.warning("OpenRouter %s failed: %s", name, e.kind)
        return None
    try:
        if response.status != 200:
            _log.warning("OpenRouter %s http-%s", name, response.status)
            return None
        return json.loads(read_capped(response, _MAX_BODY).decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TransportFailure,
        OversizedResponse,
    ):
        return None
    finally:
        response.close()


def credit_row(key_payload: object, credits_payload: object = None) -> dict | None:
    """Translate /key + /credits responses into the toolbar's rate-limit row.

    Either argument may be None — its request failed, or its shape was not
    trusted — and the row is built from whichever succeeded. See the module
    docstring for the precedence rule between a per-key cap and the account
    balance.
    """
    key_data = key_payload.get("data") if isinstance(key_payload, dict) else None
    key_data = key_data if isinstance(key_data, dict) else None
    credits_data = (
        credits_payload.get("data") if isinstance(credits_payload, dict) else None
    )
    credits_data = credits_data if isinstance(credits_data, dict) else None

    usage = _number(key_data.get("usage")) if key_data else None
    limit = _number(key_data.get("limit")) if key_data else None
    is_free_tier = bool(key_data and key_data.get("is_free_tier"))

    total_credits = _number(credits_data.get("total_credits")) if credits_data else None
    total_usage = _number(credits_data.get("total_usage")) if credits_data else None

    if usage is None and (total_credits is None or total_usage is None):
        return None

    row: dict = {
        "provider": "openrouter",
        "rateLimitType": "openrouter_credits",
        "status": "allowed",
    }
    percent: int | None = None

    if limit is not None and limit > 0 and usage is not None:
        # The key cap wins: see the module docstring for why.
        percent = max(0, min(100, int(round(100 * usage / limit))))
        row["detail"] = f"${usage:,.2f} of ${limit:,.2f}"
    elif total_credits is not None and total_usage is not None and total_credits > 0:
        remaining = max(0.0, total_credits - total_usage)
        percent = max(0, min(100, int(round(100 * total_usage / total_credits))))
        row["detail"] = f"${remaining:,.2f} of ${total_credits:,.2f} remaining"
    elif usage is not None:
        # No account credits and no key cap: spend is the only meaningful
        # figure, and a percentage of infinity is not one.
        row["detail"] = f"${usage:,.2f} spent"
    else:
        # No usable key usage, and the account has $0 or negative credits
        # (never bought any) — a free-tier-only account. total_usage is
        # still real money, usually $0 on such an account.
        row["detail"] = f"${total_usage:,.2f} spent"

    if percent is not None:
        row["usedPercent"] = percent
        if percent >= 100:
            row["status"] = "exceeded"

    if is_free_tier:
        row["detail"] = f"{row['detail']} ({_FREE_TIER_NOTE})"

    return row


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
