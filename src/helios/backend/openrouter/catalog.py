"""OpenRouter ``/models`` catalog — fetch, cache, and picker rows.

The endpoint is public and returns every model OpenRouter can route, with
``context_length`` per row. The fetch is deliberately cheap (one GET, no
auth required) and cached on disk so the picker renders instantly; the
driver itself only needs a model id string.

GTK-free; network I/O belongs on a worker thread (callers already do so).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

from helios.backend.model_catalog import (
    PROVIDER_OPENROUTER,
    ModelEntry,
    openrouter_model_selectable,
)
from helios.backend.openrouter.transport import (
    HttpRequest,
    HttpTransport,
    OversizedResponse,
    TransportFailure,
    UrlLibTransport,
    read_capped,
)
from helios.log import get_logger

_log = get_logger("openrouter-catalog")

MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_PATH = Path.home() / ".helios" / "openrouter-models.json"

_FETCH_TIMEOUT = 15.0
_MAX_BODY = 8 * 1024 * 1024

__all__ = [
    "CACHE_PATH",
    "MODELS_URL",
    "CatalogError",
    "build_entries",
    "cached_models",
    "context_length_for",
    "fetch_model_rows",
    "pricing_for",
    "refresh_models",
    "supports_reasoning",
]


class CatalogError(Exception):
    """The /models response could not be used. Content-free on purpose."""


# Display priority for vendor groups; everything else sorts alphabetically
# after these.
_VENDOR_PRIORITY: tuple[str, ...] = (
    "google",
    "deepseek",
    "moonshotai",
    "qwen",
    "x-ai",
    "meta-llama",
    "mistralai",
)

_VENDOR_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "deepseek": "DeepSeek",
    "moonshotai": "Moonshot AI",
    "qwen": "Qwen",
    "x-ai": "xAI",
    "meta-llama": "Meta Llama",
    "mistralai": "Mistral AI",
    "microsoft": "Microsoft",
    "nvidia": "NVIDIA",
    "cohere": "Cohere",
    "perplexity": "Perplexity",
    "minimax": "MiniMax",
    "zai": "Z.ai",
}


def _vendor_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else ""


def _group_label(vendor: str) -> str:
    return _VENDOR_LABELS.get(vendor, vendor.capitalize() or "Other")


#: Marker appended to the visible label of a model that cannot call tools.
#:
#: In the label rather than only the description, because the description is a
#: tooltip and this is not tooltip-grade information: an OpenRouter session is
#: an agent loop, so a model without tool support cannot read a file, run a
#: command, or edit anything. It answers in prose and never says why. Deciding
#: that is what the picker is for, and you cannot hover a decision you did not
#: know you were making.
_NO_TOOLS_SUFFIX = " · no tools"

#: Kept short on purpose: the picker button is sized to 220px and a long
#: suffix would push the model's actual name out of view, which trades one
#: unreadable row for another.
_NO_TOOLS_DETAIL = "Cannot call tools — it will answer in prose and never read, run, or edit anything."


def supports_tools(row: object) -> bool:
    """Does this ``/models`` row advertise tool calling?

    Fails closed on a malformed or unfamiliar row: an entry that cannot be
    shown to support tools is labelled as not supporting them. Mislabelling a
    capable model is a cosmetic annoyance; the reverse is the silent failure
    this exists to prevent.
    """
    if not isinstance(row, dict):
        return False
    params = row.get("supported_parameters")
    return isinstance(params, list) and "tools" in params


#: Reasoning-effort choices offered for a row that declares `reasoning`.
#:
#: Deliberately the four levels Helios's own mapping distinguishes
#: (`chat._REASONING_FOR_EFFORT`). OpenRouter documents `minimal`, `xhigh` and
#: `max` as well, but Helios collapses everything above "high" onto "high", and
#: offering a level that silently becomes another one is worse than not
#: offering it. The pinned endpoint remains authoritative either way: the
#: driver refuses `set_effort` when the endpoint does not declare `reasoning`,
#: because `require_parameters: true` would otherwise route the request away.
#:
#: ponytail: widen this when the wire mapping learns to distinguish them.
_REASONING_EFFORTS: tuple[tuple[str, str], ...] = (
    ("off", "No reasoning"),
    ("low", "Low reasoning effort"),
    ("medium", "Medium reasoning effort"),
    ("high", "High reasoning effort"),
)


def supports_reasoning(row: object) -> bool:
    """Does this ``/models`` row advertise the unified ``reasoning`` parameter?

    ``reasoning`` rather than ``reasoning_effort``: it is the portable one and
    far more endpoints declare it — the same check ``routes._choose`` makes
    against the endpoint. Fails closed on a malformed row.
    """
    if not isinstance(row, dict):
        return False
    params = row.get("supported_parameters")
    return isinstance(params, list) and "reasoning" in params


def build_entries(rows: list[dict]) -> list[ModelEntry]:
    """Translate raw /models rows into picker entries, grouped by vendor.

    Claude and GPT models use their native provider integrations, so OpenAI
    and Anthropic are excluded here for both fresh and cached catalogs.
    A model without tool support is still a legitimate
    choice for a conversation, and the image and audio models that make up much
    of that group have no business running an agent loop anyway. What changed
    is that the choice is now visible at the point it is made.
    """
    entries: list[ModelEntry] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not openrouter_model_selectable(mid) or mid in seen:
            continue
        context = row.get("context_length")
        if not isinstance(context, int) or context <= 0:
            continue
        seen.add(mid)
        vendor = _vendor_of(mid)
        name = str(row.get("name") or mid).strip()
        # OR names are "Vendor: Model" — the group header already says it.
        prefix, _, rest = name.partition(": ")
        if prefix.lower() == vendor.lower() and rest:
            name = rest
        tooled = supports_tools(row)
        detail = f"{context // 1000}k context"
        entries.append(ModelEntry(
            id=mid,
            label=name if tooled else f"{name}{_NO_TOOLS_SUFFIX}",
            group=_group_label(vendor),
            provider=PROVIDER_OPENROUTER,
            description=detail if tooled else f"{detail}\n{_NO_TOOLS_DETAIL}",
            reasoning_efforts=_REASONING_EFFORTS if supports_reasoning(row) else (),
            default_effort="medium" if supports_reasoning(row) else "",
        ))
    prio = {v: i for i, v in enumerate(_VENDOR_PRIORITY)}
    entries.sort(key=lambda e: (prio.get(_vendor_of(e.id), len(prio)), e.group, e.label.lower()))
    return entries


def fetch_model_rows(
    *,
    transport: HttpTransport | None = None,
    timeout: float = _FETCH_TIMEOUT,
) -> list[dict]:
    """GET the public catalog. Raises CatalogError on any failure."""
    client = transport or UrlLibTransport()
    request = HttpRequest(
        method="GET",
        url=MODELS_URL,
        headers={"Accept": "application/json"},
        body=b"",
    )
    try:
        response = client.open(request, timeout_seconds=timeout)
    except TransportFailure as e:
        raise CatalogError(str(e.kind)) from e
    try:
        if response.status != 200:
            raise CatalogError(f"http-{response.status}")
        try:
            body = read_capped(response, _MAX_BODY)
        except OversizedResponse as e:
            raise CatalogError("oversized") from e
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise CatalogError("bad-json") from e
    finally:
        response.close()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise CatalogError("bad-payload")
    return rows


def refresh_models(*, transport: HttpTransport | None = None) -> list[ModelEntry]:
    """Fetch + persist + return picker entries. Raises CatalogError."""
    rows = fetch_model_rows(transport=transport)
    entries = build_entries(rows)
    if not entries:
        raise CatalogError("empty")
    _write_cache(rows)
    _log.info("OpenRouter catalog refreshed: %d models", len(entries))
    return entries


def cached_models() -> list[ModelEntry]:
    """Last fetched catalog as picker entries; [] when never fetched."""
    return build_entries(_read_cache())


def pricing_for(model_id: str) -> tuple[float, float] | None:
    """Model-level ``(input, output)`` dollars per token, or None when unusable.

    The *endpoint* price is authoritative and is what ``routes.Route`` carries.
    This is the fallback for the two cases where there is no endpoint price: an
    unpinned session (endpoint discovery failed) and a pinned endpoint whose
    payload omitted or mangled its pricing. Without it those sessions had no
    monetary bound at all — only the token budget, which is not money (a
    review finding).

    Model-level figures are the maximum across that model's endpoints, so using
    them as a stand-in is conservative in the right direction: it can hold a
    round back, never wave one through that the endpoint price would have
    caught. Same validation as ``routes._price`` — finite and non-negative, so
    a sentinel or ``"Infinity"`` reads as unknown rather than free.
    """
    for row in _read_cache():
        if not isinstance(row, dict) or row.get("id") != model_id:
            continue
        pricing = row.get("pricing")
        if not isinstance(pricing, dict):
            return None
        values = []
        for key in ("prompt", "completion"):
            try:
                value = float(pricing.get(key))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value) or value < 0:
                return None
            values.append(value)
        return values[0], values[1]
    return None


def context_length_for(model_id: str) -> int:
    """Advertised context window from the cache; 0 when unknown."""
    for row in _read_cache():
        if isinstance(row, dict) and row.get("id") == model_id:
            context = row.get("context_length")
            return context if isinstance(context, int) and context > 0 else 0
    return 0


def _read_cache() -> list[dict]:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []


def _write_cache(rows: list[dict]) -> None:
    tmp: Path | None = None
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Unique tmp name per call: a key save fans out to concurrent
        # refreshes (Settings + main window), and a shared tmp path raced —
        # one winner's replace() left the loser's chmod/replace hitting
        # ENOENT ("No such file or directory: …json.tmp").
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{CACHE_PATH.name}.", suffix=".tmp", dir=CACHE_PATH.parent
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"fetched_at": int(time.time()), "rows": rows}))
        os.chmod(tmp, 0o600)
        tmp.replace(CACHE_PATH)
    except OSError as e:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        _log.warning("could not write %s: %s", CACHE_PATH, e)
