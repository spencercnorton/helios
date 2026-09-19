"""Finite, evidence-aware tool rounds; no provider retries or spend renewal."""

from __future__ import annotations

import hashlib
import json
from collections import deque


class ToolRoundPolicy:
    """Extend a productive window, or pause loops with a metered handoff.

    Novel tool output is a continuation heuristic, never proof of task
    completion. The hard ceiling and the driver's Work dollar guard remain
    authoritative even if a model keeps producing novel but unhelpful output.
    """

    def __init__(self, window: int, hard_limit: int):
        self.window = max(1, window)
        self.hard_limit = max(self.window, hard_limit)
        self.limit = self.window
        self.rounds = 0
        self._evidence: set[str] = set()
        self._recent = deque(maxlen=5)
        self._last_round = ""
        self._repetitions = 0

    @staticmethod
    def _digest(value) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def observe(self, outcomes: list[tuple[str, str, str, bool]]) -> None:
        """Observe (name, arguments, scrubbed result, error) in request order."""
        self.rounds += 1
        signature = self._digest(outcomes)
        self._repetitions = self._repetitions + 1 if signature == self._last_round else 1
        self._last_round = signature
        novel = False
        for name, arguments, content, error in outcomes:
            if error or name in {"update_plan", "ExitPlanMode", "checkpoint_context"}:
                continue
            # Varying a shell command while it returns the same output is not
            # new evidence. Writes/edits include their payload because their
            # receipts often only say "written" regardless of the change.
            evidence = self._digest((name, arguments if name in {"Write", "Edit"} else "", content))
            if content.strip() and evidence not in self._evidence:
                novel = True
                self._evidence.add(evidence)
        self._recent.append(novel)

    def pause_reason(self) -> str:
        if self._repetitions >= 3:
            return "tool_stalled"
        if self.rounds < self.limit:
            return ""
        if self.limit < self.hard_limit and any(self._recent):
            self.limit = min(self.hard_limit, self.limit + self.window)
            return ""
        return "tool_round_limit"
