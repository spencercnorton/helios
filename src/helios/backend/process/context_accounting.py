"""GTK-free context-window accounting for the Claude CLI driver.

The stream driver is a GObject and imports PyGObject at module scope.  The
accounting itself is pure data handling, so it lives here where the slim CI
lane can import and test it without silently skipping on a missing GTK stack.
"""

from __future__ import annotations

from typing import Protocol


class ContextMeterState(Protocol):
    """The small, read-only slice of driver state used by the meter."""

    _model: str
    _ctx_request_input: int
    _ctx_request_output: int


def model_family(model: str) -> str:
    """Return the Claude model family used to select a context window."""

    normalized = (model or "").lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in normalized:
            return family
    return ""


def prompt_tokens(usage: dict) -> int:
    """Tokens the model had to read for one request."""

    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )


def main_model_context(
    state: ContextMeterState,
    result: dict,
) -> tuple[int, int]:
    """Return the main conversation's current ``(used, window)`` values.

    Three fields in a result record have different meanings:

    * top-level ``usage`` is cumulative over every API request in the turn;
    * ``usage.iterations[-1]`` is the final request and therefore the current
      window occupancy;
    * ``modelUsage`` counters are cumulative across the whole session, while
      each entry's ``contextWindow`` is the authoritative capacity.

    The selected model family (including its ``[1m]`` variant) chooses the
    capacity.  When the CLI omits iterations, the streamed per-request counts
    retained on ``state`` are the exact fallback.
    """

    model_usage = result.get("modelUsage") or {}
    windows = [
        (name, int(entry.get("contextWindow") or 0))
        for name, entry in model_usage.items()
        if isinstance(entry, dict)
    ]
    if not windows:
        return 0, 0

    window = 0
    family = model_family(state._model)
    if family:
        wants_1m = "[1m]" in (state._model or "")
        for name, candidate in windows:
            if family in name.lower() and ("[1m]" in name) == wants_1m:
                window = candidate
                break
    if window == 0:
        window = max(candidate for _, candidate in windows)

    usage = result.get("usage") or {}
    iterations = usage.get("iterations")
    last = (
        iterations[-1]
        if isinstance(iterations, list)
        and iterations
        and isinstance(iterations[-1], dict)
        else None
    )
    if last is not None:
        used = prompt_tokens(last) + int(last.get("output_tokens") or 0)
    elif state._ctx_request_input > 0:
        used = state._ctx_request_input + state._ctx_request_output
    else:
        used = prompt_tokens(usage) + int(usage.get("output_tokens") or 0)
    return used, window
