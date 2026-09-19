"""Missions tab — read-only cockpit for `tandem mission` runs.

Renders the on-disk contract parsed by `backend/mission_store.py` (GTK-free
seam — see that module's docstring for the full G1-G12 gotcha table this
pane honors). Structurally follows `plan_pane.py`: header -> ScrolledWindow
-> sections, with a `_render()` that clears and repopulates.

**Single mutation path.** This pane never writes to `~/.tandem/` itself.
The only state change it can cause is the Approve button, which shells
`tandem mission approve <slug>` as a subprocess (G10) — the same
single-writer discipline as every other tandem state transition. Binary
resolution mirrors `backend/claude_binary.py`: GUI launches don't have
`~/.local/bin` on PATH, so `shutil.which("tandem")` alone is not enough.

**Refresh is transcript-follow, not Goal Mode.** `~/.tandem/` is written by
external processes (a `tandem mission run` in a terminal, cron, another
session), so this pane watches the filesystem instead of relying on an
in-process cache: one `Gio.FileMonitor` on the missions directory (catches
new/removed mission dirs) plus one on the selected mission's directory
(catches `state.json` replace + artifact writes), both funneled through a
single 400ms `GLib.timeout_add` debounce and ignoring any `*.tmp` path
(G5 — `state.json.tmp` is a real sibling file mid-write, never valid JSON).
Reloads run on a `LatestTaskRunner` with a monotonic token, delivered back
via `GLib.idle_add`, exactly like `plan_pane.py`'s session load path. There
is no periodic poll while the tab is hidden — only the monitor callbacks and
a one-shot re-scan when the tab becomes visible.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import NamedTuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from helios.backend import mission_store
from helios.backend.latest_worker import LatestTaskRunner
from helios.backend.process.env_scrub import scrubbed_child_env
from helios.backend.ui_state import store as ui_state_store
from helios.log import get_logger

_log = get_logger("mission_pane")

_DEBOUNCE_MS = 400


class TandemBinaryNotFound(RuntimeError):
    pass


def find_tandem_binary() -> Path:
    """Resolve a working `tandem` binary.

    Mirrors `backend/claude_binary.py`'s lookup order: GUI launches (GNOME
    app grid / .desktop) get a session environment WITHOUT `~/.local/bin` on
    PATH, so a bare `shutil.which("tandem")` silently misses a perfectly
    good standalone install exactly when Helios is started the normal way.
    """
    env = os.environ.get("HELIOS_TANDEM_BINARY")
    if env:
        p = Path(env).expanduser()
        if _is_runnable(p):
            return p

    on_path = shutil.which("tandem")
    if on_path:
        p = Path(on_path).resolve()
        if _is_runnable(p):
            return p

    local_bin = Path.home() / ".local" / "bin" / "tandem"
    if _is_runnable(local_bin):
        return local_bin.resolve()

    raise TandemBinaryNotFound(
        "Could not find a working `tandem` binary. Set $HELIOS_TANDEM_BINARY "
        "or fix your `tandem` install."
    )


def _is_runnable(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


class _MissionLoadRequest(NamedTuple):
    token: int
    slug: str


class _MissionLoadResult(NamedTuple):
    missions: list[mission_store.Mission]
    activity: dict[str, int]
    usage_by_slug: dict[str, dict[str, dict[str, int]]]


class MissionPane(Gtk.Box):
    """Right-side Missions surface: mission selector + phases + slices +
    usage rollup + gate banner for the selected `tandem mission`."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("helios-mission-pane")
        self.set_size_request(280, -1)

        self._missions: list[mission_store.Mission] = []
        self._selected_slug: str = ""
        self._prev_derived_state: dict[str, str] = {}
        self._seen_first_snapshot = False
        self._token = 0
        # Bumped by reload(), matched by _apply_missions once the latest load
        # has been applied. Lets a caller (or a test) wait for async delivery
        # on a real condition instead of a fixed number of loop iterations.
        self._applied_token = 0
        self._missions_monitor: Gio.FileMonitor | None = None
        self._mission_monitor: Gio.FileMonitor | None = None
        self._debounce_id = 0
        self._destroyed = False

        self._loader = LatestTaskRunner[_MissionLoadRequest, _MissionLoadResult](
            work=self._load_missions,
            deliver=self._deliver_missions,
            name="helios-mission-load",
        )
        self._activity: dict[str, int] = {}
        self._usage_by_slug: dict[str, dict[str, dict[str, int]]] = {}

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header.set_margin_top(12)
        header.set_margin_bottom(8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        title = Gtk.Label(label="Missions", xalign=0)
        title.add_css_class("title-4")
        header.append(title)
        self.append(header)

        selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        selector_box.set_margin_start(16)
        selector_box.set_margin_end(16)
        selector_box.set_margin_bottom(8)
        self._selector = Gtk.DropDown.new_from_strings([])
        self._selector.set_hexpand(True)
        self._selector.connect("notify::selected", self._on_selector_changed)
        selector_box.append(self._selector)
        self.append(selector_box)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.append(scroller)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(14)
        body.set_margin_end(14)
        body.set_margin_bottom(16)
        scroller.set_child(body)

        self._empty_label = Gtk.Label(
            label="No missions yet — run `tandem mission new <slug>` in a terminal.",
            xalign=0,
        )
        self._empty_label.add_css_class("dim-label")
        self._empty_label.add_css_class("caption")
        self._empty_label.set_wrap(True)
        body.append(self._empty_label)

        self._goal_label = Gtk.Label(xalign=0)
        self._goal_label.add_css_class("helios-mission-goal")
        self._goal_label.set_wrap(True)
        self._goal_label.set_lines(2)
        self._goal_label.set_ellipsize(Pango.EllipsizeMode.END)
        body.append(self._goal_label)

        self._repo_label = Gtk.Label(xalign=0)
        self._repo_label.add_css_class("dim-label")
        self._repo_label.add_css_class("caption")
        self._repo_label.set_wrap(True)
        body.append(self._repo_label)

        # Gate banner — revealer, only visible while a phase is gated (G2).
        self._gate_revealer = Gtk.Revealer()
        self._gate_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        gate_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        gate_box.add_css_class("helios-mission-gate-banner")
        self._gate_label = Gtk.Label(xalign=0)
        self._gate_label.set_wrap(True)
        gate_box.append(self._gate_label)
        self._approve_btn = Gtk.Button(label="Approve & continue")
        self._approve_btn.add_css_class("suggested-action")
        self._approve_btn.connect("clicked", self._on_approve_clicked)
        gate_box.append(self._approve_btn)
        self._gate_revealer.set_child(gate_box)
        body.append(self._gate_revealer)

        phases_title = Gtk.Label(label="Phases", xalign=0)
        phases_title.add_css_class("caption-heading")
        phases_title.add_css_class("dim-label")
        body.append(phases_title)
        self._phase_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(self._phase_box)

        self._slices_title = Gtk.Label(label="Slices", xalign=0)
        self._slices_title.add_css_class("caption-heading")
        self._slices_title.add_css_class("dim-label")
        body.append(self._slices_title)
        self._slices_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(self._slices_box)

        usage_title = Gtk.Label(label="Usage", xalign=0)
        usage_title.add_css_class("caption-heading")
        usage_title.add_css_class("dim-label")
        body.append(usage_title)
        self._usage_label = Gtk.Label(xalign=0)
        self._usage_label.add_css_class("caption")
        self._usage_label.add_css_class("dim-label")
        self._usage_label.set_wrap(True)
        body.append(self._usage_label)

        activity_title = Gtk.Label(label="tandem activity (5h window)", xalign=0)
        activity_title.add_css_class("caption-heading")
        activity_title.add_css_class("dim-label")
        body.append(activity_title)
        self._activity_label = Gtk.Label(xalign=0)
        self._activity_label.add_css_class("caption")
        self._activity_label.add_css_class("dim-label")
        self._activity_label.set_wrap(True)
        body.append(self._activity_label)

        artifacts_title = Gtk.Label(label="Artifacts", xalign=0)
        artifacts_title.add_css_class("caption-heading")
        artifacts_title.add_css_class("dim-label")
        body.append(artifacts_title)
        self._artifacts_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._artifacts_box.set_hexpand(True)
        body.append(self._artifacts_box)

        self._detail_body = body
        self._render_empty()
        self._start_missions_monitor()
        self.reload()

    # --- lifecycle -----------------------------------------------------

    def shutdown(self, *, join: bool = True) -> None:
        """Stop file monitors and pending debounce timers. Call from the
        window's close-request handler, same pattern as other panes.

        ``join=False`` is the fast, non-blocking close for window teardown."""
        self._destroyed = True
        # Stop the background loader from delivering a late result against the
        # finalizing widget tree, and (unless fast-closing) join its thread so
        # it isn't still walking ~/.tandem while we tear down.
        self._loader.shutdown(join=join)
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
            self._debounce_id = 0
        for attr in ("_missions_monitor", "_mission_monitor"):
            mon = getattr(self, attr, None)
            if mon is not None:
                try:
                    mon.cancel()
                except Exception:
                    pass
                setattr(self, attr, None)

    def on_tab_visible(self) -> None:
        """Fallback full re-scan when the Missions tab becomes visible —
        covers any monitor misses. No periodic poll while hidden."""
        self.reload()

    # --- monitors --------------------------------------------------------

    def _start_missions_monitor(self) -> None:
        # Helios never writes to ~/.tandem/ (single-writer discipline — the
        # tandem CLI subprocess in _on_approve_response is the only mutation
        # path in this whole feature), so this never mkdir()s the missions
        # dir. If missions_dir() doesn't exist yet — or even ~/.tandem
        # itself doesn't exist yet (tandem never run on this box at all) —
        # `monitor_directory` on a nonexistent path raises, and a naive
        # fallback to `tandem_root()` alone still raises in the
        # tandem-never-run case. Instead walk up to the nearest existing
        # ancestor (worst case `Path.home()`, which always exists) and
        # monitor that; every change there re-attempts to bind the real
        # missions-dir monitor via `_on_ancestor_monitor_changed`, so the
        # first `tandem mission new` creating `~/.tandem/missions/` for the
        # first time is picked up as soon as it appears. The tab-visible
        # full re-scan (`on_tab_visible`) remains the backstop for anything
        # this still misses.
        missions_dir = mission_store.missions_dir()
        if missions_dir.is_dir():
            self._bind_missions_monitor(missions_dir)
            return
        watch_path = _nearest_existing_ancestor(missions_dir)
        try:
            gfile = Gio.File.new_for_path(str(watch_path))
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except Exception as e:
            _log.warning("could not start missions dir monitor: %s", e)
            return
        monitor.connect("changed", self._on_ancestor_monitor_changed)
        self._missions_monitor = monitor

    def _bind_missions_monitor(self, missions_dir: Path) -> None:
        try:
            gfile = Gio.File.new_for_path(str(missions_dir))
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except Exception as e:
            _log.warning("could not start missions dir monitor: %s", e)
            return
        monitor.connect("changed", self._on_monitor_changed)
        self._missions_monitor = monitor

    def _on_ancestor_monitor_changed(self, _monitor, _file, _other, _event_type) -> None:
        # A Gio.FileMonitor "changed" signal already queued when shutdown()
        # ran would otherwise re-bind a fresh monitor and re-arm a debounce
        # timeout against the finalizing pane — resurrecting the very
        # resources teardown just released. Drop it.
        if self._destroyed:
            return
        # Watching an ancestor of missions_dir() because it didn't exist at
        # start-up. Any change under it is worth a debounced reload
        # regardless, but if the real missions dir has now appeared, rebind
        # onto it directly so future events aren't filtered through a
        # coarser ancestor watch.
        missions_dir = mission_store.missions_dir()
        if missions_dir.is_dir():
            old = self._missions_monitor
            self._bind_missions_monitor(missions_dir)
            if old is not None:
                try:
                    old.cancel()
                except Exception:
                    pass
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(_DEBOUNCE_MS, self._flush_debounce)

    def _start_mission_monitor(self, slug: str) -> None:
        if self._mission_monitor is not None:
            try:
                self._mission_monitor.cancel()
            except Exception:
                pass
            self._mission_monitor = None
        if not slug:
            return
        try:
            mission_dir = mission_store.missions_dir() / slug
            gfile = Gio.File.new_for_path(str(mission_dir))
            monitor = gfile.monitor_directory(Gio.FileMonitorFlags.NONE, None)
        except Exception as e:
            _log.warning("could not start mission monitor for %s: %s", slug, e)
            return
        monitor.connect("changed", self._on_monitor_changed)
        self._mission_monitor = monitor

    def _on_monitor_changed(self, _monitor, file, _other, _event_type) -> None:
        # A monitor "changed" signal already queued when shutdown() ran must
        # not re-arm a debounce timeout against the finalizing pane.
        if self._destroyed:
            return
        # Ignore atomic-write temp files (G5: state.json.tmp is a real sibling
        # file mid-write, never valid JSON to parse mid-flight).
        try:
            path = file.get_path() or ""
        except Exception:
            path = ""
        if path.endswith(".tmp"):
            return
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(_DEBOUNCE_MS, self._flush_debounce)

    def _flush_debounce(self) -> bool:
        self._debounce_id = 0
        if self._destroyed:
            return False
        self.reload()
        return False  # one-shot

    # --- loading ---------------------------------------------------------

    def reload(self) -> None:
        self._token += 1
        self._loader.submit(_MissionLoadRequest(token=self._token, slug=self._selected_slug))

    def _load_missions(self, _request: _MissionLoadRequest) -> _MissionLoadResult:
        # Everything here runs on the LatestTaskRunner's background thread,
        # never the GTK main thread: `tandem_activity()` tails the whole
        # unbounded ~/.tandem/log.jsonl and `usage_rollup()` walks every
        # phase/slice of every mission, so both must be fully computed here
        # and carried in the payload — `_render()` must only ever consume
        # already-computed values, never call back into mission_store's I/O.
        missions = mission_store.list_missions()
        activity = mission_store.tandem_activity()
        usage_by_slug = {m.slug: mission_store.usage_rollup(m) for m in missions}
        return _MissionLoadResult(missions=missions, activity=activity, usage_by_slug=usage_by_slug)

    def _deliver_missions(self, request: _MissionLoadRequest, result) -> None:
        if self._destroyed:
            return
        if isinstance(result, Exception):
            result = _MissionLoadResult(missions=[], activity={}, usage_by_slug={})
        GLib.idle_add(self._apply_missions, result, request.token)

    def _apply_missions(self, result: _MissionLoadResult, token: int) -> bool:
        if self._destroyed or token != self._token:
            return False
        self._applied_token = token
        missions = result.missions
        self._missions = missions
        self._activity = result.activity
        self._usage_by_slug = result.usage_by_slug
        self._check_gate_transitions(missions)

        labels = [
            f"{m.slug} — {mission_store.derived_state(m)}" for m in missions
        ]
        model = Gtk.StringList.new(labels)
        self._selector.set_model(model)

        if not missions:
            self._selected_slug = ""
            self._start_mission_monitor("")
            self._render_empty()
            return False

        slugs = [m.slug for m in missions]
        if self._selected_slug in slugs:
            idx = slugs.index(self._selected_slug)
        else:
            idx = 0
            self._selected_slug = slugs[0]
            self._start_mission_monitor(self._selected_slug)
        # Setting selected fires notify::selected only if it actually
        # changes; guard re-entrant reload from that path is harmless since
        # _render() is idempotent.
        self._selector.set_selected(idx)
        self._render(missions[idx])
        return False

    def _on_selector_changed(self, _dropdown, _pspec) -> None:
        idx = self._selector.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._missions):
            return
        mission = self._missions[idx]
        if mission.slug != self._selected_slug:
            self._selected_slug = mission.slug
            self._start_mission_monitor(mission.slug)
        self._render(mission)

    # --- gate notifications ----------------------------------------------

    def _check_gate_transitions(self, missions: list[mission_store.Mission]) -> None:
        """Raise a Gio.Notification the moment a phase transitions -> gated
        since the last snapshot (G9: the engine's own notify-send is
        best-effort and fires once; observing the store here is the
        reliable path).

        The very first snapshot after construction only *seeds*
        `_prev_derived_state` — it must never fire a notification, since an
        empty `_prev_derived_state` would otherwise make every
        already-gated mission look like a fresh transition on every Helios
        restart. Only transitions observed across subsequent reloads count.
        """
        ui = ui_state_store()
        notify_enabled = bool(ui.get("notify_on_mission_gate", True))
        first_snapshot = not self._seen_first_snapshot
        current: dict[str, str] = {}
        for mission in missions:
            state = mission_store.derived_state(mission)
            current[mission.slug] = state
            if not notify_enabled or first_snapshot:
                continue
            prev = self._prev_derived_state.get(mission.slug)
            if state == "gated" and prev != "gated":
                self._notify_gate(mission)
        self._prev_derived_state = current
        self._seen_first_snapshot = True

    def _notify_gate(self, mission: mission_store.Mission) -> None:
        phase = mission_store.gated_phase(mission)
        phase_name = phase.name if phase is not None else "?"
        safe_slug = GLib.markup_escape_text(mission.slug)
        app = None
        root = self.get_root()
        if root is not None:
            app = root.get_application()
        note = Gio.Notification.new(f"tandem mission gated: {safe_slug}")
        note.set_body(
            f"after {phase_name} — approve from the Missions pane or: "
            f"tandem mission approve {mission.slug}"
        )
        try:
            note.set_default_action("app.focus-missions")
        except Exception:
            pass
        if app is not None:
            try:
                app.send_notification(f"helios-mission-gate-{mission.slug}", note)
            except Exception:
                pass

    # --- rendering ---------------------------------------------------------

    def _render_empty(self) -> None:
        self._empty_label.set_visible(True)
        self._goal_label.set_visible(False)
        self._repo_label.set_visible(False)
        self._gate_revealer.set_reveal_child(False)
        _clear_box(self._phase_box)
        _clear_box(self._slices_box)
        _clear_box(self._artifacts_box)
        self._usage_label.set_label("—")
        self._activity_label.set_label("—")

    def _render(self, mission: mission_store.Mission) -> None:
        # Backstop: state.json / mission.toml are untrusted external-process
        # output. Every field is coerced at the parse boundary, but this
        # per-mission guard means any residual malformed value degrades to an
        # error row instead of taking down the GTK idle handler and blanking
        # the whole pane. Keep the boundary coercions too — this is defence in
        # depth, not a substitute for them.
        try:
            self._render_mission(mission)
        except Exception:  # noqa: BLE001 — never let one bad mission kill render
            _log.exception("failed to render mission %s", mission.slug)
            self._render_error(mission.slug)

    def _render_error(self, slug: str) -> None:
        self._empty_label.set_visible(False)
        self._goal_label.set_visible(True)
        self._repo_label.set_visible(False)
        safe_slug = GLib.markup_escape_text(slug or "?")
        self._goal_label.set_label(f"⚠ Could not render “{safe_slug}” — malformed mission files")
        self._gate_revealer.set_reveal_child(False)
        _clear_box(self._phase_box)
        _clear_box(self._slices_box)
        _clear_box(self._artifacts_box)
        self._usage_label.set_label("—")
        self._activity_label.set_label("—")

    def _render_mission(self, mission: mission_store.Mission) -> None:
        self._empty_label.set_visible(False)
        self._goal_label.set_visible(True)
        self._repo_label.set_visible(True)

        safe_slug = GLib.markup_escape_text(mission.slug)
        goal = mission.spec.goal if mission.spec else ""
        self._goal_label.set_label(goal or f"({safe_slug} — no readable mission.toml)")

        if mission.spec:
            repo = mission.spec.repo or "—"
            self._repo_label.set_label(f"{repo} · {mission.spec.base_branch}")
        else:
            self._repo_label.set_label("—")

        gate = mission_store.gated_phase(mission)
        if gate is not None:
            self._gate_label.set_label(f"Gated after {gate.name}")
            self._gate_revealer.set_reveal_child(True)
        else:
            self._gate_revealer.set_reveal_child(False)

        _clear_box(self._phase_box)
        gates = set(mission.spec.gates) if mission.spec else set()
        by_name = {p.name: p for p in mission.phases}
        for name in mission_store.PHASE_ORDER:
            phase = by_name.get(name, mission_store.MissionPhase(name=name))
            self._phase_box.append(
                _PhaseRow(phase, has_gate=name in gates)
            )

        _clear_box(self._slices_box)
        if mission.slices:
            self._slices_title.set_visible(True)
            for sl in mission.slices:
                self._slices_box.append(_SliceRow(sl))
        else:
            self._slices_title.set_visible(True)
            placeholder = Gtk.Label(label="No slices yet.", xalign=0)
            placeholder.add_css_class("dim-label")
            placeholder.add_css_class("caption")
            self._slices_box.append(placeholder)

        # Both values are precomputed on the background loader thread (see
        # `_load_missions`) and only consumed here — never recomputed on the
        # GTK main thread, since `usage_rollup`/`tandem_activity` do real I/O
        # (tandem_activity tails the whole unbounded log.jsonl).
        rollup = self._usage_by_slug.get(mission.slug, {"anthropic": {}, "openai": {}})
        self._usage_label.set_label(_format_usage(rollup))

        self._activity_label.set_label(_format_activity(self._activity))

        _clear_box(self._artifacts_box)
        artifact_paths: list[str] = []
        for phase in mission.phases:
            artifact_paths.extend(phase.artifacts)
        # _artifact_chip returns None for any entry that resolves outside
        # the mission directory (G8 containment check) — those are silently
        # dropped rather than shown as a dead/dangerous chip.
        chips = [
            chip
            for rel in artifact_paths
            if (chip := _artifact_chip(mission.path, rel)) is not None
        ]
        if chips:
            for chip in chips:
                self._artifacts_box.append(chip)
        else:
            placeholder = Gtk.Label(label="—", xalign=0)
            placeholder.add_css_class("dim-label")
            placeholder.add_css_class("caption")
            self._artifacts_box.append(placeholder)

    # --- approve ---------------------------------------------------------

    def _on_approve_clicked(self, _btn) -> None:
        slug = self._selected_slug
        if not slug:
            return
        safe_slug = GLib.markup_escape_text(slug)
        dialog = Adw.AlertDialog.new(
            f"Approve gate for {safe_slug}?",
            f"The mission will need `tandem mission run {safe_slug}` to "
            "continue afterward — approving does not auto-run the next leg.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("approve", "Approve & continue")
        dialog.set_response_appearance("approve", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_approve_response, slug)
        root = self.get_root()
        if root is None:
            return
        dialog.present(root)

    def _on_approve_response(self, _dlg, response: str, slug: str) -> None:
        if response != "approve":
            return
        self._approve_btn.set_sensitive(False)
        # Capture the selection token at click time (mirrors the
        # token==self._token guard `_apply_missions` and
        # plan_pane.py's `_apply_session_state` already use). The subprocess
        # can take up to the 30s timeout below; if the user picks a different
        # mission (or the selector reloads onto a different one) before it
        # returns, the slug-specific UI mutations in `_approve_done` (toast,
        # button re-enable, reload-for-that-slug) must no-op rather than
        # clobber whatever is now selected. A background reload is still
        # fine either way.
        approve_token = self._token

        def worker() -> None:
            result: tuple[bool, str]
            try:
                binary = find_tandem_binary()
            except TandemBinaryNotFound as e:
                result = (False, str(e))
            else:
                try:
                    proc = subprocess.run(
                        [str(binary), "mission", "approve", slug],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        # `mission approve` only updates local mission state; it
                        # does not run a model and needs zero provider auth.
                        env=scrubbed_child_env(),
                    )
                    if proc.returncode == 0:
                        result = (True, "")
                    else:
                        tail = (proc.stderr or proc.stdout or "").strip()[-200:]
                        result = (False, tail or f"exit {proc.returncode}")
                except Exception as e:  # noqa: BLE001 — surface any failure as a toast
                    result = (False, str(e))
            GLib.idle_add(self._approve_done, slug, approve_token, result)

        threading.Thread(target=worker, daemon=True, name="helios-mission-approve").start()

    def _approve_done(self, slug: str, approve_token: int, result: tuple[bool, str]) -> bool:
        if self._destroyed:
            return False
        stale = slug != self._selected_slug or approve_token != self._token
        ok, message = result
        # Always re-arm the button and kick a background reload so the
        # store's on-disk truth (state.json flipped by the CLI subprocess)
        # eventually surfaces — but the toast and any slug-specific
        # mutation are gated on the selection not having moved on since the
        # click, exactly like `_apply_missions`'s token guard.
        self._approve_btn.set_sensitive(True)
        if not stale:
            root = self.get_root()
            if ok:
                self._toast(root, f"Approved {slug} — re-arm with `tandem mission run {slug}`.")
            else:
                self._toast(root, f"Approve failed: {message}", timeout=6)
        if ok:
            self.reload()
        return False

    def _toast(self, root, text: str, *, timeout: int = 4) -> None:
        toast_fn = getattr(root, "_toast", None)
        if callable(toast_fn):
            toast_fn(text, timeout=timeout)
        else:
            _log.info("mission pane toast (no root): %s", text)


class _PhaseRow(Gtk.Box):
    def __init__(self, phase: mission_store.MissionPhase, *, has_gate: bool) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.add_css_class("helios-mission-phase")

        status_key = _phase_status_key(phase)
        marker = Gtk.Box()
        marker.add_css_class("helios-mission-marker")
        marker.add_css_class(f"helios-mission-marker-{status_key}")
        marker.set_valign(Gtk.Align.CENTER)
        self.append(marker)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_hexpand(True)
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name_label = Gtk.Label(label=phase.name, xalign=0)
        name_label.add_css_class("caption-heading")
        name_row.append(name_label)
        if has_gate:
            gate_badge = Gtk.Label(label="gate")
            gate_badge.add_css_class("caption")
            gate_badge.add_css_class("helios-mission-badge-gate")
            name_row.append(gate_badge)
        text.append(name_row)

        detail = _phase_detail_text(phase, status_key)
        if detail:
            detail_label = Gtk.Label(label=detail, xalign=0)
            detail_label.add_css_class("caption")
            detail_label.add_css_class("dim-label")
            detail_label.set_wrap(True)
            text.append(detail_label)
        self.append(text)


class _SliceRow(Gtk.Box):
    def __init__(self, sl: mission_store.MissionSlice) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("helios-mission-slice")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=sl.title or sl.id or "(slice)", xalign=0)
        title.add_css_class("caption-heading")
        title.set_hexpand(True)
        header.append(title)
        if sl.review_verdict:
            chip = Gtk.Label(label=sl.review_verdict)
            chip.add_css_class("caption")
            chip.add_css_class(f"helios-mission-verdict-{sl.review_verdict}")
            header.append(chip)
        self.append(header)

        detail_bits = [f"status: {sl.status}" if sl.status else ""]
        if sl.fix_rounds:
            detail_bits.append(f"fix rounds: {sl.fix_rounds}")
        if sl.branch:
            detail_bits.append(sl.branch)
        detail = " · ".join(b for b in detail_bits if b)
        if detail:
            detail_label = Gtk.Label(label=detail, xalign=0)
            detail_label.add_css_class("caption")
            detail_label.add_css_class("dim-label")
            detail_label.set_wrap(True)
            detail_label.set_selectable(True)
            self.append(detail_label)

        if sl.test_result:
            tail = sl.test_result.strip().splitlines()[-3:]
            test_label = Gtk.Label(label="\n".join(tail), xalign=0)
            test_label.add_css_class("caption")
            test_label.add_css_class("dim-label")
            test_label.set_wrap(True)
            test_label.set_selectable(True)
            self.append(test_label)


def _phase_status_key(phase: mission_store.MissionPhase) -> str:
    if mission_store.is_quota_stop(phase):
        return "quota"
    return phase.status


def _phase_detail_text(phase: mission_store.MissionPhase, status_key: str) -> str:
    if status_key == "quota":
        return "quota stop (resumable)"
    if phase.status == "failed":
        return phase.note or "failed"
    if phase.started_at and phase.ended_at:
        duration = phase.ended_at - phase.started_at
        return f"{duration:.0f}s"
    if phase.status == "running":
        return "running…"
    return ""


def _format_usage(rollup: dict[str, dict[str, int]]) -> str:
    """G11: per-provider display, never a combined total."""
    parts: list[str] = []
    anthropic = rollup.get("anthropic") or {}
    if anthropic:
        in_tok = anthropic.get("input_tokens", 0)
        out_tok = anthropic.get("output_tokens", 0)
        parts.append(f"Claude {_fmt_tok(in_tok)} in / {_fmt_tok(out_tok)} out")
    openai = rollup.get("openai") or {}
    if openai:
        in_tok = openai.get("input_tokens", 0)
        cached = openai.get("cached_input_tokens", 0)
        out_tok = openai.get("output_tokens", 0)
        cached_note = f" ({_fmt_tok(cached)} cached)" if cached else ""
        parts.append(f"GPT {_fmt_tok(in_tok)} in{cached_note} / {_fmt_tok(out_tok)} out")
    return " · ".join(parts) if parts else "—"


def _format_activity(activity: dict[str, int]) -> str:
    if not activity:
        return "—"
    return " · ".join(f"{model}: {count} calls" for model, count in sorted(activity.items()))


def _fmt_tok(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError, OverflowError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _contained_path(mission_path: Path, rel: str) -> Path | None:
    """Resolve `rel` (an untrusted `state.json` `phase.artifacts[]` entry,
    G8) against `mission_path` and return it only if it stays inside the
    mission directory. `rel` is attacker/bug-controllable file content, not
    a validated path — without this check a crafted artifact entry like
    `"../../../.ssh/id_ed25519"` would be opened via
    `Gio.AppInfo.launch_default_for_uri` with no containment at all.
    A non-string or empty entry is rejected outright — a malformed
    `state.json` artifact value must skip, not crash the render."""
    if not isinstance(rel, str) or not rel:
        return None
    try:
        mission_root = mission_path.resolve()
        candidate = (mission_path / rel).resolve()
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    if candidate != mission_root and mission_root not in candidate.parents:
        return None
    return candidate


def _artifact_chip(mission_path: Path, rel: str) -> Gtk.Widget | None:
    full_path = _contained_path(mission_path, rel)
    if full_path is None:
        _log.warning("skipping artifact outside mission dir: %s", rel)
        return None
    name = Path(rel).name
    btn = Gtk.Button(label=name)
    btn.add_css_class("helios-mission-artifact-chip")
    btn.add_css_class("flat")
    btn.connect("clicked", lambda _b: _open_artifact(full_path))
    return btn


def _open_artifact(path: Path) -> None:
    try:
        gfile = Gio.File.new_for_path(str(path))
        Gio.AppInfo.launch_default_for_uri(gfile.get_uri(), None)
    except Exception as e:
        _log.warning("could not open artifact %s: %s", path, e)


def _nearest_existing_ancestor(path: Path) -> Path:
    """Walk up from `path` until an existing directory is found. `Path.home()`
    always exists, so this always terminates — used when neither
    `missions_dir()` nor `tandem_root()` (nor any of *their* ancestors up to
    home) exists yet, i.e. tandem has genuinely never been run on this box.
    Never creates anything (no `~/.tandem` mkdir from Helios, ever)."""
    candidate = path
    home = Path.home()
    while candidate != candidate.parent:
        if candidate.is_dir():
            return candidate
        if candidate == home:
            break
        candidate = candidate.parent
    return home


def _clear_box(box: Gtk.Box) -> None:
    while (child := box.get_first_child()) is not None:
        box.remove(child)
