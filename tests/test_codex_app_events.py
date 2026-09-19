"""Captured Codex App Server v2 notification-shape tests (GTK-free)."""

from __future__ import annotations

import json

from helios.backend.process import codex_app_events as cae
from helios.backend.process import codex_events as legacy


THREAD_ID = "019c-turn-thread"
TURN_ID = "019c-turn-id"


def notification(method: str, **params) -> dict:
    return {"method": method, "params": params}


def start(acc: cae.CodexAppEventAccumulator) -> None:
    actions = acc.feed(
        notification(
            cae.METHOD_TURN_STARTED,
            threadId=THREAD_ID,
            turn={"id": TURN_ID, "items": [], "status": "inProgress"},
        )
    )
    assert [action.kind for action in actions] == [
        cae.ACT_TURN_STATUS,
        legacy.ACT_STREAMING,
    ]


def finish(acc: cae.CodexAppEventAccumulator, status: str = "completed", **turn):
    return acc.feed(
        notification(
            cae.METHOD_TURN_COMPLETED,
            threadId=THREAD_ID,
            turn={"id": TURN_ID, "items": [], "status": status, **turn},
        )
    )


def test_block_for_survives_streaming_reset_with_stale_index():
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    first = acc._block_for("item-a")
    assert first is acc._streaming.blocks[0]
    # New turn: the streaming assistant resets to a fresh (empty) block list
    # while the item->index map still holds the old index.
    acc._streaming = None
    block = acc._block_for("item-a")  # must not IndexError
    assert block is acc._streaming.blocks[-1]
    assert acc._block_by_item["item-a"] == len(acc._streaming.blocks) - 1


def test_finish_turn_clears_block_maps_and_reconcile_survives_stale_index():
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    acc._block_for("a")
    acc._block_for("b")
    acc._item_types.update({"a": "agentMessage", "b": "agentMessage"})
    # Finishing a turn drops the stream AND its block-index maps together.
    acc._finish_turn({"turn": {"id": "t", "items": [], "status": "completed"}})
    assert acc._streaming is None
    assert acc._block_by_item == {}
    assert acc._item_types == {}
    assert acc._message_phases == {}
    # Defensive: even if a stale index survives into a fresh, shorter stream,
    # reconcile skips instead of IndexError-crashing the event loop.
    acc._block_for("a")  # fresh stream, one block, a->0
    acc._block_by_item["b"] = 5  # inject a stale out-of-range index
    acc._item_types["b"] = "agentMessage"
    acc._reconcile_authoritative_content([{"id": "a", "type": "agentMessage"}])


def test_method_constants_are_string_exact_v2_routes():
    assert cae.METHOD_ITEM_STARTED == "item/started"
    assert cae.METHOD_ITEM_COMPLETED == "item/completed"
    assert cae.METHOD_AGENT_MESSAGE_DELTA == "item/agentMessage/delta"
    assert cae.METHOD_REASONING_SUMMARY_DELTA == "item/reasoning/summaryTextDelta"
    assert cae.METHOD_REASONING_TEXT_DELTA == "item/reasoning/textDelta"
    assert cae.METHOD_THREAD_TOKEN_USAGE == "thread/tokenUsage/updated"
    assert cae.METHOD_FILE_CHANGE_PATCH_UPDATED == "item/fileChange/patchUpdated"
    assert cae.METHOD_ACCOUNT_RATE_LIMITS == "account/rateLimits/updated"
    assert cae.METHOD_TURN_PLAN_UPDATED == "turn/plan/updated"
    assert cae.METHOD_TURN_DIFF_UPDATED == "turn/diff/updated"


def test_review_sentinels_drive_phase_without_duplicating_review_text() -> None:
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol", thread_id=THREAD_ID)
    start(acc)

    entered = acc.feed(
        notification(
            cae.METHOD_ITEM_STARTED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item={"id": "review-enter", "type": "enteredReviewMode"},
        )
    )
    entered_activity = next(a for a in entered if a.kind == cae.ACT_ACTIVITY)
    assert entered_activity.payload["category"] == "phase"
    assert entered_activity.payload["phase"] == "reviewing"

    exited = acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item={
                "id": "review-exit",
                "type": "exitedReviewMode",
                "review": "Finding: subtraction is wrong.",
            },
        )
    )
    exited_activity = next(a for a in exited if a.kind == cae.ACT_ACTIVITY)
    assert exited_activity.payload["phase"] == "requesting"
    assert not any(a.kind in {legacy.ACT_TURN, legacy.ACT_STREAMING} for a in exited)

    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item={
                "id": "review-answer",
                "type": "agentMessage",
                "text": "Finding: subtraction is wrong.",
                "phase": "final_answer",
            },
        )
    )
    actions = finish(acc)
    turn = next(action.payload for action in actions if action.kind == legacy.ACT_TURN)
    assert turn.text_parts == ["Finding: subtraction is wrong."]


def test_documented_review_sentinel_is_a_fallback_when_agent_message_is_absent() -> None:
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol", thread_id=THREAD_ID)
    start(acc)
    exited = {
        "id": "review-exit-only",
        "type": "exitedReviewMode",
        "review": "Finding: subtraction is wrong.",
    }
    commentary = {
        "id": "review-commentary",
        "type": "agentMessage",
        "text": "Checking the arithmetic path.",
        "phase": "commentary",
    }
    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item=commentary,
        )
    )
    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item=exited,
        )
    )

    actions = finish(acc, items=[commentary, exited])

    turn = next(action.payload for action in actions if action.kind == legacy.ACT_TURN)
    assert turn.text_parts == ["Finding: subtraction is wrong."]
    assert turn.commentary_parts == ["Checking the arithmetic path."]


def test_captured_agent_phases_deltas_and_completion_are_authoritative():
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    announced = acc.feed(
        notification(cae.METHOD_THREAD_STARTED, thread={"id": THREAD_ID})
    )
    assert [(a.kind, a.payload) for a in announced] == [
        (legacy.ACT_SESSION_STARTED, THREAD_ID)
    ]
    start(acc)

    acc.feed(
        notification(
            cae.METHOD_ITEM_STARTED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            startedAtMs=1,
            item={
                "id": "msg-commentary",
                "type": "agentMessage",
                "text": "",
                "phase": "commentary",
            },
        )
    )
    acc.feed(
        notification(
            cae.METHOD_AGENT_MESSAGE_DELTA,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            itemId="msg-commentary",
            delta="Inspecting…",
        )
    )
    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=2,
            item={
                "id": "msg-commentary",
                "type": "agentMessage",
                "text": "Inspected the repository.",
                "phase": "commentary",
            },
        )
    )
    acc.feed(
        notification(
            cae.METHOD_ITEM_STARTED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            startedAtMs=3,
            item={
                "id": "msg-final",
                "type": "agentMessage",
                "text": "",
                "phase": "final_answer",
            },
        )
    )
    acc.feed(
        notification(
            cae.METHOD_AGENT_MESSAGE_DELTA,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            itemId="msg-final",
            delta="Draft answer",
        )
    )
    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=4,
            item={
                "id": "msg-final",
                "type": "agentMessage",
                "text": "Final answer.",
                "phase": "final_answer",
            },
        )
    )
    usage_actions = acc.feed(
        notification(
            cae.METHOD_THREAD_TOKEN_USAGE,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            tokenUsage={
                "total": {
                    "inputTokens": 150,
                    "cachedInputTokens": 50,
                    "outputTokens": 20,
                    "reasoningOutputTokens": 5,
                    "totalTokens": 170,
                },
                "last": {
                    "inputTokens": 100,
                    "cachedInputTokens": 40,
                    "outputTokens": 10,
                    "reasoningOutputTokens": 3,
                    "totalTokens": 110,
                },
                "modelContextWindow": 200000,
            },
        )
    )
    assert usage_actions[0].payload["usedTokens"] == 110
    assert usage_actions[0].payload["contextWindow"] == 200000

    actions = finish(acc, durationMs=42)
    turn = next(a.payload for a in actions if a.kind == legacy.ACT_TURN)
    # phase=commentary is a public work update, NOT hidden thinking.
    assert turn.commentary_parts == ["Inspected the repository."]
    assert turn.thinking_parts == []
    assert turn.text_parts == ["Final answer."]
    result = next(a.payload for a in actions if a.kind == legacy.ACT_RESULT)
    assert result["duration_ms"] == 42
    assert result["usage"] == {
        "input_tokens": 60,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 0,
        "output_tokens": 10,
    }


def test_public_item_reasoning_allowlist_drops_everything_but_summary():
    """_public_item rebuilds a reasoning item from a NARROW allowlist (id, type,
    sanitized summary). Raw content/text/deltas, encrypted variants (snake and
    camel case), and unrelated provider metadata must never survive — a
    copy-minus-content approach would have leaked all of them."""
    hostile = {
        "id": "reason-9",
        "type": "reasoning",
        "summary": ["Public summary", 123, {"nested": "RAW-HIDDEN"}],
        "content": ["RAW-HIDDEN"],
        "text": "RAW-HIDDEN",
        "delta": "RAW-HIDDEN",
        "encrypted_content": "RAW-HIDDEN",
        "encryptedContent": "RAW-HIDDEN",
        "encryptedReasoning": "RAW-HIDDEN",
        "metadata": {"trace": "RAW-HIDDEN"},
        "provider": {"internal": "RAW-HIDDEN"},
    }
    public = cae._public_item(hostile)
    assert public == {
        "id": "reason-9",
        "type": "reasoning",
        "summary": ["Public summary"],  # non-string summary parts dropped
    }
    assert "RAW-HIDDEN" not in json.dumps(public)
    assert cae._public_item({
        "id": "reason-10",
        "type": "reasoning",
        "summary": "not-a-summary-list",
    }) == {
        "id": "reason-10",
        "type": "reasoning",
        "summary": [],
    }
    # Non-reasoning items are still copied whole (no hidden-reasoning risk).
    tool = {"id": "c1", "type": "commandExecution", "command": "echo hi"}
    assert cae._public_item(tool) == tool


def test_hostile_reasoning_summary_shapes_fail_closed_in_actions_and_state():
    """Non-list summaries are neither iterable state nor public payloads."""
    acc = cae.CodexAppEventAccumulator()
    start(acc)
    seen = []
    hostile_items = []
    for index, raw_summary in enumerate(
        ("RAW-HIDDEN", 123, {"nested": "RAW-HIDDEN"})
    ):
        item = {
            "id": f"reason-hostile-{index}",
            "type": "reasoning",
            "summary": raw_summary,
            "content": ["RAW-HIDDEN"],
            "encryptedContent": "RAW-HIDDEN",
            "metadata": {"trace": "RAW-HIDDEN"},
        }
        hostile_items.append(item)
        actions = acc.feed(
            notification(
                cae.METHOD_ITEM_COMPLETED,
                threadId=THREAD_ID,
                turnId=TURN_ID,
                item=item,
            )
        )
        seen.extend(actions)
        lifecycle = next(
            action.payload for action in actions if action.kind == cae.ACT_ITEM_LIFECYCLE
        )
        assert lifecycle["item"]["summary"] == []
        assert acc.streaming.blocks[index].text == ""

    completed = finish(acc, items=hostile_items)
    seen.extend(completed)
    blob = json.dumps([action.payload for action in seen], default=str)
    assert "RAW-HIDDEN" not in blob
    assert not [action for action in completed if action.kind == legacy.ACT_TURN]


def test_agent_completion_and_late_delta_replay_preserve_demoted_kinds_on_reload(
    monkeypatch, tmp_path
):
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    acc = cae.CodexAppEventAccumulator()
    start(acc)
    items = [
        {"id": "message-1", "type": "agentMessage", "text": "First message"},
        {"id": "message-2", "type": "agentMessage", "text": "Second message"},
    ]
    for item in items:
        acc.feed(
            notification(
                cae.METHOD_ITEM_COMPLETED,
                threadId=THREAD_ID,
                turnId=TURN_ID,
                item=item,
            )
        )
    assert [block.type for block in acc.streaming.blocks] == ["commentary", "text"]

    acc.feed(
        notification(
            cae.METHOD_AGENT_MESSAGE_DELTA,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            itemId="message-1",
            delta=" late",
        )
    )
    assert [block.type for block in acc.streaming.blocks] == ["commentary", "text"]

    actions = finish(acc, items=items)
    turn = next(action.payload for action in actions if action.kind == legacy.ACT_TURN)
    assert [(span.kind, span.text) for span in turn.content] == [
        ("commentary", "First message"),
        ("text", "Second message"),
    ]

    writer = CodexTranscriptWriter("/p", thread_id="tid-replayed-messages")
    writer.append_assistant(turn)
    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-replayed-messages.jsonl"
    )
    reloaded = parse_transcript(path)[0]
    assert [(span.kind, span.text) for span in reloaded.content] == [
        ("commentary", "First message"),
        ("text", "Second message"),
    ]


def test_turn_completion_repairs_missed_content_in_authoritative_order_on_reload(
    monkeypatch, tmp_path
):
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    acc = cae.CodexAppEventAccumulator()
    start(acc)
    commentary = {
        "id": "commentary-late",
        "type": "agentMessage",
        "phase": "commentary",
        "text": "Visible update",
    }
    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item=commentary,
        )
    )
    reasoning = {
        "id": "reasoning-missed",
        "type": "reasoning",
        "summary": ["Earlier public summary"],
    }

    actions = finish(acc, items=[reasoning, commentary])
    turn = next(action.payload for action in actions if action.kind == legacy.ACT_TURN)
    expected = [
        ("reasoning_summary", "Earlier public summary"),
        ("commentary", "Visible update"),
    ]
    assert [(span.kind, span.text) for span in turn.content] == expected

    writer = CodexTranscriptWriter("/p", thread_id="tid-repaired-order")
    writer.append_assistant(turn)
    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-repaired-order.jsonl"
    )
    reloaded = parse_transcript(path)[0]
    assert [(span.kind, span.text) for span in reloaded.content] == expected


def test_context_compaction_lifecycle_is_typed_and_deduplicated() -> None:
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    start(acc)
    item = {"id": "compact-typed", "type": "contextCompaction"}

    started = acc.feed(
        notification(
            cae.METHOD_ITEM_STARTED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item=item,
        )
    )
    duplicate = acc.feed(
        notification(
            cae.METHOD_ITEM_STARTED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item=item,
        )
    )
    completed = acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            item=item,
        )
    )

    assert [
        action.payload["lifecycle"]
        for action in [*started, *completed]
        if action.kind == cae.ACT_CONTEXT_COMPACTED
    ] == ["started", "completed"]
    assert not any(
        action.kind == cae.ACT_CONTEXT_COMPACTED for action in duplicate
    )

    terminal = finish(acc, items=[item])
    assert not any(
        action.kind == cae.ACT_CONTEXT_COMPACTED for action in terminal
    )


def test_compacted_turn_preserves_live_commentary_before_final_on_reload(
    monkeypatch, tmp_path
):
    """Production regression: Codex 0.146 streamed seven commentary messages,
    compacted the context, then streamed a final answer. ``turn/completed``
    replayed the already-seen items final-first, which used to move the final
    above every Work update in Helios's finalized and persisted transcript.

    Live first-seen order is the chronology when completion is only replaying
    known items. The contextCompaction item is deliberately present in the
    fixture because that is the production shape that exposed the reversal.
    """
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    start(acc)

    commentary = [
        {
            "id": f"msg-commentary-{index}",
            "type": "agentMessage",
            "phase": "commentary",
            "text": f"Milestone {index}.",
        }
        for index in range(1, 8)
    ]
    compaction = {"id": "compact-1", "type": "contextCompaction"}
    final = {
        "id": "msg-final",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": "Concise final.",
    }

    # First-seen production chronology: four updates, compaction, three more
    # updates, then the final answer.
    for item in [*commentary[:4], compaction, *commentary[4:], final]:
        acc.feed(
            notification(
                cae.METHOD_ITEM_COMPLETED,
                threadId=THREAD_ID,
                turnId=TURN_ID,
                item=item,
            )
        )

    expected = [
        *(("commentary", item["text"]) for item in commentary),
        ("text", final["text"]),
    ]
    assert [
        (block.type, block.text)
        for block in acc.streaming.blocks
        if block.type in {"commentary", "text"}
    ] == expected

    # App Server's completed payload after compaction was final-first even
    # though every public content item had already arrived live.
    completed_items = [final, *commentary[:4], compaction, *commentary[4:]]
    actions = finish(acc, items=completed_items)
    turn = next(action.payload for action in actions if action.kind == legacy.ACT_TURN)
    assert [(span.kind, span.text) for span in turn.content] == expected
    assert turn.text == "Concise final."

    writer = CodexTranscriptWriter("/p", thread_id="tid-compacted-order")
    writer.append_assistant(turn)
    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-compacted-order.jsonl"
    )
    reloaded = parse_transcript(path)[0]
    assert [(span.kind, span.text) for span in reloaded.content] == expected
    assert reloaded.text == "Concise final."


def test_compacted_turn_stably_inserts_missed_commentary_before_known_final():
    """A repaired item must not let a final-first replay reorder live items."""
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    start(acc)

    commentary = [
        {
            "id": f"msg-commentary-{index}",
            "type": "agentMessage",
            "phase": "commentary",
            "text": f"Milestone {index}.",
        }
        for index in range(1, 4)
    ]
    final = {
        "id": "msg-final",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": "Concise final.",
    }

    # C3 was missed live. Compaction then reports every value, but puts the
    # already-seen final first: [final, C1, C2, missed-C3].
    for item in [*commentary[:2], final]:
        acc.feed(
            notification(
                cae.METHOD_ITEM_COMPLETED,
                threadId=THREAD_ID,
                turnId=TURN_ID,
                item=item,
            )
        )

    actions = finish(acc, items=[final, *commentary])
    turn = next(action.payload for action in actions if action.kind == legacy.ACT_TURN)
    assert [(span.kind, span.text) for span in turn.content] == [
        ("commentary", "Milestone 1."),
        ("commentary", "Milestone 2."),
        ("commentary", "Milestone 3."),
        ("text", "Concise final."),
    ]
    assert turn.text == "Concise final."


def test_compacted_turn_places_repaired_final_after_known_commentary_on_reload(
    monkeypatch, tmp_path
):
    """An explicit missed final stays final despite a final-first replay."""
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    start(acc)

    commentary = [
        {
            "id": f"msg-commentary-{index}",
            "type": "agentMessage",
            "phase": "commentary",
            "text": f"Milestone {index}.",
        }
        for index in range(1, 3)
    ]
    final = {
        "id": "msg-final-missed",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": "Recovered final.",
    }

    # The two commentary events arrived live; only the final item notification
    # was missed. Compaction's terminal payload is nevertheless final-first.
    for item in commentary:
        acc.feed(
            notification(
                cae.METHOD_ITEM_COMPLETED,
                threadId=THREAD_ID,
                turnId=TURN_ID,
                item=item,
            )
        )

    actions = finish(acc, items=[final, *commentary])
    turn = next(action.payload for action in actions if action.kind == legacy.ACT_TURN)
    expected = [
        ("commentary", "Milestone 1."),
        ("commentary", "Milestone 2."),
        ("text", "Recovered final."),
    ]
    assert [(span.kind, span.text) for span in turn.content] == expected
    assert turn.text == "Recovered final."

    writer = CodexTranscriptWriter("/p", thread_id="tid-repaired-final-order")
    writer.append_assistant(turn)
    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-repaired-final-order.jsonl"
    )
    reloaded = parse_transcript(path)[0]
    assert [(span.kind, span.text) for span in reloaded.content] == expected
    assert reloaded.text == "Recovered final."


def test_compacted_turn_places_repaired_commentary_before_known_final_on_reload(
    monkeypatch, tmp_path
):
    """A known final cannot anchor a missed work update after the answer."""
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")
    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    start(acc)

    missed_commentary = {
        "id": "msg-commentary-1-missed",
        "type": "agentMessage",
        "phase": "commentary",
        "text": "Milestone 1.",
    }
    known_commentary = {
        "id": "msg-commentary-2",
        "type": "agentMessage",
        "phase": "commentary",
        "text": "Milestone 2.",
    }
    known_final = {
        "id": "msg-final-known",
        "type": "agentMessage",
        "phase": "final_answer",
        "text": "Concise final.",
    }

    # C2 and the final arrived live. Compaction reports
    # [known-final, missed-C1, known-C2], so the final must be ignored as C1's
    # predecessor and C1 can use C2 as its authoritative successor instead.
    for item in [known_commentary, known_final]:
        acc.feed(
            notification(
                cae.METHOD_ITEM_COMPLETED,
                threadId=THREAD_ID,
                turnId=TURN_ID,
                item=item,
            )
        )

    actions = finish(
        acc,
        items=[known_final, missed_commentary, known_commentary],
    )
    turn = next(action.payload for action in actions if action.kind == legacy.ACT_TURN)
    expected = [
        ("commentary", "Milestone 1."),
        ("commentary", "Milestone 2."),
        ("text", "Concise final."),
    ]
    assert [(span.kind, span.text) for span in turn.content] == expected
    assert turn.text == "Concise final."

    writer = CodexTranscriptWriter("/p", thread_id="tid-repaired-commentary-order")
    writer.append_assistant(turn)
    path = (
        tmp_path
        / "projects"
        / P.encode_project_dirname("/p")
        / "tid-repaired-commentary-order.jsonl"
    )
    reloaded = parse_transcript(path)[0]
    assert [(span.kind, span.text) for span in reloaded.content] == expected
    assert reloaded.text == "Concise final."


def test_reasoning_summary_is_public_but_raw_hidden_reasoning_never_surfaces():
    acc = cae.CodexAppEventAccumulator()
    start(acc)
    started = acc.feed(
        notification(
            cae.METHOD_ITEM_STARTED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            startedAtMs=1,
            item={
                "id": "reason-1",
                "type": "reasoning",
                "summary": [],
                "content": ["HIDDEN-START"],
                "text": "HIDDEN-START",
                "encrypted_content": "HIDDEN-START",
                "encryptedContent": "HIDDEN-START",
                "metadata": {"trace": "HIDDEN-START"},
            },
        )
    )
    lifecycle = next(a.payload for a in started if a.kind == cae.ACT_ITEM_LIFECYCLE)
    # Allowlist: only id/type/summary survive — no content/text/encrypted/meta.
    assert set(lifecycle["item"]) == {"id", "type", "summary"}
    assert "HIDDEN-START" not in json.dumps(lifecycle)

    assert acc.feed(
        notification(
            cae.METHOD_REASONING_TEXT_DELTA,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            itemId="reason-1",
            contentIndex=0,
            delta="HIDDEN-DELTA",
        )
    ) == []
    for index, text in ((0, "Public summary"), (1, "Second section")):
        acc.feed(
            notification(
                cae.METHOD_REASONING_SUMMARY_PART_ADDED,
                threadId=THREAD_ID,
                turnId=TURN_ID,
                itemId="reason-1",
                summaryIndex=index,
            )
        )
        acc.feed(
            notification(
                cae.METHOD_REASONING_SUMMARY_DELTA,
                threadId=THREAD_ID,
                turnId=TURN_ID,
                itemId="reason-1",
                summaryIndex=index,
                delta=text,
            )
        )
    assert acc.streaming.blocks[0].text == "Public summary\n\nSecond section"

    completed = acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=2,
            item={
                "id": "reason-1",
                "type": "reasoning",
                "summary": ["Authoritative public summary"],
                "content": ["HIDDEN-COMPLETED"],
            },
        )
    )
    public_item = next(
        a.payload["item"] for a in completed if a.kind == cae.ACT_ITEM_LIFECYCLE
    )
    assert public_item == {
        "id": "reason-1",
        "type": "reasoning",
        "summary": ["Authoritative public summary"],
    }
    actions = finish(acc)
    turn = next(a.payload for a in actions if a.kind == legacy.ACT_TURN)
    # Public reasoning summary is its own lane, separate from legacy thinking.
    assert turn.reasoning_summary_parts == ["Authoritative public summary"]
    assert turn.thinking_parts == []
    assert "HIDDEN" not in "".join(
        turn.reasoning_summary_parts + turn.thinking_parts + turn.commentary_parts
    )


def test_raw_hidden_reasoning_never_crosses_any_boundary(monkeypatch, tmp_path):
    """End-to-end privacy proof: raw reasoning ``content`` and ``textDelta``
    must never appear in any emitted action, in Turn state, or in the persisted
    mirror transcript. Only the PUBLIC summary and commentary survive — each in
    its own lane, never under Thinking."""
    import helios.backend.projects as P
    from helios.backend.process.codex_transcript import CodexTranscriptWriter
    from helios.backend.transcript import parse_transcript

    monkeypatch.setattr(P, "PROJECTS_DIR", tmp_path / "projects")

    acc = cae.CodexAppEventAccumulator(model="gpt-5.6-sol")
    seen: list = []

    def feed(method, **params):
        actions = acc.feed(notification(method, **params))
        seen.extend(actions)
        return actions

    feed(cae.METHOD_THREAD_STARTED, thread={"id": THREAD_ID})
    feed(
        cae.METHOD_TURN_STARTED,
        threadId=THREAD_ID,
        turn={"id": TURN_ID, "items": [], "status": "inProgress"},
    )
    # Reasoning item carrying raw hidden content on start, a hidden textDelta,
    # a public summary, and raw hidden content again on completion.
    feed(
        cae.METHOD_ITEM_STARTED,
        threadId=THREAD_ID,
        turnId=TURN_ID,
        item={
            "id": "reason-1",
            "type": "reasoning",
            "summary": [],
            "content": ["HIDDEN-START"],
            "encrypted_content": "HIDDEN-START",
            "encryptedContent": "HIDDEN-START",
            "metadata": {"trace": "HIDDEN-START"},
        },
    )
    assert feed(
        cae.METHOD_REASONING_TEXT_DELTA,
        threadId=THREAD_ID,
        turnId=TURN_ID,
        itemId="reason-1",
        contentIndex=0,
        delta="HIDDEN-DELTA",
    ) == []
    feed(
        cae.METHOD_REASONING_SUMMARY_PART_ADDED,
        threadId=THREAD_ID,
        turnId=TURN_ID,
        itemId="reason-1",
        summaryIndex=0,
    )
    feed(
        cae.METHOD_REASONING_SUMMARY_DELTA,
        threadId=THREAD_ID,
        turnId=TURN_ID,
        itemId="reason-1",
        summaryIndex=0,
        delta="Public summary",
    )
    feed(
        cae.METHOD_ITEM_COMPLETED,
        threadId=THREAD_ID,
        turnId=TURN_ID,
        item={
            "id": "reason-1",
            "type": "reasoning",
            "summary": ["Public summary"],
            "content": ["HIDDEN-DONE"],
            "encryptedContent": "HIDDEN-DONE",
            "metadata": {"trace": "HIDDEN-DONE"},
        },
    )
    # Commentary work update + a final answer.
    feed(
        cae.METHOD_ITEM_COMPLETED,
        threadId=THREAD_ID,
        turnId=TURN_ID,
        item={
            "id": "msg-c",
            "type": "agentMessage",
            "text": "Inspected the repo.",
            "phase": "commentary",
        },
    )
    feed(
        cae.METHOD_ITEM_COMPLETED,
        threadId=THREAD_ID,
        turnId=TURN_ID,
        item={
            "id": "msg-f",
            "type": "agentMessage",
            "text": "Final answer.",
            "phase": "final_answer",
        },
    )
    actions = finish(acc)
    seen.extend(actions)

    # 1. No action payload — dict, string, StreamingAssistant, or Turn (all
    #    reachable via repr through default=str) — may contain raw reasoning.
    blob = json.dumps([a.payload for a in seen], default=str, ensure_ascii=False)
    assert "HIDDEN" not in blob

    # 2. The finalized Turn keeps the taxonomy split and no hidden reasoning.
    turn = next(a.payload for a in actions if a.kind == legacy.ACT_TURN)
    assert turn.reasoning_summary_parts == ["Public summary"]
    assert turn.commentary_parts == ["Inspected the repo."]
    assert turn.text_parts == ["Final answer."]
    assert turn.thinking_parts == []

    # 3. Persist the mirror and prove the raw file never holds hidden reasoning,
    #    while every public lane round-trips with no type loss or duplication.
    writer = CodexTranscriptWriter("/proj", thread_id=THREAD_ID)
    writer.append_assistant(turn, model="gpt-5.6-sol")
    path = (
        tmp_path / "projects" / P.encode_project_dirname("/proj") / f"{THREAD_ID}.jsonl"
    )
    assert "HIDDEN" not in path.read_text(encoding="utf-8")
    reloaded = parse_transcript(path)[0]
    assert reloaded.reasoning_summary_parts == ["Public summary"]
    assert reloaded.commentary_parts == ["Inspected the repo."]
    assert reloaded.text_parts == ["Final answer."]
    assert reloaded.thinking_parts == []


def test_command_file_mcp_web_and_subagent_items_preserve_turn_shapes():
    acc = cae.CodexAppEventAccumulator()
    start(acc)

    command = {
        "id": "cmd-1",
        "type": "commandExecution",
        "command": "/bin/bash -c 'echo hello'",
        "cwd": "/tmp/project",
        "commandActions": [],
        "status": "inProgress",
    }
    acc.feed(
        notification(
            cae.METHOD_ITEM_STARTED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            startedAtMs=1,
            item=command,
        )
    )
    acc.feed(
        notification(
            cae.METHOD_COMMAND_OUTPUT_DELTA,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            itemId="cmd-1",
            delta="hello\n",
        )
    )
    command.update(status="completed", aggregatedOutput=None, exitCode=0)
    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=2,
            item=command,
        )
    )

    file_item = {
        "id": "file-1",
        "type": "fileChange",
        "changes": [
            {"path": "/tmp/project/a.py", "kind": "add", "diff": "+a"},
            {"path": "/tmp/project/b.py", "kind": "update", "diff": "+b"},
        ],
        "status": "completed",
    }
    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=3,
            item=file_item,
        )
    )

    mcp_started = {
        "id": "mcp-1",
        "type": "mcpToolCall",
        "server": "code-intel",
        "tool": "search",
        "arguments": {"query": "events"},
        "status": "inProgress",
    }
    mcp_actions = acc.feed(
        notification(
            cae.METHOD_ITEM_STARTED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            startedAtMs=4,
            item=mcp_started,
        )
    )
    assert next(a for a in mcp_actions if a.kind == cae.ACT_MCP_ACTIVITY).payload[
        "arguments"
    ] == {"query": "events"}
    progress = acc.feed(
        notification(
            cae.METHOD_MCP_PROGRESS,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            itemId="mcp-1",
            message="Searching",
        )
    )
    assert progress[0].payload["message"] == "Searching"
    mcp_started.update(
        status="completed",
        result={"content": [{"type": "text", "text": "Found it"}]},
        error=None,
    )
    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=5,
            item=mcp_started,
        )
    )

    acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=6,
            item={
                "id": "web-1",
                "type": "webSearch",
                "query": "Codex App Server",
                "action": {"type": "search", "query": "Codex App Server"},
            },
        )
    )
    subagent_actions = acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=7,
            item={
                "id": "agent-1",
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
                "senderThreadId": THREAD_ID,
                "receiverThreadIds": ["child-1"],
                "prompt": "Review events",
                "model": "gpt-5.6-sol",
                "reasoningEffort": "high",
                "agentsStates": {"child-1": {"status": "completed"}},
                "status": "completed",
            },
        )
    )
    subagent = next(a for a in subagent_actions if a.kind == cae.ACT_SUBAGENT_ACTIVITY)
    assert subagent.payload["receiverThreadIds"] == ["child-1"]

    actions = finish(acc)
    turn = next(a.payload for a in actions if a.kind == legacy.ACT_TURN)
    tools = {tool.name: tool for tool in turn.tool_uses}
    assert tools["Bash"].input == {
        "command": "echo hello",
        "cwd": "/tmp/project",
    }
    assert tools["Write"].input["additional_files"] == ["/tmp/project/b.py"]
    assert tools["mcp__code-intel__search"].input == {"query": "events"}
    assert tools["WebSearch"].input["query"] == "Codex App Server"
    assert tools["Agent"].input["thread_ids"] == ["child-1"]
    results = {result.tool_use_id: result for result in turn.tool_results}
    assert results["cmd-1"].content == "hello\n"
    assert results["file-1"].content.endswith("a.py, /tmp/project/b.py")
    assert results["mcp-1"].content == "Found it"


def test_plan_and_diff_updates_have_explicit_authoritative_payloads():
    acc = cae.CodexAppEventAccumulator()
    start(acc)
    acc.feed(
        notification(
            cae.METHOD_PLAN_DELTA,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            itemId="plan-1",
            delta="1. Inspect",
        )
    )
    completed = acc.feed(
        notification(
            cae.METHOD_ITEM_COMPLETED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            completedAtMs=1,
            item={"id": "plan-1", "type": "plan", "text": "1. Inspect\n2. Build"},
        )
    )
    item_plan = next(a.payload for a in completed if a.kind == cae.ACT_PLAN_UPDATED)
    assert item_plan["authoritative"] is True
    assert item_plan["text"].endswith("2. Build")

    plan = acc.feed(
        notification(
            cae.METHOD_TURN_PLAN_UPDATED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            explanation="Updated after inspection",
            plan=[
                {"step": "Inspect", "status": "completed"},
                {"step": "Build", "status": "inProgress"},
            ],
        )
    )[0].payload
    assert plan["source"] == "turn" and plan["authoritative"] is True
    assert plan["plan"][1] == {"step": "Build", "status": "inProgress"}

    patch = acc.feed(
        notification(
            cae.METHOD_FILE_CHANGE_PATCH_UPDATED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            itemId="file-1",
            changes=[
                {
                    "path": "/tmp/project/a.py",
                    "kind": {"type": "update"},
                    "diff": "@@ -1 +1 @@\n-old\n+new",
                }
            ],
        )
    )[0]
    assert patch.kind == cae.ACT_DIFF_UPDATED
    assert patch.payload["authoritative"] is False
    assert patch.payload["changes"][0]["path"].endswith("a.py")

    diff = acc.feed(
        notification(
            cae.METHOD_TURN_DIFF_UPDATED,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            diff="diff --git a/a.py b/a.py\n+new",
        )
    )[0]
    assert diff.kind == cae.ACT_DIFF_UPDATED
    assert diff.payload["authoritative"] is True
    assert diff.payload["diff"].startswith("diff --git")


def test_rate_limit_updates_merge_sparse_non_null_values():
    acc = cae.CodexAppEventAccumulator()
    first = acc.feed(
        notification(
            cae.METHOD_ACCOUNT_RATE_LIMITS,
            rateLimits={
                "limitId": "codex",
                "planType": "pro",
                "primary": {
                    "usedPercent": 25,
                    "windowDurationMins": 15,
                    "resetsAt": 1730947200,
                },
                "secondary": None,
            },
        )
    )[0]
    assert first.kind == cae.ACT_RATE_LIMIT_UPDATED
    second = acc.feed(
        notification(
            cae.METHOD_ACCOUNT_RATE_LIMITS,
            rateLimits={
                "limitId": None,
                "planType": None,
                "primary": {"usedPercent": 31, "windowDurationMins": None},
                "rateLimitReachedType": "rate_limit_reached",
            },
        )
    )[0].payload["rateLimits"]
    assert second["limitId"] == "codex"
    assert second["planType"] == "pro"
    assert second["primary"] == {
        "usedPercent": 31,
        "windowDurationMins": 15,
        "resetsAt": 1730947200,
    }
    assert second["rateLimitReachedType"] == "rate_limit_reached"


def test_interrupted_and_failed_turns_emit_status_result_and_errors():
    interrupted = cae.CodexAppEventAccumulator()
    start(interrupted)
    interrupted_actions = finish(interrupted, "interrupted")
    interrupted_status = next(
        a.payload for a in interrupted_actions if a.kind == cae.ACT_TURN_STATUS
    )
    interrupted_result = next(
        a.payload for a in interrupted_actions if a.kind == legacy.ACT_RESULT
    )
    assert interrupted_status["status"] == "interrupted"
    assert interrupted_result["subtype"] == "aborted"
    assert not [a for a in interrupted_actions if a.kind == legacy.ACT_ERROR]

    failed = cae.CodexAppEventAccumulator()
    start(failed)
    failed_actions = finish(
        failed,
        "failed",
        error={"message": "Unauthorized", "codexErrorInfo": "unauthorized"},
    )
    assert next(a for a in failed_actions if a.kind == legacy.ACT_ERROR).payload == (
        "Unauthorized"
    )
    result = next(a.payload for a in failed_actions if a.kind == legacy.ACT_RESULT)
    assert result["subtype"] == "error"
    assert result["error"]["codexErrorInfo"] == "unauthorized"


def test_retry_errors_are_status_only_and_other_threads_are_ignored():
    acc = cae.CodexAppEventAccumulator(thread_id=THREAD_ID, _announced=True)
    retrying = acc.feed(
        notification(
            cae.METHOD_ERROR,
            threadId=THREAD_ID,
            turnId=TURN_ID,
            error={"message": "Reconnecting"},
            willRetry=True,
        )
    )
    assert [a.kind for a in retrying] == [cae.ACT_TURN_STATUS]
    assert retrying[0].payload["status"] == "retrying"
    assert acc.feed(
        notification(
            cae.METHOD_TURN_STARTED,
            threadId="different-thread",
            turn={"id": "other", "items": [], "status": "inProgress"},
        )
    ) == []
    assert acc.feed_line("not-json") == []
    assert acc.feed({"id": 1, "result": {}}) == []
