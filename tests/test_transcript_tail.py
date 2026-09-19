"""`read_transcript_since` must survive a writer that is mid-append.

GTK-free on purpose: CI runs the backend suite in python:3.13-slim, which has
no PyGObject. Nothing here imports a widget.

The follow path used to re-read the whole transcript from byte 0 on every
file-monitor burst and slice by turn INDEX. Byte offsets are cheaper but have
two failure modes an index never had, and both are silent:

  * a record still being written has no trailing newline yet. Consuming it
    splits one JSON object across two reads; both halves raise
    JSONDecodeError, which `_turn_from_line` swallows, so the turn simply
    never appears. Hence test 3 — the one that actually earns its keep.
  * a truncated or replaced file makes the stored offset meaningless. Detected
    by size only; see the ponytail comment in transcript.py for the ceiling.

Binary iteration splits on b"\\n", and no multi-byte UTF-8 sequence can
contain that byte, so the per-line decode(errors="replace") is equivalent to
`iter_transcript`'s text-mode errors="replace".
"""

from __future__ import annotations

import json
from pathlib import Path

from helios.backend.transcript import read_transcript_since


def _record(text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": text,
            "timestamp": "2026-08-17T00:00:00Z",
            "message": {"role": "user", "content": text},
        }
    )


def _write(path: Path, *texts: str, newline: bool = True) -> None:
    body = "\n".join(_record(t) for t in texts)
    path.write_text(body + ("\n" if newline else ""), encoding="utf-8")


def test_full_read_lands_on_eof(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write(p, "a", "b", "c")

    tail = read_transcript_since(p)

    assert [t.text for t in tail.turns] == ["a", "b", "c"], "wrong turns"
    assert tail.offset == p.stat().st_size, "offset must be comparable to st_size"
    assert not tail.restarted, "a first read is not a restart"


def test_second_read_returns_only_the_appended_record(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write(p, "a", "b")
    offset = read_transcript_since(p).offset

    with p.open("a", encoding="utf-8") as f:
        f.write(_record("c") + "\n")
    tail = read_transcript_since(p, offset)

    assert [t.text for t in tail.turns] == ["c"], "should re-read nothing already seen"
    assert tail.offset == p.stat().st_size


def test_partial_line_is_not_consumed_then_yields_once(tmp_path: Path) -> None:
    """The regression the byte cursor can silently lose.

    A genuine mid-append is a TRUNCATED object, and truncation is what makes it
    unparseable — JSON is not valid until its closing brace. The earlier version
    of this test simulated the writer by appending a *complete* record without a
    newline, which is a different case entirely and is now consumed on purpose
    (see the trailing-record test below): a complete record stranded by a
    producer that closed without a newline would otherwise never render.
    """
    p = tmp_path / "s.jsonl"
    _write(p, "a")
    offset = read_transcript_since(p).offset

    whole = _record("b")
    with p.open("a", encoding="utf-8") as f:  # writer caught mid-object
        f.write(whole[: len(whole) // 2])
    mid = read_transcript_since(p, offset)

    assert mid.turns == [], "a truncated record must not be parsed"
    assert mid.offset == offset, "offset must not advance past a partial line"

    with p.open("a", encoding="utf-8") as f:  # writer finishes the record
        f.write(whole[len(whole) // 2:] + "\n")
    done = read_transcript_since(p, mid.offset)

    assert [t.text for t in done.turns] == ["b"], "the completed record was lost"
    assert done.offset == p.stat().st_size


def test_a_shrink_forces_a_full_re_read(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write(p, "a", "b", "c")
    offset = read_transcript_since(p).offset

    _write(p, "z")  # rotated / rewritten shorter under us
    tail = read_transcript_since(p, offset)

    assert tail.restarted, "a file smaller than the stored offset is a restart"
    assert [t.text for t in tail.turns] == ["z"], "restart must re-read from 0"
    assert tail.offset == p.stat().st_size


def test_complete_final_record_without_newline_loads_when_asked(tmp_path):
    """JSONL does not require a trailing newline, and set_session must not drop
    the last turn of a finished transcript that lacks one.

    The follow path deliberately does NOT opt in: there, a missing newline means
    a write is in flight. Parsing is what separates the two cases.
    """
    path = tmp_path / "s.jsonl"
    path.write_text(
        _record("first") + "\n" + _record("last")  # no trailing newline
    )

    # Both paths behave the same, because PARSING is the discriminator rather
    # than the newline: a complete record renders and is consumed.
    load = read_transcript_since(path)
    assert [t.text for t in load.turns] == ["first", "last"]
    assert load.offset == path.stat().st_size, (
        "a consumed final record must advance the offset, or the next append "
        "re-reads it and renders a duplicate"
    )


def test_half_written_final_record_is_never_accepted(tmp_path):
    """accept_final_unterminated must not swallow a partial write: the record
    is judged by whether it PARSES, not by the flag."""
    path = tmp_path / "s.jsonl"
    truncated = _record("whole")[: len(_record("whole")) // 2]
    path.write_text(_record("first") + "\n" + truncated)

    tail = read_transcript_since(path)
    assert [t.text for t in tail.turns] == ["first"]
    assert tail.offset == len(_record("first")) + 1, (
        "a half-written record must stay unconsumed so it is re-read whole"
    )


def test_wrong_shaped_json_does_not_escape_the_reader(tmp_path: Path) -> None:
    """The reader is the trust boundary for a file another process writes, and
    the follow path reaches it from a GLib callback — an escaping exception
    there breaks every later update, which the old blanket `except Exception`
    around the whole iteration used to absorb.

    Valid JSON of the wrong shape is the gap: it clears JSONDecodeError and then
    fails inside turn_from_record with AttributeError/TypeError instead.
    """
    path = tmp_path / "s.jsonl"
    path.write_text(
        "null\n"
        "[]\n"
        '"a bare string"\n'
        "12345\n"
        '{"type": "user", "message": null}\n'
        '{"type": "user", "message": {"role": "user", "content": 99}}\n'
        + _record("survivor") + "\n",
        encoding="utf-8",
    )

    tail = read_transcript_since(path)

    assert [t.text for t in tail.turns] == ["survivor"], (
        "every malformed record must be skipped, and the good one after them "
        "must still render"
    )
    assert tail.offset == path.stat().st_size
