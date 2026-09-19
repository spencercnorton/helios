from __future__ import annotations

from helios.backend.transcript import CONTENT_SCHEMA_KEY, turn_from_record


def test_legacy_codex_multi_text_records_collapse_progress():
    # GENUINE legacy fixture: a pre-H0.5 helios-codex record with NO schema
    # marker and multiple text blocks — the migration must still collapse the
    # earlier progress into thinking.
    turn = turn_from_record({
        "type": "assistant",
        "version": "helios-codex",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "progress one"},
                {"type": "text", "text": "progress two"},
                {"type": "text", "text": "final answer"},
            ],
        },
    })

    assert turn is not None
    assert CONTENT_SCHEMA_KEY  # sanity: the fixture above deliberately omits it
    assert turn.text_parts == ["final answer"]
    assert turn.thinking_parts == ["progress one", "progress two"]


def test_marked_multi_text_record_is_not_folded():
    """A text-ONLY new record (no commentary/reasoning lanes) is still protected
    from the legacy fold by the schema marker alone — this is the exact gap the
    marker closes over the earlier lane-only gate."""
    turn = turn_from_record({
        "type": "assistant",
        "version": "helios-codex",
        CONTENT_SCHEMA_KEY: "ordered-v1",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "first answer"},
                {"type": "text", "text": "second answer"},
            ],
        },
    })

    assert turn is not None
    assert turn.text_parts == ["first answer", "second answer"]
    assert turn.thinking_parts == []


def test_new_format_helios_codex_record_is_not_folded():
    """A NEW H0.5 mirror carries explicit lanes (commentary/reasoning_summary).
    The legacy multi-text fold must NOT fire on it — its text spans stay text,
    so stream->reload is order- and type-identical."""
    turn = turn_from_record({
        "type": "assistant",
        "version": "helios-codex",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "commentary", "text": "work update"},
                {"type": "text", "text": "first answer"},
                {"type": "text", "text": "second answer"},
            ],
        },
    })

    assert turn is not None
    assert turn.commentary_parts == ["work update"]
    # Both text spans survive — the old-format fold did not touch them.
    assert turn.text_parts == ["first answer", "second answer"]
    assert turn.thinking_parts == []
    assert [s.kind for s in turn.content] == ["commentary", "text", "text"]
