"""Tests for the Codex->claude transcript mirror and the provider index that
makes GPT chats first-class, resumable sidebar sessions."""
from __future__ import annotations

import json

import helios.backend.projects as P
import helios.backend.session_providers as SP
from helios.backend.process.codex_transcript import (
    CodexTranscriptWriter,
    _MAX_TOOL_RESULT_CHARS,
    _MAX_TOOL_USE_INPUT_CHARS,
    _elide_tool_result,
    _elide_tool_use_input,
    clone_transcript_for_fork,
)
from helios.backend.transcript import ToolResult, ToolUse, Turn, parse_transcript


def _redirect(monkeypatch, tmp_path):
    """Point both the claude projects dir and the provider index at tmp."""
    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    # HELIOS_STATE_DIR is already redirected to tmp_path by the autouse
    # isolate_state_dir fixture in conftest.py; just drop the cache so the
    # next read picks up the new location.
    SP.reload()


def test_provider_index_defaults_to_unknown(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    assert SP.provider_for("never-seen") == ""
    assert not SP.is_openai("never-seen")


def test_provider_index_roundtrip_and_forget(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    SP.set_provider("tid-1", "openai")
    SP.reload()  # force re-read from disk
    assert SP.provider_for("tid-1") == "openai"
    SP.forget("tid-1")
    SP.reload()
    assert SP.provider_for("tid-1") == ""


def test_anthropic_entries_are_explicitly_persisted(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    SP.set_provider("tid-a", "anthropic")
    SP.reload()
    assert SP.provider_for("tid-a") == "anthropic"
    assert (tmp_path / "session-providers.json").exists()


def test_new_chat_buffers_first_prompt_until_thread_bound(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    w = CodexTranscriptWriter("/home/alice/proj")
    w.note_user_text("hello gpt")  # sent before thread.started → buffered
    assert w.thread_id == ""
    # No file yet — nowhere to write without an id.
    assert not list((tmp_path / "projects").glob("**/*.jsonl"))

    w.bind_thread("thread-xyz")
    t = Turn(role="assistant")
    t.thinking_parts.append("reasoning")
    t.text_parts.append("hi back")
    w.append_assistant(
        t,
        model="gpt-5.5",
        result={"usage": {"input_tokens": 10, "cache_read_input_tokens": 5}},
    )

    path = tmp_path / "projects" / P.encode_project_dirname("/home/alice/proj") / "thread-xyz.jsonl"
    assert path.exists()
    turns = parse_transcript(path)
    assert [(t.role, t.text) for t in turns] == [
        ("user", "hello gpt"),
        ("assistant", "hi back"),
    ]
    assert turns[1].thinking_parts == ["reasoning"]
    # And the session is now tagged OpenAI for the sidebar/resume routing.
    assert SP.provider_for("thread-xyz") == "openai"


def test_assistant_record_preserves_usage_model_and_tool_results(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    w = CodexTranscriptWriter("/p", thread_id="tid")
    t = Turn(role="assistant")
    t.tool_results.append(ToolResult(tool_use_id="cmd1", content="ok\n"))
    w.append_assistant(
        t,
        model="gpt-5.5",
        result={"usage": {"input_tokens": 12, "output_tokens": 3}},
    )

    path = tmp_path / "projects" / P.encode_project_dirname("/p") / "tid.jsonl"
    raw = path.read_text(encoding="utf-8")
    assert '"model": "gpt-5.5"' in raw
    assert '"input_tokens": 12' in raw
    turns = parse_transcript(path)
    assert turns[0].tool_results[0].content == "ok\n"


def test_assistant_record_can_persist_its_native_turn_boundary(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    writer = CodexTranscriptWriter("/p", thread_id="tid-boundary")
    turn = Turn(role="assistant")
    turn.text_parts.append("done")

    writer.append_assistant(turn, turn_id="native-turn-7")

    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-boundary.jsonl"
    )
    assert json.loads(path.read_text(encoding="utf-8"))["providerTurnId"] == (
        "native-turn-7"
    )


def test_fork_clone_is_independent_atomic_and_visibly_bounded(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    writer = CodexTranscriptWriter("/p", thread_id="source-thread")
    writer.note_user_text("inspect this")
    answer = Turn(role="assistant")
    answer.text_parts.append("source answer")
    writer.append_assistant(answer, turn_id="source-turn")
    source = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "source-thread.jsonl"
    )
    source_before = source.read_bytes()

    target = clone_transcript_for_fork(
        cwd="/p",
        source_thread_id="source-thread",
        fork_thread_id="fork-thread",
    )

    assert source.read_bytes() == source_before
    records = [json.loads(line) for line in target.read_text().splitlines()]
    assert all(record["sessionId"] == "fork-thread" for record in records)
    assert [record["parentUuid"] for record in records] == [
        None,
        records[0]["uuid"],
        records[1]["uuid"],
    ]
    assert {record["uuid"] for record in records}.isdisjoint(
        {
            json.loads(line)["uuid"]
            for line in source.read_text(encoding="utf-8").splitlines()
        }
    )
    assert records[1]["providerTurnId"] == "source-turn"
    assert records[-1]["subtype"] == "fork_boundary"
    assert records[-1]["forkMetadata"] == {"sourceThreadId": "source-thread"}
    turns = parse_transcript(target)
    assert [turn.text for turn in turns] == [
        "inspect this",
        "source answer",
        (
            "Conversation forked here. This branch has an independent Work; "
            "the source conversation was preserved."
        ),
    ]
    assert turns[-1].is_meta


def test_fork_clone_never_overwrites_an_existing_target(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    writer = CodexTranscriptWriter("/p", thread_id="source-thread")
    writer.note_user_text("source")
    target_dir = tmp_path / "projects" / P.encode_project_dirname("/p")
    target = target_dir / "fork-thread.jsonl"
    target.write_text("keep me\n", encoding="utf-8")

    try:
        clone_transcript_for_fork(
            cwd="/p",
            source_thread_id="source-thread",
            fork_thread_id="fork-thread",
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing fork transcript was not rejected")

    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_records_carry_cwd_and_session_id(monkeypatch, tmp_path):
    """The sidebar scanner recovers a session's cwd from the transcript, so the
    mirrored records must include it (and the thread id as sessionId)."""
    _redirect(monkeypatch, tmp_path)
    w = CodexTranscriptWriter("/srv/weird-proj", thread_id="tid-r")
    w.note_user_text("resumed prompt")  # id known up front → immediate write
    path = tmp_path / "projects" / P.encode_project_dirname("/srv/weird-proj") / "tid-r.jsonl"
    assert P._cwd_from_transcript(path) == "/srv/weird-proj"


def test_resume_appends_to_existing_transcript(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    # First session run.
    w1 = CodexTranscriptWriter("/p")
    w1.note_user_text("q1")
    w1.bind_thread("tid")
    a1 = Turn(role="assistant")
    a1.text_parts.append("a1")
    w1.append_assistant(a1)

    # Later: resume the same thread id — new turns extend the same file.
    w2 = CodexTranscriptWriter("/p", thread_id="tid")
    w2.note_user_text("q2")
    a2 = Turn(role="assistant")
    a2.text_parts.append("a2")
    w2.append_assistant(a2)

    path = tmp_path / "projects" / P.encode_project_dirname("/p") / "tid.jsonl"
    turns = parse_transcript(path)
    assert [t.text for t in turns] == ["q1", "a1", "q2", "a2"]


def test_compaction_boundary_is_visible_without_storing_provider_summary(
    monkeypatch,
    tmp_path,
):
    _redirect(monkeypatch, tmp_path)
    writer = CodexTranscriptWriter("/p", thread_id="tid-compact")

    writer.append_compaction(
        trigger="manual",
        pre_tokens=120_000,
        post_tokens=18_000,
        turn_id="turn-compact",
        item_id="item-compact",
    )

    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-compact.jsonl"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["type"] == "system"
    assert record["subtype"] == "compact_boundary"
    assert record["compactMetadata"] == {
        "trigger": "manual",
        "preTokens": 120_000,
        "postTokens": 18_000,
    }
    assert record["providerTurnId"] == "turn-compact"
    assert record["providerItemId"] == "item-compact"
    assert "summary" not in record
    assert "message" not in record

    turns = parse_transcript(path)
    assert len(turns) == 1
    assert turns[0].role == "system"
    assert turns[0].is_meta
    assert "120,000 → 18,000 tokens" in turns[0].text


def test_provider_index_save_oserror_does_not_propagate(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    assert SP.set_provider("tid-disk-full", "openai") is False

    assert SP.provider_for("tid-disk-full") == ""
    assert not (tmp_path / ".helios" / "session-providers.json").exists()


# ── content taxonomy round-trip (H0.5) ─────────────────────────────────────────


def test_span_view_facade_preserves_exact_ordered_content():
    """The _SpanView facade over ordered content (H0.5 finding #2). Reads,
    appends, self-assignment, and positional replacement must never REORDER the
    canonical interleaved content — compared as full ordered content, not just
    per-lane values/counts."""
    from helios.backend.transcript import ContentSpan

    t = Turn(role="assistant")
    t.text_parts.append("A")
    t.commentary_parts.append("C")
    t.text_parts.append("B")
    interleaved = [
        ContentSpan("text", "A"),
        ContentSpan("commentary", "C"),
        ContentSpan("text", "B"),
    ]
    assert t.content == interleaved  # appends land in call order

    # Read facade.
    assert t.text_parts == ["A", "B"]
    assert list(t.text_parts) == ["A", "B"]
    assert len(t.text_parts) == 2
    assert t.text_parts[0] == "A" and t.text_parts[-1] == "B"
    assert "A" in t.text_parts and bool(t.text_parts)
    assert t.text_parts + ["Z"] == ["A", "B", "Z"]

    # Self-assignment is an EXACT no-op — same list, spans, values, and order.
    content_identity = t.content
    span_identities = tuple(map(id, t.content))
    t.text_parts = t.text_parts
    assert t.content is content_identity
    assert tuple(map(id, t.content)) == span_identities
    assert t.content == interleaved

    # += appends at the end without disturbing existing span positions.
    t.text_parts += ["D"]
    assert t.content == interleaved + [ContentSpan("text", "D")]

    # Positional replacement overwrites text spans in place; commentary C keeps
    # its middle position (the old remove-then-append reordered it to the front).
    t.text_parts = ["A2", "B2", "D2"]
    assert t.content == [
        ContentSpan("text", "A2"),
        ContentSpan("commentary", "C"),
        ContentSpan("text", "B2"),
        ContentSpan("text", "D2"),
    ]

    # Fewer values than existing spans drops the tail spans, keeps positions.
    t.text_parts = ["only"]
    assert t.content == [ContentSpan("text", "only"), ContentSpan("commentary", "C")]


def test_new_mirror_marks_schema_so_multi_text_survives_reload(monkeypatch, tmp_path):
    """A NEW mirror record carries the ordered-content schema marker, so a
    legitimate two-text-span answer reloads verbatim — the pre-H0.5 multi-text
    demotion (first text -> thinking) must NOT fire on marked records."""
    from helios.backend.process.codex_transcript import CONTENT_SCHEMA
    from helios.backend.transcript import CONTENT_SCHEMA_KEY, ContentSpan

    _redirect(monkeypatch, tmp_path)
    w = CodexTranscriptWriter("/p", thread_id="tid-schema")
    t = Turn(role="assistant")
    t.content.extend([ContentSpan("text", "answer one"), ContentSpan("text", "answer two")])
    w.append_assistant(t, model="gpt-5.6-sol")

    path = tmp_path / "projects" / P.encode_project_dirname("/p") / "tid-schema.jsonl"
    raw = path.read_text(encoding="utf-8")
    assert f'"{CONTENT_SCHEMA_KEY}": "{CONTENT_SCHEMA}"' in raw

    reloaded = parse_transcript(path)[0]
    assert reloaded.text_parts == ["answer one", "answer two"]  # both survive
    assert reloaded.thinking_parts == []  # NOT demoted
    assert reloaded.content == t.content


def test_interleaved_content_round_trips_in_source_order(monkeypatch, tmp_path):
    """A Turn whose lanes INTERLEAVE (commentary, reasoning, commentary, final)
    persists and reloads with the exact cross-lane source order — no regrouping,
    merging, type loss, or duplication."""
    from helios.backend.transcript import ContentSpan

    _redirect(monkeypatch, tmp_path)
    w = CodexTranscriptWriter("/p", thread_id="tid-lanes")
    t = Turn(role="assistant")
    ordered = [
        ContentSpan("commentary", "work update 1"),
        ContentSpan("reasoning_summary", "public reason A"),
        ContentSpan("thinking", "legacy thinking"),
        ContentSpan("commentary", "work update 2"),
        ContentSpan("text", "the final answer"),
    ]
    t.content.extend(ordered)
    w.append_assistant(t, model="gpt-5.6-sol")

    path = tmp_path / "projects" / P.encode_project_dirname("/p") / "tid-lanes.jsonl"
    raw = path.read_text(encoding="utf-8")
    # New Helios block types are written explicitly (not folded into thinking).
    assert '"type": "commentary"' in raw
    assert '"type": "reasoning_summary"' in raw

    parsed = parse_transcript(path)[0]
    # Exact cross-lane order preserved — the canonical assertion, not per-lane.
    assert parsed.content == ordered
    # Per-lane views still project correctly (each kind, in order, no dupes).
    assert parsed.commentary_parts == ["work update 1", "work update 2"]
    assert parsed.reasoning_summary_parts == ["public reason A"]
    assert parsed.thinking_parts == ["legacy thinking"]
    assert parsed.text_parts == ["the final answer"]


def test_stream_to_final_and_reload_are_identical(monkeypatch, tmp_path):
    """The turn a live StreamingAssistant finalizes to must equal the turn
    reloaded from the mirror — stream→final and reload never reorder, drop, or
    duplicate content. Compared as ORDERED content, not independent lane lists."""
    from helios.backend.process.streaming import Block, StreamingAssistant

    _redirect(monkeypatch, tmp_path)
    s = StreamingAssistant(model="gpt-5.6-sol")
    s.blocks.append(Block(type="commentary", text="doing the work"))
    s.blocks.append(Block(type="reasoning_summary", text="public reasoning"))
    s.blocks.append(Block(type="commentary", text="more work"))
    s.blocks.append(Block(type="text", text="final answer"))
    final = s.to_turn()

    w = CodexTranscriptWriter("/p", thread_id="tid-eq")
    w.append_assistant(final, model="gpt-5.6-sol")
    path = tmp_path / "projects" / P.encode_project_dirname("/p") / "tid-eq.jsonl"
    reloaded = parse_transcript(path)[0]

    # The ordered content sequence is identical (the mirror stamps its own
    # timestamp/uuid, so compare content + tools, not the whole record).
    assert reloaded.content == final.content
    assert reloaded.tool_uses == final.tool_uses
    assert reloaded.tool_results == final.tool_results


def test_first_tool_position_round_trips_through_mirror(monkeypatch, tmp_path):
    """The compact activity row reloads at the first tool's source boundary."""
    import json

    from helios.backend.process.streaming import Block, StreamingAssistant

    _redirect(monkeypatch, tmp_path)
    streaming = StreamingAssistant(model="gpt-5.6-sol")
    streaming.blocks.extend(
        [
            Block(type="commentary", text="checking"),
            Block(
                type="tool_use",
                tool_use_name="Bash",
                tool_use_id="cmd-1",
                tool_use_input_json='{"command":"pwd"}',
            ),
            Block(type="text", text="done"),
        ]
    )
    final = streaming.to_turn()
    final.tool_results.append(ToolResult(tool_use_id="cmd-1", content="/p"))
    assert final.activity_content_index == 1

    writer = CodexTranscriptWriter("/p", thread_id="tid-activity-order")
    writer.append_assistant(final, model=streaming.model)
    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-activity-order.jsonl"
    )

    record = json.loads(path.read_text(encoding="utf-8"))
    block_types = [block["type"] for block in record["message"]["content"]]
    assert block_types == ["commentary", "tool_use", "tool_result", "text"]

    reloaded = parse_transcript(path)[0]
    assert reloaded.activity_content_index == 1
    assert reloaded.content == final.content
    assert reloaded.tool_uses == final.tool_uses
    assert reloaded.tool_results == final.tool_results


def test_legacy_thinking_only_record_still_renders():
    """Old Claude-format transcripts predate the taxonomy: a bare thinking
    block must still parse as thinking, with the new lanes empty."""
    from helios.backend.transcript import turn_from_record

    turn = turn_from_record({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "old thoughts"},
                {"type": "text", "text": "old answer"},
            ],
        },
    })
    assert turn is not None
    assert turn.thinking_parts == ["old thoughts"]
    assert turn.text_parts == ["old answer"]
    assert turn.commentary_parts == []
    assert turn.reasoning_summary_parts == []


# ── size-cap tests ────────────────────────────────────────────────────────────


def test_large_tool_result_is_truncated_with_elision_marker():
    """A tool_result whose content exceeds _MAX_TOOL_RESULT_CHARS must be
    shortened and must contain the elision marker so users know data was dropped."""
    big = "x" * (_MAX_TOOL_RESULT_CHARS + 5_000)
    result = _elide_tool_result(big)
    assert len(result) < _MAX_TOOL_RESULT_CHARS + 500  # well within cap + marker overhead
    assert "Helios truncated" in result
    assert "model's own session" in result


def test_small_tool_result_is_unchanged():
    """A tool_result that fits within the cap must be stored byte-identical."""
    small = "output line\n" * 10
    assert _elide_tool_result(small) is small


def test_tool_use_large_input_string_is_bounded():
    """A tool_use whose input dict contains a very long string value must be
    capped so the serialised representation stays within _MAX_TOOL_USE_INPUT_CHARS."""
    import json

    big_input = {"command": "x" * (_MAX_TOOL_USE_INPUT_CHARS * 3), "flag": True}
    result = _elide_tool_use_input(big_input)
    serialised = json.dumps(result, ensure_ascii=False)
    # The elision marker itself adds a fixed overhead; allow generous margin.
    assert len(serialised) < _MAX_TOOL_USE_INPUT_CHARS + 500
    assert "Helios truncated" in result["command"]
    # Non-string fields must survive unchanged.
    assert result["flag"] is True


def test_size_cap_roundtrip_turn_from_record(monkeypatch, tmp_path):
    """After append_assistant with a large tool_result, reading the JSONL back
    through parse_transcript must return a valid Turn that has tool_results."""
    _redirect(monkeypatch, tmp_path)
    w = CodexTranscriptWriter("/p", thread_id="tid-cap")
    t = Turn(role="assistant")
    t.text_parts.append("done")
    # Attach a large tool_result and a tool_use with a big input string.
    t.tool_results.append(
        ToolResult(tool_use_id="r1", content="line\n" * 4_000)
    )
    t.tool_uses.append(
        ToolUse(name="bash", input={"cmd": "y" * (_MAX_TOOL_USE_INPUT_CHARS * 2)}, id="u1")
    )
    w.append_assistant(t, model="gpt-5.5")

    path = tmp_path / "projects" / P.encode_project_dirname("/p") / "tid-cap.jsonl"
    turns = parse_transcript(path)
    assert len(turns) == 1
    parsed = turns[0]
    # Text survives unmodified.
    assert parsed.text == "done"
    # tool_result is present and truncated.
    assert len(parsed.tool_results) == 1
    tr_content = parsed.tool_results[0].content
    assert len(tr_content) <= _MAX_TOOL_RESULT_CHARS + 500
    assert "Helios truncated" in tr_content
    # tool_use is present with its name intact.
    assert len(parsed.tool_uses) == 1
    assert parsed.tool_uses[0].name == "bash"
