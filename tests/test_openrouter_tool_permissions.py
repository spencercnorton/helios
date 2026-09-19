"""Workspace-scoped permission gating for OpenRouter tool calls (v0.43.0).

Before this, ``decide`` judged on tool name alone: ``plan`` and ``dontAsk`` —
the modes a user picks *for* safety — auto-approved Read/Grep/Glob anywhere on
disk, so plan mode would silently pull ``~/.ssh/id_rsa`` into the prompt and
ship it to whichever model was selected.
"""

from __future__ import annotations

import json
import shlex
import threading
import time

import pytest

from helios.backend.process import openrouter_tools as t
from helios.backend.openrouter.gateway import CancellationToken


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    return root


class TestContainment:
    def test_relative_path_is_inside(self, workspace):
        assert not t.touches_outside_workspace(
            "Read", {"path": "sub/app.py"}, str(workspace)
        )

    def test_absolute_path_inside_is_inside(self, workspace):
        assert not t.touches_outside_workspace(
            "Read", {"path": str(workspace / "sub" / "app.py")}, str(workspace)
        )

    def test_sibling_directory_is_outside(self, workspace):
        assert t.touches_outside_workspace(
            "Read", {"path": str(workspace.parent / "secrets" / "id_rsa")},
            str(workspace),
        )

    def test_dot_dot_escape_is_outside(self, workspace):
        assert t.touches_outside_workspace(
            "Read", {"path": "../secrets/id_rsa"}, str(workspace)
        )

    def test_symlink_out_of_the_workspace_is_outside(self, workspace):
        link = workspace / "shortcut"
        link.symlink_to(workspace.parent / "secrets" / "id_rsa")
        assert t.touches_outside_workspace(
            "Read", {"path": "shortcut"}, str(workspace)
        )

    def test_omitted_path_defaults_to_the_workspace(self, workspace):
        assert not t.touches_outside_workspace("Grep", {"pattern": "x"}, str(workspace))
        assert not t.touches_outside_workspace(
            "Glob", {"pattern": "*.py", "path": ""}, str(workspace)
        )

    def test_bash_is_not_path_checked(self, workspace):
        """Bash already asks in every mode except bypassPermissions and is
        denied in plan/dontAsk, so it needs no containment rule of its own."""
        assert not t.touches_outside_workspace(
            "Bash", {"command": "cat ~/.ssh/id_rsa"}, str(workspace)
        )

    def test_malformed_path_is_reported_outside(self, workspace):
        assert t.touches_outside_workspace("Read", {"path": 17}, str(workspace))

    @pytest.mark.parametrize(
        "raw",
        [
            "~nosuchuser9999xyz/config",  # expanduser -> RuntimeError
            "bad\x00path",                # resolve    -> ValueError
        ],
    )
    def test_unresolvable_paths_fail_closed(self, workspace, raw):
        """Neither of these raises an OSError, so the original
        `except (ToolFailed, OSError)` let them through. The gate runs outside
        execute_tool's blanket handler, so anything escaping here unwinds the
        caller's tool loop after the assistant tool_calls message is already in
        the history, orphaning it permanently."""
        assert t.touches_outside_workspace("Read", {"path": raw}, str(workspace)) is True

    def test_unresolvable_cwd_fails_closed(self):
        assert t.touches_outside_workspace(
            "Read", {"path": "a.py"}, "~nosuchuser9999xyz/project"
        ) is True

    @pytest.mark.parametrize(
        "raw",
        [
            "~nosuchuser9999xyz/config",
            "bad\x00path",
            "\udc80/surrogate",       # lone surrogate: raises on some platforms
            "",
            "." * 5000,
            "/proc/self/root/etc/passwd",
        ],
    )
    def test_gate_never_raises_whatever_the_model_sends(self, workspace, raw):
        """The contract that actually matters. Path arguments come straight
        from model-generated JSON, and this is the one call in the per-call
        tool loop that is not already wrapped — whatever it does, it must
        return a bool rather than unwind."""
        for tool in ("Read", "Write", "Edit", "Grep", "Glob", "Bash"):
            assert isinstance(
                t.touches_outside_workspace(tool, {"path": raw, "file_path": raw},
                                            str(workspace)),
                bool,
            )


class TestTraversalContainment:
    def test_grep_does_not_follow_a_symlink_out_of_the_workspace(self, workspace):
        """os.walk will not descend a symlinked directory, but it still lists
        symlinked files and _read_text follows them — so Grep, judged
        in-workspace on its root argument alone and therefore auto-approved in
        every mode, could read arbitrary files outside it."""
        (workspace / "leak.txt").symlink_to(
            workspace.parent / "secrets" / "id_rsa"
        )
        content, is_error = t.execute_tool(
            "Grep", {"pattern": "PRIVATE"}, cwd=str(workspace)
        )
        assert not is_error
        assert "PRIVATE KEY" not in content
        assert content == "(no matches)"

    def test_glob_does_not_list_a_symlink_out_of_the_workspace(self, workspace):
        (workspace / "leak.key").symlink_to(
            workspace.parent / "secrets" / "id_rsa"
        )
        content, _ = t.execute_tool(
            "Glob", {"pattern": "*.key"}, cwd=str(workspace)
        )
        assert content == "(no files matched)"

    def test_ordinary_files_still_traverse(self, workspace):
        content, is_error = t.execute_tool(
            "Grep", {"pattern": "print"}, cwd=str(workspace)
        )
        assert not is_error
        assert "app.py" in content

    def test_an_explicitly_approved_outside_root_still_searches(self, workspace):
        """Containment is bounded by the search root, not the workspace: once
        the gate has escalated and the user approved a search outside, files
        under that root are in scope."""
        content, is_error = t.execute_tool(
            "Grep",
            {"pattern": "PRIVATE", "path": str(workspace.parent / "secrets")},
            cwd=str(workspace),
        )
        assert not is_error
        assert "PRIVATE KEY" in content


class TestApprovalDisclosure:
    def test_prompt_discloses_an_out_of_workspace_target(self):
        summary = t.approval_summary(
            "Write",
            {"file_path": "../../elsewhere/x"},
            outside_path="/home/alice/elsewhere/x",
        )
        assert "OUTSIDE the working directory" in summary
        assert "/home/alice/elsewhere/x" in summary

    def test_prompt_is_unchanged_for_in_workspace_calls(self):
        summary = t.approval_summary("Write", {"file_path": "src/x.py"})
        assert "OUTSIDE" not in summary
        assert summary.startswith("Allow OpenRouter to use Write?")

    def test_long_command_truncation_is_visible(self):
        """F-11: a silent clean[:1200] could hide a long command's dangerous
        tail from the one dialog where the user decides whether to run it."""
        long_command = "x" * 5000
        summary = t.approval_summary("Bash", {"command": long_command})
        assert long_command not in summary
        assert "+3,800 chars not shown" in summary

    def test_short_command_is_not_marked_truncated(self):
        summary = t.approval_summary("Bash", {"command": "ls -la"})
        assert "not shown" not in summary

    def test_outside_path_truncation_is_visible(self):
        long_path = "/" + ("a" * 5000)
        summary = t.approval_summary(
            "Write", {"file_path": "x"}, outside_path=long_path
        )
        assert "chars not shown" in summary

    def test_input_fallback_truncation_is_visible(self):
        # No recognized detail key, so this falls to the redacted-JSON path.
        summary = t.approval_summary(
            "ExitPlanMode", {"plan": [{"step": "y" * 5000}]}
        )
        assert "Input:" in summary
        assert "chars not shown" in summary

    def test_destination_is_disclosed_verbatim(self):
        summary = t.approval_summary(
            "Bash",
            {"command": "env"},
            destination="deepseek/deepseek-v4-flash via Alibaba",
        )
        assert "deepseek/deepseek-v4-flash via Alibaba" in summary
        assert "output" in summary.lower()
        assert "leaves this machine" in summary

    def test_no_destination_means_no_disclosure(self):
        summary = t.approval_summary("Bash", {"command": "env"})
        assert "leaves this machine" not in summary
        assert "Sent to:" not in summary

    def test_destination_is_keyword_only_and_defaulted(self):
        """Every existing caller passes positional (tool_name, raw_input)
        plus keyword-only extras; adding `destination` must not disturb that
        or any caller that never mentions it."""
        with pytest.raises(TypeError):
            t.approval_summary("Bash", {"command": "env"}, "somewhere")


class TestDecide:
    @pytest.mark.parametrize("mode", ["plan", "dontAsk"])
    def test_read_outside_workspace_is_denied_in_safety_modes(self, mode):
        assert t.decide("Read", mode) == "auto"
        assert t.decide("Read", mode, outside_workspace=True) == "deny"

    @pytest.mark.parametrize("mode", ["default", "auto", "acceptEdits"])
    def test_read_outside_workspace_escalates_to_ask(self, mode):
        assert t.decide("Grep", mode) == "auto"
        assert t.decide("Grep", mode, outside_workspace=True) == "ask"

    @pytest.mark.parametrize("mode", ["acceptEdits", "auto"])
    @pytest.mark.parametrize("tool", ["Write", "Edit"])
    def test_workspace_edit_modes_ask_before_writing_outside(self, mode, tool):
        assert t.decide(tool, mode) == "auto"
        assert t.decide(tool, mode, outside_workspace=True) == "ask"

    @pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "mcp__x"])
    def test_bypass_runs_everything_without_asking(self, tool):
        """Same contract as Claude's Bypass: no prompt inside or outside the
        workspace. 58 exact-command approvals in one Kimi K3 turn is why."""
        assert t.decide(tool, "bypassPermissions") == "auto"
        assert t.decide(tool, "bypassPermissions", outside_workspace=True) == "auto"

    def test_inside_workspace_behavior_is_unchanged(self):
        assert t.decide("Read", "plan") == "auto"
        assert t.decide("Write", "plan") == "deny"
        assert t.decide("Bash", "acceptEdits") == "ask"
        assert t.decide("Bash", "dontAsk") == "deny"
        assert t.decide("Unknown", "default") == "ask"

    def test_denial_message_names_the_real_reason(self):
        message = t.denial_message("Read", "plan", outside_workspace=True)
        assert "outside the working directory" in message
        assert "read-only" not in message


class TestEndToEndGate:
    """The gate is only real if the driver consults it — exercise the same
    path ``_execute_tool_call`` takes, without GTK."""

    def test_plan_mode_refuses_to_read_a_key_outside_the_project(self, workspace):
        arguments = {"path": str(workspace.parent / "secrets" / "id_rsa")}
        outside = t.touches_outside_workspace("Read", arguments, str(workspace))
        assert t.decide("Read", "plan", outside_workspace=outside) == "deny"

    def test_plan_mode_still_reads_project_files(self, workspace):
        arguments = {"path": "sub/app.py"}
        outside = t.touches_outside_workspace("Read", arguments, str(workspace))
        assert t.decide("Read", "plan", outside_workspace=outside) == "auto"
        content, is_error = t.execute_tool("Read", arguments, cwd=str(workspace))
        assert not is_error
        assert "print" in content


def test_bash_cancellation_kills_process_group_and_spawned_child(tmp_path):
    token = CancellationToken()
    ready = tmp_path / "ready"
    survived = tmp_path / "child-survived"
    command = (
        f"( sleep 0.8; echo survived > {shlex.quote(str(survived))} ) & "
        f"echo ready > {shlex.quote(str(ready))}; wait"
    )
    result = []

    worker = threading.Thread(
        target=lambda: result.append(
            t.execute_tool(
                "Bash",
                {"command": command, "timeout": 30},
                cwd=str(tmp_path),
                cancellation=token,
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "Bash process did not reach its child wait"

    token.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive(), "cancelled Bash stayed blocked in communicate()"
    assert result and result[0][1] is True
    assert "process group was killed" in result[0][0]
    time.sleep(1)
    assert not survived.exists(), "spawned Bash child survived Work cancellation"


class TestRoutingPolicy:
    def test_chat_requests_deny_data_collection_and_require_parameters(self):
        from helios.backend.openrouter import chat as or_chat

        body = json.loads(
            or_chat._build_body(
                [{"role": "user", "content": "hi"}],
                model="vendor/model",
                tools=(),
            )
        )
        provider = body["provider"]
        assert provider["data_collection"] == "deny"
        assert provider["require_parameters"] is True
        assert provider["allow_fallbacks"] is False
