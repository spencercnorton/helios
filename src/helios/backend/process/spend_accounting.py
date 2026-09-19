"""Cumulative token spend for a Claude session, split root vs delegated.

Deliberately a separate module from ``context_accounting``, which measures how
*full* the window is right now. Conflating the two is the defect this exists to
answer: the 2026-08-03 runaway put 89.4% of its burn on subagent fan-out, and
the only number on screen was a window-occupancy gauge that excludes subagent
traffic by design — correct for a context meter, and useless as a cost signal.
Keeping them in one module is how they get confused again.

Two channels, two meanings (measured; see the private model-usage capture notes):

* top-level ``usage`` on a root ``result`` is **per-turn and root-only** — it
  excludes subagent traffic;
* ``modelUsage`` is **cumulative for the process and includes** it.

So delegated spend is exactly the difference, and it is an identity rather than
an estimate — the stage-1 capture showed zero drift on every turn that ran no
subagents. It is also the only way to get the split at all: a subagent that
inherits the root's model has its spend merged into the root's ``modelUsage``
entry and is not separable there.

Both counters restart at zero in a fresh process, including one that resumes an
existing session with ``--resume`` (measured on claude 2.1.235: a resumed run
reported only its own turn). So no baseline is needed, and a snapshot describes
**this process**, not the session's whole history — a driver respawn resets it.

Tokens only, never dollars. ``costUSD`` is a notional API-equivalent estimate on
a subscription account, not a charge, and a precise, prominently displayed
number that is not the thing it appears to be is the exact bug this replaces.

GTK-free so the slim CI lane can test it.
"""

from __future__ import annotations

from dataclasses import dataclass

from helios.log import get_logger

_log = get_logger("spend")

__all__ = [
    "ModelSpend",
    "SpendAccumulator",
    "SpendSnapshot",
    "model_spend_rows",
    "root_turn_tokens",
]

#: The four token counters that make up billed volume, in `modelUsage` spelling.
_MODEL_FIELDS = (
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("cache_read_tokens", "cacheReadInputTokens"),
    ("cache_creation_tokens", "cacheCreationInputTokens"),
)

#: The same four in top-level `usage` spelling. Different case convention on
#: the same wire, which is a fine way to lose a counter silently.
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _int(value: object) -> int:
    """A counter, or zero. Never raises on drift or a hostile payload."""
    return value if type(value) is int and value >= 0 else 0


@dataclass(frozen=True, slots=True)
class ModelSpend:
    """Cumulative tokens for one model id within this process."""

    model: str
    canonical: str
    provider: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    web_search_requests: int

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


@dataclass(frozen=True, slots=True)
class SpendSnapshot:
    """Where this process's tokens went, as of the latest root result."""

    total_tokens: int
    root_tokens: int
    delegated_tokens: int
    models: tuple[ModelSpend, ...]

    @property
    def delegated_fraction(self) -> float:
        """Share of total spend attributable to delegation, 0.0–1.0."""
        return self.delegated_tokens / self.total_tokens if self.total_tokens else 0.0

    @property
    def has_delegation(self) -> bool:
        return self.delegated_tokens > 0


def model_spend_rows(result: object) -> tuple[ModelSpend, ...]:
    """Per-model cumulative rows from one ``result`` record.

    ``canonicalModel`` groups the ``[1m]`` variants that the family-substring
    match elsewhere only approximates; it falls back to the raw key when the CLI
    omits it.
    """
    if not isinstance(result, dict):
        return ()
    usage = result.get("modelUsage")
    if not isinstance(usage, dict):
        return ()
    rows: list[ModelSpend] = []
    for name, entry in usage.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        counters = {field: _int(entry.get(key)) for field, key in _MODEL_FIELDS}
        rows.append(
            ModelSpend(
                model=name,
                canonical=str(entry.get("canonicalModel") or name),
                provider=str(entry.get("provider") or ""),
                web_search_requests=_int(entry.get("webSearchRequests")),
                **counters,
            )
        )
    rows.sort(key=lambda row: (-row.total_tokens, row.model))
    return tuple(rows)


def root_turn_tokens(result: object) -> int:
    """Root-only tokens for one turn, from the record's top-level ``usage``.

    Deliberately *not* ``usage.iterations[-1]``: that is the last request and is
    what the context meter wants. Spend wants everything the turn burned.
    """
    if not isinstance(result, dict):
        return 0
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return 0
    return sum(_int(usage.get(field)) for field in _USAGE_FIELDS)


class SpendAccumulator:
    """Track one process's spend across the root results it emits.

    One user message can produce several root results — with background
    subagents the root replies, then wakes again as each child finishes — so
    every root result contributes its turn to the root total, and ``modelUsage``
    is simply read at its latest value.
    """

    def __init__(self) -> None:
        self._root_tokens = 0
        self._models: tuple[ModelSpend, ...] = ()
        self._total_tokens = 0
        self._warned_negative = False

    def observe(self, result: object) -> SpendSnapshot | None:
        """Fold one ``result`` record in. Returns None when it carries nothing.

        A child result is ignored: its spend is already inside the cumulative
        ``modelUsage`` the root reports, so counting it here would inflate the
        root side and erase the very delegation the split exists to show.
        """
        if not isinstance(result, dict) or result.get("parent_tool_use_id"):
            return None
        rows = model_spend_rows(result)
        if rows:
            self._models = rows
            self._total_tokens = sum(row.total_tokens for row in rows)
        self._root_tokens += root_turn_tokens(result)
        if not self._models:
            return None
        return self.snapshot()

    def snapshot(self) -> SpendSnapshot:
        delegated = self._total_tokens - self._root_tokens
        if delegated < 0:
            # The identity is exact when both channels mean what they were
            # measured to mean, so a negative is schema drift, not arithmetic.
            # Clamp rather than render a negative, and say so once — a wrong
            # number that looks plausible is worse than one that looks broken,
            # but a log line nobody reads is worse than both.
            if not self._warned_negative:
                self._warned_negative = True
                _log.warning(
                    "delegated spend went negative (modelUsage %d < root %d); "
                    "reporting 0 — the usage/modelUsage identity no longer holds",
                    self._total_tokens,
                    self._root_tokens,
                )
            delegated = 0
        return SpendSnapshot(
            total_tokens=self._total_tokens,
            root_tokens=min(self._root_tokens, self._total_tokens),
            delegated_tokens=delegated,
            models=self._models,
        )
