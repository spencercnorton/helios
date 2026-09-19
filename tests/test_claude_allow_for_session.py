"""Claude approvals gain "allow for session" — the Codex parity gap.

Codex approvals have offered `acceptForSession` since the App Server landed;
Claude's offered Allow once / Deny, so a default-mode session re-prompted for
every single call of the same tool.

The grant is enforced by the CLI, not by a Helios-side cache: the response to
`can_use_tool` carries `updatedPermissions`, which the CLI folds into its own
session permission context. That was verified against the installed
claude 2.1.245 rather than inferred —

  * the accept-set for a host-supplied permission update is exactly
    `{"localSettings", "session"}` (any other destination is dropped), and an
    update whose behavior is not "allow" is dropped too;
  * a live run answering ONE `can_use_tool` for `touch /tmp/probe518/a` with
    `rules: [{toolName: "Bash", ruleContent: "touch:*"}]`, destination
    `"session"`, let the next two `touch` calls run unprompted and **still
    raised a prompt for `rm -f`** — the CLI's matcher does the scoping;
  * no settings file was written by that run (destination "session" is
    memory-only), so the grant dies with the process.

Which is why Bash is scoped by command and everything else is tool-wide, and
why a compound command gets no session option at all: the first token of
`touch a && rm -rf b` is "touch", and a button reading "Allow all touch
commands" is not the grant that would be made.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.backend.process.cli_driver import (  # noqa: E402
    ClaudeCliDriver,
    session_permission_rule,
)


def _driver():
    driver = ClaudeCliDriver(cwd="/tmp", permission_mode="default")
    writes: list[dict] = []
    driver._write_stdin_line = writes.append
    return driver, writes


def _request(tool_name="Bash", tool_input=None):
    return {
        "request_id": "request-1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": tool_name,
            "tool_use_id": "tool-1",
            "input": {"command": "make test"} if tool_input is None else tool_input,
        },
    }


def _ask(driver, request):
    seen: list[tuple[dict, str]] = []
    driver.connect(
        "question-asked",
        lambda _d, payload, token: seen.append((payload, token)),
    )
    driver._handle_control_request(request)
    return seen[0]


# ── the rule builder ───────────────────────────────────────────────────────


def test_bash_is_scoped_to_the_command_not_to_the_whole_shell():
    assert session_permission_rule("Bash", {"command": "make test"}) == {
        "toolName": "Bash",
        "ruleContent": "make:*",
    }


def test_a_compound_bash_command_gets_no_session_grant():
    """The first token of a compound command is not what the grant covers."""

    for command in (
        "touch a && rm -rf b",
        "cat x | sh",
        "ls; rm -rf /",
        "echo $(whoami)",
        "sh -c `id`",
        "cat f > /etc/passwd",
    ):
        assert session_permission_rule("Bash", {"command": command}) is None, command


def test_a_command_that_cannot_be_named_plainly_gets_no_session_grant():
    assert session_permission_rule("Bash", {"command": '"my prog" x'}) is None
    assert session_permission_rule("Bash", {"command": "$CMD x"}) is None
    assert session_permission_rule("Bash", {"command": ""}) is None
    assert session_permission_rule("Bash", None) is None


def test_non_bash_tools_are_granted_tool_wide():
    assert session_permission_rule("Read", {"file_path": "/etc/hosts"}) == {
        "toolName": "Read"
    }
    assert session_permission_rule("mcp__helios__dispatch", {}) == {
        "toolName": "mcp__helios__dispatch"
    }
    assert session_permission_rule("", {}) is None


# ── the prompt ─────────────────────────────────────────────────────────────


def test_the_prompt_offers_a_third_option_that_names_the_grant():
    driver, _writes = _driver()
    payload, _token = _ask(driver, _request())
    labels = [o["label"] for o in payload["questions"][0]["options"]]
    assert labels == [
        "Allow once",
        "Allow all make commands for this session",
        "Deny",
    ]


def test_a_compound_command_still_offers_only_allow_once_and_deny():
    driver, _writes = _driver()
    payload, _token = _ask(
        driver, _request(tool_input={"command": "make test && rm -rf build"})
    )
    labels = [o["label"] for o in payload["questions"][0]["options"]]
    assert labels == ["Allow once", "Deny"]


# ── the answer ─────────────────────────────────────────────────────────────


def test_choosing_the_session_option_sends_a_session_scoped_add_rule():
    driver, writes = _driver()
    payload, token = _ask(driver, _request())
    driver.answer_question(token, payload["questions"][0]["options"][1]["label"])

    assert writes[0]["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "make test"},
        "updatedPermissions": [
            {
                "type": "addRules",
                "behavior": "allow",
                "destination": "session",
                "rules": [{"toolName": "Bash", "ruleContent": "make:*"}],
            }
        ],
    }


def test_allow_once_is_unchanged_and_grants_nothing_beyond_the_call():
    driver, writes = _driver()
    _payload, token = _ask(driver, _request())
    driver.answer_question(token, "Allow once")
    assert writes[0]["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "make test"},
    }


def test_deny_and_dismissal_never_carry_a_permission_update():
    for answer in ("Deny", None, "Allow all rm commands for this session"):
        driver, writes = _driver()
        _payload, token = _ask(driver, _request())
        driver.answer_question(token, answer)
        response = writes[0]["response"]["response"]
        assert response["behavior"] == "deny", answer
        assert "updatedPermissions" not in response, answer
