"""The composer draft swap is actually wired into the two switch paths.

`_on_session_selected` re-pointed the transcript, plan pane, toolbar
and queue but never the composer, so half-typed text followed you into the
next conversation. These tests fail if either insertion point is removed.

The second half of the same defect is here too: the read-only note used to be
a placeholder, which any non-empty buffer hides.
"""

from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
try:
    gi.require_version("GtkSource", "5")
except ValueError:
    pytest.skip("GtkSource 5 unavailable", allow_module_level=True)

from helios.backend import session_providers  # noqa: E402
from helios.backend.composer_state import DraftBook, draft_key  # noqa: E402
from helios.backend.projects import Project, Session  # noqa: E402
from helios.main_window import MainWindow  # noqa: E402
from helios.widgets.composer import Composer  # noqa: E402

from gi.repository import Gtk  # noqa: E402

_HAVE_DISPLAY = Gtk.init_check()


class _Composer:
    """Just the buffer — the one piece of the composer this feature moves."""

    def __init__(self) -> None:
        self.text = ""
        self.read_only = False

    def current_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text

    def set_read_only(self, read_only: bool, note: str = "") -> None:
        # Recorded rather than swallowed by __getattr__: whether the banner
        # comes down on deselect is a real assertion.
        self.read_only = read_only

    def __getattr__(self, _name):  # set_visible / set_read_only / ...
        return lambda *a, **k: None


class _Noop:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _session(tmp_path, session_id: str, cwd: str) -> Session:
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text("", encoding="utf-8")
    session_providers.set_provider(session_id, "anthropic")
    return Session(
        project=Project(dirname=cwd.replace("/", "-"), cwd=cwd, path=tmp_path),
        session_id=session_id,
        path=path,
        mtime=0.0,
        size=0,
    )


def _window():
    return types.SimpleNamespace(
        _drafts=DraftBook(),
        _draft_key="",
        _composer=_Composer(),
        _driver=None,
        _driver_manager=types.SimpleNamespace(
            driver_for_session=lambda _sid: None,
            bind_current=lambda _drv: None,
        ),
        _work_coordinator=None,
        _model="",
        _next_chat=None,
        _conversation_perms=None,
        _shared=_Noop(),
        _plan=_Noop(),
        _transcript=_Noop(),
        _context=_Noop(),
        _chat_toolbar=_Noop(),
        _main_stack=_Noop(),
        _goal_strip=_Noop(),
        _adopt_session_provider=lambda _p: None,
        _driver_provider=lambda _d: "anthropic",
        _selected_provider=lambda: "anthropic",
        _sync_assistant_labels=lambda: None,
        _assistant_label=lambda: "Assistant",
        _bind_visible_driver=lambda _d: None,
        _stop_following=lambda: None,
        _start_following=lambda _s: None,
        _sync_execution_control=lambda: None,
        _load_context_fill_async=lambda _s: None,
        _refresh_goal_strip=lambda: None,
        _pump_questions=lambda: None,
        _clear_busy_ui=lambda: None,
        _toast=lambda _m: None,
        _staged_permission_mode="",
        _staged_effort_key="",
        _execution_target_lock_reason=lambda: "",
    )


def test_a_draft_stays_with_the_session_it_was_typed_in(tmp_path) -> None:
    win = _window()
    a = _session(tmp_path, "sess-a", "/repo/a")
    b = _session(tmp_path, "sess-b", "/repo/b")

    MainWindow._on_session_selected(win, None, a)
    win._composer.text = "half a thought for A"

    MainWindow._on_session_selected(win, None, b)
    assert win._composer.text == ""  # A's text must not follow us into B

    MainWindow._on_session_selected(win, None, a)
    assert win._composer.text == "half a thought for A"


def test_new_chat_does_not_inherit_the_previous_conversations_text(
    tmp_path,
) -> None:
    win = _window()
    a = _session(tmp_path, "sess-a", "/repo/a")

    MainWindow._on_session_selected(win, None, a)
    win._composer.text = "meant for A"

    MainWindow._show_fresh_chat_ui(win, a.project)
    assert win._composer.text == ""

    MainWindow._on_session_selected(win, None, a)
    assert win._composer.text == "meant for A"


@pytest.mark.skipif(not _HAVE_DISPLAY, reason="no display available")
def test_the_read_only_reason_survives_text_in_the_buffer() -> None:
    """As a placeholder it vanished the moment the buffer was non-empty,
    leaving a live-looking composer with an inert Send and no stated reason."""

    c = Composer()
    c.set_read_only(True, "Read-only - session from macbook (shared pool).")
    c.set_text("text carried in from another session")

    assert c._ro_banner.get_revealed()
    assert "shared pool" in c._ro_banner.get_title()


@pytest.mark.skipif(not _HAVE_DISPLAY, reason="no display available")
def test_leaving_read_only_hides_the_banner() -> None:
    c = Composer()
    c.set_read_only(True, "Read-only.")
    c.set_read_only(False)
    assert not c._ro_banner.get_revealed()


def _guarded_window(monkeypatch, session):
    """A window that will actually take _on_session_selected's early return.

    Three things gate it and all three must be forced, or the test drives the
    NORMAL path and passes whether or not the fix is present -- which is exactly
    what the first version of this test did.
    """
    from helios.backend import session_providers

    win = _window()
    win._driver = types.SimpleNamespace(session_id=session.session_id)
    win._driver_manager = types.SimpleNamespace(
        driver_for_session=lambda _sid: win._driver,
        bind_current=lambda _drv: None,
    )
    win._driver_matches_selected_provider = lambda _d: True
    # The REAL dataclass, not a stand-in: `known` is a derived property and
    # downstream code also reads `conflicted`, so a SimpleNamespace passes the
    # guard and then blows up 600 lines later.
    monkeypatch.setattr(
        session_providers, "resolve_provider",
        lambda *_a, **_k: session_providers.ProviderResolution(provider="anthropic"),
    )
    # Called unbound on the CLASS, so an instance attribute would be ignored.
    monkeypatch.setattr(MainWindow, "_driver_matches_project", lambda *_a: True)
    return win


def test_a_chat_you_started_stops_being_keyed_on_its_cwd(tmp_path, monkeypatch) -> None:
    """The guard path has to update the draft key too.

    _on_session_selected early-returns when the selection already matches the
    running driver, and `_refresh_sidebar_for_live` auto-selects the new row
    ~400ms after `session-started` precisely because that return is safe. So
    this is the path EVERY chat the user starts takes.

    The stash was hoisted above every return; the key update was not. A started
    chat therefore kept its `new:<cwd>` key for life: a follow-up typed into it
    parked under the cwd key, vanished from the composer on the next switch, and
    resurfaced in the next New chat in that folder -- primed to send into a
    different conversation.
    """
    live = _session(tmp_path, "sess-live", "/repo/a")
    win = _guarded_window(monkeypatch, live)
    win._draft_key = "new:/repo/a"      # what a fresh chat in that folder keys on
    win._composer.text = "sentinel"

    MainWindow._on_session_selected(win, None, live)

    # Prove the guard fired: the normal path would have called set_text() and
    # replaced this. Without this assertion the test passes on the normal path
    # and proves nothing.
    assert win._composer.text == "sentinel", "the early return was not taken"
    assert win._draft_key == draft_key(live), (
        f"guard path left the key at {win._draft_key!r} -- a started chat stays "
        "keyed on its cwd forever"
    )


def test_a_follow_up_in_a_started_chat_does_not_leak_into_a_new_chat(
    tmp_path, monkeypatch
) -> None:
    """The user-visible consequence of the key desync, end to end."""
    live = _session(tmp_path, "sess-live", "/repo/a")
    other = _session(tmp_path, "sess-other", "/repo/b")
    win = _guarded_window(monkeypatch, live)
    win._draft_key = "new:/repo/a"

    MainWindow._on_session_selected(win, None, live)      # guard path
    win._composer.text = "follow-up for sess-live"

    win._driver = None                                    # now switch away
    win._driver_manager = types.SimpleNamespace(
        driver_for_session=lambda _sid: None, bind_current=lambda _drv: None
    )
    MainWindow._on_session_selected(win, None, other)
    assert win._composer.text == "", "the follow-up followed us into another session"

    MainWindow._on_session_selected(win, None, live)
    assert win._composer.text == "follow-up for sess-live", (
        "the follow-up was lost -- it parked under the stale cwd key"
    )


def test_deselecting_takes_the_read_only_banner_down(tmp_path) -> None:
    """A pooled session's banner must not outlive the selection.

    The `session is None` branch returns early and never touched read-only
    state, so leaving a pool row for the welcome page left a revealed
    "Read-only" strip over a composer with nothing selected. `_read_only`
    already leaked this way on main; the banner turned an invisible leak into a
    visible artefact.
    """
    win = _window()
    pooled = _session(tmp_path, "sess-pool", "/repo/pool")
    pooled.project.read_only = True

    MainWindow._on_session_selected(win, None, pooled)
    assert win._composer.read_only is True

    MainWindow._on_session_selected(win, None, None)
    assert win._composer.read_only is False, "the banner outlived the selection"


def test_a_started_chats_draft_does_not_stay_in_the_fresh_chat_slot(
    tmp_path, monkeypatch
) -> None:
    """The guard path must MIGRATE the draft, not leave a copy behind.

    `_on_session_selected` stashes the outgoing buffer under the previous key
    before anything else. On the guard path that key is the fresh chat's
    `new:<cwd>` — but the composer keeps its text and the key becomes the
    session id. `DraftBook.take()` is a non-destructive get, so without an
    eviction the same text sits under both identities, and the next New chat in
    that folder restores the stale copy: cross-conversation misattribution,
    which is the defect this change exists to remove.
    """
    live = _session(tmp_path, "sess-live", "/repo/a")
    win = _guarded_window(monkeypatch, live)
    win._draft_key = "new:/repo/a"
    win._composer.text = "typed before the row auto-selected"

    MainWindow._on_session_selected(win, None, live)

    assert win._drafts.take("sess-live") == "typed before the row auto-selected"
    assert win._drafts.take("new:/repo/a") == "", (
        "the draft is still sitting in the fresh-chat slot — a New chat in this "
        "folder will resurrect it into a different conversation"
    )
