"""Per-endpoint route table for OpenRouter models.

A model slug is not a deployment. One slug is served by several endpoints that
differ in context length, max output, quantization, supported parameters, and
whether they cache prompts — and with no ``provider`` pin OpenRouter
load-balances across them weighted by inverse price squared, re-choosing on
every request. Three consequences the chat driver cares about:

* **The advertised context length is an upper bound, not a guarantee.** Sizing a
  prompt from ``/models`` overruns the smaller endpoints. Compaction needs the
  *endpoint's* number.
* **A silent provider swap changes behavior mid-session** — different
  quantization, different tool-schema tolerance — with no signal.
* **Prompt caching is per-provider.** A swap discards the cache, and cache reads
  are where the money is: an order of magnitude or more off replayed input.

So Helios picks one endpoint per session and pins it. This module owns that
choice: fetch ``/models/{slug}/endpoints``, cache it, and answer "which endpoint
should this session use, and does it cache implicitly or need explicit
breakpoints?"

Selection is deliberately boring and deterministic — health, then capability,
then caching, then price. A cleverer policy would need evidence this one is
insufficient, and it would make sessions harder to reason about.

GTK-free.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from helios.backend.openrouter.transport import (
    HttpRequest,
    HttpTransport,
    TransportFailure,
    UrlLibTransport,
)
from helios.log import get_logger
from helios.paths import state_dir

_log = get_logger("openrouter-routes")

__all__ = [
    "Route",
    "verify_endpoint",
    "RouteError",
    "cached_route",
    "exclude",
    "refresh_routes",
    "route_for",
]

_ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{model}/endpoints"
_FETCH_TIMEOUT = 20.0
_MAX_BODY = 4 * 1024 * 1024
_CACHE_TTL_SECONDS = 3600.0

# Providers that cache a stable prefix with no request-side markup. Everything
# else needs explicit ``cache_control`` breakpoints, which cost a content-parts
# migration — so we only send them where the endpoint says they are needed.
_EXPLICIT_CACHE_VENDORS = frozenset({"anthropic", "google", "google-vertex", "qwen", "alibaba"})

# An endpoint below this recent uptime is demoted. Kept lenient because the
# field is often absent, and a null must never read as "unhealthy" — see _score.
_MIN_UPTIME = 95.0


class RouteError(Exception):
    """Route discovery failed. The caller falls back to unpinned routing."""


def _cache_path() -> Path:
    return state_dir() / "openrouter-routes.json"


@dataclass(frozen=True, slots=True)
class Route:
    """One chosen endpoint for a model, and what the caller must know about it."""

    model: str
    provider_slug: str
    provider_name: str
    context_length: int
    max_completion_tokens: int
    quantization: str
    supports_tools: bool
    supports_reasoning: bool
    implicit_caching: bool
    explicit_caching: bool
    input_price: float
    cache_read_price: float
    #: False when the catalog gave no usable input or output price for this
    #: endpoint. The prices below then read 0.0, which must NOT be taken as
    #: "free" — see ``_price``.
    pricing_known: bool = True
    #: Dollars per completion token on this endpoint. Load-bearing for the
    #: spend ceiling, not decoration: output is routinely several times the
    #: price of input (measured on the live catalog, $30/M in against $180/M
    #: out for `openai/gpt-5.5-pro`), so a projection that counts only the
    #: prompt can wave through a round whose reply alone breaks the ceiling.
    output_price: float = 0.0

    @property
    def completion_reserve(self) -> int:
        """Tokens held back for this turn's reply, and the ``max_tokens`` cap.

        Reserving the endpoint's *advertised maximum* output is the obvious
        reading and it is wrong: that number is a ceiling on what the endpoint
        will allow, not an estimate of what a turn will use. Measured
        2026-09-03 across five live endpoints, it left 61-68 % of the window
        usable on most models but **10 %** on `kimi-k3` and `glm-5.2:free`,
        whose endpoints advertise a ~900k max output — so compaction began at
        roughly 7 % window fill and a long session was trimmed continuously
        for no reason.

        An eighth of the window, floored at 4k, is a generous allowance for an
        agent reply, and the endpoint's own maximum still clamps it so this can
        never promise more output than the endpoint will produce.

        The 4k floor is then capped at **half** the window, because on a small
        endpoint the floor swallows it: without the cap, a 4,095-token model
        reserved all 4,095 for the reply, leaving a prompt budget of zero and
        sending ``max_tokens`` equal to the entire context on every request —
        rejected every time, so the model was permanently unusable through
        this driver. `openai/gpt-3.5-turbo-0613` is exactly that shape and is
        tool-capable, so it is reachable. Found by a cross-model review of
        this change, 2026-09-03.
        """
        if self.context_length <= 0:
            return 0
        reserve = min(max(4096, self.context_length // 8), self.context_length // 2)
        if self.max_completion_tokens > 0:
            reserve = min(reserve, self.max_completion_tokens)
        return max(0, min(reserve, self.context_length))

    @property
    def prompt_budget(self) -> int:
        """Tokens available for the replayed prompt on *this* endpoint.

        The endpoint's own context length minus the completion reserve, so a
        long reply cannot push the request over.
        """
        if self.context_length <= 0:
            return 0
        return max(0, self.context_length - self.completion_reserve)

    @property
    def caches_prompt(self) -> bool:
        return self.implicit_caching or self.explicit_caching


def route_for(model: str, *, transport: HttpTransport | None = None) -> Route | None:
    """Best endpoint for ``model``, from cache when fresh. None when unknown.

    Never raises: an unroutable model means the caller sends an unpinned
    request, which is what it did before this module existed.
    """
    if not model or "/" not in model:
        return None
    cached = cached_route(model)
    if cached is not None:
        return cached
    try:
        return refresh_routes(model, transport=transport)
    except RouteError as e:
        _log.info("route discovery failed for %s: %s", model, e)
        return None
    except Exception as e:  # noqa: BLE001 — see the docstring
        # Deliberately total. This runs inside OpenRouterDriver.start(), so
        # anything escaping here stops a session from starting — a strictly
        # worse outcome than the unpinned request this falls back to. A corrupt
        # cache, an evolved payload shape, or a mid-response transport failure
        # must degrade, not block.
        _log.warning("unexpected route discovery failure for %s: %s", model, e)
        return None


def exclude(model: str, provider_slug: str) -> None:
    """Record ``provider_slug`` as unusable for ``model`` and drop the choice.

    The endpoints API does not publish each endpoint's data policy, so an
    endpoint that ``data_collection: "deny"`` will reject cannot be filtered out
    in advance — it is only discoverable by being told "no endpoints found
    matching your data policy" after pinning to it. Recording it here means the
    cost is one failed request per provider ever, rather than once per session.
    """
    if not model or not provider_slug:
        return
    cache = _read_cache()
    entry = cache.setdefault(model, {})
    excluded = entry.get("excluded")
    excluded = list(excluded) if isinstance(excluded, list) else []
    if provider_slug not in excluded:
        excluded.append(provider_slug)
    entry["excluded"] = excluded
    entry["route"] = None
    entry["fetched_at"] = 0  # force a re-choose on the next lookup
    _write_cache(cache)
    _log.info("excluded %s for %s (ineligible under the request policy)",
              provider_slug, model)


def excluded_for(model: str) -> frozenset[str]:
    entry = _read_cache().get(model)
    values = entry.get("excluded") if isinstance(entry, dict) else None
    return frozenset(v for v in values if isinstance(v, str)) if isinstance(values, list) else frozenset()


def verify_endpoint(
    model: str,
    provider_slug: str,
    quantization: str,
    *,
    transport: HttpTransport | None = None,
) -> bool:
    """Confirm the live catalog serves ``model`` on exactly this variant.

    This is the "external catalog gate" ``EndpointRef`` names as the missing
    piece: a pinned provider slug alone can cover future operator variants, so
    a receipt could not honestly claim the exact endpoint was confirmed. The
    endpoints API publishes each variant's own slug and quantization, so the
    claim can now be checked against something the broker did not author.

    Deliberately uncached and fail-closed: an unreachable catalog, an evolved
    payload, or no matching variant all return False, which keeps the
    ``ENDPOINT_VARIANT_PIN_REQUIRED`` gate shut. Confirmation must be positive
    evidence, never the absence of a contradiction.
    """
    if not model or not provider_slug:
        return False
    try:
        endpoints = _fetch_endpoints(model, transport=transport)
    except Exception as e:  # noqa: BLE001 — unverifiable is unverified
        _log.info("endpoint verification failed for %s: %s", model, e)
        return False
    wanted_quant = (quantization or "").strip().lower()
    for endpoint in endpoints:
        if _slug(endpoint).lower() != provider_slug.strip().lower():
            continue
        actual = str(endpoint.get("quantization") or "").strip().lower()
        if wanted_quant in ("", "unknown"):
            # The profile does not constrain quantization, so slug alone is
            # the whole claim and matching it is sufficient.
            return True
        if actual == wanted_quant:
            return True
    _log.info(
        "no live endpoint for %s matches %s/%s", model, provider_slug, quantization
    )
    return False


def cached_route(model: str) -> Route | None:
    """Cached choice for ``model`` when the entry is younger than the TTL."""
    entry = _read_cache().get(model)
    if not isinstance(entry, dict):
        return None
    fetched_at = entry.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - fetched_at > _CACHE_TTL_SECONDS:
        return None
    route = entry.get("route")
    if not isinstance(route, dict):
        return None
    try:
        return Route(**route)
    except TypeError:
        return None  # cache written by a different version


def refresh_routes(model: str, *, transport: HttpTransport | None = None) -> Route | None:
    """Fetch endpoints for ``model``, choose one, persist, and return it."""
    endpoints = _fetch_endpoints(model, transport=transport)
    route = _choose(model, endpoints, excluded=excluded_for(model))
    cache = _read_cache()
    entry = cache.setdefault(model, {})
    entry["fetched_at"] = time.time()
    entry["route"] = None if route is None else asdict(route)
    _write_cache(cache)
    if route is not None:
        _log.info(
            "route for %s: %s ctx=%d cache=%s",
            model, route.provider_slug, route.context_length,
            "implicit" if route.implicit_caching
            else ("explicit" if route.explicit_caching else "none"),
        )
    return route


def _fetch_endpoints(model: str, *, transport: HttpTransport | None) -> list[dict]:
    client = transport or UrlLibTransport()
    request = HttpRequest(
        method="GET",
        url=_ENDPOINTS_URL.format(model=model),
        headers={"Accept": "application/json"},
        body=b"",
    )
    try:
        response = client.open(request, timeout_seconds=_FETCH_TIMEOUT)
    except TransportFailure as e:
        raise RouteError(str(e.kind)) from e
    try:
        if response.status != 200:
            raise RouteError(f"http-{response.status}")
        try:
            body = b"".join(response.iter_bytes())
        except TransportFailure as e:
            raise RouteError(f"read-{e.kind}") from e
        if len(body) > _MAX_BODY:
            raise RouteError("oversized")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise RouteError("bad-json") from e
    finally:
        response.close()
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise RouteError("bad-payload")
    return [e for e in endpoints if isinstance(e, dict)]


def _price(raw: object) -> float | None:
    """A non-negative price, or ``None`` when the catalog did not give one.

    ``None`` and ``0.0`` are different facts and collapsing them was a real
    hole: a confirmed-free endpoint and one whose pricing is missing,
    unparsable or a sentinel both read as "costs nothing", so the spend
    projection skipped the round entirely. Five live
    models carry negative sentinel prices (`openrouter/auto` and friends), and
    the endpoints payload is a different response from `/models` — it can be
    incomplete without the catalog being.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # `math.isfinite` rejects NaN *and* both infinities. The string
    # "Infinity" parses to a float and used to sail through as a known price,
    # which then poisoned every projection that multiplied by it (
    # round 11).
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _params(endpoint: dict) -> frozenset[str]:
    """Declared parameters as a set. Never trusts the payload's shape."""
    raw = endpoint.get("supported_parameters")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(v for v in raw if isinstance(v, str))


def _pricing(endpoint: dict) -> dict:
    raw = endpoint.get("pricing")
    return raw if isinstance(raw, dict) else {}


def _slug(endpoint: dict) -> str:
    """Provider slug for ``provider.only``.

    The endpoints API has no ``provider_slug``. It carries ``tag``, which is
    ``provider`` or ``provider/quantization`` ("deepinfra/fp4", "deepseek"), and
    ``provider_name`` for display ("DeepInfra", "Io Net"). The tag prefix is the
    pinnable identifier; ``provider_name`` lowercased is not — "Io Net" would
    give "io net" where the slug is "io-net".
    """
    tag = endpoint.get("tag")
    if isinstance(tag, str) and tag.strip():
        return tag.split("/")[0].strip()
    name = endpoint.get("provider_name")
    if isinstance(name, str) and name.strip():
        return name.strip().lower().replace(" ", "-")
    return ""


def _healthy(endpoint: dict) -> bool:
    # status 0 is healthy; negative values are degraded/offline.
    status = endpoint.get("status")
    if isinstance(status, (int, float)) and status != 0:
        return False
    uptime = endpoint.get("uptime_last_30m")
    # A missing uptime is unknown, not unhealthy. Treating null as 0 would
    # silently demote every endpoint that simply does not report the field.
    if isinstance(uptime, (int, float)) and uptime < _MIN_UPTIME:
        return False
    return True


def _choose(
    model: str,
    endpoints: list[dict],
    *,
    excluded: frozenset[str] = frozenset(),
) -> Route | None:
    """Pick one endpoint: health, then tools, then caching, then price."""
    if excluded:
        endpoints = [e for e in endpoints if _slug(e) not in excluded]
    candidates = [e for e in endpoints if _healthy(e)]
    if not candidates:
        # Everything is degraded; prefer a pinned degraded endpoint over an
        # unpinned request that would swap providers mid-session anyway.
        candidates = list(endpoints)
    if not candidates:
        return None

    def rank(endpoint: dict) -> tuple:
        params = _params(endpoint)
        tools = "tools" in params and "tool_choice" in params
        pricing = _pricing(endpoint)
        cache_read = _price(pricing.get("input_cache_read")) or 0.0
        implicit = bool(endpoint.get("supports_implicit_caching"))
        explicit = _slug(endpoint).lower() in _EXPLICIT_CACHE_VENDORS
        # Tool support first — a coding agent without tools is useless, and a
        # cheaper endpoint that silently ignores `tools` is the worst outcome.
        return (
            not tools,
            not (implicit or explicit),
            cache_read if (implicit or explicit) else 0.0,
            # Unknown price sorts last: an endpoint we cannot cost is the least
            # attractive of otherwise-equal candidates, not the cheapest.
            _price(pricing.get("prompt")) if _price(pricing.get("prompt")) is not None
            else float("inf"),
            _slug(endpoint),
        )

    best = sorted(candidates, key=rank)[0]
    if not _slug(best):
        _log.warning("endpoint for %s has no pinnable slug; leaving unpinned", model)
        return None
    params = _params(best)
    pricing = _pricing(best)
    prompt_price = _price(pricing.get("prompt"))
    completion_price = _price(pricing.get("completion"))
    slug = _slug(best)
    context = best.get("context_length")
    max_out = best.get("max_completion_tokens")
    return Route(
        model=model,
        provider_slug=slug,
        provider_name=str(best.get("provider_name") or slug),
        context_length=context if isinstance(context, int) and context > 0 else 0,
        max_completion_tokens=max_out if isinstance(max_out, int) and max_out > 0 else 0,
        quantization=str(best.get("quantization") or ""),
        supports_tools="tools" in params and "tool_choice" in params,
        # "reasoning" is the portable unified parameter (declared by far more
        # endpoints than "reasoning_effort"), so that is what is checked.
        supports_reasoning="reasoning" in params,
        implicit_caching=bool(best.get("supports_implicit_caching")),
        explicit_caching=slug.lower() in _EXPLICIT_CACHE_VENDORS,
        input_price=prompt_price or 0.0,
        cache_read_price=_price(pricing.get("input_cache_read")) or 0.0,
        output_price=completion_price or 0.0,
        pricing_known=prompt_price is not None and completion_price is not None,
    )


def _read_cache() -> dict:
    try:
        payload = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError: a truncated
        # or non-UTF-8 cache is a cache miss, never an exception.
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_cache(cache: dict) -> None:
    tmp: Path | None = None
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(cache))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as e:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        _log.warning("could not write %s: %s", path, e)
