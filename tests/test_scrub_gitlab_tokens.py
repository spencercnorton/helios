"""GitLab tokens must not survive into the model or the Work ledger.

Verified missing 2026-08-06 while building the work contract: a GitLab personal
access token pasted into an objective or a definition of done reached both the
model and the durable ledger **in the clear**, while `sk-ant-*`, `AKIA*` and
`password=` were all caught. This is the one estate whose tokens are handled
constantly, so the gap mattered more than the list length suggests.

Tokens here are GENERATED, never literal — a realistic literal in a fixture is
what tripped the `secret_scan` job on helios!122.
"""

from __future__ import annotations

import secrets
import string

import pytest

from helios.backend.sensitive_text import REDACTED, scrub_sensitive


def _token(prefix: str, length: int = 20) -> str:
    body = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(length)
    )
    return f"{prefix}-{body}"


# Every routable prefix GitLab documents.
PREFIXES = [
    "glpat",    # personal access token
    "glptt",    # project trigger token
    "gldt",     # deploy token
    "glrt",     # runner token
    "glcbt",    # CI build token
    "glsoat",   # SCIM OAuth token
    "glimt",    # incoming mail token
    "glagent",  # agent token
    "glffct",   # feature flag client token
    "gloat",    # OAuth application secret
]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_every_gitlab_token_prefix_is_redacted(prefix: str) -> None:
    token = _token(prefix)

    scrubbed, hit = scrub_sensitive(token)

    assert token not in scrubbed
    assert scrubbed == REDACTED
    assert hit is True


def test_a_token_embedded_in_prose_is_redacted_in_place() -> None:
    """The realistic shape: pasted mid-sentence into an objective."""

    token = _token("glpat")
    text = f"deploy using {token} then verify"

    scrubbed, _hit = scrub_sensitive(text)

    assert token not in scrubbed
    assert scrubbed.startswith("deploy using ")
    assert scrubbed.endswith(" then verify")


def test_it_reaches_the_work_contract_fields() -> None:
    """Objective and definition_of_done both go through this scrubber."""

    from helios.backend import session_goals

    token = _token("glpat")
    goal = session_goals.GoalState(
        objective=f"rotate {token}", definition_of_done=f"old {token} revoked"
    )

    safe = session_goals._normalize_goal(goal)

    assert token not in safe.objective
    assert token not in safe.definition_of_done


def test_ordinary_words_starting_with_gl_are_untouched() -> None:
    """An over-broad `gl[a-z]+-` pattern would eat real prose."""

    text = "global-warming glue-stick gl-inspect glass-box"

    scrubbed, hit = scrub_sensitive(text)

    assert scrubbed == text
    assert hit is False


def test_a_short_lookalike_is_not_a_token() -> None:
    """20+ chars of body is the floor; `glpat-abc` is somebody's variable."""

    scrubbed, _hit = scrub_sensitive("glpat-abc")

    assert scrubbed == "glpat-abc"


def test_the_previously_covered_shapes_still_work() -> None:
    """Guard against a regex edit breaking a sibling alternative."""

    for token in (_token("sk-ant-api03", 24), "AKIA" + "A" * 16):
        scrubbed, _hit = scrub_sensitive(token)
        assert token not in scrubbed
