"""Per-conversation composer drafts. GTK-free — runs in slim CI."""

from __future__ import annotations

import types

from helios.backend.composer_state import DraftBook, draft_key


def _session(session_id: str = "", cwd: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        session_id=session_id,
        project=types.SimpleNamespace(cwd=cwd),
    )


def test_draft_key_is_the_session_id_when_there_is_one() -> None:
    assert draft_key(_session("abc-123", "/repo")) == "abc-123"


def test_unstarted_chats_key_on_cwd_so_two_folders_do_not_share() -> None:
    assert draft_key(_session(cwd="/a")) == "new:/a"
    assert draft_key(_session(cwd="/a")) != draft_key(_session(cwd="/b"))


def test_draft_key_survives_a_target_with_no_project() -> None:
    assert draft_key(types.SimpleNamespace()) == "new:"


def test_stash_then_take_round_trips() -> None:
    book = DraftBook()
    book.stash("s1", "half a thought")
    assert book.take("s1") == "half a thought"


def test_an_unknown_key_takes_the_empty_string_not_none() -> None:
    assert DraftBook().take("never-seen") == ""


def test_a_blank_stash_evicts_rather_than_stores() -> None:
    """Clearing the composer must not resurrect the old draft on return."""

    book = DraftBook()
    book.stash("s1", "typed then deleted")
    book.stash("s1", "   \n ")
    assert book.take("s1") == ""


def test_an_empty_key_is_ignored() -> None:
    book = DraftBook()
    book.stash("", "nothing owns this yet")
    assert book.take("") == ""
