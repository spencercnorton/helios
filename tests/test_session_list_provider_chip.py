"""Sidebar backend chips are resolved on the status-tick worker.

`resolve_provider` walks a transcript to EOF by design (that re-scan is the
fail-closed safety property), and it used to run once per row inside
`_SessionRow.__init__` — i.e. on the GTK thread, on every render, and a render
fires on every completed turn. These pin the new arrangement end to end: the
row paints "…" with no I/O, the tick worker resolves, and the idle repaints.
"""

from __future__ import annotations

import json
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError:  # pragma: no cover - host without the GTK4/Adw typelibs
    pytest.skip("Gtk 4.0 / Adw 1 unavailable", allow_module_level=True)

from gi.repository import Adw, GLib  # noqa: E402

from helios.backend import session_providers  # noqa: E402
from helios.backend.projects import Project, Session  # noqa: E402
from helios.widgets import session_list as SL  # noqa: E402

Adw.init()


def _session(tmp_path, session_id: str, version: str, *, read_only: bool = False):
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"sessionId": session_id, "version": version, "type": "assistant"})
        + "\n",
        encoding="utf-8",
    )
    project = Project(
        dirname="-home-alice-proj",
        # NOT tmp_path: on Linux CI that is under /tmp, which _matches_filter
        # classifies as a throwaway cwd and hides from the default filter.
        cwd="/home/alice/proj",
        path=tmp_path,
        origin="macbook" if read_only else "local",
        read_only=read_only,
    )
    return Session(
        project=project,
        session_id=session_id,
        path=path,
        mtime=time.time(),
        size=path.stat().st_size,
    )


@pytest.fixture()
def sidebar():
    widget = SL.SessionList()
    widget._local_sessions = []
    widget._pool_sessions = []
    try:
        yield widget
    finally:
        widget.shutdown()


def _pump(predicate, timeout: float = 10.0) -> bool:
    """Spin the default main context until `predicate` holds (or we give up).

    The tick's worker is a daemon thread that hands its result back through
    GLib.idle_add, so nothing lands until this loop runs the idle.
    """
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        ctx.iteration(False)
        time.sleep(0.01)
    return predicate()


def test_a_freshly_built_row_asserts_no_owner(sidebar, tmp_path) -> None:
    """The row does no I/O: with nothing supplied the chip is the ellipsis,
    which is a different claim from "Unknown"."""
    row = SL._SessionRow(
        _session(tmp_path, "pending", "helios-codex"),
        SL.SessionStatus(state=SL.STATE_IDLE, last_response_at=0.0),
        on_delete=lambda _s: None,
    )

    assert row._provider_lbl.get_label() == "…"
    assert row.provider_resolution is None


def test_update_provider_drops_the_previous_class(sidebar, tmp_path) -> None:
    """Two provider classes on one label would style it twice — the
    remove-old-class loop is the easy half to forget."""
    row = SL._SessionRow(
        _session(tmp_path, "restyled", "helios-codex"),
        SL.SessionStatus(state=SL.STATE_IDLE, last_response_at=0.0),
        on_delete=lambda _s: None,
    )
    lbl = row._provider_lbl
    assert lbl.has_css_class("helios-provider-unknown")

    row.update_provider(
        session_providers.ProviderResolution(provider="openai", sources=("index",))
    )

    assert lbl.get_label() == "GPT"
    assert lbl.has_css_class("helios-provider-gpt")
    assert not lbl.has_css_class("helios-provider-unknown")


def test_the_tick_worker_resolves_the_chip(sidebar, tmp_path) -> None:
    """End to end: render paints "…", the worker resolves, the idle repaints.

    Nothing here is faked — this is the real `_tick_status` thread calling the
    real `resolve_provider` against a real transcript.
    """
    sidebar._local_sessions = [_session(tmp_path, "codexrow", "helios-codex")]
    sidebar._render(preserve_selection=False)
    row = sidebar._rows_by_id["codexrow"]
    assert row._provider_lbl.get_label() == "…"

    assert _pump(lambda: row._provider_lbl.get_label() != "…"), "chip never resolved"
    assert row._provider_lbl.get_label() == "GPT"
    assert row._provider_lbl.has_css_class("helios-provider-gpt")


def test_a_rebuild_carries_the_last_resolution_forward(sidebar, tmp_path) -> None:
    """The paint hint: a rebuilt row shows the previous answer for at most one
    tick rather than flashing every chip back through "…"."""
    sidebar._local_sessions = [_session(tmp_path, "codexrow", "helios-codex")]
    sidebar._render(preserve_selection=False)
    row = sidebar._rows_by_id["codexrow"]
    assert _pump(lambda: row._provider_lbl.get_label() != "…")

    sidebar._render(preserve_selection=True)

    rebuilt = sidebar._rows_by_id["codexrow"]
    assert rebuilt is not row
    assert rebuilt._provider_lbl.get_label() == "GPT"


def test_render_kicks_a_tick_without_waiting_for_the_timer(sidebar, tmp_path) -> None:
    """_STATUS_REFRESH_MS is 2000ms; a rebuilt list must not sit on "…" for it."""
    calls: list[int] = []
    sidebar._tick_status = lambda: calls.append(1)
    sidebar._local_sessions = [_session(tmp_path, "kicked", "helios-codex")]

    sidebar._render(preserve_selection=False)
    assert calls == [], "the kick must be an idle, not a synchronous call"

    ctx = GLib.MainContext.default()
    for _ in range(20):
        if calls:
            break
        ctx.iteration(False)
    assert calls, "no tick was queued after the rebuild"


def test_pool_chips_resolve_once_per_reload_not_every_tick(sidebar, tmp_path) -> None:
    """Pool transcripts live on a slow shared mount and `resolve_provider`
    stats one unconditionally, ahead of its lru_cache. Local rows re-derive
    every tick (that is the safety property); pool rows keep their previous
    once-per-reload cadence instead of moving onto a 2s timer."""
    seen: list[list[str]] = []
    real = session_providers.resolve_provider

    def spy(session_id, transcript_path=None, **kw):
        seen[-1].append(session_id)
        return real(session_id, transcript_path, **kw)

    sidebar._local_sessions = [_session(tmp_path, "localrow", "helios-codex")]
    sidebar._pool_sessions = [
        _session(tmp_path, "poolrow", "helios-codex", read_only=True)
    ]
    sidebar._render(preserve_selection=False)
    assert set(sidebar._rows_by_id) == {"localrow", "poolrow"}

    SL.session_providers.resolve_provider = spy
    try:
        for _ in range(2):
            seen.append([])
            sidebar._status_tick_running = False
            sidebar._tick_status()
            assert _pump(lambda: not sidebar._status_tick_running), "tick never landed"
    finally:
        SL.session_providers.resolve_provider = real

    assert sorted(seen[0]) == ["localrow", "poolrow"], seen
    assert seen[1] == ["localrow"], seen


def test_a_pool_free_tick_does_not_burn_the_generation(sidebar, tmp_path) -> None:
    """Cold start: the pool scan lands AFTER the first tick, on the same
    generation.

    `_reload_gen` advances only in `reload()`; every other render path
    (`_apply_pool_sessions`, `_on_filter_changed`, `_set_filter`,
    `set_live_session_ids`) re-renders on the same one. So a tick that runs
    before the CIFS scan returns — with no pool rows in `_rows_by_id` — must
    not mark that generation's pool work done. It used to, and every pool row
    arriving afterwards was excluded from `provider_rows` for the life of the
    generation, leaving its chip at "…" until the next full reload.

    This is the cold-start path verbatim: `reload(rescan_pool=True)` renders
    with `_pool_sessions == []` and kicks a tick immediately.
    """
    seen: list[list[str]] = []
    real = session_providers.resolve_provider

    def spy(session_id, transcript_path=None, **kw):
        seen[-1].append(session_id)
        return real(session_id, transcript_path, **kw)

    # Tick one: local rows only, exactly as at cold start.
    sidebar._local_sessions = [_session(tmp_path, "localrow", "helios-codex")]
    sidebar._pool_sessions = []
    sidebar._render(preserve_selection=False)

    SL.session_providers.resolve_provider = spy
    try:
        seen.append([])
        sidebar._status_tick_running = False
        sidebar._tick_status()
        assert _pump(lambda: not sidebar._status_tick_running), "tick never landed"
        assert seen[0] == ["localrow"], seen

        # The slow scan lands on the SAME generation and re-renders.
        sidebar._pool_sessions = [
            _session(tmp_path, "poolrow", "helios-codex", read_only=True)
        ]
        sidebar._render(preserve_selection=True)
        assert "poolrow" in sidebar._rows_by_id

        seen.append([])
        sidebar._status_tick_running = False
        sidebar._tick_status()
        assert _pump(lambda: not sidebar._status_tick_running), "tick never landed"
    finally:
        SL.session_providers.resolve_provider = real

    assert "poolrow" in seen[1], (
        "the pool row was never resolved — a pool-free tick burned the "
        f"generation and its chip stays at '…' forever. saw: {seen}"
    )
