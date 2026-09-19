from __future__ import annotations

import json
import stat
import time
from collections.abc import Callable

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from helios.widgets.mission_pane import (  # noqa: E402
    MissionPane,
    _MissionLoadResult,
    find_tandem_binary,
    TandemBinaryNotFound,
)

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)
Adw.init()


@pytest.fixture
def make_pane():
    """Construct MissionPanes and guarantee teardown even when a test's
    assertions fail partway through.

    A MissionPane owns a background loader thread, a debounce timer and one or
    two Gio.FileMonitors. If a test asserts and raises before an inline
    ``pane.shutdown()``, those live GObjects leak into the *next* test in the
    same process — the exact mechanism behind the cross-test PyGObject
    segfault this suite hit. A finalizer makes teardown unconditional.
    """
    panes: list[MissionPane] = []

    def _make() -> MissionPane:
        pane = MissionPane()
        panes.append(pane)
        return pane

    yield _make

    # Attempt to tear down every pane, but do NOT swallow failures — a
    # shutdown() that raises is itself a lifecycle regression and must surface.
    errors = []
    for pane in panes:
        try:
            pane.shutdown()
        except Exception as exc:  # noqa: BLE001 — re-raised after all cleanup
            errors.append(exc)
    if errors:
        raise AssertionError(f"pane.shutdown() raised during teardown: {errors!r}")


def _write_mission(tandem_root, slug: str, *, status="pending", phases=None, slices=None):
    mission_dir = tandem_root / "missions" / slug
    mission_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "mission": slug,
        "created_at": time.time(),
        "status": status,
        "phases": phases or {},
        "slices": slices or [],
    }
    (mission_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (mission_dir / "mission.toml").write_text(
        'goal = "Test goal"\nrepo = "/tmp/repo"\nbase_branch = "main"\ngates = ["reconcile"]\n',
        encoding="utf-8",
    )
    return mission_dir


def _loaded(pane: MissionPane) -> Callable[[], bool]:
    """Predicate: the latest reload() has been delivered and applied.

    ``_applied_token`` only reaches ``_token`` once the load for the newest
    submitted token has run through ``_apply_missions`` — so this is true
    exactly when the most recent reload's result is on screen, never on a
    stale earlier delivery."""
    return lambda: pane._applied_token == pane._token


def test_mission_pane_constructs_empty(monkeypatch, tmp_path, make_pane):
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    pane = make_pane()
    assert isinstance(pane, Gtk.Box)


def test_mission_pane_lists_mission_and_renders_phases(monkeypatch, tmp_path, make_pane):
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    _write_mission(
        tmp_path,
        "demo-mission",
        status="pending",
        phases={
            "plan": {"status": "done"},
            "audit": {"status": "done"},
            "reconcile": {"status": "gated"},
        },
    )
    pane = make_pane()
    pane.reload()
    assert _pump_until(_loaded(pane)), "async mission load never delivered"

    assert pane._selected_slug == "demo-mission"
    # 8 fixed phase rows regardless of what's actually present (G4 shape).
    assert len(list(_iter_children(pane._phase_box))) == 8
    # Gate banner should be visible: reconcile is gated.
    assert pane._gate_revealer.get_reveal_child() is True


def test_approve_button_shells_fake_tandem_binary(monkeypatch, tmp_path, make_pane):
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    _write_mission(
        tmp_path,
        "gated-mission",
        status="gated",
        phases={"plan": {"status": "done"}, "reconcile": {"status": "gated"}},
    )

    # A fake `tandem` binary on $HELIOS_TANDEM_BINARY records its argv to a
    # marker file, mirroring claude_binary.py's env-override resolution step.
    marker = tmp_path / "approve-called.txt"
    fake_bin = tmp_path / "fake-tandem"
    fake_bin.write_text(
        "#!/bin/sh\n"
        f'echo "$@" > "{marker}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("HELIOS_TANDEM_BINARY", str(fake_bin))

    resolved = find_tandem_binary()
    assert str(resolved) == str(fake_bin)

    pane = make_pane()
    pane.reload()
    assert _pump_until(_loaded(pane))
    assert pane._selected_slug == "gated-mission"

    pane._on_approve_response(None, "approve", "gated-mission")
    # Approve shells out on a daemon thread; wait on the marker it writes.
    assert _pump_until(lambda: marker.exists()), "approve subprocess never ran"
    assert marker.read_text().strip() == "mission approve gated-mission"


def test_find_tandem_binary_raises_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("HELIOS_TANDEM_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir, no tandem on PATH
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.local/bin/tandem either
    with pytest.raises(TandemBinaryNotFound):
        find_tandem_binary()


def test_render_never_calls_blocking_store_io_on_main_thread(monkeypatch, tmp_path, make_pane):
    """[MAJOR — UI thread block] tandem_activity()/usage_rollup() must be
    computed on the LatestTaskRunner background thread inside
    `_load_missions`, never from `_render`/`_on_selector_changed` on the GTK
    main thread. Patch both store functions to explode if called at all
    after the background load has already populated the pane's cached
    values — `_render` must only consume `self._activity` /
    `self._usage_by_slug`."""
    from helios.backend import mission_store as store

    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    _write_mission(tmp_path, "demo-mission", status="pending", phases={"plan": {"status": "done"}})

    pane = make_pane()
    pane.reload()
    assert _pump_until(_loaded(pane))
    assert pane._selected_slug == "demo-mission"

    def _boom(*_a, **_kw):
        raise AssertionError("tandem_activity/usage_rollup must not run on the main thread render path")

    monkeypatch.setattr(store, "tandem_activity", _boom)
    monkeypatch.setattr(store, "usage_rollup", _boom)

    # Re-render via the selector-changed path (main thread only) — must not
    # touch either patched function.
    pane._render(pane._missions[0])


def test_approve_stale_selection_does_not_clobber_new_selection(monkeypatch, tmp_path, make_pane):
    """[MAJOR — async race] If the user switches the selected mission (which
    bumps self._token) before a pending approve's subprocess result comes
    back, `_approve_done` must no-op the toast/button mutation for the
    stale slug rather than clobber whatever is now selected. Mirrors the
    token==self._token guard in `_apply_missions`/plan_pane.py's
    `_apply_session_state`."""
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    _write_mission(tmp_path, "mission-a", status="gated", phases={"plan": {"status": "gated"}})
    _write_mission(tmp_path, "mission-b", status="gated", phases={"plan": {"status": "gated"}})

    pane = make_pane()
    pane.reload()
    assert _pump_until(_loaded(pane))

    toasts: list[str] = []
    monkeypatch.setattr(pane, "_toast", lambda root, text, timeout=4: toasts.append(text))

    approve_token_at_click = pane._token
    # Simulate the user switching selection after the click but before the
    # background subprocess result lands (token now stale).
    pane._token += 1
    pane._selected_slug = "mission-b"

    pane._approve_done("mission-a", approve_token_at_click, (True, ""))
    assert toasts == [], "stale approve result must not toast for a selection that's moved on"
    assert pane._approve_btn.get_sensitive() is True


def test_approve_current_selection_still_toasts(monkeypatch, tmp_path, make_pane):
    """The non-stale case must still behave as before: same slug, same
    token -> toast fires."""
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    _write_mission(tmp_path, "mission-a", status="gated", phases={"plan": {"status": "gated"}})

    pane = make_pane()
    pane.reload()
    assert _pump_until(lambda: pane._applied_token == pane._token and pane._selected_slug == "mission-a")

    toasts: list[str] = []
    monkeypatch.setattr(pane, "_toast", lambda root, text, timeout=4: toasts.append(text))

    pane._approve_done("mission-a", pane._token, (True, ""))
    assert len(toasts) == 1
    assert "Approved mission-a" in toasts[0]


def test_gate_notification_suppressed_on_first_load(monkeypatch, tmp_path, make_pane):
    """[MINOR] An already-gated mission observed on the very first snapshot
    after construction must NOT fire a desktop notification — only
    transitions observed across subsequent reloads should. Otherwise every
    Helios restart re-fires a notification for a mission that's been sitting
    gated for days."""
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    _write_mission(
        tmp_path,
        "already-gated",
        status="gated",
        phases={"plan": {"status": "gated"}},
    )

    pane = make_pane()
    notified = []
    monkeypatch.setattr(pane, "_notify_gate", lambda mission: notified.append(mission.slug))

    pane.reload()
    assert _pump_until(_loaded(pane))
    assert notified == [], "first snapshot must only seed state, never notify"

    # Now simulate a real transition on a *subsequent* reload: mission goes
    # from gated -> pending (unlikely in practice but exercises the seeded
    # state) and then back to gated; the second transition should notify.
    _write_mission(tmp_path, "already-gated", status="pending", phases={"plan": {"status": "done"}})
    pane.reload()
    assert _pump_until(_loaded(pane))
    assert notified == []

    _write_mission(
        tmp_path,
        "already-gated",
        status="gated",
        phases={"plan": {"status": "done"}, "audit": {"status": "gated"}},
    )
    pane.reload()
    assert _pump_until(_loaded(pane))
    assert notified == ["already-gated"]


def test_stale_delivery_after_shutdown_is_discarded(monkeypatch, tmp_path, make_pane):
    """[async lifecycle] A background load that finishes after the pane is
    torn down must not mutate the finalizing widget tree. Both the delivery
    hop and the applied-callback have to no-op once shut down."""
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path))
    _write_mission(tmp_path, "demo", phases={"plan": {"status": "done"}})

    pane = make_pane()
    pane.reload()
    assert _pump_until(_loaded(pane))

    pane.shutdown()
    before = pane._applied_token
    result = _MissionLoadResult(missions=[], activity={}, usage_by_slug={})

    # The idle-hop must not even schedule an apply after shutdown...
    scheduled: list[object] = []
    monkeypatch.setattr(
        "helios.widgets.mission_pane.GLib.idle_add",
        lambda *a, **k: scheduled.append(a),
    )
    from helios.widgets.mission_pane import _MissionLoadRequest

    pane._deliver_missions(_MissionLoadRequest(token=pane._token, slug="demo"), result)
    assert scheduled == [], "no delivery may be scheduled after shutdown"

    # ...and a direct apply-call (e.g. an idle already in the queue) no-ops.
    assert pane._apply_missions(result, pane._token) is False
    assert pane._applied_token == before


def test_late_monitor_callbacks_after_shutdown_are_inert(monkeypatch, tmp_path, make_pane):
    """[blocker #2] A Gio.FileMonitor 'changed' signal already queued when
    shutdown() ran must not re-bind a monitor or re-arm a debounce timeout
    against the finalizing pane. Both monitor callbacks must early-out on
    _destroyed, keeping monitor=None / debounce=0 invariant."""
    monkeypatch.setenv("TANDEM_STATE_DIR", str(tmp_path / ".tandem"))
    (tmp_path / ".tandem" / "missions").mkdir(parents=True)

    pane = make_pane()
    pane.shutdown()
    assert pane._destroyed is True
    assert pane._missions_monitor is None
    assert pane._debounce_id == 0

    class _FakeFile:
        def get_path(self):
            return str(tmp_path / ".tandem" / "missions" / "state.json")

    # Deliver both queued-callback shapes GTK could still dispatch.
    pane._on_monitor_changed(None, _FakeFile(), None, None)
    pane._on_ancestor_monitor_changed(None, None, None, None)

    # Invariant preserved: no resurrected monitor, no re-armed timer.
    assert pane._missions_monitor is None
    assert pane._mission_monitor is None
    assert pane._debounce_id == 0


def test_state_isolation_never_touches_real_tandem(monkeypatch, tmp_path, make_pane):
    """[state isolation] With no TANDEM_STATE_DIR override the pane resolves
    ~/.tandem, which the autouse conftest fixture points at the per-test temp
    HOME. It must read only that isolated tree and never create ~/.tandem."""
    from helios.backend import mission_store

    monkeypatch.delenv("TANDEM_STATE_DIR", raising=False)
    # conftest already set HOME=tmp_path; prove the resolution stays inside it.
    assert str(mission_store.tandem_root()).startswith(str(tmp_path))

    pane = make_pane()
    pane.reload()
    assert _pump_until(_loaded(pane))
    assert pane._missions == []
    # Helios never writes to the tandem tree — single-writer discipline.
    assert not (tmp_path / ".tandem").exists()


def test_artifact_chip_skips_paths_escaping_mission_dir(tmp_path):
    """[MINOR — security] `_artifact_chip` must resolve the candidate path
    and verify it stays inside the mission directory before building a
    launchable chip — a crafted `state.json` `artifacts[]` entry pointing
    outside the mission dir (e.g. `../../../.ssh/id_ed25519`) must be
    dropped, not opened."""
    from helios.widgets.mission_pane import _artifact_chip, _contained_path

    mission_dir = tmp_path / "missions" / "demo"
    mission_dir.mkdir(parents=True)
    (mission_dir / "artifacts").mkdir()
    (mission_dir / "artifacts" / "PLAN.md").write_text("plan", encoding="utf-8")

    outside_target = tmp_path / "secret.txt"
    outside_target.write_text("secret", encoding="utf-8")

    # Safe, contained path.
    assert _contained_path(mission_dir, "artifacts/PLAN.md") is not None
    chip = _artifact_chip(mission_dir, "artifacts/PLAN.md")
    assert chip is not None

    # Escaping path must be rejected.
    assert _contained_path(mission_dir, "../../secret.txt") is None
    escaping_chip = _artifact_chip(mission_dir, "../../secret.txt")
    assert escaping_chip is None


def test_missions_monitor_falls_back_to_nearest_existing_ancestor(monkeypatch, tmp_path, make_pane):
    """[MINOR] If `~/.tandem` itself doesn't exist, the monitor must not
    silently fail to bind at all — it should watch the nearest existing
    ancestor (worst case `Path.home()`) instead of raising and giving up.
    No directory under the tandem tree should be created as a side effect
    of starting the monitor (no mkdir of ~/.tandem or missions/ from
    Helios)."""
    nonexistent_root = tmp_path / "does-not-exist-yet" / ".tandem"
    monkeypatch.setenv("TANDEM_STATE_DIR", str(nonexistent_root))

    pane = make_pane()
    assert pane._missions_monitor is not None
    assert not nonexistent_root.exists()
    assert not nonexistent_root.parent.exists()


def _iter_children(box):
    child = box.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _pump_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Drive the default GLib main context until *predicate* holds or *timeout*
    elapses, returning the predicate's final value.

    Deterministic where a fixed iteration count is not: a MissionPane reload
    hands work to a background thread that only schedules its `idle_add`
    delivery once its file I/O finishes, so the main-context queue is
    routinely empty for the first few milliseconds. A loop that stops the
    moment the queue drains (the old `_pump_main_loop`) asserts before the
    result ever lands. This one keeps pumping-then-yielding until the caller's
    real condition is met, and fails loudly (returns False) if it never is —
    it does not paper over a missed delivery with a blind sleep."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while True:
        # Drain ready sources, but never past the hard deadline — a
        # continuously-pending source (e.g. a repeating timer that is always
        # ready) must not trap us in this inner loop forever.
        while ctx.pending() and time.monotonic() < deadline:
            ctx.iteration(False)
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return predicate()
        # Yield so the background loader thread can run and schedule its
        # delivery; the next loop pumps it. Not a fixed wait — a poll on an
        # explicit condition with a hard deadline.
        time.sleep(0.01)
