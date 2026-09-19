"""Conversation workflow modes, independent from permission profiles.

Workflow answers *how the model should approach the next response*; the
permission profile answers *what the provider may do while producing it*.
Keeping those axes separate is load-bearing: a read-only Default turn may
still answer a question, while native Plan mode must use Codex's advertised
collaboration preset and stop with a reviewable plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from helios.backend import model_catalog


DEFAULT_WORKFLOW_MODE = "default"
PLAN_WORKFLOW_MODE = "plan"


@dataclass(frozen=True, slots=True)
class WorkflowMode:
    key: str
    label: str
    description: str
    providers: frozenset[str]


WORKFLOW_MODE_DESCRIPTORS: tuple[WorkflowMode, ...] = (
    WorkflowMode(
        DEFAULT_WORKFLOW_MODE,
        "Default",
        "Answer or execute the request while maintaining a task plan when useful.",
        frozenset(
            {
                model_catalog.PROVIDER_ANTHROPIC,
                model_catalog.PROVIDER_OPENAI,
                model_catalog.PROVIDER_OPENROUTER,
            }
        ),
    ),
    WorkflowMode(
        PLAN_WORKFLOW_MODE,
        "Plan",
        "Investigate read-only and return a reviewable plan without implementing it.",
        frozenset({model_catalog.PROVIDER_OPENAI}),
    ),
)

WORKFLOW_MODES = tuple(mode.key for mode in WORKFLOW_MODE_DESCRIPTORS)
WORKFLOW_MODE_LABELS = {mode.key: mode.label for mode in WORKFLOW_MODE_DESCRIPTORS}
WORKFLOW_MODE_DESCRIPTIONS = {
    mode.key: mode.description for mode in WORKFLOW_MODE_DESCRIPTORS
}
_WORKFLOW_BY_KEY = {mode.key: mode for mode in WORKFLOW_MODE_DESCRIPTORS}


def canonical_workflow_mode(value: object) -> str:
    """Return a known workflow key, defaulting safely to Default."""

    mode = str(value or "").strip()
    return mode if mode in _WORKFLOW_BY_KEY else DEFAULT_WORKFLOW_MODE


def workflow_modes_for_provider(
    provider: str,
    *,
    advertised: tuple[str, ...] | list[str] | frozenset[str] = (),
) -> tuple[str, ...]:
    """Return selectable modes in stable UI order.

    Codex modes are capability-gated.  Other providers currently expose only
    Default even though their permission pickers may separately offer a
    read-only profile.
    """

    advertised_modes = {
        canonical_workflow_mode(mode)
        for mode in advertised
        if str(mode or "").strip() in WORKFLOW_MODES
    }
    if provider != model_catalog.PROVIDER_OPENAI:
        advertised_modes = {DEFAULT_WORKFLOW_MODE}
    elif not advertised_modes:
        advertised_modes = {DEFAULT_WORKFLOW_MODE}
    advertised_modes.add(DEFAULT_WORKFLOW_MODE)
    return tuple(
        descriptor.key
        for descriptor in WORKFLOW_MODE_DESCRIPTORS
        if provider in descriptor.providers and descriptor.key in advertised_modes
    )


def provider_allows_workflow(
    provider: str,
    mode: str,
    *,
    advertised: tuple[str, ...] | list[str] | frozenset[str] = (),
) -> bool:
    return mode in workflow_modes_for_provider(provider, advertised=advertised)
