"""codex_events — JSONL translation, tested against a captured real stream
(codex-cli 0.139.0, 2026-06-10). GTK-free."""

from __future__ import annotations

import pytest


from helios.backend.process import codex_events as ce


# Verbatim lines from the live probe (thread id shortened).
REAL_STREAM = [
    '{"type":"thread.started","thread_id":"019eb428-2fb0"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"I\'ll run the requested shell command."}}',
    '{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/bin/bash -c \'echo helios-probe-42\'","aggregated_output":"","exit_code":null,"status":"in_progress"}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/bash -c \'echo helios-probe-42\'","aggregated_output":"helios-probe-42\\n","exit_code":0,"status":"completed"}}',
    '{"type":"item.started","item":{"id":"item_3","type":"file_change","changes":[{"path":"/tmp/codex-probe/probe.txt","kind":"add"}],"status":"in_progress"}}',
    '{"type":"item.completed","item":{"id":"item_3","type":"file_change","changes":[{"path":"/tmp/codex-probe/probe.txt","kind":"add"}],"status":"completed"}}',
    '{"type":"item.completed","item":{"id":"item_4","type":"agent_message","text":"DONE"}}',
    '{"type":"turn.completed","usage":{"input_tokens":31186,"cached_input_tokens":20224,"output_tokens":212,"reasoning_output_tokens":66}}',
]


def drain(acc: ce.CodexTurnAccumulator, lines: list[str]) -> list[ce.Action]:
    out: list[ce.Action] = []
    for line in lines:
        out.extend(acc.feed_line(line))
    return out


def test_real_stream_end_to_end():
    acc = ce.CodexTurnAccumulator(model="gpt-5.4-mini")
    actions = drain(acc, REAL_STREAM)
    kinds = [a.kind for a in actions]

    assert kinds[0] == ce.ACT_SESSION_STARTED
    assert actions[0].payload == "019eb428-2fb0"
    assert acc.thread_id == "019eb428-2fb0"

    # Streaming actions fired for turn start + every item event.
    assert kinds.count(ce.ACT_STREAMING) >= 6

    # Exactly one finalized turn + one result, in that order, at the end.
    assert kinds[-2:] == [ce.ACT_TURN, ce.ACT_RESULT]

    turn = actions[-2].payload
    assert turn.has_content
    assert turn.text_parts == ["DONE"]
    # Superseded prior agent narration is a public work update, not thinking.
    assert turn.commentary_parts == ["I'll run the requested shell command."]
    assert turn.thinking_parts == []
    assert "DONE" in turn.text_parts
    tools = {t.name for t in turn.tool_uses}
    assert tools == {"Bash", "Write"}
    bash = next(t for t in turn.tool_uses if t.name == "Bash")
    # /bin/bash -c wrapper stripped for display
    assert bash.input["command"] == "echo helios-probe-42"
    write = next(t for t in turn.tool_uses if t.name == "Write")
    assert write.input["file_path"] == "/tmp/codex-probe/probe.txt"
    assert [r.content for r in turn.tool_results] == [
        "helios-probe-42\n",
        "Changed /tmp/codex-probe/probe.txt",
    ]

    result = actions[-1].payload
    assert result["provider"] == "openai"
    usage = result["usage"]
    # codex input_tokens INCLUDES cached — the split must not double-count.
    assert usage["input_tokens"] == 31186 - 20224
    assert usage["cache_read_input_tokens"] == 20224
    assert usage["output_tokens"] == 212
    total = (usage["input_tokens"] + usage["cache_read_input_tokens"]
             + usage["output_tokens"])
    assert total == 31186 + 212


def test_streaming_blocks_update_in_place():
    acc = ce.CodexTurnAccumulator()
    drain(acc, REAL_STREAM[:4])  # through item.started of the command
    streaming = [a for a in drain(acc, [REAL_STREAM[4]]) if a.kind == ce.ACT_STREAMING][-1].payload
    # item_1 started then completed → still ONE block, mutated in place.
    bash_blocks = [b for b in streaming.blocks if b.tool_use_name == "Bash"]
    assert len(bash_blocks) == 1


def test_reconnecting_noise_suppressed_but_real_errors_surface():
    acc = ce.CodexTurnAccumulator()
    assert acc.feed({"type": "error", "message": "Reconnecting... 2/5 (401)"}) == []
    actions = acc.feed({"type": "error", "message": "stream disconnected"})
    assert [a.kind for a in actions] == [ce.ACT_ERROR]
    actions = acc.feed({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})
    assert [a.kind for a in actions] == [ce.ACT_ERROR]
    assert "401" in actions[0].payload


def test_session_started_suppressed_on_resume():
    acc = ce.CodexTurnAccumulator(model="gpt-5.4-mini")
    acc.thread_id = "existing-thread"
    acc._announced = True
    actions = acc.feed({"type": "thread.started", "thread_id": "existing-thread"})
    assert actions == []


def test_reasoning_maps_to_reasoning_summary():
    # `codex exec` reasoning text is the PUBLIC summary (App Server
    # Reasoning{summary} joined), so it lands in the reasoning_summary lane,
    # not the private thinking lane — and never carries raw hidden reasoning.
    acc = ce.CodexTurnAccumulator()
    acc.feed({"type": "turn.started"})
    acc.feed({"type": "item.completed",
              "item": {"id": "r1", "type": "reasoning", "text": "hmm"}})
    actions = acc.feed({"type": "turn.completed", "usage": {}})
    turn = next(a.payload for a in actions if a.kind == ce.ACT_TURN)
    assert turn.reasoning_summary_parts == ["hmm"]
    assert turn.thinking_parts == []


def test_exec_reasoning_reads_only_public_summary_text():
    """Privacy: the exec accumulator reads ONLY the reasoning item's public
    `text` (summary). Any other field a future/verbose exec build might attach
    (raw content, encrypted payload) is never surfaced."""
    import json

    acc = ce.CodexTurnAccumulator()
    acc.feed({"type": "turn.started"})
    all_actions = acc.feed({
        "type": "item.completed",
        "item": {
            "id": "r1",
            "type": "reasoning",
            "text": "Public summary only",
            "content": ["RAW-HIDDEN"],
            "encrypted_content": "RAW-HIDDEN",
        },
    })
    all_actions += acc.feed({"type": "turn.completed", "usage": {}})
    blob = json.dumps([a.payload for a in all_actions], default=str)
    assert "RAW-HIDDEN" not in blob
    turn = next(a.payload for a in all_actions if a.kind == ce.ACT_TURN)
    assert turn.reasoning_summary_parts == ["Public summary only"]


def test_prior_agent_messages_demote_to_commentary():
    acc = ce.CodexTurnAccumulator()
    acc.feed({"type": "turn.started"})
    acc.feed({"type": "item.completed",
              "item": {"id": "a1", "type": "agent_message", "text": "progress"}})
    acc.feed({"type": "item.completed",
              "item": {"id": "a2", "type": "agent_message", "text": "final"}})
    actions = acc.feed({"type": "turn.completed", "usage": {}})
    turn = next(a.payload for a in actions if a.kind == ce.ACT_TURN)
    # Only the latest agent message is the prominent final answer; earlier
    # public narration demotes to a visible work update, never to thinking.
    assert turn.text_parts == ["final"]
    assert turn.commentary_parts == ["progress"]
    assert turn.thinking_parts == []


@pytest.mark.parametrize("late_type", ["item.updated", "item.completed"])
def test_late_older_agent_replay_cannot_displace_latest_final(late_type):
    acc = ce.CodexTurnAccumulator()
    acc.feed({"type": "turn.started"})
    acc.feed(
        {
            "type": "item.completed",
            "item": {"id": "m1", "type": "agent_message", "text": "progress"},
        }
    )
    acc.feed(
        {
            "type": "item.completed",
            "item": {"id": "m2", "type": "agent_message", "text": "final"},
        }
    )

    live = acc.feed(
        {
            "type": late_type,
            "item": {
                "id": "m1",
                "type": "agent_message",
                "text": "progress replayed late",
            },
        }
    )[-1].payload
    assert [(block.type, block.text) for block in live.blocks] == [
        ("commentary", "progress replayed late"),
        ("text", "final"),
    ]

    actions = acc.feed({"type": "turn.completed", "usage": {}})
    turn = next(action.payload for action in actions if action.kind == ce.ACT_TURN)
    assert [(span.kind, span.text) for span in turn.content] == [
        ("commentary", "progress replayed late"),
        ("text", "final"),
    ]
    assert turn.text == "final"


def test_garbage_lines_ignored():
    acc = ce.CodexTurnAccumulator()
    assert acc.feed_line("not json at all") == []
    assert acc.feed_line("") == []


def test_mcp_web_search_todo_mapping():
    acc = ce.CodexTurnAccumulator()
    acc.feed({"type": "turn.started"})
    acc.feed({"type": "item.completed",
              "item": {"id": "m1", "type": "mcp_tool_call",
                       "server": "intel", "tool": "search"}})
    acc.feed({"type": "item.completed",
              "item": {"id": "w1", "type": "web_search", "query": "gtk4 css"}})
    acc.feed({"type": "item.completed",
              "item": {"id": "t1", "type": "todo_list", "items": [{"text": "x"}]}})
    actions = acc.feed({"type": "turn.completed", "usage": {}})
    turn = next(a.payload for a in actions if a.kind == ce.ACT_TURN)
    names = [t.name for t in turn.tool_uses]
    assert names == ["mcp__intel__search", "WebSearch", "TodoWrite"]
    assert turn.tool_uses[1].input == {"query": "gtk4 css"}


# ── argv builder ───────────────────────────────────────────────────────────


def test_argv_stale_bypass_value_stays_sandboxed():
    """Full access requires the App Server, never the retired exec transport."""
    argv = ce.build_exec_argv(
        "/usr/bin/codex", model="gpt-5.4-mini",
        permission_mode="bypassPermissions",
    )
    assert argv[:2] == ["/usr/bin/codex", "exec"]
    assert "resume" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[-1] == "-"  # prompt via stdin


def test_argv_unknown_mode_still_defaults_to_a_sandbox():
    """Junk must never select an unsandboxed Codex invocation."""
    argv = ce.build_exec_argv(
        "/usr/bin/codex", model="gpt-5.4-mini", permission_mode="not-a-mode",
    )
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--json" in argv and "--skip-git-repo-check" in argv
    m = argv.index("-m")
    assert argv[m + 1] == "gpt-5.4-mini"


def test_argv_resume_and_sandbox_mapping():
    argv = ce.build_exec_argv(
        "codex", model="o4-mini", permission_mode="plan",
        resume_thread_id="abc-123",
    )
    r = argv.index("resume")
    assert argv[r + 1] == "abc-123"
    s = argv.index("-s")
    assert argv[s + 1] == "read-only"
    # Regression: exec-level options MUST precede the `resume` subcommand.
    # codex-cli 0.139's `exec resume` parser rejects `-s` (and other exec
    # flags) placed after `resume <id>`, which silently broke every resume.
    assert s < r, f"sandbox flag must come before 'resume': {argv}"
    assert argv.index("--skip-git-repo-check") < r
    assert argv.index("-m") < r
    assert argv[-1] == "-" and argv[-2] == "abc-123"  # resume <id> then stdin

    argv2 = ce.build_exec_argv("codex", model="x", permission_mode="acceptEdits")
    assert argv2[argv2.index("-s") + 1] == "workspace-write"

    # Unknown mode → safe default, never the dangerous bypass.
    argv3 = ce.build_exec_argv("codex", model="x", permission_mode="someFutureMode")
    assert argv3[argv3.index("-s") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv3


def test_argv_safe_fallback_mode_is_sandboxed_not_bypassed():
    """The unconfigured-workspace fallback must map to a sandbox, not bypass."""
    from helios.backend.project_perms import SAFE_FALLBACK_MODE

    argv = ce.build_exec_argv("codex", model="x", permission_mode=SAFE_FALLBACK_MODE)
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_argv_reasoning_effort_precedes_resume():
    argv = ce.build_exec_argv(
        "codex",
        model="gpt-5.6-sol",
        permission_mode="plan",
        resume_thread_id="thread-56",
        effort="ultra",
    )
    config_idx = argv.index("-c")
    resume_idx = argv.index("resume")
    assert argv[config_idx + 1] == 'model_reasoning_effort="ultra"'
    assert config_idx < resume_idx


def test_shell_wrapper_stripping():
    assert ce._strip_shell_wrapper("/bin/bash -c 'echo hi'") == "echo hi"
    assert ce._strip_shell_wrapper('sh -c "ls -la"') == "ls -la"
    assert ce._strip_shell_wrapper("plain command") == "plain command"


def test_a_successful_send_marks_the_driver_busy_before_returning(monkeypatch, tmp_path):
    """A review finding: v0.85.0 inserted `return MessageDelivery("accepted")`
    above the state assignment, so the driver reported idle for the whole of an
    active `codex exec` and a second send could overwrite self._proc."""
    # This file is the GTK-free slim lane; the driver imports gi, so skip
    # rather than fail where PyGObject is absent (a review finding).
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from helios.backend.process.codex_driver import CodexCliDriver

    driver = CodexCliDriver.__new__(CodexCliDriver)
    CodexCliDriver.__init__(
        driver, cwd=str(tmp_path), permission_mode="dontAsk", model="gpt-5.5"
    )

    class _Stdin:
        def write_all(self, *_a):
            return True

        def close(self, *_a):
            return True

    class _Proc:
        def get_stdin_pipe(self):
            return _Stdin()

        def get_stdout_pipe(self):
            return object()

        def get_stderr_pipe(self):
            return object()

        def wait_async(self, *_a):
            return None

    monkeypatch.setattr(driver, "_prepare_prompt_context", lambda text: _Prepared(text))
    monkeypatch.setattr(
        "helios.backend.process.codex_driver.Gio.SubprocessLauncher.new",
        lambda *_a, **_k: _Launcher(_Proc()),
    )
    monkeypatch.setattr(
        "helios.backend.process.codex_driver.Gio.DataInputStream.new", lambda *_a: object()
    )
    monkeypatch.setattr(driver, "_read_next_stdout_line", lambda: None)
    monkeypatch.setattr(driver, "_read_next_stderr_line", lambda: None)

    first = driver.send_user_text("do the thing")

    assert first.accepted is True
    assert driver._busy is True, "an accepted send must leave the driver busy"
    assert driver._stop_requested is False

    errors = []
    driver.connect("error", lambda _d, message: errors.append(message))
    second = driver.send_user_text("and another")
    assert second.accepted is False, "a second send must be rejected while busy"
    assert errors and "still working" in errors[0]


class _Prepared:
    def __init__(self, text):
        self.text = text

    def mark_sent(self):
        return None


class _Launcher:
    def __init__(self, proc):
        self._proc = proc

    def set_cwd(self, _cwd):
        return None

    def setenv(self, *_a):
        return None

    def unsetenv(self, *_a):
        return None

    def spawnv(self, _argv):
        return self._proc
