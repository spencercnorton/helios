from __future__ import annotations

import os
import threading
import time
from typing import NamedTuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gio, Gtk  # noqa: E402

from helios.backend import (
    checkpoints,
    claude_env,
    codex_env,
    context_breakdown,
    model_catalog,
    session_archiver,
    session_goals,
    session_providers,
)
from helios.backend.agent_activity import AgentActivityModel, AgentActivityScope
from helios.backend.agent_commands import (
    COMPACT_CONTEXT_COMMAND,
    FORK_CONVERSATION_COMMAND,
    REVIEW_CHANGES_COMMAND,
    REVERT_CONVERSATION_COMMAND,
    commands_for_provider,
    resolve_command,
)
from helios.backend.router_client import refresh_dispatch_available
from helios.backend.process.cli_driver import ClaudeCliDriver, DriverSpawnError
from helios.backend.process.codex_app_driver import (
    NATIVE_DELIVERY_ACCEPTED,
    NATIVE_DELIVERY_IDLE,
    NATIVE_DELIVERY_LOCAL,
    NATIVE_DELIVERY_REJECTED,
    NATIVE_DELIVERY_UNKNOWN,
    NATIVE_DELIVERY_WIRE,
    CodexAppServerDriver,
)
from helios.backend.process.codex_app_hub import get_shared_hub
from helios.backend.process.codex_transcript import clone_transcript_for_fork
from helios.backend.process.message_queue import (
    ExecutionAcceptanceEvidence,
    ExecutionDispatchEvidence,
    ExecutionStopEvidence,
    ExecutionTerminalEvidence,
    MessageDelivery,
)
from helios.backend.process.openrouter_driver import (
    OpenRouterDriver,
)
from helios.backend.process.driver_manager import (
    COMMON_DRIVER_SIGNALS,
    DriverManager,
)
from helios.backend.claude_binary import ClaudeBinaryNotFound
from helios.backend.composer_state import DraftBook, draft_key
from helios.backend.conversation_perms import store as conversation_perms_store
from helios.backend.latest_worker import LatestTaskRunner
from helios.backend.projects import (
    HOME_CWD,
    Project,
    Session,
    archive_stale_throwaways,
    discover_projects,
    ensure_local_project,
    is_throwaway_cwd,
    sweep_empty_project_dirs,
    sweep_orphan_sidecar_sessions,
)
from helios.backend.session_router import (
    ChatTarget,
    clear_deleted_resume,
    fresh_chat_target,
    route_session_selection,
    stage_resume,
    stage_work,
    switch_work_participant,
)
from helios.backend.process.streaming import format_turn_footer, is_background_wakeup
from helios.backend.project_perms import (
    MODE_LABELS,
    PERMISSION_MODES,
    SAFE_FALLBACK_MODE,
    canonical_cwd,
    effective_execution_mode,
    legacy_permission,
    more_restrictive_mode,
    resolve_startup_default,
    sanitize_global_default,
)
from helios.backend.ui_state import (
    configured_default_cwd,
    default_cwd,
    migrate_effort_level,
    store as ui_state_store,
)
from helios.backend.workflow_modes import (
    DEFAULT_WORKFLOW_MODE,
    canonical_workflow_mode,
    provider_allows_workflow,
    workflow_modes_for_provider,
)
from helios.log import get_logger

_log = get_logger("window")


def _flush_pending_checkpoints(drv, session_id: str) -> None:
    """File checkpoints taken before the session had a provider-native id.

    Module-level rather than a method: `_on_session_started` is exercised
    directly against minimal stand-ins in the tests, and every `self.` it
    touches is another attribute those stand-ins must grow.
    """
    pending = getattr(drv, "_helios_pending_checkpoints", None)
    if not pending or not session_id:
        return
    for point in pending:
        checkpoints.record(session_id, point)
    drv._helios_pending_checkpoints = []

# OpenRouter credit lookups are cheap but not free; a turn can finish every
# few seconds. Spend moves slowly enough that a minute is plenty.
_OR_CREDITS_MIN_INTERVAL = 60.0

# How long a pre-turn snapshot may hold up the send. Milliseconds on a normal
# repo; this only bites on a pathological one, where shipping the message
# without an undo point beats freezing the composer.
_CHECKPOINT_DEADLINE_MS = 4000


class _CheckpointDispatch(NamedTuple):
    """One pre-turn snapshot that still owns an unsent composer submit."""

    driver: object
    generation: int
    text: str
    provider: str
    work_id: str
    cwd: str


class _NativeRecoveryAnchor(NamedTuple):
    """Where a wire-pending native prompt belongs if rejection is definite."""

    driver: object
    text: str
    draft_key: str
    insertion_index: int
    queue_id: int | None


from helios.backend.session_state import context_fill_for
from helios.backend.session_goals import GoalState
from helios.backend.transcript import Turn, parse_transcript
from helios.backend.work_coordinator import WorkCoordinator, tag_driver
from helios.backend.work_store import ExecutionAdmissionError
from helios.widgets.activity_indicator import (
    STATE_COMPACTING,
    STATE_REVIEWING,
    STATE_THINKING,
    ActivityIndicator,
    activity_summary,
    derive_state,
    estimate_streamed_tokens,
    native_activity_state,
    STATE_IDLE,
)
from helios.widgets.agent_dock import AgentDock
from helios.widgets.chat_toolbar import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    EFFORT_STOPS,
    ChatToolbar,
    context_window_for_model,
)
from helios.widgets.composer import Composer
from helios.widgets.changes_pane import ChangesPane
from helios.widgets.command_palette import CommandPalette
from helios.widgets.context_pane import ContextPane
from helios.widgets.capabilities_pane import CapabilitiesPane
from helios.widgets.goal_strip import GoalStrip
from helios.widgets.handoff_dialog import present_handoff_dialog
from helios.widgets.work_indicator import WorkIndicator
from helios.window_goal import GoalWorkMixin
from helios.widgets.mission_pane import MissionPane
from helios.widgets.plan_pane import PlanPane
from helios.widgets.plan_progress_strip import PlanProgressStrip
from helios.widgets.session_list import SessionList
from helios.widgets.shared_context_pane import SharedContextPane
from helios.widgets.settings_dialog import SettingsDialog
from helios.widgets.transcript_view import TranscriptView
from helios.widgets.welcome import build_welcome_view
from helios.widgets._motion import BASE_MS


# Safe-by-default: every unconfigured conversation — including the blank chat
# Helios starts in — falls back to "Ask", not unrestricted execution. Bypass
# is an explicit choice for Claude and OpenRouter and narrows to Ask for Codex.
# Migration: users who never saved a global default (no ui_state
# "permission_mode") move from the old implicit bypass to this on upgrade;
# valid non-Bypass choices are preserved, while legacy global Bypass is clamped.
DEFAULT_PERMISSION_MODE = SAFE_FALLBACK_MODE


def _home_execution_locked(cwd: str) -> bool:
    """HOME is a discovery surface, never an executable workspace."""

    return bool(cwd) and canonical_cwd(cwd) == canonical_cwd(HOME_CWD)


def _default_chat_project() -> Project | None:
    """Most recent project a fresh chat can actually execute in.

    $HOME is forced to read-only permissions below the UI, so seeding a fresh
    chat there hands the user a chat that looks ordinary and then silently
    cannot change a file — the two halves were added independently ($HOME as the
    fresh-install catch-all, then the HOME clamp in v0.55.0) and only collide
    at the default. Prefer the newest real project; $HOME stays the
    fresh-install fallback for a profile that genuinely has nothing else.

    The directory must still EXIST. `discover_projects()` reads
    ``~/.claude/projects/``, which outlives the working directory it describes —
    a deleted scratch dir keeps its transcripts and therefore keeps appearing as
    the newest project. Seeding a chat there sets a cwd that is gone, and the
    spawn fails. This was a live regression: v0.58.0 shipped without the check
    and pointed every fresh chat at a deleted `~/Documents/temp`.

    Throwaways (`/tmp`, `_tmp_*` MR clones) are *deprioritised, not excluded*.
    The Sessions list hides them because they clutter it, but that is a display
    concern and does not make them unusable as a cwd. Excluding them outright
    made a profile whose only surviving projects were throwaways fall through to
    $HOME — which is read-only, so the fix reintroduced the very bug it was
    meant to remove, by another route. An existing writable scratch dir beats a
    policy-blocked one, so it is a second-choice candidate rather than no
    candidate.
    """

    def usable(project: Project) -> bool:
        if project.read_only or _home_execution_locked(project.cwd):
            return False
        # os.path.isdir, not exists: a stale file at that path is not a cwd.
        return os.path.isdir(project.cwd)

    projects = [p for p in discover_projects() if usable(p)]
    for project in projects:
        if not is_throwaway_cwd(project.cwd):
            return project
    return projects[0] if projects else None


class ContextFillRequest(NamedTuple):
    session: Session
    sid: str
    model: str


class MainWindow(GoalWorkMixin, Adw.ApplicationWindow):
    """Two+1 columns: Sessions | Transcript+Composer | Context/Shared.

    There is no projects panel: the session list is unified across every
    local project dir and the shared pool, with the cwd demoted to a chip on
    each row and a filter dropdown where the folder tree used to be. New
    chats default to $HOME; "New chat in folder…" picks a different cwd."""

    # ── Background-driver lifecycle policy ──
    # A driver bound to the visible UI is never reaped. Background drivers
    # idle longer than _IDLE_REAP_SECONDS are wound down via stdin-EOF
    # (claude finishes any in-flight turn, then exits cleanly — no interrupt
    # marker in the JSONL). Returning to a reaped session stages a normal
    # --resume, exactly like opening any historical session. Without this,
    # every chat opened during the day kept a ~250-320 MB claude process
    # alive until app close (observed: 6 children / ~1.7 GB).
    _IDLE_REAP_SECONDS = 10 * 60
    _REAP_TICK_SECONDS = 60
    # How often to re-run the orphan sidecar sweep (disk hygiene for CLI-written
    # ghost stubs). The sweep itself only moves stubs older than its own 10-min
    # grace, so a couple-minute tick keeps disk clean without racing new chats.
    _ORPHAN_SWEEP_SECONDS = 120
    #: How often to re-ask the broker whether it can dispatch. Matched to the
    #: broker's own credential-health TTL — asking more often than it refreshes
    #: the answer buys nothing.
    _ROUTER_DISPATCH_SECONDS = 300
    # Soft cap on concurrent claude subprocesses. When a new spawn would
    # exceed it, the least-recently-active idle background driver is wound
    # down first. Busy drivers are never evicted, so the cap can be exceeded
    # transiently when everything is genuinely working.
    _MAX_LIVE_DRIVERS = 4

    @property
    def _driver(self):
        return self._driver_manager.current

    @_driver.setter
    def _driver(self, driver) -> None:
        demoted = self._driver_manager.current
        self._driver_manager.current = driver
        # The row's live-activity caption is the *background* view of a
        # session; the session you are looking at has the strip above the
        # composer. Clearing on promotion is what stops a row that was
        # "Reading foo.py" while backgrounded from keeping that text forever
        # once it becomes the visible one.
        self._set_row_activity(driver, "")
        # And the mirror of that: the session being switched *away* from is
        # mid-activity, with its words in the strip that is about to show
        # someone else's. A long tool call can emit nothing between the
        # switch and its completion, so waiting for the next event would
        # leave the demoted row blank for the whole command.
        if demoted is not None and demoted is not driver:
            self._set_row_activity(demoted, getattr(demoted, "_activity_summary", ""))

    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application)
        self.set_title("Helios")
        # Persisted UI/layout state (panel visibility, window size, splitter
        # positions, sticky model/effort). Loaded once here; individual
        # setters below read from it, and changes are written back as they
        # happen + on close. See backend/ui_state.py.
        self._ui_state = ui_state_store()
        # Tidy up empty project dirs left by abandoned "add project" picks or
        # by Claude Code touching a cwd it never wrote a session into.
        try:
            n = sweep_empty_project_dirs()
            if n:
                _log.info("swept %d empty project dir(s)", n)
        except Exception:
            pass
        # Move sidecar-only .jsonl ghost files (ai-title stubs with no real
        # conversation) to the session archive so they stop appearing as
        # "Session xxxxxxxx" rows.  live_ids is empty at this point because
        # the DriverManager hasn't spawned anything yet; the 10-minute grace
        # window is the safety net for any session that is mid-first-turn.
        try:
            n = sweep_orphan_sidecar_sessions()
            if n:
                _log.info("swept %d orphan sidecar session(s)", n)
        except Exception:
            pass
        # Move dead one-shot agent projects (/tmp, _tmp_*) out of the scan
        # path. Reversible — they land in ~/.claude/projects-archive/.
        try:
            n = archive_stale_throwaways()
            if n:
                _log.info("archived %d stale throwaway project(s)", n)
        except Exception:
            pass
        self.set_default_size(
            int(self._ui_state.get("window_width", 1400)),
            int(self._ui_state.get("window_height", 900)),
        )
        self.add_css_class("helios-window")

        # Honor the system-wide reduced-motion preference. When the user
        # has Animations disabled in GNOME Settings → Accessibility, GTK
        # sets gtk-enable-animations=False; we toggle a `.reduced-motion`
        # class on the window so CSS keyframes (helios-pulse, helios-dot-glow)
        # can `animation: none`. CSS see helios.css for the reduced-motion
        # selectors.
        settings = Gtk.Settings.get_default()
        if settings is not None:
            self._sync_motion(settings)
            settings.connect("notify::gtk-enable-animations",
                             lambda s, _p: self._sync_motion(s))

        # --- Driver state (concurrent live sessions) ---
        # DriverManager owns the live/current/starting registry and lifecycle
        # policy. MainWindow still owns UI callbacks; compatibility properties
        # below keep the larger follow-up extraction incremental.
        self._driver_manager = DriverManager(
            max_live=self._MAX_LIVE_DRIVERS,
            idle_seconds=self._IDLE_REAP_SECONDS,
            on_live_ids_changed=self._set_live_session_ids,
            log=_log,
        )
        # Provider-neutral durable identity.  This embedded coordinator is the
        # v0.27 compatibility boundary; the same API moves behind supervisor
        # IPC in the next rollout stage without changing driver semantics.
        try:
            self._work_coordinator: WorkCoordinator | None = WorkCoordinator()
        except Exception as exc:
            _log.warning("Work graph unavailable; using legacy sessions: %s", exc)
            self._work_coordinator = None
        self._reclaim_orphaned_execution_lanes()
        # AskUserQuestion dedup — tool_use_id -> None, insertion-ordered so
        # it can be pruned FIFO instead of growing for the window's lifetime.
        self._answered_question_ids: dict[str, None] = {}
        # AskUserQuestion serialization. Multiple (incl. background) sessions
        # can ask near-simultaneously; presenting each immediately would stack
        # modal dialogs on one window. Queue them and show one at a time.
        self._question_queue: list[tuple] = []
        self._question_active = False
        self._question_active_key: tuple[object, str] | None = None
        self._question_dialog = None
        # Sessions with an outstanding 'needs your answer' desktop notification,
        # so it can be withdrawn once the question leaves the queue.
        self._notified_question_sids: set[str] = set()
        self._settings_dialog: SettingsDialog | None = None
        self._command_palette: CommandPalette | None = None
        # Direct GPT prompts become visible only after native turn acceptance.
        # id(driver) -> (driver, visible user text)
        self._native_pending_user: dict[int, tuple[object, str]] = {}
        # Claude receives its first prompt before system/init establishes the
        # native id. Keep that first bubble provisional until the synchronous
        # identity boundary accepts it; rejection restores the draft with an
        # explicit may-have-been-delivered warning instead of showing a false
        # sent bubble.
        self._identity_pending_user: dict[int, tuple[object, str]] = {}
        # Provider ownership is possible but unconfirmed (for example a
        # partial Claude stdin write). These prompts are non-sendable until a
        # provider event promotes them; exit without proof quarantines them.
        self._uncertain_pending_user: dict[int, tuple[object, str]] = {}
        # Rejected background prompts remain recoverable when that Work is
        # rebound during this app session; never persist raw drafts to disk.
        self._native_unsent_drafts: dict[str, list[str]] = {}
        # One composer buffer, many conversations: keyed unsent text.
        self._drafts = DraftBook()
        self._draft_key = ""
        # Background Stop may preserve successors while GPT turn/start FIRST
        # is still on the wire. The raw FIRST is withheld until acceptance is
        # authoritative (discard anchor) or rejection is definite (insert at
        # this exact boundary, after older drafts and before successors).
        self._native_recovery_anchors: dict[int, _NativeRecoveryAnchor] = {}
        # A composer submit is not provider-owned until its pre-turn checkpoint
        # releases it.  Keep that interval explicit: driver.is_busy is still
        # false, but Stop and a second submit must treat it as an in-flight turn.
        # The generation is a one-shot CAS token shared by the worker/deadline;
        # a late callback can neither revive a stopped submit nor release a
        # newer checkpoint for the same driver.
        self._checkpoint_dispatch_generation = 0
        self._pending_checkpoint_dispatches: dict[int, _CheckpointDispatch] = {}
        # Driver ``send_user_text`` methods may emit ``error`` synchronously.
        # While admission is on the stack, the outer dispatch still owns FIRST
        # and must recover it together with its queue in chronological order.
        self._provider_dispatch_in_progress: set[int] = set()
        # Fast in-memory half of the durable Work budget breaker. This closes
        # turn admission immediately while SQLite persistence/cancellation is
        # still being dispatched on the same main-loop callback.
        self._budget_blocked_work_ids: set[str] = set()
        # Set once the window is closing, so timers/idle callbacks and the
        # daemon threads that marshal back via idle_add bail out instead of
        # touching a finalizing widget tree (a GTK shutdown-crash vector).
        self._destroyed = False
        # Periodic reaper for idle background drivers (policy constants on
        # the class above).
        self._reap_timer_id = GLib.timeout_add_seconds(
            self._REAP_TICK_SECONDS, self._reap_idle_drivers
        )
        self._archive_timer_id = GLib.timeout_add_seconds(
            24 * 3600, lambda: self._archive_old_sessions_async(repeat=True)
        )
        # Periodic hygiene: the claude CLI keeps writing sidecar-only ghost
        # stubs (ai-title/last-prompt with no conversation) into the projects
        # dir. The startup sweep alone lets them accumulate on disk between
        # restarts, so re-run it on a timer (off-thread) and reload if any moved.
        self._orphan_sweep_timer_id = GLib.timeout_add_seconds(
            self._ORPHAN_SWEEP_SECONDS, self._sweep_orphans_async
        )
        # Whether the Router MCP is worth advertising to a Claude session is
        # read from a cache on the main thread, and the broker's preview bit
        # persists across a Helios restart. Warm it now so a broker that was
        # already dispatching before the app closed is not treated as dead, and
        # keep re-asking: a one-shot warmup leaves the cache false forever if
        # Helios wins the race against the broker's socket at boot, or if the
        # broker restarts while Helios stays open. Neither has any other way
        # back — new sessions only read the cache.
        self._refresh_router_dispatch_async()
        self._router_dispatch_timer_id = GLib.timeout_add_seconds(
            self._ROUTER_DISPATCH_SECONDS, self._refresh_router_dispatch_async
        )
        # Live-follow of the open transcript when it has NO in-process driver
        # (a session driven by an external claude/IDE, or a remote pool one).
        # A GFileMonitor on the .jsonl triggers a debounced incremental append.
        self._follow_monitor = None
        self._follow_debounce_id = 0
        self._ctx_fill_runner = LatestTaskRunner[ContextFillRequest, int](
            work=self._context_fill_work,
            deliver=self._context_fill_done,
            name="helios-ctx-fill",
        )
        # Sticky toolbar/preference state. Model remains a spawn-time choice;
        # permission and reasoning can also be updated on a live conversation.
        # Seed from persisted state so all three survive restarts.
        _persisted_perm = str(
            self._ui_state.get("permission_mode", DEFAULT_PERMISSION_MODE)
            or DEFAULT_PERMISSION_MODE
        )
        # The confirmation bit remains for migration compatibility, but cannot
        # revive retired Bypass. Any retired or invalid persisted value is
        # clamped to Ask and re-persisted. Legacy project-perms.json overrides
        # are no longer consulted here.
        _perm_confirmed = bool(
            self._ui_state.get("permission_mode_confirmed", False)
        )
        self._permission_mode = resolve_startup_default(
            _persisted_perm, confirmed=_perm_confirmed
        )
        if self._permission_mode != _persisted_perm:
            self._ui_state.set("permission_mode", self._permission_mode)
        self._conversation_perms = conversation_perms_store()
        # Provider changes are asynchronous and several conversations can stay
        # alive at once.  Track acknowledgement state per driver so switching
        # chats cannot make a late ACK commit (or clear) another chat's change.
        self._permission_changes: dict[object, object] = {}
        self._effort_changes: dict[object, object] = {}
        self._workflow_changes: dict[object, object] = {}
        self._live_toast: Adw.Toast | None = None
        # A blank composer is already the user's current conversation even
        # though the provider has not assigned its native id yet.  Keep its
        # explicit execution choices here until session-started can bind them.
        self._staged_permission_mode = ""
        self._staged_effort_key = ""
        self._staged_workflow_mode = ""
        # Default to the latest Fable with 1M context. The `fable` alias tracks
        # the newest Fable automatically, so this stays current as models ship.
        self._model = self._ui_state.get("model", DEFAULT_MODEL) or DEFAULT_MODEL
        self._effort_key: str = migrate_effort_level(self._ui_state)
        self._provider_efforts: dict[str, str] = {
            model_catalog.PROVIDER_ANTHROPIC: str(
                self._ui_state.get("effort_anthropic", self._effort_key) or ""
            ),
            model_catalog.PROVIDER_OPENAI: str(
                # OpenAI effort is conversation-owned. A persisted Ultra
                # must never become the default for every fresh GPT Work.
                ""
            ),
            model_catalog.PROVIDER_OPENROUTER: str(
                self._ui_state.get("effort_openrouter", "") or ""
            ),
        }
        # Last model used per provider, backing the header Claude/GPT toggle:
        # flipping sides restores what you last used there. Persisted as
        # model_anthropic / model_openai; the restored sticky model seeds its
        # own side so the toggle reflects reality on first launch.
        self._provider_models: dict[str, str] = {}
        for prov in (
            model_catalog.PROVIDER_ANTHROPIC,
            model_catalog.PROVIDER_OPENAI,
            model_catalog.PROVIDER_OPENROUTER,
        ):
            remembered = self._ui_state.get(f"model_{prov}", None)
            if remembered is not None:
                self._provider_models[prov] = remembered
        self._provider_models[model_catalog.provider_for(self._model)] = self._model
        # OpenAI entries currently proven by a live App Server catalog. Curated
        # compatibility rows are diagnostic only and never enter this set.
        self._openai_catalog_authoritative = False
        self._openai_entries: list = []
        self._openai_workflow_modes: tuple[str, ...] = (DEFAULT_WORKFLOW_MODE,)
        self._catalog_entries_by_id: dict[str, model_catalog.ModelEntry] = {}
        # True while we programmatically sync the toggle — its `toggled`
        # handlers must not re-enter the model-change path.
        self._provider_guard = False
        # Where the composer's next send will go. An explicitly configured
        # default workspace wins; otherwise seed the most recent executable
        # project so a fresh chat can actually run something. Selecting any
        # session re-points it. $HOME remains the fresh-install fallback, but
        # it is read-only, so it must not be the common case — which is why the
        # unset setting must NOT be allowed to resolve to $HOME here.
        self._next_chat: ChatTarget | None = None
        _default_cwd = default_cwd()
        try:
            configured = configured_default_cwd()
            seed = (
                ensure_local_project(configured)
                if configured
                else (_default_chat_project() or ensure_local_project(HOME_CWD))
            )
            self._next_chat = ChatTarget(project=seed)
        except OSError as e:
            _log.warning("couldn't ensure default project for %s: %s", _default_cwd, e)
        # Fresh chats have no session id until the backend reports
        # session-started. A goal created before first send lives here, then
        # gets bound to the real id in _on_session_started.
        self._pending_goal: GoalState | None = None
        # --- Widgets ---
        self._sessions = SessionList()
        self._transcript = TranscriptView()
        self._goal_strip = GoalStrip()
        self._plan_progress = PlanProgressStrip()
        self._activity = ActivityIndicator()
        self._agent_activity = AgentActivityModel()
        self._agent_dock = AgentDock()
        self._chat_toolbar = ChatToolbar()
        self._composer = Composer()
        self._plan = PlanPane()
        self._missions = MissionPane()
        self._context = ContextPane()
        self._capabilities = CapabilitiesPane()
        self._capabilities.connect("refresh-requested", self._refresh_capabilities)
        self._changes = ChangesPane()
        self._shared = SharedContextPane()
        self._shared.connect("handoff-requested", self._on_handoff_requested)
        self._welcome = build_welcome_view()

        # Reflect persisted sticky model/effort in the toolbar UI so it matches
        # what the next spawn will actually use.
        self._chat_toolbar.set_model(self._model)
        self._chat_toolbar.set_effort(self._effort_key)
        self._chat_toolbar.set_permission_mode(
            self._effective_permission_mode(self._current_project_cwd())
        )
        self._chat_toolbar.set_workflow_options((DEFAULT_WORKFLOW_MODE,))
        self._chat_toolbar.set_workflow_mode(DEFAULT_WORKFLOW_MODE)
        # The catalog later replaces GPT's temporary disabled state with the
        # exact reasoning efforts advertised for the selected model.
        self._sync_effort_sensitivity()
        self._sync_assistant_labels()

        # Agent activity is an in-conversation surface, never a SessionList row.
        # It stays between the ordinary activity strip and composer controls.
        self._transcript_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._transcript.set_vexpand(True)
        self._transcript_column.append(self._transcript)
        self._transcript_column.append(self._goal_strip)
        self._transcript_column.append(self._plan_progress)
        self._transcript_column.append(self._activity)
        self._transcript_column.append(self._agent_dock)
        self._transcript_column.append(self._chat_toolbar)
        self._transcript_column.append(self._composer)
        # Toolbar + composer are hidden until a session is selected.
        self._chat_toolbar.set_visible(False)
        self._composer.set_visible(False)

        # Main content stack: welcome OR transcript column.
        self._main_stack = Gtk.Stack()
        self._main_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._main_stack.set_transition_duration(BASE_MS)
        self._main_stack.add_named(self._welcome, "welcome")
        self._main_stack.add_named(self._transcript_column, "transcript")
        self._main_stack.set_visible_child_name("welcome")

        # --- Resizable columns via Gtk.Paned ---
        # The old [Projects | Sessions] inner paned is gone — the unified
        # session list is the whole left column. Fresh ui-state key
        # ("sessions_paned") so the old two-panel position isn't restored
        # onto the single panel.

        self._sessions.set_size_request(220, -1)

        # Middle paned: [Sessions column | Content]. Both children are
        # Adw.ToolbarViews built below, each with its OWN header bar — that is
        # what lets the solid session column run to the very top of the window
        # instead of starting under a full-width bar. Same arrangement
        # Adw.NavigationSplitView gives GNOME Settings, done by hand because the
        # resizable Gtk.Paned position is persisted and NavigationSplitView does
        # not offer one.
        self._middle_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._middle_paned.set_position(int(self._ui_state.get("sessions_paned", 320)))
        self._middle_paned.set_resize_start_child(False)
        self._middle_paned.set_resize_end_child(True)
        self._middle_paned.set_shrink_start_child(False)
        self._middle_paned.set_shrink_end_child(False)
        self._middle_paned.set_wide_handle(True)

        # Right sidebar: Plan (live work phases), Context (CLAUDE.md + memory),
        # and Shared (the scratchpad) as switchable tabs in one hideable pane.
        self._right_stack = Adw.ViewStack()
        self._right_stack.set_vexpand(True)
        self._right_stack.add_titled_with_icon(
            self._plan, "plan", "Plan", "checkbox-checked-symbolic"
        )
        self._right_stack.add_titled_with_icon(
            self._changes, "changes", "Changes", "document-edit-symbolic"
        )
        self._right_stack.add_titled_with_icon(
            self._missions, "missions", "Missions", "emblem-system-symbolic"
        )
        self._right_stack.add_titled_with_icon(
            self._context, "context", "Claude memory", "text-x-generic-symbolic"
        )
        self._right_stack.add_titled_with_icon(
            self._capabilities, "capabilities", "Capabilities", "dialog-information-symbolic"
        )
        self._right_stack.add_titled_with_icon(
            self._shared, "shared", "Shared", "network-workgroup-symbolic"
        )
        # Fallback full re-scan when the Missions tab becomes visible — covers
        # any file-monitor misses. No periodic poll while it's hidden.
        self._right_stack.connect("notify::visible-child", self._on_right_stack_child_changed)
        # Labeled tabs cannot fit the pane's 280px minimum. A native
        # selector keeps every page named and keyboard-accessible at any width.
        self._right_pages = ("plan", "changes", "missions", "context", "capabilities", "shared")
        right_switcher = Gtk.DropDown.new_from_strings(
            ["Plan", "Changes", "Missions", "Claude memory", "Capabilities", "Shared"]
        )
        self._right_switcher = right_switcher
        right_switcher.set_tooltip_text("Choose a workspace pane")
        right_switcher.update_property([Gtk.AccessibleProperty.LABEL], ["Workspace pane"])
        right_switcher.connect("notify::selected", self._on_right_pane_selected)
        right_switcher.set_margin_top(8)
        right_switcher.set_margin_start(8)
        right_switcher.set_margin_end(8)
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # The fill goes on this box, not on the individual pages. The pages are
        # what it used to be on, and three of the four happened to carry a
        # @sidebar_bg_color rule while the shared scratchpad did not — so under
        # glass that one page and the view switcher above ALL of them rendered
        # at alpha 0 straight onto the desktop. Same failure the deleted
        # `.helios-glass-opaque` was written for: a layout Box paints nothing,
        # so per-page rules leave whatever sits between them transparent. This
        # box is the first node spanning the switcher and every page, present
        # and future.
        right_box.add_css_class("helios-context-column")
        right_box.append(right_switcher)
        right_box.append(self._right_stack)

        # Outer: [Middle paned] | [right sidebar (hideable)]
        self._outer = Adw.OverlaySplitView()
        self._outer.set_sidebar_position(Gtk.PackType.END)
        self._outer.set_sidebar(right_box)
        self._outer.set_content(self._main_stack)
        self._outer.set_min_sidebar_width(280)
        self._outer.set_max_sidebar_width(420)
        self._outer.set_sidebar_width_fraction(0.22)
        self._outer.set_collapsed(False)
        # Context pane hidden by default — uncluttered start. Header toggle
        # re-opens it. Persisted across restarts.
        self._show_context = bool(self._ui_state.get("panel_context", False))
        self._outer.set_show_sidebar(self._show_context)

        # --- Headers ---
        # TWO header bars, one per column, which is the whole reason the session
        # column reaches the window top. `show_*_title_buttons` is split so the
        # window controls land on the window's outer edge whichever side the
        # desktop puts them: the sidebar header gives up the end, the content
        # header gives up the start.
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_show_end_title_buttons(False)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        # show-title stays on: the title slot hosts the provider toggle.

        title_widget = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_widget.set_margin_start(4)
        icon = Gtk.Image.new_from_icon_name("network-workgroup-symbolic")
        icon.set_pixel_size(18)
        title_widget.append(icon)
        title_label = Gtk.Label(label="Helios")
        title_label.add_css_class("title-4")
        title_widget.append(title_label)
        # set_title_widget, NOT pack_start: an Adw.HeaderBar renders the window
        # title in its centre slot unless something else occupies it, so packing
        # the identity at the start left "Helios" drawn twice. Centring it is
        # also what the reference does with "Settings".
        sidebar_header.set_title_widget(title_widget)

        # Panel toggles. Sessions is the left column; Context/Shared is the
        # right pane (its toggle is packed at the end).
        show_sessions = bool(self._ui_state.get("panel_sessions", True))
        self._sessions.set_visible(show_sessions)
        self._sessions_btn = Gtk.ToggleButton()
        self._sessions_btn.set_icon_name("view-list-symbolic")
        self._sessions_btn.set_tooltip_text("Toggle sessions panel (Ctrl+1)")
        self._sessions_btn.set_active(show_sessions)
        # Hides the whole column (header included); hiding just the list would
        # leave its header bar behind as an empty strip.
        self._sessions_btn.connect(
            "toggled", self._on_panel_toggle_column, "panel_sessions"
        )
        header.pack_start(self._sessions_btn)
        self._compact_new_chat = Gtk.Button.new_from_icon_name("list-add-symbolic")
        self._compact_new_chat.set_tooltip_text("New chat (Ctrl+N)")
        self._compact_new_chat.connect("clicked", lambda *_: self._start_new_chat())
        self._compact_new_chat.set_visible(not show_sessions)
        header.pack_start(self._compact_new_chat)

        # NEW CHAT split control. The primary side keeps the fast path
        # (selected agent, Ctrl+N); the menu makes Claude/GPT explicit so the
        # provider toggle is no longer a hidden mode switch.
        sidebar_header.pack_end(self._build_new_chat_control())

        # No goal button here. Setting a goal moved to the main menu ("Set a
        # goal…", win.goal): once a goal exists the GoalStrip reveals itself
        # and carries its own edit/pause/complete controls, so a permanent
        # header button was chrome for a one-off action. The ACTION had to stay
        # reachable, though — the strip only appears once a goal exists, and a
        # tandem Work is illegal without an objective + definition of done.
        # `_update_goal_button` and the `.helios-goal-btn-*` rules went with
        # the button; they had no other caller.

        # Work indicator: participants + lead for a multi-provider tandem
        # Work (hidden for solo work). Read-only surface of the Work ledger.
        self._work_indicator = WorkIndicator()
        header.pack_start(self._work_indicator)

        # Centered Claude/GPT provider toggle — quick backend switch without
        # opening the model popover. Each side remembers the last model used
        # with that provider; crossing providers stages a fresh chat in the
        # current cwd so the next prompt really goes to that backend.
        header.set_title_widget(self._build_provider_toggle())

        # Right side.
        search_btn = Gtk.Button.new_from_icon_name("system-search-symbolic")
        search_btn.set_tooltip_text("Search all sessions (Ctrl+F)")
        search_btn.connect("clicked", lambda *_: self._open_search())
        sidebar_header.pack_end(search_btn)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Reload sessions (Ctrl+R)")
        refresh_btn.connect("clicked", lambda *_: self._reload())
        header.pack_end(refresh_btn)

        self._context_btn = Gtk.ToggleButton()
        self._context_btn.set_icon_name("sidebar-show-right-symbolic")
        self._context_btn.set_tooltip_text(
            "Toggle right pane — plan, changes, missions, context and shared scratchpad (Ctrl+3)"
        )
        self._context_btn.set_active(self._show_context)  # matches restored _outer state
        self._context_btn.connect("toggled", self._on_context_toggle)
        self._outer.connect("notify::show-sidebar", self._on_right_visibility_changed)
        header.pack_end(self._context_btn)

        # Visible settings button — account/sign-in, permission mode, tools.
        settings_btn = Gtk.Button.new_from_icon_name("emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings — account, permissions, tools")
        settings_btn.connect("clicked", lambda *_: self._show_preferences())
        header.pack_end(settings_btn)

        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Set a goal…", "win.goal")
        menu.append("Emergency stop all sessions", "win.stop-all")
        menu.append("Clear budget block", "win.clear-budget-block")
        menu.append("Rewind files…", "win.rewind")
        menu.append("Settings", "win.preferences")
        menu.append("About Helios", "app.about")
        menu.append("Quit", "app.quit")
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)

        # The session column: its own header above the list, so the column is one
        # unbroken surface from the window top down. `.helios-session-column` is
        # the node that carries the fill for the whole column — see helios.css.
        self._sessions_column = Adw.ToolbarView()
        self._sessions_column.add_css_class("helios-session-column")
        self._sessions_column.add_top_bar(sidebar_header)
        self._sessions_column.set_content(self._sessions)
        self._sessions_column.set_visible(self._sessions.get_visible())

        # The content column: main header spanning the chat AND the right pane,
        # so the window buttons stay on the window's right edge rather than
        # landing mid-window whenever the context pane opens.
        toolbar.add_top_bar(header)
        toolbar.set_content(self._outer)

        self._middle_paned.set_start_child(self._sessions_column)
        self._middle_paned.set_end_child(toolbar)

        # Toast overlay wraps everything so toasts can appear over any pane.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._middle_paned)
        self.set_content(self._toast_overlay)
        # Let the conversation use the available width on smaller displays.
        # The session toggle remains available, and resizing restores the
        # user's wide-layout preference rather than persisting auto-collapse.
        self._compact_layout = False
        self._layout_auto_change = False
        self._wide_sessions_visible = show_sessions
        self._compact_breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 1000px")
        )
        self._compact_breakpoint.connect("apply", lambda *_: self._set_compact_layout(True))
        self._compact_breakpoint.connect("unapply", lambda *_: self._set_compact_layout(False))
        self.add_breakpoint(self._compact_breakpoint)

        # --- Window-level actions ---
        prefs_act = Gio.SimpleAction.new("preferences", None)
        prefs_act.connect("activate", lambda *_: self._show_preferences())
        self.add_action(prefs_act)

        rewind_act = Gio.SimpleAction.new("rewind", None)
        rewind_act.connect("activate", lambda *_a: self._show_rewind())
        self.add_action(rewind_act)
        self._app_quit_act = Gio.SimpleAction.new("quit", None)
        self.add_action(self._app_quit_act)
        self.connect("close-request", self._on_close_request)

        # App-level action so a "session finished" notification can deep-link
        # back to the right session when clicked. Must be on the application
        # (notification actions are routed via "app.").
        focus_act = Gio.SimpleAction.new("focus-session", GLib.VariantType.new("s"))
        focus_act.connect("activate", self._on_focus_session_action)
        application.add_action(focus_act)

        # Same pattern for the mission-gate notification's default action —
        # bring the window forward and switch the right sidebar to Missions.
        focus_missions_act = Gio.SimpleAction.new("focus-missions", None)
        focus_missions_act.connect("activate", self._on_focus_missions_action)
        application.add_action(focus_missions_act)

        # --- Keyboard shortcuts (win.* actions + app accelerators) ---
        self._install_shortcuts(application)

        # --- Signals ---
        self._sessions.connect("session-selected", self._on_session_selected)
        self._sessions.connect("sessions-deleted", self._on_sessions_deleted)
        self._sessions.connect("session-stop-requested", self._on_session_stop_requested)
        self._goal_strip.connect("edit-requested", lambda *_: self._edit_goal())
        self._goal_strip.connect("pause-requested", lambda *_: self._set_goal_status(session_goals.GOAL_PAUSED))
        self._goal_strip.connect("resume-requested", lambda *_: self._set_goal_status(session_goals.GOAL_ACTIVE))
        self._goal_strip.connect("complete-requested", lambda *_: self._set_goal_status(session_goals.GOAL_COMPLETE))
        self._goal_strip.connect("clear-requested", lambda *_: self._clear_goal())
        self._plan_progress.connect(
            "open-requested", lambda *_: self._open_execution_plan()
        )
        self._composer.connect("send", self._on_composer_send)
        self._composer.connect("stop", self._on_composer_stop)
        self._composer.connect(
            "commands-requested",
            lambda *_: self._open_command_palette(),
        )
        # "Compact now" in the context popover. Wired once; the
        # handler itself guards on provider and busy state, so there is no
        # per-driver bookkeeping to drift.
        self._chat_toolbar.set_compact_handler(self._compact_current_chat)
        self._chat_toolbar.connect("model-changed", self._on_toolbar_model_changed)
        self._chat_toolbar.connect("effort-changed", self._on_toolbar_effort_changed)
        self._chat_toolbar.connect(
            "permission-changed", self._on_toolbar_permission_changed
        )
        self._chat_toolbar.connect(
            "workflow-changed", self._on_toolbar_workflow_changed
        )

        # Esc aborts the in-flight turn (the metrics strip advertises it).
        # Plain EventControllerKey on the window, default (bubble) phase:
        # popovers/dialogs that want Esc for dismiss consume it first.
        esc = Gtk.EventControllerKey()
        esc.connect("key-pressed", self._on_window_key)
        self.add_controller(esc)

        # --- Dynamic model catalog ---
        # Discovery is I/O (binary scan, Codex App Server) → background thread,
        # results re-applied on the main loop. Re-checked every 6h because
        # the app runs for days: a claude self-update or a key change should
        # surface new models without a restart.
        self._catalog_refresh_running = False
        self._catalog_force_pending = False
        # Context-breakdown throttle: one transcript read at a time, and only
        # when the file has changed since the last one.
        self._breakdown_busy = False
        self._breakdown_stamp = 0
        # Last OpenRouter credit lookup (monotonic), for the per-turn throttle.
        self._or_credits_checked = 0.0
        # One restore at a time: two concurrent rewinds would interleave
        # writes to the same worktree.
        self._restore_in_flight = False
        self._refresh_model_catalog()
        self._refresh_openrouter_credits()
        self._catalog_tick_id = GLib.timeout_add_seconds(
            6 * 3600, self._on_catalog_tick
        )

        # Initial load — auto-selects the newest session, which fires
        # session-selected and builds the transcript view. A completely empty
        # disk falls through to the fresh-chat UI so the composer is usable
        # immediately.
        self._sessions.reload()
        if not self._sessions.has_rows() and self._next_chat is not None:
            self._show_fresh_chat_ui(self._next_chat.project)
        self._startup_archive_timer_id = GLib.timeout_add_seconds(
            15, self._run_startup_archive
        )

    # --- helpers ---

    def _reload(self) -> None:
        self._sessions.reload(preserve_selection=True)

    def _run_startup_archive(self) -> bool:
        self._startup_archive_timer_id = 0
        return self._archive_old_sessions_async(repeat=False)

    def _archive_old_sessions_async(self, *, repeat: bool) -> bool:
        if self._destroyed:
            return False
        live_ids = self._driver_manager.live_ids()

        def worker() -> None:
            try:
                report = session_archiver.archive_old_sessions(live_ids=live_ids)
            except Exception as exc:  # noqa: BLE001 — background hygiene
                _log.warning("session archival failed: %s", exc)
                return
            # Logging is non-UI and remains valuable even if the window closed
            # while the durable archive operation was running.
            if report.errors:
                _log.warning("session archival errors: %s", report.errors[:3])
            GLib.idle_add(_finish, report)

        def _finish(report) -> bool:
            if self._destroyed:
                return False
            if report.archived:
                self._sessions.reload(preserve_selection=True, rescan_pool=False)
                self._toast(f"Archived {len(report.archived)} old session(s).")
            return False

        threading.Thread(
            target=worker, name="helios-session-archive", daemon=True
        ).start()
        return repeat

    def _sweep_orphans_async(self) -> bool:
        """Periodic disk-hygiene sweep of CLI-written sidecar-only ghost stubs.
        Runs off-thread; reloads the sidebar only if something moved."""
        if self._destroyed:
            return False
        live_ids = self._driver_manager.live_ids()

        def worker() -> None:
            try:
                n = sweep_orphan_sidecar_sessions(live_ids=live_ids)
            except Exception as e:  # noqa: BLE001 — hygiene must never crash
                _log.warning("orphan sweep failed: %s", e)
                n = 0
            if n:
                GLib.idle_add(_finish, n)

        def _finish(n: int) -> bool:
            if self._destroyed:
                return False
            _log.info("swept %d orphan sidecar session(s) (periodic)", n)
            self._sessions.reload(preserve_selection=True, rescan_pool=False)
            return False

        threading.Thread(
            target=worker, name="helios-orphan-sweep", daemon=True
        ).start()
        return True  # keep the timer running

    # --- dynamic model catalog ---

    def _refresh_model_catalog(self, *, force: bool = False) -> None:
        """Discover models off the main loop and swap them into the picker."""
        if force:
            # A credential mutation invalidates the prior process/account proof
            # immediately, not only after its replacement worker completes.
            self._openai_catalog_authoritative = False
            self._openai_entries = []
            self._openai_workflow_modes = (DEFAULT_WORKFLOW_MODE,)
            self._gpt_toggle.set_sensitive(False)
            self._gpt_toggle.set_tooltip_text(
                "Checking OpenAI model availability…"
            )
            self._sync_new_chat_actions()
        if self._catalog_refresh_running:
            if force:
                # A credential change must supersede a startup/periodic result
                # that may belong to the previous account. Coalesce repeated
                # requests, discard that worker's result in _apply, then rerun.
                self._catalog_force_pending = True
            return
        self._catalog_refresh_running = True

        def work() -> None:
            # Warm required Claude capability probes on this background thread
            # so the first send never blocks the GTK main loop on CLI probes
            # (cli_driver reads the cached results during argv construction).
            try:
                from helios.backend.claude_binary import capability_report

                report = capability_report()
                _log.info(
                    "claude CLI %s; degraded: %s",
                    report.get("version") or "unknown version",
                    ", ".join(report.get("degraded") or []) or "nothing",
                )
                GLib.idle_add(self._report_cli_degradation, "Claude", report)
            except Exception:
                pass
            try:
                entries = model_catalog.anthropic_entries(force=force)
            except Exception as e:  # noqa: BLE001 — retain a safe picker baseline
                _log.warning("Anthropic model catalog refresh failed: %s", e)
                entries = list(model_catalog.FALLBACK_ANTHROPIC)
            openai_authoritative = False
            openai_workflow_modes = (DEFAULT_WORKFLOW_MODE,)
            try:
                openai, status = model_catalog.openai_entries(force=force)
                openai_authoritative = status == "app-server"
                if openai_authoritative:
                    openai_workflow_modes = model_catalog.openai_workflow_modes()
                if openai and openai_authoritative:
                    entries = entries + openai
                _log.info(
                    "model catalog: %d entries (openai: %s)", len(entries), status
                )
            except Exception as e:  # noqa: BLE001 — discovery must never crash the app
                # Keep the Anthropic baseline non-empty so applying this result
                # also removes stale GPT rows from a previous credential.
                _log.warning("OpenAI model catalog refresh failed: %s", e)
            # OpenRouter catalog (cached; no network on the refresh thread
            # unless the user just added a key, which triggers a force).
            try:
                or_entries, or_status = model_catalog.openrouter_entries(force=force)
                if or_entries:
                    entries = entries + or_entries
                _log.info(
                    "model catalog: %d entries (openrouter: %s)",
                    len(entries),
                    or_status,
                )
            except Exception as e:  # noqa: BLE001
                _log.warning("OpenRouter model catalog refresh failed: %s", e)
            GLib.idle_add(
                self._apply_model_catalog,
                entries,
                openai_authoritative,
                openai_workflow_modes,
            )

        threading.Thread(target=work, daemon=True, name="model-catalog").start()

    def _apply_model_catalog(
        self,
        entries: list,
        openai_authoritative: bool,
        openai_workflow_modes: tuple[str, ...] = (DEFAULT_WORKFLOW_MODE,),
    ) -> bool:
        self._catalog_refresh_running = False
        force_pending = self._catalog_force_pending
        self._catalog_force_pending = False
        if self._destroyed:
            return False
        if force_pending:
            # Do not briefly publish a result discovered before the credential
            # change. The replacement worker starts only after this one has
            # fully yielded its slot, so at most one refresh remains active.
            self._refresh_model_catalog(force=True)
            return False
        # The worker normally supplies this baseline itself. Retain the same
        # fail-closed behavior for a malformed/empty callback payload so stale
        # GPT choices can never survive an unsuccessful refresh.
        if not entries:
            entries = list(model_catalog.FALLBACK_ANTHROPIC)
        entries = [
            entry
            for entry in entries
            if not (
                getattr(entry, "provider", model_catalog.PROVIDER_ANTHROPIC)
                == model_catalog.PROVIDER_ANTHROPIC
                and (
                    not str(getattr(entry, "id", "") or "")
                    or str(getattr(entry, "id", "") or "").endswith("[1m]")
                )
            )
        ]
        if not entries:
            entries = list(model_catalog.FALLBACK_ANTHROPIC)
        self._openai_catalog_authoritative = bool(openai_authoritative)
        self._openai_workflow_modes = (
            tuple(openai_workflow_modes)
            if self._openai_catalog_authoritative
            else (DEFAULT_WORKFLOW_MODE,)
        )
        self._openai_entries = (
            [
                entry
                for entry in entries
                if getattr(entry, "provider", "")
                == model_catalog.PROVIDER_OPENAI
            ]
            if self._openai_catalog_authoritative
            else []
        )
        selectable = [
            entry
            for entry in entries
            if (
                getattr(entry, "provider", model_catalog.PROVIDER_ANTHROPIC)
                != model_catalog.PROVIDER_OPENAI
                or entry in self._openai_entries
            )
        ]
        self._chat_toolbar.set_choices(selectable)
        self._catalog_entries_by_id = {entry.id: entry for entry in selectable}
        self._normalize_openai_model_memory()
        self._sync_effort_sensitivity()
        self._sync_execution_control()
        available = (
            self._openai_catalog_authoritative
            and bool(self._openai_entries)
        )
        self._gpt_toggle.set_sensitive(available)
        self._gpt_toggle.set_tooltip_text(
            "Chat with GPT (OpenAI)" if available
            else "Add an OpenAI key in Settings → Providers to enable GPT"
        )
        if hasattr(self, "_sync_openrouter_toggle_sensitivity"):
            self._sync_openrouter_toggle_sensitivity()
        self._sync_new_chat_actions()
        return False  # one-shot idle

    def _refresh_openrouter_credits(self, *, throttle: bool = False) -> None:
        """Populate the usage panel's OpenRouter row from the saved key.

        OpenRouter has no rolling window to report, so without this the panel
        stayed empty until a 429 happened to fire. Only fetched while an
        OpenRouter model is selected — the popover discards rows from a
        provider other than the one on screen anyway.

        ``throttle=True`` is for the per-turn refresh: spend only changes when
        a turn completes, but turns can be seconds apart, and the usage meter
        now updates several times per turn. One lookup a minute keeps the
        balance honest without a request per turn.
        """
        if model_catalog.provider_for(self._model) != model_catalog.PROVIDER_OPENROUTER:
            return
        now = time.monotonic()
        if throttle and (now - self._or_credits_checked) < _OR_CREDITS_MIN_INTERVAL:
            return
        self._or_credits_checked = now

        def work() -> None:
            from helios.backend.openrouter import account

            try:
                row = account.fetch_credit_row()
            except Exception as exc:  # noqa: BLE001 — a status panel is not critical
                _log.warning("OpenRouter credit lookup failed: %s", exc)
                return
            if row:
                GLib.idle_add(self._apply_openrouter_credits, row)

        threading.Thread(
            target=work, daemon=True, name="openrouter-credits"
        ).start()

    def _apply_openrouter_credits(self, row: dict) -> bool:
        if self._destroyed:
            return False
        if model_catalog.provider_for(self._model) == model_catalog.PROVIDER_OPENROUTER:
            self._chat_toolbar.update_rate_limit(row)
        return False

    def _normalize_openai_model_memory(self) -> None:
        """Migrate stale remembered GPT models to the agent-ranked default.

        Older Helios builds allowed generic `*-chat-latest` ids into the GPT
        side. If one is still persisted, the header toggle should fall forward
        to the first Codex/agent model instead of recreating a chatbot session.
        """
        preferred = model_catalog.preferred_openai_model(self._openai_entries)
        if not preferred:
            # A mode-only login cannot prove the active credential identity.
            # Never retain a model from the previous account when the current
            # authoritative catalog has no GPT rows.
            self._provider_models[model_catalog.PROVIDER_OPENAI] = ""
            self._ui_state.set(f"model_{model_catalog.PROVIDER_OPENAI}", "")

            if model_catalog.provider_for(self._model) == model_catalog.PROVIDER_OPENAI:
                anthropic_entries = [
                    entry
                    for entry in self._catalog_entries_by_id.values()
                    if getattr(entry, "provider", model_catalog.PROVIDER_ANTHROPIC)
                    == model_catalog.PROVIDER_ANTHROPIC
                ]
                valid_anthropic = {entry.id for entry in anthropic_entries}
                remembered = self._provider_models.get(
                    model_catalog.PROVIDER_ANTHROPIC
                )
                candidates = (remembered, DEFAULT_MODEL, "")
                fallback = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate is not None and candidate in valid_anthropic
                    ),
                    anthropic_entries[0].id if anthropic_entries else "",
                )
                # Quietly switch the next-send backend without tearing down a
                # live/background historical GPT driver. Viewing stays intact;
                # a subsequent send stages the safe Claude Work participant.
                self._apply_model_choice(fallback, quiet=True)
            return
        valid = {e.id for e in self._openai_entries}
        remembered = self._provider_models.get(model_catalog.PROVIDER_OPENAI, "")
        if remembered not in valid:
            self._provider_models[model_catalog.PROVIDER_OPENAI] = preferred
            self._ui_state.set(f"model_{model_catalog.PROVIDER_OPENAI}", preferred)
        if (
            model_catalog.provider_for(self._model) == model_catalog.PROVIDER_OPENAI
            and self._model not in valid
        ):
            self._model = preferred
            self._ui_state.set("model", preferred)
            self._provider_models[model_catalog.PROVIDER_OPENAI] = preferred
            self._ui_state.set(f"model_{model_catalog.PROVIDER_OPENAI}", preferred)
            self._chat_toolbar.set_model(preferred)
            self._chat_toolbar.set_context_model(preferred)
            self._sync_provider_toggle()

    def _on_catalog_tick(self) -> bool:
        if self._destroyed:
            return False  # drop the timer
        # Cheap stat per 6h; the full rescan only runs when the claude
        # binary actually changed (self-update); Codex identity is checked live.
        if model_catalog.claude_binary_changed():
            self._refresh_model_catalog()
        else:
            self._refresh_model_catalog(force=False)
        return True  # keep ticking

    # --- keyboard ---

    def _on_window_key(self, _ctl, keyval: int, _keycode: int, _state) -> bool:
        from gi.repository import Gdk

        if (
            keyval == Gdk.KEY_Escape
            and self._driver is not None
            and (
                self._driver.is_busy
                or MainWindow._checkpoint_dispatch_pending_for(self, self._driver)
            )
        ):
            self._on_composer_stop()
            return True
        return False

    # --- keyboard shortcuts ---

    def _install_shortcuts(self, application: Adw.Application) -> None:
        """Wire win.* actions and bind accelerators for keyboard-driven use."""
        specs = [
            ("new-chat", lambda *_: self._start_new_chat(), ["<Primary>n"]),
            ("new-chat-folder", lambda *_: self._start_new_chat_in_folder(), ["<Primary><Shift>n"]),
            ("search", lambda *_: self._open_search(), ["<Primary>f"]),
            (
                "agent-commands",
                lambda *_: self._open_command_palette(),
                ["<Primary><Shift>p"],
            ),
            ("stop-turn", lambda *_: self._on_composer_stop(), ["<Primary>period"]),
            (
                "stop-all",
                lambda *_: self._on_emergency_stop_all(),
                ["<Primary><Shift>period"],
            ),
            # No accelerator on purpose: reopening a Work the budget breaker
            # closed should be a deliberate menu choice, never a stray chord.
            ("clear-budget-block", lambda *_: self._on_clear_budget_block(), []),
            # Menu-only too. The header button this replaced was removed, and
            # the GoalStrip's own edit control only exists once a goal does, so
            # this is the sole way to create the first one.
            ("goal", lambda *_: self._edit_goal(), []),
            # Ctrl+1 and Ctrl+2 both toggle the (single) left panel — Ctrl+2
            # kept for muscle memory from the old two-panel layout.
            ("toggle-sessions",
             lambda *_: self._sessions_btn.set_active(not self._sessions_btn.get_active()),
             ["<Primary>1", "<Primary>2"]),
            ("toggle-context",
             lambda *_: self._context_btn.set_active(not self._context_btn.get_active()),
             ["<Primary>3"]),
            ("focus-composer", lambda *_: self._composer.grab_input_focus(), ["<Primary>l"]),
            ("reload", lambda *_: self._reload(), ["<Primary>r"]),
        ]
        for name, cb, accels in specs:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            application.set_accels_for_action(f"win.{name}", accels)

    # --- provider-native command palette ---

    def _agent_command_capabilities(self):
        """Return an action snapshot for the selected provider and binding."""

        provider = self._selected_provider()
        driver = getattr(self, "_driver", None)
        session_ready = bool(
            driver is not None
            and getattr(driver, "is_accepting_input", False)
            and str(getattr(driver, "session_id", "") or "")
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
            and getattr(driver, "_helios_identity_confirmed", True) is True
        )
        manual_compaction = False
        native_review = False
        native_fork = False
        if session_ready and isinstance(driver, CodexAppServerDriver):
            manual_compaction = bool(driver.supports_manual_compaction)
            native_review = bool(driver.supports_native_review)
            native_fork = bool(driver.supports_native_fork)
        elif session_ready and isinstance(driver, ClaudeCliDriver):
            manual_compaction = "compact" in driver.init_slash_commands

        block_reason = ""
        guard = getattr(driver, "_execution_block_reason", None)
        if session_ready and callable(guard):
            block_reason = str(guard() or "")
        session_busy = bool(
            session_ready
            and (
                getattr(driver, "is_busy", False)
                or bool(getattr(driver, "execution_attempt_id", ""))
                or MainWindow._checkpoint_dispatch_pending_for(self, driver)
                or MainWindow._execution_change_pending_for(self, driver)
                or getattr(self, "_restore_in_flight", False)
            )
        )
        return commands_for_provider(
            provider,
            session_ready=session_ready,
            session_busy=session_busy,
            manual_compaction=manual_compaction,
            native_review=native_review,
            native_fork=native_fork,
            safe_revert=False,
            blocked_reason=block_reason,
        )

    def _sync_agent_command_capabilities(self) -> None:
        MainWindow._sync_composer_commands(self)
        toolbar = getattr(self, "_chat_toolbar", None)
        setter = getattr(toolbar, "set_compact_capability", None)
        if not callable(setter):
            return
        command = next(
            (
                candidate
                for candidate in MainWindow._agent_command_capabilities(self)
                if candidate.command_id == COMPACT_CONTEXT_COMMAND
            ),
            None,
        )
        if command is None:
            setter(False, "No native compaction capability.")
            return
        setter(command.supported, command.unavailable_reason)

    def _open_command_palette(self) -> None:
        if getattr(self, "_destroyed", False):
            return
        existing = getattr(self, "_command_palette", None)
        if existing is not None:
            existing.present()
            existing.focus_entry()
            return
        palette = CommandPalette(
            self,
            MainWindow._agent_command_capabilities(self),
        )
        self._command_palette = palette
        palette.connect("activated", self._on_agent_command_activated)
        palette.connect("close-request", self._on_command_palette_closed)
        palette.present()
        palette.focus_entry()

    def _on_command_palette_closed(self, palette, *_args) -> bool:
        if getattr(self, "_command_palette", None) is palette:
            self._command_palette = None
        return False

    def _on_agent_command_activated(self, _palette, command_id: str) -> None:
        MainWindow._execute_agent_command(self, command_id)

    def _execute_agent_command(
        self,
        command_id: str,
        arguments: str = "",
    ) -> bool:
        """Recheck and execute one registered control-plane action."""

        command = next(
            (
                candidate
                for candidate in MainWindow._agent_command_capabilities(self)
                if candidate.command_id == command_id
            ),
            None,
        )
        if command is None:
            self._toast("That agent command is not available in this build.")
            return False
        if arguments and not command.accepts_arguments:
            self._toast(f"{command.invocation} does not accept arguments.")
            return False
        if not command.enabled:
            self._toast(
                command.unavailable_reason
                or "That command is unavailable for this conversation."
            )
            return False
        if command.command_id == COMPACT_CONTEXT_COMMAND:
            return bool(MainWindow._compact_current_chat(self, arguments))
        if command.command_id == REVIEW_CHANGES_COMMAND:
            return bool(MainWindow._review_current_chat(self, arguments))
        if command.command_id == FORK_CONVERSATION_COMMAND:
            return bool(MainWindow._fork_current_chat(self))
        if command.command_id == REVERT_CONVERSATION_COMMAND:
            # This remains unreachable while the capability is disabled. Keep
            # the branch explicit so enabling it later cannot fall through to
            # a pretend prompt or an accidental deprecated rollback call.
            self._toast(command.unavailable_reason)
            return False
        self._toast("That agent command is not implemented yet.")
        return False

    def _maybe_dispatch_registered_command(self, text: str) -> bool:
        """Intercept only registered slash actions; leave paths/prose alone."""

        stripped = str(text or "").lstrip()
        if not stripped.startswith("/"):
            return False
        if MainWindow._dispatch_claude_setting_command(self, stripped):
            return True
        invocation = resolve_command(
            text,
            MainWindow._agent_command_capabilities(self),
        )
        if invocation is None:
            return False
        accepted = MainWindow._execute_agent_command(
            self,
            invocation.command.command_id,
            invocation.arguments,
        )
        if not accepted:
            # Composer clears after its synchronous send signal returns. Keep a
            # command that could not run so the user can retry or edit it.
            GLib.idle_add(self._restore_blocked_execution_send, text)
        return True

    def _known_claude_models(self, driver) -> set[str]:
        """Aliases the picker would offer, plus what the CLI itself listed."""
        known: set[str] = set()
        try:
            known.update(entry.id for entry in model_catalog.anthropic_entries())
        except Exception:  # ponytail: the scan is cached; a failure just shrinks the list
            pass
        for model in getattr(driver, "_cli_models", None) or []:
            if isinstance(model, dict):
                for key in ("value", "id", "displayName"):
                    value = model.get(key)
                    if isinstance(value, str) and value.strip():
                        known.add(value.strip())
        return known

    def _dispatch_claude_setting_command(self, text: str) -> bool:
        """Typed `/model <alias>` or `/effort <level>` for a Claude chat.

        Sent as prose the CLI would apply them itself and the toolbar chip,
        the durable conversation settings and the meter would all desync
        (audit §1). Route them through the same handlers the toolbar uses.
        """
        driver = getattr(self, "_driver", None)
        if not isinstance(driver, ClaudeCliDriver):
            return False
        parts = text.split(maxsplit=1)
        name = parts[0][1:].casefold()
        argument = parts[1].strip() if len(parts) == 2 else ""
        if name == "model":
            if not argument:
                self._toast("Usage: /model <alias>, e.g. /model sonnet")
                return True
            known = MainWindow._known_claude_models(self, driver)
            alias = next((k for k in known if k.casefold() == argument.casefold()), "")
            if not alias:
                self._toast(
                    f"Unknown Claude model {argument!r}. Try one of: "
                    + ", ".join(sorted(known)[:8])
                )
                return True
            self._on_toolbar_model_changed(getattr(self, "_chat_toolbar", None), alias)
            return True
        if name == "effort":
            levels = set(getattr(driver, "supported_effort_levels", lambda: [])() or [])
            levels |= {"low", "medium", "high", "xhigh", "max"}
            key = argument.casefold()
            if key not in levels:
                self._toast(
                    "Usage: /effort <level>: " + ", ".join(sorted(levels))
                )
                return True
            self._on_toolbar_effort_changed(getattr(self, "_chat_toolbar", None), key)
            return True
        return False

    # --- execution settings for the selected conversation ---

    def _current_project_cwd(self) -> str:
        if self._next_chat is not None and self._next_chat.project is not None:
            return self._next_chat.project.cwd
        return ""

    def _effective_permission_mode(self, cwd: str) -> str:
        """Safe, disclosed fallback for a conversation without its own choice."""

        mode, _source = MainWindow._permission_fallback(self, cwd)
        return mode

    def _permission_fallback(self, cwd: str) -> tuple[str, str]:
        """Return ``(mode, provenance)`` for an unconfigured conversation.

        The retired workspace file is read-only compatibility evidence. Its
        restrictive choices remain in force and visible during migration;
        legacy Bypass is permanently clamped to Ask.

        Exception: once the user has confirmed an explicit GLOBAL default in
        Settings (`permission_mode_confirmed`), that modern, disclosed choice is
        authoritative — the retired workspace file must not clamp it down. Its
        whole purpose was to avoid silently *widening* permissions for un-migrated
        users; it must not silently *narrow* a default the user deliberately set
        (for example, Auto reduced to a stale per-cwd `dontAsk`).
        """

        if _home_execution_locked(cwd):
            return "plan", "home-plan"
        if bool(self._ui_state.get("permission_mode_confirmed", False)):
            return sanitize_global_default(self._permission_mode), "default"
        legacy = legacy_permission(cwd)
        if legacy is not None:
            return (
                more_restrictive_mode(self._permission_mode, legacy.mode),
                "legacy-invalid"
                if legacy.invalid
                else "legacy-bypass-retired"
                if legacy.bypass_retired
                else "legacy",
            )
        return sanitize_global_default(self._permission_mode), "default"

    def _execution_target_provider(self) -> str:
        """Provider that will execute the selected conversation's next turn.

        Session selection normally adopts the transcript's provider. In the
        degraded case where a historical provider is unavailable, Helios keeps
        the safe available provider and stages that Work participant on send;
        execution controls must follow that provider now, not write settings
        into the historical sibling's native id.
        """

        target = self._next_chat
        if (
            target is not None
            and target.resume_id
            and not MainWindow._target_resume_resolution(self, target).known
        ):
            return ""
        return self._selected_provider()

    def _target_resume_resolution(
        self,
        target: ChatTarget | None,
    ) -> session_providers.ProviderResolution:
        """Re-resolve native ownership at the activation boundary.

        ``ChatTarget.resume_provider`` is display/routing context, not
        authority. The index, conversation record, and transcript marker must
        still agree immediately before a resume id can affect persistence or
        provider activation.
        """

        if target is None or not target.resume_id:
            return session_providers.ProviderResolution()
        return MainWindow._resolve_resume_id(
            self,
            target.resume_id,
            getattr(target, "project", None),
            cached_provider=str(getattr(target, "resume_provider", "") or ""),
        )

    def _resolve_resume_id(
        self,
        session_id: str,
        project,
        *,
        cached_provider: str = "",
    ) -> session_providers.ProviderResolution:
        """Resolve one native id using durable evidence available right now."""

        if not session_id:
            return session_providers.ProviderResolution()
        project_path = getattr(project, "path", None)
        transcript_path = (
            project_path / f"{session_id}.jsonl"
            if project_path is not None
            else None
        )
        resolution = session_providers.resolve_provider(
            session_id,
            transcript_path,
            conversation_store=getattr(self, "_conversation_perms", None),
        )
        cached = str(cached_provider or "")
        if cached and resolution.known and cached != resolution.provider:
            return session_providers.ProviderResolution(
                sources=(*resolution.sources, "target-cache-conflict"),
                conflicted=True,
            )
        return resolution

    def _execution_target_session_id(
        self,
        target: ChatTarget | None,
        provider: str,
    ) -> str:
        """Native id for the participant that will execute, never a sibling id."""

        if target is None:
            return ""
        if target.resume_id:
            resolution = MainWindow._target_resume_resolution(self, target)
            if resolution.known and resolution.provider == provider:
                return target.resume_id
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is not None and target.work_id:
            try:
                candidate = str(
                    coordinator.resume_id(target.work_id, provider) or ""
                )
            except Exception as exc:
                _log.warning("could not resolve execution participant: %s", exc)
            else:
                resolution = MainWindow._resolve_resume_id(
                    self,
                    candidate,
                    target.project,
                )
                if resolution.known and resolution.provider == provider:
                    return candidate
        return ""

    def _execution_target_lock_reason(self) -> str:
        """Return why Execution/send must be view-only for this target."""

        if MainWindow._execution_target_is_read_only(self):
            return "read-only"
        target = self._next_chat
        if target is None:
            return ""
        if target.resume_id:
            resolution = MainWindow._target_resume_resolution(self, target)
            if resolution.conflicted:
                return "conflict"
            if not resolution.known:
                return "unknown"

        # In a multi-provider Work, the transcript being viewed and the
        # participant that will execute can be different. Validate the latter
        # too so controls never advertise a writable Claude/GPT target only to
        # reject its stale or corrupt native binding at send time.
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is not None and target.work_id:
            try:
                candidate = str(
                    coordinator.resume_id(
                        target.work_id,
                        self._selected_provider(),
                    )
                    or ""
                )
            except Exception as exc:
                _log.warning("could not resolve execution participant: %s", exc)
                return "unknown"
            if candidate:
                candidate_resolution = MainWindow._resolve_resume_id(
                    self,
                    candidate,
                    target.project,
                )
                if candidate_resolution.conflicted:
                    return "conflict"
                if (
                    not candidate_resolution.known
                    or candidate_resolution.provider != self._selected_provider()
                ):
                    return "unknown"
        return ""

    def _execution_target_is_read_only(self) -> bool:
        target = self._next_chat
        return bool(
            target is not None
            and target.project is not None
            and target.project.read_only
        )

    def _stored_conversation_permission(self) -> str:
        target = self._next_chat
        provider = MainWindow._execution_target_provider(self)
        session_id = MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        if not session_id:
            return ""
        return self._conversation_perms.get(
            provider,
            session_id,
        )

    def _save_conversation_permission(
        self,
        provider: str,
        session_id: str,
        mode: str,
    ) -> bool:
        if not session_id:
            return False
        return self._conversation_perms.set(provider, session_id, mode)

    def _save_conversation_effort(
        self,
        provider: str,
        session_id: str,
        key: str,
        *,
        permission_mode: str,
    ) -> bool:
        if not session_id:
            return False
        # Ultra proactively delegates. Until Helios has a real Parallel
        # lease and family budget it is intentionally live-session-only;
        # old incident-era persisted Ultra records are quarantined on resume.
        if provider == model_catalog.PROVIDER_OPENAI and key == "ultra":
            key = ""
        return self._conversation_perms.set_effort(
            provider,
            session_id,
            key,
            permission_mode=permission_mode,
        )

    def _save_conversation_workflow(
        self,
        provider: str,
        session_id: str,
        mode: str,
        *,
        permission_mode: str,
    ) -> bool:
        if not session_id:
            return False
        return self._conversation_perms.set_workflow(
            provider,
            session_id,
            mode,
            permission_mode=permission_mode,
        )

    def _workflow_for_spawn(self, target: ChatTarget, provider: str) -> str:
        """Resolve workflow independently from the permission profile.

        Catalog discovery controls what the picker offers, but it must never
        silently turn a persisted Plan request into Default. The live OpenAI
        driver performs the authoritative capability check before thread bind
        and fails closed when Plan cannot be proved.
        """

        session_id = MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        stored = (
            self._conversation_perms.get_workflow(provider, session_id)
            if session_id
            else DEFAULT_WORKFLOW_MODE
        )
        requested = canonical_workflow_mode(
            getattr(self, "_staged_workflow_mode", "") or stored
        )
        if provider == model_catalog.PROVIDER_OPENAI:
            return requested
        return DEFAULT_WORKFLOW_MODE

    def _execution_settings_for_spawn(
        self,
        target: ChatTarget,
        provider: str,
    ) -> tuple[str, str]:
        """Resolve the exact permission/effort pair for a provider spawn."""

        session_id = MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        permission_mode = (
            self._staged_permission_mode
            or self._conversation_perms.get(provider, session_id)
            or self._effective_permission_mode(target.project.cwd)
        )
        # Enforce provider support and the HOME read-only rule at the spawn
        # boundary, independently of the settings store and displayed mode.
        permission_mode = effective_execution_mode(
            permission_mode,
            target.project.cwd,
            provider=provider,
        )
        effort_key, _effort_source = MainWindow._execution_effort_for_spawn(
            self,
            target,
            provider,
        )
        return permission_mode, effort_key

    def _execution_effort_for_spawn(
        self,
        target: ChatTarget,
        provider: str,
    ) -> tuple[str, str]:
        """Return the validated effort and the provenance of that exact value."""

        session_id = MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        stored_effort = self._conversation_perms.get_effort(provider, session_id)
        if provider == model_catalog.PROVIDER_OPENAI and stored_effort == "ultra":
            stored_effort = ""
        if self._staged_effort_key:
            requested_effort = self._staged_effort_key
            source = "Staged"
        elif stored_effort:
            requested_effort = stored_effort
            source = "Saved"
        elif provider == model_catalog.PROVIDER_OPENAI:
            requested_effort = ""
            source = "Model default"
        else:
            requested_effort = (
                self._provider_efforts.get(provider, "") or self._effort_key
            )
            source = "Global default"
        if provider == model_catalog.PROVIDER_ANTHROPIC:
            allowed = {key for key, _label, _description in EFFORT_STOPS}
            if requested_effort in allowed:
                return requested_effort, source
            return DEFAULT_EFFORT, "Global default"
        else:
            entry = self._catalog_entries_by_id.get(self._model)
            advertised = (
                tuple(key for key, _description in entry.reasoning_efforts)
                if entry is not None
                else ()
            )
            allowed = set(advertised)
            if requested_effort in allowed:
                return requested_effort, source
            if (
                entry is not None
                and entry.default_effort in allowed
                and entry.default_effort != "ultra"
            ):
                return entry.default_effort, "Model default"
            # Ultra is an explicit one-session lease, never an ambient model
            # default. Prefer the familiar balanced level when advertised,
            # then the first non-Ultra capability.
            standard = tuple(key for key in advertised if key != "ultra")
            if "high" in standard:
                return "high", "Helios standard"
            return (standard[0], "Helios standard") if standard else ("", "")

    def _sync_execution_control(self) -> None:
        """Reflect the selected conversation's actual execution settings.

        A bound driver is authoritative: background conversations can retain
        different live modes even when they share a workspace.  When no child
        is bound, a durable conversation record (or unsaved-composer staging)
        wins; workspace/global policy is only the new-conversation fallback.
        """
        if getattr(self, "_destroyed", False):
            return
        # Keep the header Work indicator in step with participant/lead
        # changes. Guarded: mocked-window test harnesses borrow this method
        # without the real header widgets.
        _update_work = getattr(self, "_update_work_indicator", None)
        if _update_work is not None:
            _update_work()
        toolbar = getattr(self, "_chat_toolbar", None)
        if toolbar is None:
            return

        driver = self._driver
        lock_reason = MainWindow._execution_target_lock_reason(self)
        live = (
            not lock_reason
            and driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        cwd = self._current_project_cwd()
        target = self._next_chat
        viewed_resolution = MainWindow._target_resume_resolution(self, target)
        provider = (
            self._driver_provider(driver)
            if live
            else MainWindow._execution_target_provider(self)
        )
        # A read-only historical row has no executor, but its durable values
        # and provider label should still describe the transcript being viewed.
        if lock_reason == "read-only" and viewed_resolution.known:
            provider = viewed_resolution.provider
        session_id = MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        stored_mode = (
            self._conversation_perms.get(provider, session_id)
            if provider and session_id
            else ""
        )
        stored_settings = (
            self._conversation_perms.get_settings(provider, session_id)
            if provider and session_id
            else None
        )
        stored_workflow = (
            str(getattr(stored_settings, "workflow_mode", "") or "")
            if stored_settings is not None
            else ""
        )
        fallback_method = getattr(self, "_permission_fallback", None)
        if callable(fallback_method):
            fallback_mode, fallback_source = fallback_method(cwd)
        else:
            fallback_mode = self._effective_permission_mode(cwd)
            fallback_source = "default"
        fallback_label = {
            "home-plan": "HOME safety",
            "legacy-bypass-retired": "Legacy retired",
            "legacy-invalid": "Legacy invalid",
            "legacy": "Legacy safeguard",
        }.get(fallback_source, "Global default")
        live_mode = (
            str(getattr(driver, "permission_mode", "") or "") if live else ""
        )
        mode = (
            live_mode
            or self._staged_permission_mode
            or stored_mode
            or fallback_mode
        )
        if provider:
            # Show what the spawn will actually run. A confirmed global Bypass
            # narrows to Ask for a provider that cannot select it; the chip
            # said "Bypass" for a GPT chat that was about to prompt.
            mode = effective_execution_mode(mode, cwd, provider=provider)
        if _home_execution_locked(cwd):
            mode = "plan"
        permission_source = (
            "HOME safety"
            if _home_execution_locked(cwd)
            else "Live"
            if live_mode
            else "Staged"
            if self._staged_permission_mode
            else "Saved"
            if stored_mode
            else fallback_label
        )
        toolbar.set_permission_mode(mode)

        live_workflow = (
            str(getattr(driver, "workflow_mode", "") or "") if live else ""
        )
        advertised_workflows = (
            tuple(getattr(driver, "supported_workflow_modes", ()) or ())
            if live and provider == model_catalog.PROVIDER_OPENAI
            else tuple(
                getattr(
                    self,
                    "_openai_workflow_modes",
                    (DEFAULT_WORKFLOW_MODE,),
                )
            )
            if provider == model_catalog.PROVIDER_OPENAI
            else (DEFAULT_WORKFLOW_MODE,)
        )
        workflow_options = workflow_modes_for_provider(
            provider,
            advertised=advertised_workflows,
        )
        set_workflow_options = getattr(toolbar, "set_workflow_options", None)
        if callable(set_workflow_options):
            set_workflow_options(workflow_options)
        workflow_mode = canonical_workflow_mode(
            live_workflow
            or getattr(self, "_staged_workflow_mode", "")
            or stored_workflow
            or DEFAULT_WORKFLOW_MODE
        )
        if workflow_mode not in workflow_options:
            workflow_mode = DEFAULT_WORKFLOW_MODE
        set_workflow_mode = getattr(toolbar, "set_workflow_mode", None)
        if callable(set_workflow_mode):
            set_workflow_mode(workflow_mode)
        workflow_source = (
            "Live"
            if live_workflow
            else "Staged"
            if getattr(self, "_staged_workflow_mode", "")
            else "Saved"
            if stored_workflow
            else "Default"
        )

        # A live conversation may carry a different effort from the provider
        # default (for example after switching between two background chats).
        live_effort = str(getattr(driver, "effort_key", "") or "") if live else ""
        if live_effort:
            selected_effort = live_effort
            effort_source = "Live"
        elif target is not None and provider:
            selected_effort, effort_source = MainWindow._execution_effort_for_spawn(
                self,
                target,
                provider,
            )
        else:
            selected_provider = self._selected_provider()
            selected_effort = (
                self._provider_efforts.get(selected_provider, "")
                or self._effort_key
            )
            effort_source = "Global default"
        if selected_effort:
            toolbar.set_effort(selected_effort)

        if lock_reason == "conflict":
            scope = "Conflict · view only"
            scope_detail = (
                "Provider evidence conflicts; execution changes and sends are disabled."
            )
        elif lock_reason == "unknown":
            scope = "Unknown provider · view only"
            scope_detail = (
                "Provider ownership is unverified; execution changes and sends are disabled."
            )
        elif lock_reason == "read-only":
            viewed = model_catalog.PROVIDER_LABELS.get(viewed_resolution.provider, "Assistant")
            scope = f"Read-only · {viewed}"
            scope_detail = "This pooled transcript cannot execute on this machine."
        else:
            source = (
                permission_source
                if not selected_effort or permission_source == effort_source
                else "Mixed"
            )
            scope_detail = f"Permissions: {permission_source}"
            if workflow_mode != DEFAULT_WORKFLOW_MODE:
                scope_detail += f"; Workflow: {workflow_source}"
            if selected_effort:
                scope_detail += f"; Reasoning: {effort_source}"
            executor = model_catalog.PROVIDER_LABELS.get(provider, "Assistant")
            scope = f"{source} · {executor}"
            if (
                viewed_resolution.known
                and provider
                and viewed_resolution.provider != provider
            ):
                short_source = {
                    "Global default": "Global",
                    "Legacy safeguard": "Legacy",
                    "Legacy retired": "Retired",
                    "Legacy invalid": "Invalid",
                }.get(source, source)
                viewed = model_catalog.PROVIDER_LABELS.get(viewed_resolution.provider, "Assistant")
                scope = f"{short_source} · {executor} · view {viewed}"
        toolbar.set_execution_scope(scope, scope_detail)
        toolbar.set_execution_sensitive(not bool(lock_reason))

        toolbar.set_execution_pending(
            not lock_reason
            and MainWindow._execution_change_pending_for(self, driver)
        )
        MainWindow._sync_agent_command_capabilities(self)

    def _on_toolbar_permission_changed(self, _toolbar, mode: str) -> None:
        if mode not in PERMISSION_MODES or self._destroyed:
            self._sync_execution_control()
            return
        if MainWindow._execution_target_lock_reason(self):
            self._sync_execution_control()
            return
        target = self._next_chat
        if target is None or target.project is None:
            self._sync_execution_control()
            return
        if _home_execution_locked(target.project.cwd) and mode != "plan":
            self._toast(
                "HOME chats use read-only permissions. Select a project folder "
                "before granting file changes."
            )
            self._sync_execution_control()
            return
        driver = self._driver
        live = (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        if not live:
            provider = MainWindow._execution_target_provider(self)
            session_id = MainWindow._execution_target_session_id(
                self,
                target,
                provider,
            )
            if session_id:
                saved = MainWindow._save_conversation_permission(
                    self,
                    provider,
                    session_id,
                    mode,
                )
                if not saved:
                    self._toast("Could not save permissions for this conversation.")
                    self._sync_execution_control()
                    return
                self._staged_permission_mode = ""
            else:
                self._staged_permission_mode = mode
            # No toast: the toolbar's permission label IS the confirmation,
            # and a banner per change queues over the composer (Spencer,
            # 2026-08-22). Only failures and partial saves toast.
            self._sync_execution_control()
            return

        changes = self._permission_changes
        if driver in changes:
            self._sync_execution_control()
            return
        token = object()
        changes[driver] = token
        provider = self._driver_provider(driver)
        session_id = str(
            getattr(driver, "session_id", "")
            or MainWindow._execution_target_session_id(self, target, provider)
        )
        self._sync_execution_control()

        def applied(success: bool, detail: str = "") -> None:
            if self._permission_changes.get(driver) is not token:
                return
            del self._permission_changes[driver]
            durable_saved = True
            if success:
                durable_id = str(getattr(driver, "session_id", "") or session_id)
                if durable_id:
                    durable_saved = MainWindow._save_conversation_permission(
                        self,
                        provider,
                        durable_id,
                        mode,
                    )
                else:
                    # A fresh process can ACK before system/init gives it a
                    # native id. session-started persists this attached value.
                    driver._helios_staged_permission_mode = mode
            restart_required = bool(
                not success
                and getattr(driver, "execution_restart_required", False)
            )
            if restart_required:
                self._teardown_driver(driver)
            if getattr(self, "_destroyed", False):
                return
            if success and durable_saved:
                pass  # see above: the toolbar label is the confirmation
            elif success:
                self._toast(
                    "Permissions changed for the running conversation, but "
                    "could not be saved for restart.",
                    timeout=6,
                )
            else:
                message = detail or "the provider rejected the change"
                self._toast(f"Could not change permissions: {message}", timeout=6)
            self._sync_execution_control()

        try:
            accepted = driver.set_permission_mode(mode, applied)
        except Exception as exc:
            _log.exception("permission change failed")
            applied(False, str(exc))
            return
        if not accepted and self._permission_changes.get(driver) is token:
            applied(False, "the conversation is not idle")

    def _on_toolbar_workflow_changed(self, _toolbar, mode: str) -> None:
        mode = canonical_workflow_mode(mode)
        if self._destroyed or MainWindow._execution_target_lock_reason(self):
            self._sync_execution_control()
            return
        target = self._next_chat
        if target is None or target.project is None:
            self._sync_execution_control()
            return
        driver = self._driver
        live = (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        provider = (
            self._driver_provider(driver)
            if live
            else MainWindow._execution_target_provider(self)
        )
        advertised = (
            tuple(getattr(driver, "supported_workflow_modes", ()) or ())
            if live and provider == model_catalog.PROVIDER_OPENAI
            else self._openai_workflow_modes
            if provider == model_catalog.PROVIDER_OPENAI
            else (DEFAULT_WORKFLOW_MODE,)
        )
        if not provider_allows_workflow(
            provider,
            mode,
            advertised=advertised,
        ):
            self._toast("This provider does not advertise that workflow.")
            self._sync_execution_control()
            return

        session_id = MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        permission_mode = (
            self._conversation_perms.get(provider, session_id)
            or self._staged_permission_mode
            or self._effective_permission_mode(target.project.cwd)
        )
        if not live:
            if session_id:
                if not MainWindow._save_conversation_workflow(
                    self,
                    provider,
                    session_id,
                    mode,
                    permission_mode=permission_mode,
                ):
                    self._toast("Could not save workflow for this conversation.")
                    self._sync_execution_control()
                    return
                self._staged_workflow_mode = ""
            else:
                self._staged_workflow_mode = mode
            self._sync_execution_control()
            return

        changes = self._workflow_changes
        if driver in changes:
            self._sync_execution_control()
            return
        token = object()
        changes[driver] = token
        self._sync_execution_control()

        def applied(success: bool, detail: str = "") -> None:
            if self._workflow_changes.get(driver) is not token:
                return
            del self._workflow_changes[driver]
            durable_saved = True
            if success:
                durable_id = str(getattr(driver, "session_id", "") or session_id)
                if durable_id:
                    durable_saved = MainWindow._save_conversation_workflow(
                        self,
                        provider,
                        durable_id,
                        mode,
                        permission_mode=str(
                            getattr(driver, "permission_mode", "")
                            or permission_mode
                        ),
                    )
                else:
                    driver._helios_staged_workflow_mode = mode
            if not getattr(self, "_destroyed", False):
                if success and not durable_saved:
                    self._toast(
                        "Workflow changed for the running conversation, but "
                        "could not be saved for restart.",
                        timeout=6,
                    )
                elif not success:
                    self._toast(
                        "Could not change workflow: "
                        + (detail or "the provider rejected the change"),
                        timeout=6,
                    )
                self._sync_execution_control()

        setter = getattr(driver, "set_workflow_mode", None)
        if not callable(setter):
            applied(False, "the provider has no native workflow control")
            return
        try:
            accepted = setter(mode, applied)
        except Exception as exc:
            _log.exception("workflow change failed")
            applied(False, str(exc))
            return
        if not accepted and self._workflow_changes.get(driver) is token:
            applied(False, "the conversation rejected the workflow")

    # --- provider toggle (Claude / GPT / OpenRouter) ---

    def _build_provider_toggle(self) -> Gtk.Widget:
        """Segmented Claude|GPT|OpenRouter switch. Selecting a side restores
        the last model used with that provider (toast: applies to the next
        chat, same semantics as the model picker). The GPT side stays
        insensitive until the catalog proves OpenAI availability; OpenRouter
        stays insensitive until an API key is saved."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.add_css_class("linked")
        box.set_valign(Gtk.Align.CENTER)

        self._claude_toggle = Gtk.ToggleButton(label="Claude")
        self._claude_toggle.set_tooltip_text("Chat with Claude — the model picker below chooses which")

        self._gpt_toggle = Gtk.ToggleButton(label="GPT")
        self._gpt_toggle.set_group(self._claude_toggle)
        self._gpt_toggle.set_sensitive(False)
        self._gpt_toggle.set_tooltip_text("Checking OpenAI model availability…")

        self._openrouter_toggle = Gtk.ToggleButton(label="OpenRouter")
        self._openrouter_toggle.set_group(self._claude_toggle)
        self._openrouter_toggle.set_sensitive(False)
        self._openrouter_toggle.set_tooltip_text("Add an OpenRouter API key in Settings → Providers")

        provider = model_catalog.provider_for(self._model)
        self._provider_guard = True
        if provider == model_catalog.PROVIDER_OPENAI:
            self._gpt_toggle.set_active(True)
        elif provider == model_catalog.PROVIDER_OPENROUTER:
            self._openrouter_toggle.set_active(True)
        else:
            self._claude_toggle.set_active(True)
        self._provider_guard = False

        self._claude_toggle.connect(
            "toggled", self._on_provider_toggled, model_catalog.PROVIDER_ANTHROPIC
        )
        self._gpt_toggle.connect(
            "toggled", self._on_provider_toggled, model_catalog.PROVIDER_OPENAI
        )
        self._openrouter_toggle.connect(
            "toggled", self._on_provider_toggled, model_catalog.PROVIDER_OPENROUTER
        )
        box.append(self._claude_toggle)
        box.append(self._gpt_toggle)
        box.append(self._openrouter_toggle)
        return box

    def _build_new_chat_control(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.add_css_class("linked")
        box.set_valign(Gtk.Align.CENTER)

        new_chat_btn = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        new_chat_btn.set_tooltip_text(
            "New chat with the selected agent (Ctrl+N)"
        )
        new_chat_btn.add_css_class("suggested-action")
        new_chat_btn.connect("clicked", lambda *_: self._start_new_chat())
        box.append(new_chat_btn)

        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("pan-down-symbolic")
        menu_btn.set_tooltip_text("Choose agent or folder")
        menu_btn.set_popover(self._build_new_chat_popover())
        box.append(menu_btn)

        return box

    def _build_new_chat_popover(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.add_css_class("helios-new-chat-popover")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_top(8)
        inner.set_margin_bottom(8)
        inner.set_margin_start(8)
        inner.set_margin_end(8)

        self._new_claude_btn = Gtk.Button()
        self._new_claude_btn.add_css_class("flat")
        self._new_claude_btn.set_child(
            self._new_chat_row(
                "Claude Chat",
                "Use the selected Claude model in this working directory",
                "text-x-generic-symbolic",
            )
        )
        self._new_claude_btn.connect(
            "clicked",
            lambda *_: (
                pop.popdown(),
                self._start_new_chat_with_provider(model_catalog.PROVIDER_ANTHROPIC),
            ),
        )
        inner.append(self._new_claude_btn)

        self._new_gpt_btn = Gtk.Button()
        self._new_gpt_btn.add_css_class("flat")
        self._new_gpt_btn.set_child(
            self._new_chat_row(
                "GPT Chat",
                "Use Codex/OpenAI in this working directory",
                "applications-science-symbolic",
            )
        )
        self._new_gpt_btn.connect(
            "clicked",
            lambda *_: (
                pop.popdown(),
                self._start_new_chat_with_provider(model_catalog.PROVIDER_OPENAI),
            ),
        )
        inner.append(self._new_gpt_btn)

        self._new_or_btn = Gtk.Button()
        self._new_or_btn.add_css_class("flat")
        self._new_or_btn.set_child(
            self._new_chat_row(
                "OpenRouter Chat",
                "Use an OpenRouter model in this working directory",
                "network-server-symbolic",
            )
        )
        self._new_or_btn.connect(
            "clicked",
            lambda *_: (
                pop.popdown(),
                self._start_new_chat_with_provider(model_catalog.PROVIDER_OPENROUTER),
            ),
        )
        inner.append(self._new_or_btn)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        inner.append(sep)

        folder_btn = Gtk.Button()
        folder_btn.add_css_class("flat")
        folder_btn.set_child(
            self._new_chat_row(
                "Chat in Folder...",
                "Choose a working directory first (Ctrl+Shift+N)",
                "folder-open-symbolic",
            )
        )
        folder_btn.connect(
            "clicked", lambda *_: (pop.popdown(), self._start_new_chat_in_folder())
        )
        inner.append(folder_btn)

        pop.set_child(inner)
        self._sync_new_chat_actions()
        return pop

    def _new_chat_row(self, title: str, subtitle: str, icon_name: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_margin_top(4)
        row.set_margin_bottom(4)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        icon.add_css_class("dim-label")
        row.append(icon)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("caption-heading")
        subtitle_label = Gtk.Label(label=subtitle, xalign=0)
        subtitle_label.add_css_class("caption")
        subtitle_label.add_css_class("dim-label")
        labels.append(title_label)
        labels.append(subtitle_label)
        row.append(labels)
        return row

    def _sync_new_chat_actions(self) -> None:
        btn = getattr(self, "_new_gpt_btn", None)
        if btn is not None:
            available = (
                self._openai_catalog_authoritative
                and bool(self._openai_entries)
            )
            btn.set_sensitive(available)
            btn.set_tooltip_text(
                "Start a new GPT chat"
                if available
                else "Add an OpenAI key in Settings -> Providers to enable GPT"
            )
        or_btn = getattr(self, "_new_or_btn", None)
        if or_btn is not None:
            from helios.backend.openrouter import key as or_key
            has_or = bool(or_key.load_key())
            or_btn.set_sensitive(has_or)
            or_btn.set_tooltip_text(
                "Start a new OpenRouter chat"
                if has_or
                else "Add an OpenRouter API key in Settings → Providers"
            )

    def _on_provider_toggled(self, btn: Gtk.ToggleButton, provider: str) -> None:
        if self._provider_guard or not btn.get_active():
            return
        if model_catalog.provider_for(self._model) == provider:
            return
        # One canonical default-resolution path per provider. The OpenRouter
        # branch previously read a ``_openrouter_entries`` attribute that was
        # never assigned, so the toggle always toasted "No OpenRouter models".
        alias = self._model_for_provider(provider)
        if not alias:
            label = {
                model_catalog.PROVIDER_OPENAI: "OpenAI",
                model_catalog.PROVIDER_OPENROUTER: "OpenRouter",
            }.get(provider, "Claude")
            self._toast(f"No {label} models available — add a key in Settings → Providers.")
            self._sync_provider_toggle()
            return
        self._apply_model_choice(alias)

    def _sync_provider_toggle(self) -> None:
        provider = model_catalog.provider_for(self._model)
        if provider == model_catalog.PROVIDER_OPENAI:
            target = self._gpt_toggle
        elif provider == model_catalog.PROVIDER_OPENROUTER:
            target = self._openrouter_toggle
        else:
            target = self._claude_toggle
        if not target.get_active():
            self._provider_guard = True
            target.set_active(True)
            self._provider_guard = False

    def _apply_model_choice(self, alias: str, *, quiet: bool = False) -> None:
        """The single path for every model change (picker, provider toggle,
        settings): persist, remember per provider, reflect everywhere."""
        old_provider = model_catalog.provider_for(self._model)
        self._model = alias
        self._ui_state.set("model", alias)  # sticky across restarts
        provider = model_catalog.provider_for(alias)
        self._provider_models[provider] = alias
        self._ui_state.set(f"model_{provider}", alias)
        self._chat_toolbar.set_model(alias)
        # Rescale the context meter to the newly-selected model's window now,
        # rather than waiting for the next turn's usage report.
        self._chat_toolbar.set_context_model(alias)
        self._sync_provider_toggle()
        self._sync_assistant_labels()
        self._sync_effort_sensitivity()
        if provider != old_provider:
            self._refresh_openrouter_credits()
        if quiet:
            return
        if provider != old_provider:
            lead = model_catalog.PROVIDER_LABELS.get(provider, "Claude")
            peer = model_catalog.PROVIDER_LABELS.get(old_provider, "Claude")
            if not self._stage_fresh_chat_for_provider_switch():
                self._toast(
                    f"New {lead} chat — the running {peer} chat continues "
                    "in the background."
                )
                return
            if self._current_work_id():
                self._toast(
                    f"{lead} is now leading this Work — {peer} remains available."
                )
                return
        label = alias or "default"
        if self._driver is not None and self._driver.is_running:
            if self._try_live_model_change(alias, label):
                return
            self._toast(f"Model set to {label} — applies to your next chat.")
        # No live process: the picker chip already shows the choice, and it
        # simply applies at the next spawn. Nothing worth a toast.

    def _try_live_model_change(self, alias: str, label: str) -> bool:
        """Apply a same-provider model pick to the live Claude process.

        The control channel's `set_model` spares the respawn that this change
        prices at ~44.5k tokens of re-created session baseline. Returns True
        only when the request was dispatched — the toast then comes from the
        async outcome. Any refusal falls back to the staged
        "applies to your next chat" semantics unchanged; a fenced (ambiguous)
        outcome closes the process, which makes that fallback literally true.
        """
        driver = self._driver
        set_model = getattr(driver, "set_model", None)
        if not callable(set_model) or not alias:
            return False
        if not (
            driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        ):
            return False

        def applied(success: bool, detail: str) -> None:
            restart_required = bool(
                not success
                and getattr(driver, "execution_restart_required", False)
            )
            if restart_required:
                self._teardown_driver(driver)
            if getattr(self, "_destroyed", False):
                return
            if success:
                self._toast(f"Model set to {label} — applied to the live chat.")
                self._sync_effort_sensitivity()
            else:
                self._toast(f"Model set to {label} — applies to your next chat.")

        try:
            set_model(alias, applied)
        except Exception:
            _log.exception("live model change failed")
            return False
        # The driver invokes the callback exactly once on every path —
        # synchronous refusals included — so the toast is always its job.
        return True

    def _sync_effort_sensitivity(self) -> None:
        """Render only the reasoning efforts the selected model supports."""
        # The model picker is a spawn-time choice.  While a same-provider
        # conversation is live it may therefore differ from the newly staged
        # picker value; derive capabilities from the process we will actually
        # update, not from a future model selection.
        execution_model = self._model
        driver = self._driver
        if (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        ):
            execution_model = str(getattr(driver, "model", "") or execution_model)
        provider = model_catalog.provider_for(execution_model)
        target = self._next_chat
        session_id = MainWindow._execution_target_session_id(
            self,
            target,
            provider,
        )
        stored_effort = ""
        if session_id and MainWindow._execution_target_provider(self) == provider:
            stored_effort = self._conversation_perms.get_effort(
                provider,
                session_id,
            )
        live = (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        live_effort = str(getattr(driver, "effort_key", "") or "") if live else ""
        conversation_effort = (
            live_effort or self._staged_effort_key or stored_effort
        )
        conversation_owned = live or bool(self._staged_effort_key or stored_effort)
        remembered = (
            ""
            if provider == model_catalog.PROVIDER_OPENAI
            else self._provider_efforts.get(provider, "")
        )
        selected_effort = conversation_effort or remembered or self._effort_key
        if provider == model_catalog.PROVIDER_OPENAI and not conversation_effort:
            selected_effort = ""
        if provider == model_catalog.PROVIDER_OPENROUTER:
            # The driver has mapped Helios effort keys onto OpenRouter's
            # unified `reasoning` parameter since the provider landed, with
            # tests, and refuses when the pinned endpoint does not declare it;
            # only this control was ever disabled. So offer the choices for a
            # model whose catalog row declares `reasoning`, and let the
            # driver's endpoint check stay authoritative — it has to be,
            # because `require_parameters: true` reroutes a request carrying a
            # parameter the endpoint does not advertise.
            entry = self._catalog_entries_by_id.get(execution_model)
            if entry is None or not entry.reasoning_efforts:
                self._chat_toolbar.set_effort_sensitive(False)
                self._sync_execution_control()
                return
            effective = self._chat_toolbar.set_effort_options(
                entry.reasoning_efforts,
                default_effort=entry.default_effort,
                selected_effort=selected_effort,
            )
        elif provider == model_catalog.PROVIDER_OPENAI:
            entry = self._catalog_entries_by_id.get(execution_model)
            if entry is None:
                self._chat_toolbar.set_effort_sensitive(False)
                self._sync_execution_control()
                return
            if not entry.reasoning_efforts:
                if not conversation_owned:
                    self._effort_key = ""
                    self._provider_efforts[provider] = ""
                    self._ui_state.set(f"effort_{provider}", "")
                    self._ui_state.set("effort_level", "")
                self._chat_toolbar.set_effort_sensitive(False)
                self._sync_execution_control()
                return
            effective = self._chat_toolbar.set_effort_options(
                entry.reasoning_efforts,
                default_effort=entry.default_effort,
                selected_effort=selected_effort,
            )
        else:
            # Prefer what the CLI stated for this model over the fixed
            # six-stop list. Only a live driver can have been told, and only
            # the driver we would actually update — which is the same process
            # `execution_model` was derived from above.
            supported: list[str] = []
            if live and hasattr(driver, "supported_effort_levels"):
                try:
                    supported = driver.supported_effort_levels()
                except Exception as exc:  # pragma: no cover - defensive
                    _log.warning("could not read supported effort levels: %s", exc)
            effective = self._chat_toolbar.set_anthropic_effort_options(
                selected_effort,
                supported_levels=supported,
            )
        if (
            effective
            and not conversation_owned
            and provider != model_catalog.PROVIDER_OPENAI
        ):
            self._effort_key = effective
            self._provider_efforts[provider] = effective
            self._ui_state.set(f"effort_{provider}", effective)
            self._ui_state.set("effort_level", effective)
        # Re-apply the selected conversation after rebuilding the slider.  A
        # catalog refresh must never replace a live/staged/stored choice with
        # the provider-wide fallback used for brand-new conversations.
        self._sync_execution_control()

    def _work_is_executing(self, work_id: str) -> bool:
        """True while any participant of ``work_id`` holds a running attempt."""

        coordinator = getattr(self, "_work_coordinator", None)
        store = getattr(coordinator, "store", None)
        if not work_id or store is None:
            return False
        try:
            return any(
                attempt.work_id == work_id
                for attempt in store.list_active_execution_attempts()
            )
        except Exception as exc:
            _log.warning("could not read running attempts for %s: %s", work_id, exc)
            return False

    def _stage_fresh_chat_for_provider_switch(self) -> bool:
        """Switch native participants while preserving one logical Work.

        Returns False only when the Work is mid-turn and the selected provider
        has no conversation in it yet: a Work admits one running attempt, so
        joining it as a second participant just earns "This Work is already
        executing" on send (2026-09-16: every Claude send failed while Kimi
        ran). That case opens a fresh chat outside the Work instead and leaves
        the running chat in the background.
        """
        if self._main_stack.get_visible_child_name() != "transcript":
            return True
        target = self._next_chat
        if target is None or target.project is None or target.project.read_only:
            return True
        coordinator = getattr(self, "_work_coordinator", None)
        busy_work = self._current_work_id()
        if busy_work and coordinator is not None and self._work_is_executing(busy_work):
            try:
                sibling = coordinator.resume_id(busy_work, self._selected_provider())
            except Exception as exc:
                _log.warning("could not read Work participant: %s", exc)
                sibling = ""
            if not sibling:
                self._show_fresh_chat_ui(target.project)
                return False
        # The staged resume id belongs to the provider we just left. Preserve
        # Work identity, but never attach that opaque id to the new provider.
        work_id = self._ensure_target_work(bind_resume=False)
        resume_id = ""
        participant = None
        if coordinator is not None and work_id:
            try:
                resume_id = coordinator.resume_id(
                    work_id,
                    self._selected_provider(),
                )
                participant = coordinator.participant(
                    work_id,
                    self._selected_provider(),
                )
            except Exception as exc:
                _log.warning("could not switch Work participant: %s", exc)
                self._toast("Could not switch this Work participant.")
                return True
        if resume_id:
            resolution = MainWindow._resolve_resume_id(
                self,
                str(resume_id),
                target.project,
            )
            if (
                not resolution.known
                or resolution.provider != self._selected_provider()
            ):
                self._toast(
                    "The selected Work participant could not be verified; "
                    "Helios kept the current conversation unchanged."
                )
                return True
        if resume_id and self._sessions.reveal_session(resume_id):
            return True
        # A newly-created native transcript can exist before SessionList's
        # delayed reload has rendered its row.  The sidebar miss is not proof
        # that the sibling is a fresh chat: recover the exact live participant
        # from the driver registry and route it through the normal historical-
        # session selection path so its own transcript remains authoritative.
        live = self._driver_manager.driver_for_work_participant(
            work_id,
            self._selected_provider(),
            generation=(participant.generation if participant is not None else None),
        )
        if live is not None and (
            not live.is_accepting_input
            or not self._driver_matches_selected_provider(live)
        ):
            live = None
        if live is None and resume_id:
            candidate = self._driver_manager.driver_for_session(resume_id)
            if (
                candidate is not None
                and candidate.is_accepting_input
                and self._driver_matches_selected_provider(candidate)
            ):
                live = candidate
                if participant is not None:
                    tag_driver(live, participant)
        if live is not None and live.is_accepting_input:
            native_id = resume_id or str(getattr(live, "session_id", "") or "")
            if native_id:
                session = self._session_for_native_binding(target.project, native_id)
                self._on_session_selected(self._sessions, session)
                return True
        self._show_fresh_chat_ui(
            target.project,
            work_id=work_id,
            resume_id=resume_id,
        )
        return True

    @staticmethod
    def _session_for_native_binding(project, session_id: str) -> Session:
        """Return the provider transcript even before its sidebar row exists."""

        try:
            for session in project.load_sessions(refresh=True):
                if session.session_id == session_id:
                    return session
        except Exception as exc:
            _log.warning("could not refresh native transcript %s: %s", session_id, exc)
        path = project.path / f"{session_id}.jsonl"
        try:
            stat = path.stat()
            mtime, size = stat.st_mtime, stat.st_size
        except OSError:
            # The provider may publish its native id just before its mirror is
            # flushed.  This still represents an existing native conversation,
            # not a new one; TranscriptView tolerates a not-yet-present path.
            mtime, size = 0.0, 0
        return Session(
            project=project,
            session_id=session_id,
            path=path,
            mtime=mtime,
            size=size,
        )

    def _show_native_execution_binding(self, project, session_id: str) -> None:
        """Make the executor's own transcript visible without losing staging."""

        staged_permission = self._staged_permission_mode
        staged_effort = self._staged_effort_key
        staged_workflow = self._staged_workflow_mode
        current = self._driver
        if current is not None and current.session_id == session_id:
            # Force the full selection path even if an earlier degraded route
            # raw-bound this driver while a sibling transcript stayed visible.
            self._bind_visible_driver(None)
        session = MainWindow._session_for_native_binding(project, session_id)
        self._on_session_selected(self._sessions, session)
        self._staged_permission_mode = staged_permission
        self._staged_effort_key = staged_effort
        self._staged_workflow_mode = staged_workflow
        self._sync_execution_control()

    def _show_fresh_execution_binding(self, target: ChatTarget) -> None:
        """Show a fresh selected-provider participant while preserving choices."""

        staged_permission = self._staged_permission_mode
        staged_effort = self._staged_effort_key
        staged_workflow = self._staged_workflow_mode
        self._show_fresh_chat_ui(
            target.project,
            work_id=target.work_id,
        )
        self._staged_permission_mode = staged_permission
        self._staged_effort_key = staged_effort
        self._staged_workflow_mode = staged_workflow
        self._sync_execution_control()

    def _model_for_provider(self, provider: str) -> str:
        """The model alias to use when switching to `provider`: the last one
        used with it, else a sensible default for that side."""
        remembered = self._provider_models.get(provider, "")
        if provider == model_catalog.PROVIDER_OPENAI:
            valid = {e.id for e in self._openai_entries}
            if remembered in valid:
                return remembered
            return model_catalog.preferred_openai_model(self._openai_entries)
        if provider == model_catalog.PROVIDER_OPENROUTER:
            if model_catalog.openrouter_model_selectable(remembered):
                return remembered
            or_entries, _ = model_catalog.openrouter_entries()
            return model_catalog.preferred_openrouter_model(or_entries)
        return remembered or DEFAULT_MODEL

    def _valid_anthropic_fallback_model(self) -> str | None:
        """A catalog-proven Claude model for a fail-closed provider fallback."""
        anthropic_entries = [
            entry
            for entry in self._catalog_entries_by_id.values()
            if getattr(entry, "provider", model_catalog.PROVIDER_ANTHROPIC)
            == model_catalog.PROVIDER_ANTHROPIC
        ]
        if not anthropic_entries:
            return None
        valid = {entry.id for entry in anthropic_entries}
        remembered = self._provider_models.get(model_catalog.PROVIDER_ANTHROPIC)
        for candidate in (remembered, DEFAULT_MODEL, ""):
            if candidate is not None and candidate in valid:
                return candidate
        return anthropic_entries[0].id

    def _guard_openai_driver_activation(self) -> bool:
        """Require an authoritative exact-model match before any GPT send.

        This guard runs before driver reuse as well as spawn.  A persisted or
        programmatically assigned GPT id therefore cannot cross the startup
        discovery window.  Once a catalog exists, quietly fall back to a model
        it proves is valid for Claude; without that baseline, fail closed.
        """
        if self._selected_provider() != model_catalog.PROVIDER_OPENAI:
            return True
        valid = {entry.id for entry in self._openai_entries}
        if (
            self._openai_catalog_authoritative
            and self._openai_entries
            and self._model in valid
        ):
            return True
        fallback = self._valid_anthropic_fallback_model()
        if fallback is not None:
            self._apply_model_choice(fallback, quiet=True)
            return True
        self._toast(
            "No OpenAI models available — add a key in Settings → Providers."
        )
        return False

    def _adopt_session_provider(self, provider: str) -> None:
        """Point the active model/toggle at the provider that produced
        the selected session, quietly (no toast, no fresh-chat staging) — used when a
        session is selected so the header tracks the chat you're viewing."""
        if provider not in (
            model_catalog.PROVIDER_ANTHROPIC,
            model_catalog.PROVIDER_OPENAI,
            model_catalog.PROVIDER_OPENROUTER,
        ):
            return
        if provider == self._selected_provider():
            return
        if provider == model_catalog.PROVIDER_OPENAI and not self._openai_entries:
            # Historical GPT transcripts remain viewable, but without an
            # authoritative catalog we cannot safely resume/spawn GPT. Keep the
            # fail-closed Claude selection; a send stages that Work participant.
            return
        alias = self._model_for_provider(provider)
        if alias:
            self._apply_model_choice(alias, quiet=True)

    def _selected_provider(self) -> str:
        return model_catalog.provider_for(self._model)

    def _sync_openrouter_toggle_sensitivity(self) -> None:
        """Enable/disable the OpenRouter toggle based on whether a key exists."""
        or_toggle = getattr(self, "_openrouter_toggle", None)
        if or_toggle is None:
            return
        from helios.backend.openrouter import key as or_key
        has_key = bool(or_key.load_key())
        or_toggle.set_sensitive(has_key)
        or_toggle.set_tooltip_text(
            "Chat with OpenRouter" if has_key
            else "Add an OpenRouter API key in Settings → Providers"
        )

    def _ensure_target_work(self, *, bind_resume: bool = True) -> str:
        """Lazily give the staged chat a durable provider-neutral identity."""

        target = self._next_chat
        coordinator = getattr(self, "_work_coordinator", None)
        if target is None or target.project is None or coordinator is None:
            return target.work_id if target is not None else ""
        if target.project.read_only:
            return target.work_id
        try:
            work = coordinator.ensure_work(
                work_id=target.work_id,
                cwd=target.project.cwd,
                lead_provider=self._selected_provider(),
                objective=(self._pending_goal.objective if self._pending_goal else ""),
            )
            work = coordinator.select_lead(work.work_id, self._selected_provider())
            self._next_chat = stage_work(target, work.work_id)
            participant = coordinator.bind_participant(
                work.work_id,
                self._selected_provider(),
                native_id=target.resume_id if bind_resume else "",
                model=self._model,
            )
            if bind_resume and target.resume_id:
                session_goals.rekey_goal(target.resume_id, work.work_id)
            if self._pending_goal is not None:
                goal = self._goal_with_current_metadata(self._pending_goal)
                session_goals.set_goal(work.work_id, goal)
                coordinator.record_goal(
                    work_id=work.work_id,
                    provider=participant.provider,
                    goal=goal,
                )
                self._pending_goal = None
            return work.work_id
        except Exception as exc:
            _log.warning("could not ensure Work for %s: %s", target.project.cwd, exc)
            return target.work_id

    def _stage_selected_work_participant(self) -> bool:
        """Point the current Work at the selected provider's native binding."""

        target = self._next_chat
        if target is None:
            return False
        coordinator = getattr(self, "_work_coordinator", None)
        work_id = target.work_id
        resume_id = ""
        # Existing Work metadata is untrusted until its opaque native binding
        # resolves. Validate before select_lead/bind/Goal writes mutate the
        # Work; a fail-closed result must truly leave it unchanged.
        try:
            resume_id = (
                coordinator.resume_id(work_id, self._selected_provider())
                if coordinator is not None and work_id
                else ""
            )
        except Exception as exc:
            _log.warning("could not resolve selected Work participant: %s", exc)
            return False
        if resume_id:
            resolution = MainWindow._resolve_resume_id(
                self,
                str(resume_id),
                target.project,
            )
            if (
                not resolution.known
                or resolution.provider != self._selected_provider()
            ):
                _log.warning(
                    "refusing unresolved %s participant %s for Work %s",
                    self._selected_provider(),
                    resume_id,
                    work_id,
                )
                return False
        work_id = self._ensure_target_work(bind_resume=False)
        if not resume_id and coordinator is not None and work_id:
            try:
                resume_id = coordinator.resume_id(
                    work_id,
                    self._selected_provider(),
                )
            except Exception as exc:
                _log.warning("could not resolve selected Work participant: %s", exc)
                return False
        if not resume_id and target.resume_id:
            current_resolution = MainWindow._target_resume_resolution(self, target)
            if (
                current_resolution.known
                and current_resolution.provider == self._selected_provider()
            ):
                resume_id = target.resume_id
        if resume_id:
            resolution = MainWindow._resolve_resume_id(
                self,
                str(resume_id),
                target.project,
            )
            if (
                not resolution.known
                or resolution.provider != self._selected_provider()
            ):
                _log.warning(
                    "refusing unresolved %s participant %s for Work %s",
                    self._selected_provider(),
                    resume_id,
                    work_id,
                )
                return False
        self._next_chat = switch_work_participant(
            stage_work(target, work_id),
            str(resume_id or ""),
            self._selected_provider() if resume_id else "",
        )
        return True

    def _fence_mismatched_target_resume(self) -> bool:
        """Never pass one provider's native id to another provider's driver.

        Normal Work routing replaces a foreign resume id with the selected
        participant's binding. This is the final fail-closed boundary for the
        degraded cases where WorkCoordinator is unavailable or normalization
        raises: preserve project/Work/view state, but make the next spawn fresh.
        """
        target = self._next_chat
        if target is None or not target.resume_id:
            return True
        resolution = MainWindow._target_resume_resolution(self, target)
        if not resolution.known:
            _log.warning(
                "refusing unresolved resume id %s",
                target.resume_id,
            )
            return False
        source_provider = resolution.provider
        selected_provider = self._selected_provider()
        if source_provider == selected_provider:
            return True
        _log.warning(
            "clearing %s resume id before %s driver activation",
            source_provider,
            selected_provider,
        )
        self._next_chat = switch_work_participant(target, "")
        return True

    def _driver_matches_target_binding(self, driver) -> bool:
        target = self._next_chat
        if getattr(driver, "_helios_identity_confirmed", True) is not True:
            return False
        driver_session_id = str(getattr(driver, "session_id", "") or "")
        driver_cwd = str(getattr(driver, "_cwd", "") or "")
        target_cwd = str(
            getattr(getattr(target, "project", None), "cwd", "") or ""
        )
        if (
            driver_cwd
            and target_cwd
            and canonical_cwd(driver_cwd) != canonical_cwd(target_cwd)
        ):
            return False
        if (
            target is not None
            and target.resume_id
            and driver_session_id
            and driver_session_id != target.resume_id
        ):
            return False
        coordinator = getattr(self, "_work_coordinator", None)
        if target is None or not target.work_id or coordinator is None:
            return True
        participant = coordinator.participant(
            target.work_id,
            self._selected_provider(),
        )
        if participant is None:
            return False
        return (
            getattr(driver, "_helios_participant_generation", None)
            == participant.generation
            and (
                not target.resume_id
                or getattr(driver, "session_id", "") == target.resume_id
            )
        )

    def _can_activate_target_binding(self, target: ChatTarget) -> bool:
        """Fence an older primary binding before activating historical native state."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None or not target.work_id or not target.resume_id:
            return True
        participant = coordinator.participant(
            target.work_id,
            self._selected_provider(),
        )
        if (
            participant is None
            or not participant.native_id
            or participant.native_id == target.resume_id
        ):
            return True
        active = self._driver_manager.driver_for_session(participant.native_id)
        if active is None:
            active = self._driver_manager.driver_for_work_participant(
                target.work_id,
                self._selected_provider(),
                generation=participant.generation,
            )
        if active is None:
            return True
        if active.is_busy:
            self._toast(
                "Wait for the active provider turn to finish before resuming "
                "an older binding in this Work."
            )
            return False
        self._teardown_driver(active)
        return True

    def _driver_provider(self, driver) -> str:
        provider = self._driver_manager.driver_provider(driver, "")
        return (
            provider
            if provider
            in (
                model_catalog.PROVIDER_ANTHROPIC,
                model_catalog.PROVIDER_OPENAI,
                model_catalog.PROVIDER_OPENROUTER,
            )
            else ""
        )

    def _driver_matches_selected_provider(self, driver) -> bool:
        return self._driver_manager.driver_matches_provider(
            driver, self._selected_provider()
        )

    def _assistant_label(self) -> str:
        target = self._next_chat
        if (
            target is not None
            and target.resume_id
            and not MainWindow._target_resume_resolution(self, target).known
        ):
            return "Assistant"
        provider = self._selected_provider()
        if provider == model_catalog.PROVIDER_OPENAI:
            return "GPT"
        if provider == model_catalog.PROVIDER_OPENROUTER:
            return "OpenRouter"
        return "Claude"

    def _sync_assistant_labels(self) -> None:
        self._composer.set_assistant_label(self._assistant_label())

    # --- goals ---

    # --- persisted layout toggles ---

    def _on_panel_toggle(self, btn: Gtk.ToggleButton, panel: Gtk.Widget, key: str) -> None:
        active = btn.get_active()
        panel.set_visible(active)
        self._ui_state.set(key, active)

    def _on_panel_toggle_column(self, btn: Gtk.ToggleButton, key: str) -> None:
        """Toggle the session column, header bar and all.

        Separate from _on_panel_toggle because the column is built after the
        button is wired, so it cannot be bound as a connect() argument.
        """
        active = btn.get_active()
        self._sessions_column.set_visible(active)
        sessions = getattr(self, "_sessions", None)
        if sessions is not None:
            sessions.set_visible(active)
        new_chat = getattr(self, "_compact_new_chat", None)
        if new_chat is not None:
            new_chat.set_visible(not active)
        if not getattr(self, "_layout_auto_change", False):
            self._ui_state.set(key, active)
            self._wide_sessions_visible = active

    def _set_compact_layout(self, compact: bool) -> None:
        self._compact_layout = compact
        if compact:
            self._wide_sessions_visible = self._sessions_btn.get_active()
        self._layout_auto_change = True
        try:
            visible = self._outer.get_show_sidebar()
            self._outer.set_collapsed(compact)
            self._outer.set_show_sidebar(visible)
            self._sessions_btn.set_active(False if compact else self._wide_sessions_visible)
        finally:
            self._layout_auto_change = False

    def _on_right_visibility_changed(self, split, _pspec) -> None:
        # A narrow overlay can be dismissed by clicking its scrim or Escape.
        # Keep its header toggle in sync so one click can reopen it.
        if not getattr(self, "_layout_auto_change", False):
            self._context_btn.set_active(split.get_show_sidebar())

    def _on_context_toggle(self, btn: Gtk.ToggleButton) -> None:
        active = btn.get_active()
        self._show_context = active
        self._outer.set_show_sidebar(active)
        self._ui_state.set("panel_context", active)
        if active and self._right_stack.get_visible_child_name() == "changes":
            self._changes.refresh()
        if active:
            MainWindow._refresh_capabilities(self)

    def _save_window_layout(self) -> None:
        """Persist window size + splitter positions. Cheap; called on close."""
        try:
            self._ui_state.update(
                window_width=self.get_width() or 1400,
                window_height=self.get_height() or 900,
                sessions_paned=self._middle_paned.get_position(),
            )
        except Exception:
            pass

    # --- missions tab ---

    def _on_right_pane_selected(self, selector, _pspec) -> None:
        index = selector.get_selected()
        if index < len(self._right_pages):
            self._right_stack.set_visible_child_name(self._right_pages[index])

    def _on_right_stack_child_changed(self, _stack, _pspec) -> None:
        page = self._right_stack.get_visible_child_name()
        selector = getattr(self, "_right_switcher", None)
        if selector is not None and page in self._right_pages:
            selector.set_selected(self._right_pages.index(page))
        if page == "missions":
            self._missions.on_tab_visible()
        elif page == "changes":
            self._changes.refresh()
        elif page == "capabilities":
            MainWindow._refresh_capabilities(self)

    def _refresh_capabilities(self, *_args) -> None:
        """Copy only the selected driver's reports; never initiate discovery."""
        pane = getattr(self, "_capabilities", None)
        if pane is None or getattr(self, "_destroyed", False):
            return
        driver = self._driver
        if driver is not None and (
            not self._driver_matches_selected_provider(driver)
            or not self._driver_matches_target_binding(driver)
        ):
            driver = None
        pane.set_driver(driver, self._selected_provider(), self._current_project_cwd())

    def _on_focus_missions_action(self, _action, _param) -> None:
        self.present()
        self._show_context = True
        self._outer.set_show_sidebar(True)
        self._context_btn.set_active(True)
        self._right_stack.set_visible_child_name("missions")

    # --- session selection ---

    @staticmethod
    def _driver_matches_project(driver, project) -> bool:
        """Require a live driver to belong to this exact local workspace."""

        if driver is None or project is None or project.read_only:
            return False
        driver_cwd = str(getattr(driver, "_cwd", "") or "")
        project_cwd = str(getattr(project, "cwd", "") or "")
        return bool(
            driver_cwd
            and project_cwd
            and canonical_cwd(driver_cwd) == canonical_cwd(project_cwd)
        )

    def _on_session_selected(self, _list, session) -> None:
        changes = getattr(self, "_changes", None)
        if changes is not None:
            changes.set_project(session.project if session is not None else None)
        # Whatever is (or isn't) open is what the hand-off button targets.
        self._shared.set_handoff_target(session)
        # Park the outgoing draft before anything re-points the view. Must
        # precede every return below, including the `session is None` one.
        prev_draft_key = self._draft_key
        self._drafts.stash(prev_draft_key, self._composer.current_text())
        # Hoisted with the stash: the guard below early-returns on EVERY chat
        # the user starts (_refresh_sidebar_for_live auto-selects the new row
        # 400ms after session-started), so a key update left beneath it meant a
        # started chat kept its `new:<cwd>` key for life.
        self._draft_key = draft_key(session) if session is not None else ""
        if session is None:
            self._stop_following()
            # Take the pooled-session banner down; it outlived the selection.
            self._composer.set_read_only(False)
            self._staged_permission_mode = ""
            self._staged_effort_key = ""
            self._staged_workflow_mode = ""
            self._goal_strip.clear_goal()
            progress = getattr(self, "_plan_progress", None)
            if progress is not None:
                progress.clear()
            self._main_stack.set_visible_child_name("welcome")
            self._plan.clear()
            MainWindow._reset_observed_agent_activity(self)
            return
        provider_resolution = session_providers.resolve_provider(
            session.session_id,
            session.path,
            conversation_store=self._conversation_perms,
        )
        # Defensive guard: if a sidebar refresh "re-selects" the row that
        # belongs to the currently-visible driver, this is not a user-initiated
        # switch — it's a status-dot refresh disguised as a click. Bail.
        if (
            self._driver is not None
            and self._driver.session_id == session.session_id
            and provider_resolution.known
            and self._driver_provider(self._driver)
            == provider_resolution.provider
            and self._driver_matches_selected_provider(self._driver)
            and MainWindow._driver_matches_project(
                self._driver,
                session.project,
            )
        ):
            # MIGRATE, do not duplicate: the composer keeps its text here, and
            # `take()` is a non-destructive get, so leaving the copy under the
            # old key lets the next New chat in that cwd resurrect it. A blank
            # stash evicts (see DraftBook).
            if self._draft_key and self._draft_key != prev_draft_key:
                self._drafts.stash(self._draft_key, self._composer.current_text())
                self._drafts.stash(prev_draft_key, "")
            self._refresh_goal_strip()
            MainWindow._refresh_execution_plan(self)
            self._main_stack.set_visible_child_name("transcript")
            return

        self._composer.set_text(self._drafts.take(self._draft_key))
        self._staged_permission_mode = ""
        self._staged_effort_key = ""
        self._staged_workflow_mode = ""

        # Per-chat provider: the SELECTED SESSION decides the backend, not the
        # global toggle. Sync the model/toggle to this session's provider so the
        # header reflects the chat you're viewing and the next send resumes the
        # right CLI (claude --resume vs codex exec resume). Skip for read-only
        # pool rows — they can't be resumed, so don't disturb the active model.
        session_provider = (
            provider_resolution.provider if provider_resolution.known else ""
        )
        live_candidate = self._driver_manager.driver_for_session(
            session.session_id
        )
        live = (
            live_candidate
            if not session.project.read_only
            and session_provider
            and live_candidate is not None
            and self._driver_provider(live_candidate) == session_provider
            and MainWindow._driver_matches_project(
                live_candidate,
                session.project,
            )
            else None
        )
        work_id = ""
        if not session.project.read_only and session_provider:
            self._adopt_session_provider(session_provider)
            coordinator = getattr(self, "_work_coordinator", None)
            if coordinator is not None:
                try:
                    work = coordinator.resolve_native_work(
                        session_provider,
                        session.session_id,
                        cwd=session.project.cwd,
                        model=self._model,
                    )
                    work_id = work.work_id
                except Exception as exc:
                    _log.warning(
                        "could not attach session %s to Work: %s",
                        session.session_id,
                        exc,
                    )
        # Concurrent sessions: switching away does NOT stop the previous
        # driver — it stays live in `_drivers`, finishing its turn / ready for
        # more. We just re-point the visible UI. If the session we're switching
        # TO has a live driver, bind it (and reflect its busy state); otherwise
        # unbind and stage a resume for the next send.
        route = route_session_selection(
            session,
            live,
            live_matches_provider=(
                live is not None and self._driver_matches_selected_provider(live)
            ),
            work_id=(
                str(getattr(live, "_helios_work_id", "") or "")
                if live is not None
                else work_id
            )
            or work_id,
            resume_provider=session_provider,
        )
        self._next_chat = route.target
        self._sync_assistant_labels()
        if route.live_driver is not None:
            # Bound to a live driver — its events drive the view; stop any
            # file-follow so we don't double-append.
            self._bind_visible_driver(route.live_driver)
            self._stop_following()
        else:
            # Resume by id for either backend — the provider was just adopted,
            # so the spawned driver class matches this session's transcript.
            self._bind_visible_driver(None)
            # No in-process driver — live-follow the file so an externally
            # driven (or remote-synced) session updates as it's written.
            if route.follow_transcript:
                self._start_following(session)

        # The selected session now defines the "current cwd": reflect its
        # permission mode and point the context editor at its project. Remote
        # pool sessions reference a cwd that doesn't exist locally — hide the
        # editor for those.
        self._sync_execution_control()
        self._context.set_project(
            None if session.project.read_only else session.project
        )
        MainWindow._refresh_capabilities(self)

        self._chat_toolbar.set_context_usage(0, 0)
        # Size the meter to the selected model's window even before a turn runs.
        self._chat_toolbar.set_context_model(self._model)
        # Show this session's ACTUAL current context fill immediately (read from
        # its transcript tail), rather than 0% until the next turn runs.
        self._load_context_fill_async(session)
        self._transcript.set_session(session)
        self._refresh_goal_strip()
        self._plan.set_session(session)
        MainWindow._refresh_execution_plan(self)
        if live is not None:
            MainWindow._sync_visible_agent_activity(self, live)
        # Re-show any messages still queued on this session's live driver —
        # the queue kept draining (or waiting) while we were elsewhere.
        if live is not None and live.is_accepting_input:
            remover = self._make_queued_remover(live)
            for qid, text in live.queued_messages():
                self._transcript.append_queued(qid, text, remover)
        self._chat_toolbar.set_visible(True)
        self._composer.set_visible(True)
        # Remote (pooled) sessions are view-only: their cwd/session-id don't
        # exist on this machine, so resuming would fail. Lock the composer.
        lock_reason = MainWindow._execution_target_lock_reason(self)
        ro = bool(lock_reason)
        read_only_note = {
            "read-only": (
                f"Read-only — session from {session.project.origin} "
                "(shared pool)."
            ),
            "conflict": (
                "Provider ownership conflicts across saved evidence; Helios "
                "will not guess how to resume this conversation."
            ),
            "unknown": (
                "Provider ownership is unknown; Helios will not guess how "
                "to resume this conversation."
            ),
        }.get(lock_reason, "")
        self._composer.set_read_only(
            ro,
            read_only_note,
        )
        self._main_stack.set_visible_child_name("transcript")
        # Now that this session is visible, present any question it had waiting
        # (a background question defers its modal until you switch to it).
        self._pump_questions()

    def _show_fresh_chat_ui(
        self,
        project,
        *,
        work_id: str = "",
        resume_id: str = "",
    ) -> None:
        """Stage a native chat, optionally as a participant in existing Work."""
        if project is not None and project.read_only:
            # Can't start a live chat against another machine's directory.
            self._toast("Remote pool session is view-only.")
            return
        if project is not None and _home_execution_locked(project.cwd):
            # Say it up front. This chat can inspect and answer, but cannot
            # change files; the mode was previously disclosed only in the
            # execution capsule.
            self._toast(
                "This chat is in $HOME, so permissions are read only. It can "
                "inspect and answer, but pick a project folder to change files."
            )
        if resume_id:
            resolution = MainWindow._resolve_resume_id(
                self,
                resume_id,
                project,
            )
            if (
                not resolution.known
                or resolution.provider != self._selected_provider()
            ):
                self._toast(
                    "Provider ownership for this Work participant could not "
                    "be verified, so Helios did not stage it."
                )
                return
        self._drafts.stash(self._draft_key, self._composer.current_text())
        # A fresh chat has no transcript on disk yet — nothing to hand off.
        self._staged_permission_mode = ""
        self._staged_effort_key = ""
        self._staged_workflow_mode = ""
        self._shared.set_handoff_target(None)
        # Don't stop the current session — it keeps running in the background
        # (registry). Just unbind the visible UI so the next send spawns a
        # fresh driver for this cwd.
        self._bind_visible_driver(None)
        self._stop_following()  # fresh chat isn't an on-disk session to follow
        self._next_chat = switch_work_participant(
            fresh_chat_target(project, work_id=work_id),
            resume_id,
            self._selected_provider() if resume_id else "",
        )
        # A ChatTarget has no session_id, so this keys on the cwd.
        self._draft_key = draft_key(self._next_chat)
        self._composer.set_text(self._drafts.take(self._draft_key))
        self._plan.set_live_pending(self._assistant_label())
        self._sync_execution_control()
        self._context.set_project(project)
        MainWindow._refresh_capabilities(self)
        changes = getattr(self, "_changes", None)
        if changes is not None:
            changes.set_project(project)
        self._chat_toolbar.set_context_usage(0, 0)
        self._chat_toolbar.set_context_model(self._model)
        self._transcript.show_live_session(cwd=project.cwd, model=self._model or "default")
        self._refresh_goal_strip()
        MainWindow._refresh_execution_plan(self)
        self._sync_assistant_labels()
        self._chat_toolbar.set_visible(True)
        self._composer.set_visible(True)
        # A fresh chat clears a pooled-session view lock. HOME remains
        # execution-locked to Plan by the central settings resolver.
        self._composer.set_read_only(False)
        self._clear_busy_ui()
        self._main_stack.set_visible_child_name("transcript")

    # --- live chat ---

    def _start_new_chat(self) -> None:
        """Header-button action: start a fresh chat in the configured default
        workspace.

        The configured default wins outright. Selecting a session re-points
        `_next_chat` so that session's sends go to its own cwd, but that must
        not then leak into the *next* new chat — a different folder is an
        explicit per-chat choice via `_start_new_chat_in_folder`
        (Ctrl+Shift+N), which does not route through here.

        With no default configured, fall back as before: the staged target,
        then the most recent executable project, then $HOME. $HOME stays last
        because it is clamped to read-only, so it must not be the common case.
        """
        try:
            configured = configured_default_cwd()
            if configured:
                project = ensure_local_project(configured)
            else:
                project = self._next_chat.project if self._next_chat else None
                if project is None or project.read_only:
                    project = (
                        _default_chat_project() or ensure_local_project(HOME_CWD)
                    )
        except OSError as e:
            self._toast(f"Couldn't prepare {default_cwd()}: {e}")
            return
        self._show_fresh_chat_ui(project)
        self._composer.grab_input_focus()

    def _start_new_chat_with_provider(self, provider: str) -> None:
        alias = self._model_for_provider(provider)
        if not alias:
            label = {
                model_catalog.PROVIDER_OPENAI: "OpenAI",
                model_catalog.PROVIDER_OPENROUTER: "OpenRouter",
            }.get(provider, "Claude")
            self._toast(f"No {label} models available — add a key in Settings → Providers.")
            self._sync_provider_toggle()
            return
        self._apply_model_choice(alias, quiet=True)
        self._start_new_chat()

    def _start_new_chat_in_folder(self) -> None:
        """Ctrl+Shift+N: pick a working directory, then start a fresh chat
        there. Replaces the old 'add project' flow — the folder just becomes
        the chat's cwd (and a chip on its row), not a navigation bucket."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose a working directory for the new chat")
        dialog.set_modal(True)

        def on_pick(dlg, result) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return  # user cancelled
            if folder is None:
                return
            path = folder.get_path()
            if not path:
                return
            try:
                project = ensure_local_project(path)
            except OSError as e:
                self._toast(f"Couldn't use {path}: {e}")
                return
            self._show_fresh_chat_ui(project)
            self._composer.grab_input_focus()

        dialog.select_folder(self, None, on_pick)

    def _work_execution_block_reason(self, work_id: str) -> str:
        """Return a fail-closed reason when a Work may not start another turn."""

        if not work_id:
            return (
                "Helios could not establish a bounded Work, so it did not "
                "send your message."
            )
        blocked = getattr(self, "_budget_blocked_work_ids", set())
        if work_id in blocked:
            return "This Work reached its execution budget. Start a new bounded Work."
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            return (
                "Helios could not verify this Work's execution state, so it "
                "did not send your message."
            )
        try:
            return str(coordinator.execution_block_reason(work_id) or "")
        except Exception as exc:
            _log.warning("could not verify Work execution budget: %s", exc)
            return (
                "Helios could not verify this Work's execution budget, so it "
                "did not send your message."
            )

    def _execution_guard_for_driver(self, drv) -> str:
        work_id = str(
            getattr(drv, "_helios_work_id", "")
            or getattr(drv, "_helios_work_id_hint", "")
            or ""
        )
        block_reason = MainWindow._work_execution_block_reason(self, work_id)
        if block_reason:
            return block_reason

        coordinator = getattr(self, "_work_coordinator", None)
        provider = str(
            getattr(drv, "_helios_participant_provider", "")
            or self._driver_provider(drv)
            or ""
        )
        try:
            participant = coordinator.participant(work_id, provider)
        except Exception as exc:
            _log.warning("could not verify Work participant binding: %s", exc)
            participant = None
        if participant is None:
            return (
                "Helios could not verify this Work participant binding. "
                "The message was not sent."
            )
        if (
            str(getattr(drv, "_helios_participant_id", "") or "")
            != participant.participant_id
            or getattr(drv, "_helios_participant_generation", None)
            != participant.generation
        ):
            return (
                "This Work participant binding changed. Reopen the current "
                "participant before sending another message."
            )
        driver_native_id = str(
            getattr(drv, "session_id", "")
            or getattr(drv, "_helios_expected_resume_id", "")
            or ""
        )
        if (
            participant.native_id
            and driver_native_id
            and participant.native_id != driver_native_id
        ):
            return (
                "This Work participant now points to a different native "
                "conversation. Reopen it before sending another message."
            )
        return ""

    def _reclaim_orphaned_execution_lanes(self) -> None:
        """Free execution lanes a Helios that exited mid-turn still holds.

        Startup only, and it must stay that way: a Helios killed mid-turn
        leaves its attempt 'running', and per-Work admission then refuses every
        later message in that chat with "already executing" — permanently,
        since the driver that would produce a terminal receipt died with the
        process. Nothing is live this early, so any running row is an orphan.
        """

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            return
        try:
            orphans = coordinator.reclaim_orphaned_execution_attempts()
        except Exception as exc:
            _log.warning("could not reclaim orphaned execution attempts: %s", exc)
            return
        for orphan in orphans:
            _log.warning(
                "released execution attempt %s orphaned by a previous Helios",
                orphan.attempt_id,
            )

    def _start_execution_attempt_for_driver(self, drv) -> tuple[str, str]:
        """Atomically reserve this driver's durable execution lane."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            return "", (
                "Helios execution admission is unavailable. "
                "The message was not sent."
            )
        work_id = str(getattr(drv, "_helios_work_id", "") or "")
        participant_id = str(
            getattr(drv, "_helios_participant_id", "") or ""
        )
        provider = str(
            getattr(drv, "_helios_participant_provider", "") or ""
        )
        generation = getattr(drv, "_helios_participant_generation", None)
        if not work_id or not participant_id or not provider or not generation:
            return "", (
                "Helios could not verify this Work participant before execution. "
                "The message was not sent."
            )
        metadata = {
            "model": str(getattr(drv, "model", "") or ""),
            "effort": str(
                getattr(drv, "effort_key", "")
                or getattr(drv, "_effort", "")
                or ""
            ),
            "transport": str(getattr(drv, "display_name", "") or ""),
        }
        # The lease keys on the driver's OWN cwd and mode, which are the exact
        # values it will execute with — not the composer's current selection,
        # which may already have moved on.
        workspace_root = str(getattr(drv, "cwd", "") or getattr(drv, "_cwd", "") or "")
        permission_mode = str(getattr(drv, "permission_mode", "") or "")
        try:
            attempt = coordinator.start_execution_attempt(
                work_id=work_id,
                participant_id=participant_id,
                provider=provider,
                participant_generation=int(generation),
                metadata=metadata,
                workspace_root=workspace_root,
                permission_mode=permission_mode,
            )
        except ExecutionAdmissionError as exc:
            return "", str(exc)
        except Exception as exc:
            _log.warning("could not reserve Helios execution lane: %s", exc)
            return "", (
                "Helios could not reserve the execution lane. "
                "The message was not sent."
            )
        return attempt.attempt_id, ""

    def _finish_execution_attempt_for_driver(
        self,
        _drv,
        attempt_id: str,
        status: str,
        terminal_reason: str,
    ) -> None:
        """Commit terminal accounting before the driver surfaces its result."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        coordinator.finish_execution_attempt(
            attempt_id,
            status=status,
            terminal_reason=terminal_reason,
        )

    def _record_execution_dispatch_for_driver(
        self,
        _drv,
        attempt_id: str,
        evidence: ExecutionDispatchEvidence,
    ) -> None:
        """Hash the exact wire prompt before any provider I/O can occur."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        MainWindow._verify_execution_native_binding(
            self,
            _drv,
            evidence.native_binding_id,
        )
        coordinator.record_execution_dispatch(
            attempt_id,
            wire_prompt_text=evidence.wire_prompt_text,
            provider_request_key=evidence.provider_request_key,
        )

    def _record_execution_acceptance_for_driver(
        self,
        _drv,
        attempt_id: str,
        evidence: ExecutionAcceptanceEvidence,
    ) -> None:
        """Persist provider-native turn identity without releasing the slot."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        MainWindow._verify_execution_native_binding(
            self,
            _drv,
            evidence.native_binding_id,
            attempt_id=attempt_id,
        )
        coordinator.record_execution_acceptance(
            attempt_id,
            accepted_turn_id=evidence.accepted_turn_id,
            provider_request_key=evidence.provider_request_key,
        )

    def _verify_execution_native_binding(
        self,
        drv,
        native_binding_id: str,
        *,
        attempt_id: str = "",
    ) -> None:
        """Verify dispatch against current state, receipts against admission.

        A rebind fences future sends, but must not discard a terminal receipt
        for the already admitted generation and leave its lane held forever.
        Historical evidence never retags the driver or changes the binding.
        """

        native_binding_id = str(native_binding_id or "")
        if not native_binding_id:
            return
        if getattr(drv, "_helios_identity_confirmed", False) is not True:
            raise RuntimeError("provider-native identity is not confirmed")
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        work_id = str(getattr(drv, "_helios_work_id", "") or "")
        provider = str(getattr(drv, "_helios_participant_provider", "") or "")
        if attempt_id:
            attempt = coordinator.store.get_execution_attempt(attempt_id)
            if attempt is None or (
                attempt.work_id != work_id
                or attempt.provider != provider
                or attempt.participant_id
                != str(getattr(drv, "_helios_participant_id", "") or "")
                or attempt.participant_generation
                != getattr(drv, "_helios_participant_generation", None)
            ):
                raise RuntimeError("execution receipt does not match admitted driver")
            if attempt.native_binding_id:
                if attempt.native_binding_id != native_binding_id:
                    raise RuntimeError("provider-native identity conflicts with admission")
                return
            # A first native identity may arrive after dispatch. It still
            # requires the current generation before attaching a late id.
        participant = coordinator.participant(work_id, provider)
        if participant is None:
            raise RuntimeError("Work participant is unavailable")
        if participant.native_id and participant.native_id != native_binding_id:
            raise RuntimeError("provider-native identity conflicts with Work binding")
        if not participant.native_id:
            participant = coordinator.bind_participant(
                work_id,
                provider,
                native_id=native_binding_id,
                model=str(getattr(drv, "model", "") or ""),
            )
        if (
            participant.participant_id
            != str(getattr(drv, "_helios_participant_id", "") or "")
            or participant.generation
            != getattr(drv, "_helios_participant_generation", None)
        ):
            raise RuntimeError("provider-native binding changed participant generation")

    def _record_execution_stop_for_driver(
        self,
        _drv,
        attempt_id: str,
        evidence: ExecutionStopEvidence,
    ) -> None:
        """Persist a typed cancellation acknowledgement and queue disposition."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        MainWindow._verify_execution_native_binding(
            self,
            _drv,
            str(evidence.acknowledgement.get("native_id") or ""),
            attempt_id=attempt_id,
        )
        coordinator.record_execution_stop(
            attempt_id,
            acknowledgement=evidence.acknowledgement or None,
            queue_disposition=evidence.queue_disposition,
        )

    def _finish_execution_attempt_with_evidence_for_driver(
        self,
        _drv,
        attempt_id: str,
        evidence: ExecutionTerminalEvidence,
    ) -> None:
        """Translate typed provider evidence into the durable storage schema."""

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None:
            raise RuntimeError("Helios execution accounting is unavailable")
        MainWindow._verify_execution_native_binding(
            self,
            _drv,
            evidence.native_id,
            attempt_id=attempt_id,
        )
        terminal_receipt = None
        if evidence.evidence_type in {"verified_rejection", "provider_terminal"}:
            terminal_receipt = {
                "evidence_type": evidence.evidence_type,
                "provider_status": evidence.provider_status,
            }
            if evidence.request_id:
                terminal_receipt["request_id"] = evidence.request_id
            if evidence.turn_id:
                terminal_receipt["turn_id"] = evidence.turn_id
            if evidence.response_id:
                terminal_receipt["response_id"] = evidence.response_id
            if evidence.native_id:
                terminal_receipt["native_id"] = evidence.native_id
        coordinator.finish_execution_attempt(
            attempt_id,
            status=evidence.status,
            terminal_reason=evidence.reason_code,
            terminal_receipt=terminal_receipt,
            usage=evidence.usage,
            cost_micro_usd=evidence.cost_micro_usd,
            stop_acknowledgement=evidence.stop_acknowledgement,
            queue_disposition=evidence.queue_disposition,
        )

    def _ensure_driver(self) -> bool:
        """Spawn a driver if one isn't already running. Returns True if ready."""
        target = self._next_chat
        if target is None:
            self._toast("Pick a project first.")
            return False
        coordinator = getattr(self, "_work_coordinator", None)
        if target.work_id:
            budget_block = MainWindow._work_execution_block_reason(
                self, target.work_id
            )
            if budget_block:
                self._toast(budget_block)
                return False
        viewed_resolution = MainWindow._target_resume_resolution(self, target)
        viewed_provider = (
            viewed_resolution.provider if viewed_resolution.known else ""
        )
        lock_reason = MainWindow._execution_target_lock_reason(self)
        if lock_reason:
            if lock_reason == "read-only":
                self._toast(
                    f"Read-only session from {target.project.origin} "
                    "(shared pool) — view only."
                )
            else:
                self._toast(
                    "Provider ownership is unknown or conflicted, so Helios "
                    "will not guess how to resume this conversation."
                )
            return False
        # Validate before even reusing a live driver: persisted state and a
        # provider-only match cannot authorize an exact OpenAI model.
        if not self._guard_openai_driver_activation():
            return False
        # `is_accepting_input`, not just `is_running`: a driver that was
        # wound down via end_input() may still be flushing its final turn —
        # alive, but a send would never reach it. Spawn fresh instead.
        if self._driver is not None and self._driver.is_accepting_input:
            if self._driver_manager.current_accepting_matches_provider(
                self._selected_provider()
            ):
                if self._driver_matches_target_binding(self._driver):
                    return True
                # Same provider but a retired generation/native binding. Keep
                # the selected target intact; it will be reactivated below.
                self._bind_visible_driver(None)
            else:
                # The header/model picker crossed providers after this driver
                # was bound. Keep the old session alive in the registry, but do
                # not let the next prompt go to the wrong backend.
                self._bind_visible_driver(None)
                if not self._stage_selected_work_participant():
                    return False
        target = self._next_chat
        if target is None:
            return False
        if not self._can_activate_target_binding(target):
            return False
        if target.work_id:
            if not self._stage_selected_work_participant():
                self._toast(
                    "The selected Work participant could not be verified, so "
                    "Helios did not send your message."
                )
                return False
        elif target.resume_id and coordinator is not None:
            source_resolution = MainWindow._target_resume_resolution(self, target)
            source_provider = source_resolution.provider
            if source_provider != self._selected_provider():
                try:
                    work = coordinator.resolve_native_work(
                        source_provider,
                        target.resume_id,
                        cwd=target.project.cwd,
                    )
                    self._next_chat = stage_work(target, work.work_id)
                    if not self._stage_selected_work_participant():
                        return False
                except Exception as exc:
                    _log.warning("could not normalize provider target: %s", exc)
                    self._toast(
                        "Helios could not establish a bounded Work for this "
                        "provider, so it did not send your message."
                    )
                    return False
        # Coordinator-less and failed-normalization paths must not bind the
        # selected provider to another provider's opaque native id.
        pre_fence_target = self._next_chat
        if not self._fence_mismatched_target_resume():
            return False
        if MainWindow._execution_target_lock_reason(self):
            return False
        ensured_work_id = self._ensure_target_work()
        if not ensured_work_id:
            # Fencing a foreign native id is a tentative spawn decision. If
            # Work creation fails, restore the user's view/resume identity so
            # a later healthy attempt can normalize it transactionally.
            self._next_chat = pre_fence_target
            self._toast(
                "Helios could not establish a bounded Work, so it did not "
                "send your message."
            )
            return False
        target = self._next_chat
        if target is None:
            return False
        budget_block = MainWindow._work_execution_block_reason(
            self, ensured_work_id
        )
        if budget_block:
            self._toast(budget_block)
            return False
        if MainWindow._execution_target_lock_reason(self):
            return False
        # A degraded provider choice can intentionally execute a sibling (for
        # example viewing GPT while only Claude is currently available). Make
        # that participant's own transcript authoritative before any live bind
        # or spawn, otherwise new Claude turns would be appended into the GPT
        # history and mislabeled as GPT.
        if viewed_provider and viewed_provider != self._selected_provider():
            selected_resume = str(target.resume_id or "")
            if selected_resume:
                selected_resolution = MainWindow._target_resume_resolution(
                    self,
                    target,
                )
                if (
                    not selected_resolution.known
                    or selected_resolution.provider != self._selected_provider()
                ):
                    return False
                MainWindow._show_native_execution_binding(
                    self,
                    target.project,
                    selected_resume,
                )
            else:
                MainWindow._show_fresh_execution_binding(self, target)
            target = self._next_chat
            if target is None or MainWindow._execution_target_lock_reason(self):
                return False
            if (
                self._driver is not None
                and self._driver.is_accepting_input
                and self._driver_matches_selected_provider(self._driver)
                and self._driver_matches_target_binding(self._driver)
            ):
                return True
        current_participant = (
            coordinator.participant(target.work_id, self._selected_provider())
            if coordinator is not None and target.work_id
            else None
        )
        work_live = self._driver_manager.driver_for_work_participant(
            target.work_id,
            self._selected_provider(),
            generation=(
                current_participant.generation
                if current_participant is not None
                else None
            ),
        )
        if work_live is not None:
            if work_live.session_id and self._sessions.reveal_session(
                work_live.session_id
            ):
                return True
            if work_live.session_id:
                MainWindow._show_native_execution_binding(
                    self,
                    target.project,
                    work_live.session_id,
                )
            else:
                MainWindow._show_fresh_execution_binding(self, target)
                self._bind_visible_driver(work_live)
            return True
        # If this Work's sibling participant is already live in-process, bind
        # it instead of spawning a duplicate process against the same native
        # session/thread.
        if target.resume_id:
            live = self._driver_manager.driver_for_session(target.resume_id)
            if (
                live is not None
                and live.is_accepting_input
                and self._driver_matches_selected_provider(live)
            ):
                if current_participant is not None:
                    tag_driver(live, current_participant)
                if self._sessions.reveal_session(target.resume_id):
                    return True
                MainWindow._show_native_execution_binding(
                    self,
                    target.project,
                    target.resume_id,
                )
                return True
        provider = self._selected_provider()
        spawn_permission_mode, spawn_effort_key = self._execution_settings_for_spawn(
            target, provider
        )
        spawn_workflow_mode = MainWindow._workflow_for_spawn(
            self,
            target,
            provider,
        )
        participant = None
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is not None and target.work_id:
            try:
                participant = coordinator.bind_participant(
                    target.work_id,
                    provider,
                    native_id=target.resume_id,
                    model=self._model,
                )
            except Exception as exc:
                _log.warning("could not stage Work participant: %s", exc)
                self._toast(
                    "Helios could not verify this Work participant, so it did "
                    "not start the provider."
                )
                return False
        # The selected model decides the backend: OpenAI ids run through the
        # Codex CLI, OpenRouter ids (vendor/model) through the OpenRouter
        # driver, everything else through claude. Same signal surface, so
        # the rest of the window doesn't care which one it's driving.
        _provider = model_catalog.provider_for(self._model)
        if _provider == model_catalog.PROVIDER_OPENAI:
            driver_cls = CodexAppServerDriver
        elif _provider == model_catalog.PROVIDER_OPENROUTER:
            driver_cls = OpenRouterDriver
        else:
            driver_cls = ClaudeCliDriver

        def make_driver():
            # Translate the effort key to the driver parameters.
            effort_kw: dict = {}
            if driver_cls is ClaudeCliDriver:
                if spawn_effort_key == "off":
                    effort_kw["max_thinking_tokens"] = 0
                elif spawn_effort_key:
                    effort_kw["effort"] = spawn_effort_key
            elif driver_cls is OpenRouterDriver and spawn_effort_key:
                # Staged, then validated against the pinned endpoint in
                # start(); the driver clears it if that endpoint does not
                # declare `reasoning`.
                effort_kw["effort"] = spawn_effort_key
            elif driver_cls is CodexAppServerDriver and spawn_effort_key:
                effort_kw["effort"] = spawn_effort_key
            elif spawn_effort_key:
                effort_kw["effort"] = spawn_effort_key
            if driver_cls is CodexAppServerDriver:
                effort_kw["workflow_mode"] = spawn_workflow_mode
            driver = driver_cls(
                cwd=target.project.cwd,
                model=self._model,
                permission_mode=spawn_permission_mode,
                resume_session_id=target.resume_id,
                **effort_kw,
            )
            driver._helios_staged_permission_mode = spawn_permission_mode
            driver._helios_staged_effort_key = spawn_effort_key
            driver._helios_staged_workflow_mode = spawn_workflow_mode
            driver._helios_identity_confirmed = False
            # A resume is exact, not a hint.  The runtime must report this
            # same native identity before Helios can bind or persist it.
            driver._helios_expected_resume_id = str(target.resume_id or "")
            driver.set_prompt_context_provider(self._goal_context_for_driver)
            driver.set_execution_guard(
                lambda candidate: MainWindow._execution_guard_for_driver(
                    self, candidate
                )
            )
            driver._helios_goal_session_id_hint = target.resume_id
            driver._helios_work_id_hint = target.work_id
            if participant is not None:
                tag_driver(driver, participant)
            else:
                driver._helios_work_id = target.work_id
                driver._helios_participant_provider = provider
            driver.set_execution_attempt_controller(
                lambda candidate: MainWindow._start_execution_attempt_for_driver(
                    self, candidate
                ),
                lambda candidate, attempt_id, status, reason: (
                    MainWindow._finish_execution_attempt_for_driver(
                        self,
                        candidate,
                        attempt_id,
                        status,
                        reason,
                    )
                ),
                record_dispatch=lambda candidate, attempt_id, evidence: (
                    MainWindow._record_execution_dispatch_for_driver(
                        self,
                        candidate,
                        attempt_id,
                        evidence,
                    )
                ),
                record_acceptance=lambda candidate, attempt_id, evidence: (
                    MainWindow._record_execution_acceptance_for_driver(
                        self,
                        candidate,
                        attempt_id,
                        evidence,
                    )
                ),
                record_stop=lambda candidate, attempt_id, evidence: (
                    MainWindow._record_execution_stop_for_driver(
                        self,
                        candidate,
                        attempt_id,
                        evidence,
                    )
                ),
                finish_with_evidence=lambda candidate, attempt_id, evidence: (
                    MainWindow._finish_execution_attempt_with_evidence_for_driver(
                        self,
                        candidate,
                        attempt_id,
                        evidence,
                    )
                ),
                record_contribution=lambda candidate, turn: (
                    self._work_coordinator.record_turn(candidate, turn)
                ),
            )
            if isinstance(driver, OpenRouterDriver):
                driver.set_work_budget_store(self._work_coordinator.store)
            return driver

        def connect_handlers(driver):
            # Per-driver handler ids so we can disconnect exactly this driver's
            # wires later — every driver keeps its handlers for its whole life;
            # the in-callback sender guard (`_drv_is_current`) ensures only
            # the visible driver's events touch the UI.
            common = {
                "session-started": self._on_session_started,
                "assistant-streaming": self._on_assistant_streaming,
                "turn-appended": self._on_turn_appended,
                "result": self._on_turn_result,
                "usage-updated": self._on_usage_updated,
                "rate-limit-updated": self._on_rate_limit_updated,
                "question-asked": self._on_question_asked,
                "queued-user-sent": self._on_queued_user_sent,
                "error": self._on_driver_error,
                "exited": self._on_driver_exited,
            }
            handlers = [
                driver.connect(name, common[name])
                for name in COMMON_DRIVER_SIGNALS
            ]
            if isinstance(driver, ClaudeCliDriver):
                handlers.extend(
                    [
                        # Claude-only: the CLI's `initialize` capability report
                        # and its native context gauge. No other provider
                        # defines these signals, and connecting one that a
                        # driver does not define raises.
                        driver.connect(
                            "capabilities-updated", self._on_cli_capabilities
                        ),
                        driver.connect(
                            "context-usage", self._on_cli_context_usage
                        ),
                        driver.connect(
                            "delivery-confirmed",
                            self._on_provider_delivery_confirmed,
                        ),
                        # Same handler as Codex — the driver emits the same
                        # payload shape, so the dock needs no provider branch.
                        driver.connect(
                            "agents-updated", self._on_native_agents_updated
                        ),
                        driver.connect(
                            "context-compacted", self._on_context_compacted
                        ),
                        driver.connect("spend-updated", self._on_spend_updated),
                        # v0.88.0 (2026-09-02 audit): the CLI's command
                        # inventory, hook lifecycle, withdrawn prompts, and
                        # mode changes the CLI made on its own (plan approved,
                        # setMode granted).
                        driver.connect(
                            "commands-updated", self._on_cli_commands_updated
                        ),
                        driver.connect("hook-event", self._on_hook_event),
                        driver.connect(
                            "question-cancelled", self._on_question_cancelled
                        ),
                        driver.connect(
                            "permission-mode-changed",
                            self._on_driver_permission_mode_changed,
                        ),
                        # Same handler and payload shape as Codex's
                        # turn/plan/updated: Claude's task list becomes the
                        # durable execution plan and the X/Y chip (Phase 3D).
                        driver.connect("plan-updated", self._on_native_plan_updated),
                        # Also the same handler as Codex: retry/compaction
                        # phases the stream cannot show.
                        driver.connect(
                            "activity-updated", self._on_native_activity_updated
                        ),
                    ]
                )
            if isinstance(driver, OpenRouterDriver):
                # OpenRouter uses the same turn/plan lifecycle contract, while
                # its compaction boundary belongs to Helios's own harness.
                handlers.extend(
                    [
                        driver.connect(
                            "turn-status-updated", self._on_native_turn_status
                        ),
                        driver.connect("plan-updated", self._on_native_plan_updated),
                        driver.connect(
                            "context-compacted", self._on_context_compacted
                        ),
                    ]
                )
            if isinstance(driver, CodexAppServerDriver):
                handlers.extend(
                    [
                        driver.connect(
                            "turn-status-updated", self._on_native_turn_status
                        ),
                        driver.connect("plan-updated", self._on_native_plan_updated),
                        driver.connect("diff-updated", self._on_native_diff_updated),
                        driver.connect("agents-updated", self._on_native_agents_updated),
                        driver.connect(
                            "native-goal-updated", self._on_native_goal_updated
                        ),
                        driver.connect(
                            "activity-updated", self._on_native_activity_updated
                        ),
                        driver.connect(
                            "mcp-status-updated", self._on_native_mcp_status_updated
                        ),
                        driver.connect(
                            "interaction-resolved", self._on_interaction_resolved
                        ),
                        driver.connect(
                            "transport-status-updated",
                            self._on_codex_transport_status,
                        ),
                        driver.connect(
                            "capability-drift", self._on_codex_capability_drift
                        ),
                        driver.connect(
                            "provider-notice", self._on_codex_provider_notice
                        ),
                        driver.connect(
                            "thread-forked", self._on_codex_thread_forked
                        ),
                        driver.connect(
                            "context-compacted", self._on_context_compacted
                        ),
                        driver.connect(
                            "prompt-accepted",
                            self._on_native_prompt_accepted,
                        ),
                    ]
                )
            if isinstance(
                driver,
                (ClaudeCliDriver, CodexAppServerDriver, OpenRouterDriver),
            ):
                handlers.append(
                    driver.connect("budget-exhausted", self._on_budget_exhausted)
                )
            return handlers

        try:
            self._driver_manager.start_new(make_driver, connect_handlers)
        except (ClaudeBinaryNotFound, DriverSpawnError) as e:
            self._toast(str(e))
            return False

        # DriverManager binds a new driver before its asynchronous native
        # session-started event arrives. Show Current immediately during that
        # startup interval; _on_session_started refreshes again after binding.
        self._sync_execution_control()

        # Leave `_next_chat` staged for resume. While this driver is alive the
        # `is_accepting_input` short-circuit at the top of _ensure_driver sends
        # to the live process and resume_id is never consulted. Once the driver
        # reports its live session id (`_on_session_started`) we sync resume_id
        # to it via stage_resume(), so if the process later dies (Stop button,
        # idle-reap, crash) the next send resumes the SAME conversation instead
        # of starting a fresh, context-free session. (Previously this cleared
        # resume_id, which is what made Stop / restart lose all context.)
        return True

    def _sync_live_ids(self) -> None:
        """Tell the sidebar which session ids have a live driver, so their dots
        reflect working/ready. Includes a starting driver once it has an id."""
        self._set_live_session_ids(self._driver_manager.live_ids())

    def _set_live_session_ids(self, ids: set[str]) -> None:
        sessions = getattr(self, "_sessions", None)
        if sessions is not None:
            sessions.set_live_session_ids(ids)

    # ── Background-driver lifecycle (policy constants on the class) ──

    def _reap_idle_drivers(self) -> bool:
        """Periodic tick: wind down background drivers idle past the policy
        window. EOF-based (end_input) so no interrupt marker lands in the
        JSONL and an in-flight turn still completes; the process exits on its
        own and `exited` does the registry cleanup."""
        if self._destroyed:
            return False  # drop the timer
        self._driver_manager.reap_idle()
        return True  # keep the timer alive

    def _bind_visible_driver(self, driver: ClaudeCliDriver | None) -> None:
        """Make `driver` the one whose events drive the UI, and reflect its
        current busy/idle state (used when switching back to a session that's
        still running in the background)."""
        previous = self._driver
        self._driver_manager.bind_current(driver)
        if previous is not driver:
            self._goal_strip.clear_native_goal()
            MainWindow._sync_visible_agent_activity(self, driver)
        checkpoint_pending = MainWindow._checkpoint_dispatch_pending_for(self, driver)
        if driver is not None:
            if not driver.is_busy and not checkpoint_pending:
                self._return_native_pending_user(driver)
            # An unresolved native anchor owns this Work's staged successors
            # and their FIFO insertion boundary. Do not expose them in the
            # global composer until acceptance/rejection/ambiguity resolves.
            if MainWindow._native_recovery_anchor_for(self, driver) is None:
                self._restore_native_unsent_drafts(driver)
            if getattr(driver, "_helios_execution_persistence_failed", False):
                driver._helios_execution_persistence_failed = False
                self._toast(
                    "Execution settings are active, but could not be saved for "
                    "restart.",
                    timeout=6,
                )
        if driver is not None and (driver.is_busy or checkpoint_pending):
            self._composer.set_busy(True)
            self._chat_toolbar.set_busy(True)
            self._activity.set_activity(STATE_THINKING)
        else:
            self._clear_busy_ui()
        # Central seam for the visible driver changing (bind/switch/unbind):
        # reflect that conversation's live execution settings, its durable
        # record, or the safe defaults when it has no bound child.
        self._sync_effort_sensitivity()
        self._sync_execution_control()

    def _observed_agent_scope(
        self,
        driver,
        root_turn_id: str = "",
    ) -> AgentActivityScope | None:
        """Build the exact Work/root-turn/root-actor observation owner."""

        # One scope builder for both observing providers. The provider
        # id must come from the driver's type, not the current toggle: a
        # background driver can report while another provider is selected, and
        # attributing its actors to that provider would cross the causal fence.
        if isinstance(driver, CodexAppServerDriver):
            provider = model_catalog.PROVIDER_OPENAI
        elif isinstance(driver, ClaudeCliDriver):
            provider = model_catalog.PROVIDER_ANTHROPIC
        else:
            return None
        scope = AgentActivityScope(
            provider=provider,
            work_id=str(
                getattr(driver, "_helios_work_id", "")
                or getattr(driver, "_helios_work_id_hint", "")
                or ""
            ),
            root_turn_id=str(
                root_turn_id
                or getattr(driver, "observed_agent_root_turn_id", "")
                or ""
            ),
            root_actor_id=str(getattr(driver, "session_id", "") or ""),
        )
        return scope if scope.is_valid else None

    def _refresh_router_dispatch_async(self) -> bool:
        """Re-ask the broker whether it can dispatch, off the main thread.

        The answer is read on the GTK main loop when a session launches, so it
        has to be cached; a cache with no refresh is a cache that can be wrong
        forever. Returns True to keep the timer armed.
        """
        if self._destroyed:
            return False
        threading.Thread(
            target=refresh_dispatch_available,
            name="helios-router-dispatch",
            daemon=True,
        ).start()
        return True

    def _on_spend_updated(self, driver, snapshot) -> None:
        """Show what a delegating turn actually cost, beside who is running it.

        Only for the visible driver: the dock projects one Work, and a
        background session's totals appearing under another Work's actors would
        be worse than showing nothing.
        """
        dock = getattr(self, "_agent_dock", None)
        if dock is None or driver is not self._driver:
            return
        dock.set_spend(snapshot)

    def _reset_observed_agent_activity(self) -> None:
        model = getattr(self, "_agent_activity", None)
        dock = getattr(self, "_agent_dock", None)
        plan = getattr(self, "_plan", None)
        if model is None or dock is None or plan is None:
            return
        snapshot = model.reset()
        dock.set_snapshot(snapshot)
        plan.show_agent_activity(snapshot)

    def _begin_observed_agent_scope(self, driver, root_turn_id: str) -> None:
        model = getattr(self, "_agent_activity", None)
        dock = getattr(self, "_agent_dock", None)
        plan = getattr(self, "_plan", None)
        if model is None or dock is None or plan is None:
            return
        scope = MainWindow._observed_agent_scope(self, driver, root_turn_id)
        if scope is None:
            MainWindow._reset_observed_agent_activity(self)
            return
        snapshot = model.begin_scope(scope)
        dock.set_snapshot(snapshot)
        plan.show_agent_activity(snapshot)

    def _sync_visible_agent_activity(self, driver) -> None:
        """Reproject a live Codex driver's current observed turn on rebind."""

        model = getattr(self, "_agent_activity", None)
        dock = getattr(self, "_agent_dock", None)
        plan = getattr(self, "_plan", None)
        if model is None or dock is None or plan is None:
            return
        snapshot = model.reset()
        if driver is not None:
            scope = MainWindow._observed_agent_scope(self, driver)
            reader = getattr(driver, "observed_agent_snapshot", None)
            if scope is not None and callable(reader):
                model.begin_scope(scope)
                snapshot = model.observe(scope, reader())
        dock.set_snapshot(snapshot)
        plan.show_agent_activity(snapshot)

    def _execution_change_pending_for(self, driver) -> bool:
        """Whether ``driver`` is waiting for an execution-setting ACK."""

        if driver is None:
            return False
        return (
            driver in getattr(self, "_permission_changes", {})
            or driver in getattr(self, "_effort_changes", {})
            or driver in getattr(self, "_workflow_changes", {})
        )

    def _checkpoint_dispatch_for(self, drv) -> _CheckpointDispatch | None:
        """Return the current unsent checkpoint dispatch for ``drv``."""

        if drv is None:
            return None
        pending = getattr(self, "_pending_checkpoint_dispatches", None)
        if not isinstance(pending, dict):
            return None
        entry = pending.get(id(drv))
        if not isinstance(entry, _CheckpointDispatch) or entry.driver is not drv:
            return None
        return entry

    def _checkpoint_dispatch_pending_for(self, drv) -> bool:
        return MainWindow._checkpoint_dispatch_for(self, drv) is not None

    def _begin_checkpoint_dispatch(self, drv, text: str) -> int:
        """Install a new one-shot dispatch generation for ``drv``."""

        generation = int(getattr(self, "_checkpoint_dispatch_generation", 0)) + 1
        self._checkpoint_dispatch_generation = generation
        pending = getattr(self, "_pending_checkpoint_dispatches", None)
        if not isinstance(pending, dict):
            pending = {}
            self._pending_checkpoint_dispatches = pending
        target = getattr(self, "_next_chat", None)
        target_project = getattr(target, "project", None)
        pending[id(drv)] = _CheckpointDispatch(
            drv,
            generation,
            text,
            str(getattr(drv, "provider", "") or ""),
            str(
                getattr(drv, "_helios_work_id", "")
                or getattr(drv, "_helios_work_id_hint", "")
                or getattr(target, "work_id", "")
                or ""
            ),
            canonical_cwd(
                str(
                    getattr(target_project, "cwd", "")
                    or getattr(drv, "_cwd", "")
                    or ""
                )
            ),
        )
        return generation

    def _checkpoint_dispatch_matches_target(self, drv) -> bool:
        """Verify the staged provider/Work/cwd without requiring a native id."""

        entry = MainWindow._checkpoint_dispatch_for(self, drv)
        target = getattr(self, "_next_chat", None)
        if entry is None or target is None:
            return False
        selected_provider = str(self._selected_provider() or "")
        current_work_id = str(getattr(target, "work_id", "") or "")
        current_cwd = canonical_cwd(
            str(getattr(getattr(target, "project", None), "cwd", "") or "")
        )
        return (
            (not entry.provider or entry.provider == selected_provider)
            and entry.work_id == current_work_id
            and entry.cwd == current_cwd
        )

    def _take_checkpoint_dispatch(
        self,
        drv,
        generation: int | None = None,
    ) -> _CheckpointDispatch | None:
        """Atomically claim/cancel the matching dispatch on the GTK thread."""

        entry = MainWindow._checkpoint_dispatch_for(self, drv)
        if entry is None or (
            generation is not None and entry.generation != generation
        ):
            return None
        self._pending_checkpoint_dispatches.pop(id(drv), None)
        return entry

    def _restore_checkpoint_dispatch_text(self, drv, text: str) -> bool:
        """Return one unsent checkpoint-owned prompt to its visible/background owner."""

        if not text:
            return False
        if self._destroyed or not self._drv_is_current(drv):
            drafts = getattr(self, "_native_unsent_drafts", None)
            if not isinstance(drafts, dict):
                drafts = {}
                self._native_unsent_drafts = drafts
            bucket = drafts.setdefault(MainWindow._native_draft_key(drv), [])
            bucket.append(text)
            return True
        existing = self._composer.current_text().strip()
        self._composer.set_text(f"{text}\n\n{existing}" if existing else text)
        return True

    def _restore_blocked_execution_send(self, text: str) -> bool:
        """Restore a submit that could not be routed to an active driver.

        Composer clears after its synchronous ``send`` signal returns, so the
        restoration must run on the next main-loop tick.  Preserve anything
        the user managed to type meanwhile rather than overwriting it.
        """

        if self._destroyed:
            return False
        current = self._composer.current_text().strip()
        restored = text if not current else f"{text}\n\n{current}"
        self._composer.set_text(restored)
        self._composer.grab_input_focus()
        return False

    def _on_composer_send(self, _composer, text: str) -> None:
        # Registered commands are control-plane actions. Resolve them before
        # checkpoints, execution admission, transcript insertion, and prompt
        # wrapping so GPT can never receive `/compact` as ordinary prose.
        if MainWindow._maybe_dispatch_registered_command(self, text):
            return
        # A permission/reasoning mutation and a turn must be ordered. Sending
        # under the old policy while the new one is merely spinning would be a
        # dangerous UI lie. Keep the text and ask for a retry after the bounded
        # provider acknowledgement completes.
        drv = self._driver
        if self._restore_in_flight:
            # The other half of the rewind exclusion. Refusing a restore while
            # a turn runs is only useful if a turn also cannot start while a
            # restore runs — otherwise the same mixed worktree state appears,
            # just with the writes in the opposite order. `_on_composer_send`
            # is the only path that starts a turn from an idle driver, and a
            # restore is only ever started when the driver IS idle, so gating
            # here closes the window (the queued-message auto-send fires from
            # a turn completing, which requires a turn to have been running).
            GLib.idle_add(self._restore_blocked_execution_send, text)
            self._toast(
                "A file restore is still running — your message was kept. "
                "Send it again in a moment."
            )
            return
        if self._execution_change_pending_for(drv):
            GLib.idle_add(self._restore_blocked_execution_send, text)
            self._toast(
                "Execution settings are still applying — your message was kept."
            )
            return
        # Mid-turn submit → queue on the driver (visible as a "Queued" row;
        # auto-sent, one per turn, as results land). Never blocks typing.
        checkpoint_pending = MainWindow._checkpoint_dispatch_pending_for(self, drv)
        queued_pending = bool(
            drv is not None
            and hasattr(drv, "queued_messages")
            and drv.queued_messages()
        )
        if (
            drv is not None
            and (drv.is_busy or checkpoint_pending or queued_pending)
            and drv.is_accepting_input
            and self._driver_matches_selected_provider(drv)
        ):
            lock_reason = MainWindow._execution_target_lock_reason(self)
            identity_confirmed = (
                getattr(drv, "_helios_identity_confirmed", True) is True
            )
            target_matches = (
                MainWindow._checkpoint_dispatch_matches_target(self, drv)
                if checkpoint_pending
                else self._driver_matches_target_binding(drv)
            )
            if (
                (not identity_confirmed and not checkpoint_pending)
                or lock_reason
                or not target_matches
            ):
                GLib.idle_add(self._restore_blocked_execution_send, text)
                self._toast(
                    (
                        "Helios is still verifying the native conversation — "
                        "your message was kept."
                    )
                    if not identity_confirmed
                    else (
                        "The selected execution target changed or could not be "
                        "verified — your message was kept."
                    )
                )
                return
            budget_block = MainWindow._execution_guard_for_driver(self, drv)
            if budget_block:
                GLib.idle_add(self._restore_blocked_execution_send, text)
                self._toast(budget_block)
                return
            if MainWindow._steer_running_turn(self, drv, text):
                return
            qid = drv.queue_user_text(text)
            self._transcript.append_queued(qid, text, self._make_queued_remover(drv))
            return
        if not self._ensure_driver():
            GLib.idle_add(self._restore_blocked_execution_send, text)
            return
        # `_ensure_driver` may have rebound a sibling that was working in the
        # background. Re-enter the normal queue path instead of attempting a
        # direct send that the busy backend will reject (and must not record).
        drv = self._driver
        checkpoint_pending = MainWindow._checkpoint_dispatch_pending_for(self, drv)
        queued_pending = bool(
            drv is not None
            and hasattr(drv, "queued_messages")
            and drv.queued_messages()
        )
        if drv is not None and (drv.is_busy or checkpoint_pending or queued_pending):
            lock_reason = MainWindow._execution_target_lock_reason(self)
            target_matches = (
                MainWindow._checkpoint_dispatch_matches_target(self, drv)
                if checkpoint_pending
                else self._driver_matches_target_binding(drv)
            )
            if (
                not drv.is_accepting_input
                or (
                    getattr(drv, "_helios_identity_confirmed", True) is not True
                    and not checkpoint_pending
                )
                or not self._driver_matches_selected_provider(drv)
                or not target_matches
                or lock_reason
            ):
                GLib.idle_add(self._restore_blocked_execution_send, text)
                self._toast(
                    "The selected execution target changed or could not be "
                    "verified — your message was kept."
                )
                return
            budget_block = MainWindow._execution_guard_for_driver(self, drv)
            if budget_block:
                GLib.idle_add(self._restore_blocked_execution_send, text)
                self._toast(budget_block)
                return
            if MainWindow._steer_running_turn(self, drv, text):
                return
            qid = drv.queue_user_text(text)
            self._transcript.append_queued(qid, text, self._make_queued_remover(drv))
            return
        if drv is None:
            GLib.idle_add(self._restore_blocked_execution_send, text)
            return
        # The pre-turn checkpoint is now the sole owner of ``text``.  Do not
        # create a visible/native/identity claim until its generation wins: Stop
        # must be able to cancel this interval without undoing provider state.
        self._composer.set_busy(True)
        self._chat_toolbar.set_busy(True)
        self._activity.set_activity(STATE_THINKING)

        def dispatch() -> None:
            """Hand the message to the driver. Runs on the main loop."""
            if self._destroyed:
                return
            budget_block = MainWindow._execution_guard_for_driver(self, drv)
            if budget_block:
                MainWindow._preserve_driver_queue(
                    self,
                    drv,
                    leading_texts=(text,),
                )
                if self._drv_is_current(drv):
                    self._clear_busy_ui()
                self._toast(budget_block)
                return
            # We're now driving this session — the driver owns the view; stop
            # any file-follow so turns aren't appended twice. A background
            # dispatch records natively/into the Work ledger and must not touch
            # whichever transcript the user switched to during the snapshot.
            if self._drv_is_current(drv):
                self._stop_following()
            # Stage every provider until its own acceptance boundary. Native
            # GPT acknowledges asynchronously through ``prompt-accepted``;
            # Claude and OpenRouter return an explicit synchronous delivery
            # result. Staging established non-native sessions too prevents a
            # pre-wire rejection from leaving a false transcript bubble.
            native_pending = isinstance(drv, CodexAppServerDriver)
            identity_pending = not native_pending and not str(
                getattr(drv, "session_id", "") or ""
            )
            if native_pending:
                self._native_pending_user[id(drv)] = (drv, text)
            else:
                self._identity_pending_user[id(drv)] = (drv, text)
            admissions = getattr(self, "_provider_dispatch_in_progress", None)
            if not isinstance(admissions, set):
                admissions = set()
                self._provider_dispatch_in_progress = admissions
            admissions.add(id(drv))
            try:
                delivery = drv.send_user_text(text)
            finally:
                admissions.discard(id(drv))
            explicit = delivery if isinstance(delivery, MessageDelivery) else None

            denied = explicit is not None and (
                explicit.rejected or explicit.uncertain
            )
            legacy_denied = explicit is None and not drv.is_busy
            if denied or legacy_denied:
                # The synchronous error signal deliberately leaves FIFO
                # recovery to this frame while its admission marker is set.
                # A native UNKNOWN head is quarantined, never replayed; a
                # non-native uncertain write is quarantined by the durable
                # dispatch receipt too. Only a definite rejection restores
                # FIRST as sendable text.
                if native_pending:
                    pending_text = MainWindow._take_native_pending_user(self, drv)
                    native_state = MainWindow._native_delivery_state(drv)
                    replayable = bool(pending_text) and (
                        (explicit is not None and explicit.rejected)
                        or (
                            explicit is None
                            and native_state
                            in (
                                "",
                                NATIVE_DELIVERY_LOCAL,
                                NATIVE_DELIVERY_REJECTED,
                            )
                        )
                    )
                else:
                    pending_text = MainWindow._take_identity_pending_user(self, drv)
                    replayable = bool(pending_text) and not (
                        explicit is not None and explicit.uncertain
                    )
                    if (
                        pending_text
                        and explicit is not None
                        and explicit.uncertain
                    ):
                        uncertain = getattr(self, "_uncertain_pending_user", None)
                        if not isinstance(uncertain, dict):
                            uncertain = {}
                            self._uncertain_pending_user = uncertain
                        uncertain[id(drv)] = (drv, pending_text)
                if pending_text and not replayable:
                    _log.warning(
                        "quarantined acceptance-unknown %s prompt; inspect "
                        "durable execution recovery before retrying",
                        getattr(drv, "provider", "provider"),
                    )
                MainWindow._preserve_driver_queue(
                    self,
                    drv,
                    leading_texts=(pending_text,) if replayable else (),
                )
                if not drv.is_busy and self._drv_is_current(drv):
                    self._clear_busy_ui()
                return

            if not native_pending:
                if explicit is not None and explicit.accepted:
                    if not identity_pending:
                        MainWindow._accept_identity_pending_user(self, drv)
                elif explicit is None and drv.is_busy and not identity_pending:
                    # An established Claude/OpenRouter session has accepted
                    # synchronously. New sessions remain provisional until
                    # their provider-native identity is confirmed.
                    MainWindow._accept_identity_pending_user(self, drv)

        # The send is deliberately sequenced BEHIND the snapshot. Firing them
        # concurrently left no ordering guarantee that `git add -A` had even
        # begun before the agent started writing files, so a checkpoint could
        # capture a half-applied turn — an undo that restores to a state that
        # never existed is worse than no undo. The snapshot is a few
        # milliseconds on a normal repo, and the composer is already showing
        # the message and a busy spinner by this point.
        self._capture_checkpoint(drv, text, dispatch)

    def _make_queued_remover(self, drv):
        """✕ callback for a queued row. Captures the driver (not
        self._driver — the user may have switched sessions by click time)."""

        def _remove(qid: int) -> None:
            if drv.remove_queued(qid):
                self._transcript.remove_queued(qid)

        return _remove

    def _on_queued_user_sent(self, drv, qid: int, text: str) -> None:
        """A queued message just went out as its own turn. For the visible
        session, promote its row to a real user bubble and re-enter the busy
        state; a background session updates its on-disk transcript, which the
        view reloads on switch-back."""
        if not isinstance(drv, CodexAppServerDriver):
            coordinator = getattr(self, "_work_coordinator", None)
            if coordinator is not None:
                coordinator.record_user_message(drv, text)
        # Ledger recorded above; the promotion to a live bubble + busy state is
        # UI and must not run after close.
        if self._destroyed or not self._drv_is_current(drv):
            return
        self._transcript.remove_queued(qid)
        user_turn = Turn(role="user")
        user_turn.text_parts.append(text)
        self._transcript.append_turn(user_turn)
        self._plan.append_turn(user_turn)
        self._composer.set_busy(True)
        self._chat_toolbar.set_busy(True)
        self._activity.set_activity(STATE_THINKING)

    def _steer_running_turn(self, drv, text: str) -> bool:
        """Send `text` INTO the live turn instead of queueing it after.

        Queueing means a correction only reaches the model once it has already
        finished going the wrong way. Codex App Server exposes `turn/steer`,
        so for that provider a mid-turn message adjusts the running turn. Any
        provider without a steer contract (Claude, OpenRouter) keeps queueing —
        the driver simply has no `steer_user_text`.
        """

        steer = getattr(drv, "steer_user_text", None)
        if not callable(steer):
            return False
        try:
            if not steer(text):
                return False
        except Exception as exc:
            _log.warning("could not steer the running turn: %s", exc)
            return False
        # Same acceptance boundary as an ordinary native send: the bubble is
        # committed by ``prompt-accepted`` once App Server confirms, never on
        # dispatch. A declined steer falls back to the queue inside the driver.
        self._native_pending_user[id(drv)] = (drv, text)
        return True

    def _on_native_prompt_accepted(self, drv, text: str) -> None:
        """Commit a GPT user contribution only after native turn acceptance."""

        anchor = MainWindow._native_recovery_anchor_for(self, drv)
        if anchor is not None and anchor.text == text:
            # FIRST was accepted. Its successors are already recoverable at
            # the saved boundary; discard only the conditional insertion.
            MainWindow._take_native_recovery_anchor(self, drv)
            if not self._destroyed and self._drv_is_current(drv):
                MainWindow._restore_native_unsent_drafts(self, drv)
        pending = self._native_pending_user.get(id(drv))
        accepted_pending = pending == (drv, text)
        if accepted_pending:
            # Pending-state cleanup is durable and must run even during close.
            self._native_pending_user.pop(id(drv), None)
        # The canonical ledger must commit before a visible bubble can claim
        # the prompt was accepted.
        coordinator = getattr(self, "_work_coordinator", None)
        receipt = None
        if coordinator is not None:
            receipt = coordinator.record_user_message(drv, text)
        if accepted_pending:
            # The visible user bubble is UI — gate it on the window being alive.
            if not self._destroyed and self._drv_is_current(drv):
                self._append_visible_user_turn(text)
        if receipt is not None:
            MainWindow._acknowledge_accepted_send(self, drv)

    def _on_provider_delivery_confirmed(self, drv) -> None:
        """Promote one quarantined CLI prompt after native provider proof."""

        MainWindow._accept_uncertain_pending_user(self, drv)

    def _append_visible_user_turn(self, text: str) -> None:
        user_turn = Turn(role="user")
        user_turn.text_parts.append(text)
        self._transcript.append_turn(user_turn)
        self._plan.append_turn(user_turn)

    def _acknowledge_accepted_send(self, drv) -> None:
        """Run the send flourish only for a live, visible, durable ACK."""

        if self._destroyed or not self._drv_is_current(drv):
            return
        composer = getattr(self, "_composer", None)
        acknowledge = getattr(composer, "acknowledge_accepted", None)
        if callable(acknowledge):
            acknowledge()

    def _accept_identity_pending_user(self, drv) -> bool:
        """Commit a staged non-native turn after provider acceptance.

        New Claude sessions call this after identity proof. Established Claude
        and OpenRouter sessions call it after their synchronous send boundary.
        """

        pending_users = getattr(self, "_identity_pending_user", None)
        if not isinstance(pending_users, dict):
            return False
        pending = pending_users.get(id(drv))
        if pending is None or pending[0] is not drv:
            return False
        pending_users.pop(id(drv), None)
        text = pending[1]
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is not None:
            try:
                coordinator.record_user_message(drv, text)
            except Exception as exc:
                _log.warning("could not record accepted first user turn: %s", exc)
        if not self._destroyed and self._drv_is_current(drv):
            self._append_visible_user_turn(text)
        return True

    def _return_identity_pending_user(self, drv) -> bool:
        """Restore a staged non-native turn that was not accepted."""

        text = MainWindow._take_identity_pending_user(self, drv)
        if text is None:
            return False
        if self._destroyed or not self._drv_is_current(drv):
            drafts = getattr(self, "_native_unsent_drafts", None)
            if not isinstance(drafts, dict):
                drafts = {}
                self._native_unsent_drafts = drafts
            bucket = drafts.setdefault(self._native_draft_key(drv), [])
            bucket.append(text)
            return True
        existing = self._composer.current_text().strip()
        # A synchronous denial can arrive before Gtk clears the just-submitted
        # composer. Do not duplicate text that is already still present.
        if existing != text.strip():
            self._composer.set_text(f"{text}\n\n{existing}" if existing else text)
        return True

    def _take_identity_pending_user(self, drv) -> str | None:
        """Claim a provisional first turn without choosing a recovery surface."""

        pending_users = getattr(self, "_identity_pending_user", None)
        if not isinstance(pending_users, dict):
            return None
        pending = pending_users.get(id(drv))
        if pending is None or pending[0] is not drv:
            return None
        pending_users.pop(id(drv), None)
        return pending[1]

    def _take_uncertain_pending_user(self, drv) -> str | None:
        """Consume one non-sendable provider-ambiguous direct prompt."""

        pending_users = getattr(self, "_uncertain_pending_user", None)
        if not isinstance(pending_users, dict):
            return None
        pending = pending_users.get(id(drv))
        if pending is None or pending[0] is not drv:
            return None
        pending_users.pop(id(drv), None)
        return pending[1]

    def _accept_uncertain_pending_user(self, drv) -> bool:
        """Promote an ambiguous prompt once provider activity proves receipt."""

        text = MainWindow._take_uncertain_pending_user(self, drv)
        if text is None:
            return False
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is not None:
            try:
                coordinator.record_user_message(drv, text)
            except Exception as exc:
                _log.warning("could not record provider-confirmed user turn: %s", exc)
        if not self._destroyed and self._drv_is_current(drv):
            self._append_visible_user_turn(text)
        return True

    def _quarantine_uncertain_queue_delivery(self, drv) -> bool:
        """Consume one ambiguous queued prompt without restoring or replaying it."""

        quarantine = getattr(drv, "_quarantine_uncertain_queue_delivery", None)
        delivery = quarantine() if callable(quarantine) else None
        if delivery is None:
            return False
        qid, _text = delivery
        if not self._destroyed and self._drv_is_current(drv):
            self._transcript.remove_queued(qid)
        _log.warning("quarantined provider-ambiguous queued prompt")
        return True

    def _preserve_identity_pending_user(self, drv) -> bool:
        """Move a provisional Claude turn to its in-memory Work draft bucket."""

        text = MainWindow._take_identity_pending_user(self, drv)
        if text is None:
            return False
        drafts = getattr(self, "_native_unsent_drafts", None)
        if not isinstance(drafts, dict):
            drafts = {}
            self._native_unsent_drafts = drafts
        bucket = drafts.setdefault(self._native_draft_key(drv), [])
        bucket.append(text)
        return True

    def _return_native_pending_user(self, drv) -> bool:
        """Return a native prompt that failed before acceptance to its author."""

        state = MainWindow._native_delivery_state(drv)
        if state == NATIVE_DELIVERY_WIRE:
            # Resolution is still possible; keep pending ownership + anchor so
            # acceptance can discard it or a definite rejection can restore it.
            return False
        text = MainWindow._take_native_pending_user(self, drv)
        if text is None:
            return False
        anchor = MainWindow._take_native_recovery_anchor(self, drv)
        if state not in (
            "",
            NATIVE_DELIVERY_IDLE,
            NATIVE_DELIVERY_LOCAL,
            NATIVE_DELIVERY_REJECTED,
        ):
            # Wire/accepted/unknown FIRST is not known unsent. Consume it from
            # pending ownership, but never make it a replayable draft.
            _log.warning(
                "quarantined native prompt with delivery state %s",
                state,
            )
            return False
        if anchor is not None and anchor.text == text:
            return MainWindow._restore_native_recovery_anchor_text(
                self,
                drv,
                anchor,
            )
        # Preserve the un-accepted prompt in the non-UI draft bucket when the
        # driver isn't visible OR the window is closing — in both cases we must
        # NOT write the (finalizing) composer. Only a live, current driver
        # returns its prompt straight to the composer.
        if self._destroyed or not self._drv_is_current(drv):
            drafts = getattr(self, "_native_unsent_drafts", None)
            if not isinstance(drafts, dict):
                drafts = {}
                self._native_unsent_drafts = drafts
            key = self._native_draft_key(drv)
            bucket = drafts.setdefault(key, [])
            bucket.append(text)
            return True
        existing = self._composer.current_text().strip()
        self._composer.set_text(f"{text}\n\n{existing}" if existing else text)
        return True

    def _take_native_pending_user(self, drv) -> str | None:
        """Claim an unaccepted native prompt without restoring it yet."""

        pending_users = getattr(self, "_native_pending_user", None)
        if not isinstance(pending_users, dict):
            return None
        pending = pending_users.get(id(drv))
        if pending is None or pending[0] is not drv:
            return None
        pending_users.pop(id(drv), None)
        return pending[1]

    def _preserve_native_pending_user(self, drv) -> bool:
        """Move an unaccepted native prompt to the non-UI draft bucket."""
        state = MainWindow._native_delivery_state(drv)
        if state == NATIVE_DELIVERY_WIRE:
            return False
        text = MainWindow._take_native_pending_user(self, drv)
        if text is None:
            return False
        anchor = MainWindow._take_native_recovery_anchor(self, drv)
        if state not in (
            "",
            NATIVE_DELIVERY_IDLE,
            NATIVE_DELIVERY_LOCAL,
            NATIVE_DELIVERY_REJECTED,
        ):
            _log.warning(
                "quarantined native prompt with delivery state %s",
                state,
            )
            return False
        if anchor is not None and anchor.text == text:
            return MainWindow._restore_native_recovery_anchor_text(
                self,
                drv,
                anchor,
                force_background=True,
            )
        drafts = getattr(self, "_native_unsent_drafts", None)
        if not isinstance(drafts, dict):
            drafts = {}
            self._native_unsent_drafts = drafts
        bucket = drafts.setdefault(self._native_draft_key(drv), [])
        bucket.append(text)
        return True

    @staticmethod
    def _native_delivery_state(drv) -> str:
        snapshot = getattr(drv, "_native_delivery_state_snapshot", None)
        if callable(snapshot):
            state = str(snapshot() or "")
        else:
            state = str(getattr(drv, "_helios_native_delivery_state", "") or "")
        if state:
            return state
        # Compatibility for injected/legacy drivers that predate the explicit
        # state. Definitely local native buffers remain replayable.
        if isinstance(drv, CodexAppServerDriver) and (
            getattr(drv, "native_starting", False)
            or getattr(drv, "_goal_pending_text", None) is not None
        ):
            return NATIVE_DELIVERY_LOCAL
        return ""

    def _native_recovery_anchor_for(self, drv) -> _NativeRecoveryAnchor | None:
        anchors = getattr(self, "_native_recovery_anchors", None)
        if not isinstance(anchors, dict):
            return None
        anchor = anchors.get(id(drv))
        if not isinstance(anchor, _NativeRecoveryAnchor) or anchor.driver is not drv:
            return None
        return anchor

    def _take_native_recovery_anchor(self, drv) -> _NativeRecoveryAnchor | None:
        anchor = MainWindow._native_recovery_anchor_for(self, drv)
        if anchor is None:
            return None
        self._native_recovery_anchors.pop(id(drv), None)
        return anchor

    def _restore_native_recovery_anchor_text(
        self,
        drv,
        anchor: _NativeRecoveryAnchor,
        *,
        force_background: bool = False,
    ) -> bool:
        """Insert definitely-rejected FIRST at its saved successor boundary."""

        if anchor.queue_id is not None:
            expected = (anchor.queue_id, anchor.text)
            if getattr(drv, "_pending_queue_delivery", None) == expected:
                quarantine = getattr(drv, "_quarantine_pending_queue_delivery", None)
                if callable(quarantine):
                    quarantine()
                else:
                    drv._pending_queue_delivery = None
                    remove_queued = getattr(drv, "remove_queued", None)
                    if callable(remove_queued):
                        remove_queued(anchor.queue_id)
            else:
                remove_queued = getattr(drv, "remove_queued", None)
                if callable(remove_queued):
                    remove_queued(anchor.queue_id)
            if not self._destroyed and self._drv_is_current(drv):
                self._transcript.remove_queued(anchor.queue_id)
        drafts = getattr(self, "_native_unsent_drafts", None)
        if not isinstance(drafts, dict):
            drafts = {}
            self._native_unsent_drafts = drafts
        bucket = drafts.setdefault(anchor.draft_key, [])
        index = max(0, min(anchor.insertion_index, len(bucket)))
        bucket.insert(index, anchor.text)
        # Do not apply the normal 20-draft trim here: if the bucket is full,
        # dropping either FIRST or a successor would violate at-most-once/FIFO.
        if not force_background and not self._destroyed and self._drv_is_current(drv):
            MainWindow._restore_native_unsent_drafts(self, drv)
        return True

    def _restore_rejected_native_recovery_anchor(self, drv) -> bool:
        if MainWindow._native_delivery_state(drv) != NATIVE_DELIVERY_REJECTED:
            return False
        anchor = MainWindow._take_native_recovery_anchor(self, drv)
        if anchor is None:
            return False
        return MainWindow._restore_native_recovery_anchor_text(
            self,
            drv,
            anchor,
        )

    def _quarantine_nonreplayable_native_recovery(self, drv) -> bool:
        """Consume acceptance-unknown FIRST/head without making it replayable."""

        state = MainWindow._native_delivery_state(drv)
        if state not in (
            NATIVE_DELIVERY_WIRE,
            NATIVE_DELIVERY_ACCEPTED,
            NATIVE_DELIVERY_UNKNOWN,
        ):
            return False
        native_text = MainWindow._take_native_pending_user(self, drv)
        anchor = MainWindow._take_native_recovery_anchor(self, drv)
        delivery = getattr(drv, "_pending_queue_delivery", None)
        queue_id = anchor.queue_id if anchor is not None else None
        if queue_id is None and isinstance(delivery, tuple) and len(delivery) == 2:
            queue_id = int(delivery[0])
        quarantined_delivery = None
        quarantine = getattr(drv, "_quarantine_pending_queue_delivery", None)
        if callable(quarantine) and delivery is not None:
            quarantined_delivery = quarantine()
        if queue_id is not None:
            if (
                quarantined_delivery is None
                or int(quarantined_delivery[0]) != queue_id
            ):
                if hasattr(drv, "_pending_queue_delivery"):
                    drv._pending_queue_delivery = None
                remove_queued = getattr(drv, "remove_queued", None)
                if callable(remove_queued):
                    remove_queued(queue_id)
            if not self._destroyed and self._drv_is_current(drv):
                self._transcript.remove_queued(queue_id)
        elif hasattr(drv, "_pending_queue_delivery"):
            drv._pending_queue_delivery = None
        uncertain = native_text is not None or anchor is not None or queue_id is not None
        if uncertain:
            _log.warning(
                "quarantined acceptance-unknown native prompt; inspect native thread"
            )
            # Definitely-unsent successors/current draft were staged in this
            # Work's bucket. Return them only if this Work is still visible;
            # otherwise leave them owned by it for the next rebind.
            if not self._destroyed and self._drv_is_current(drv):
                MainWindow._restore_native_unsent_drafts(self, drv)
        return uncertain

    @staticmethod
    def _native_draft_key(drv) -> str:
        provider = str(getattr(drv, "provider", "openai") or "openai")
        identity = str(
            getattr(drv, "_helios_work_id", "")
            or getattr(drv, "_helios_work_id_hint", "")
            or getattr(drv, "session_id", "")
            or getattr(drv, "_helios_goal_session_id_hint", "")
            or f"driver:{id(drv)}"
        )
        return f"{provider}:{identity}"

    def _restore_native_unsent_drafts(self, drv) -> None:
        drafts = getattr(self, "_native_unsent_drafts", None)
        if not isinstance(drafts, dict):
            return
        texts = drafts.pop(self._native_draft_key(drv), [])
        if not texts:
            return
        restored_count = len(texts)
        existing = self._composer.current_text().strip()
        if existing:
            texts.append(existing)
        self._composer.set_text("\n\n".join(texts))
        self._toast(
            f"Restored {restored_count} unaccepted GPT "
            f"prompt{'s' if restored_count != 1 else ''}."
        )

    def _return_native_locally_buffered_user(self, drv) -> None:
        if not isinstance(drv, CodexAppServerDriver):
            return
        if drv.native_starting or getattr(drv, "_goal_pending_text", None) is not None:
            self._return_native_pending_user(drv)

    def _drain_queue_to_composer(
        self,
        drv,
        *,
        leading_texts: tuple[str, ...] = (),
    ) -> None:
        """Hand un-sent queued messages back to the composer (stop path) so
        an interrupt never auto-sends or silently drops them.

        ``leading_texts`` belong to the checkpoint fence and predate every
        driver queue entry and the current composer draft. Keeping that order is
        what makes Stop lossless rather than merely non-empty.
        """
        pending = drv.queued_messages() if hasattr(drv, "queued_messages") else []
        uncertain = getattr(drv, "_uncertain_queue_delivery", None)
        recoverable_pending = [row for row in pending if row != uncertain]
        leading = [text for text in leading_texts if text]
        if not recoverable_pending and not leading:
            return
        for qid, _text in recoverable_pending:
            self._transcript.remove_queued(qid)
        texts = leading + (
            drv.take_queued() if hasattr(drv, "take_queued") else []
        )
        if hasattr(drv, "_pending_queue_delivery"):
            drv._pending_queue_delivery = None
        existing = self._composer.current_text().strip()
        if existing:
            texts.append(existing)
        self._composer.set_text("\n\n".join(texts))

    def _preserve_driver_queue(
        self,
        drv,
        *,
        leading_texts: tuple[str, ...] = (),
    ) -> int:
        """Remove queued turns before cancellation and keep them recoverable.

        Visible drafts return to the composer. Background/closing drafts stay
        in the existing in-memory, provider-scoped recovery bucket so a sibling
        cannot auto-send them after a Work-wide circuit breaker trips.
        """

        pending = drv.queued_messages() if hasattr(drv, "queued_messages") else []
        uncertain = getattr(drv, "_uncertain_queue_delivery", None)
        recoverable_pending = [row for row in pending if row != uncertain]
        leading = [text for text in leading_texts if text]
        if not recoverable_pending and not leading:
            return 0
        if not self._destroyed and self._drv_is_current(drv):
            MainWindow._drain_queue_to_composer(
                self,
                drv,
                leading_texts=tuple(leading),
            )
            return len(leading) + len(recoverable_pending)
        texts = drv.take_queued() if hasattr(drv, "take_queued") else []
        if hasattr(drv, "_pending_queue_delivery"):
            drv._pending_queue_delivery = None
        texts = leading + texts
        if not texts:
            return 0
        drafts = getattr(self, "_native_unsent_drafts", None)
        if not isinstance(drafts, dict):
            drafts = {}
            self._native_unsent_drafts = drafts
        bucket = drafts.setdefault(MainWindow._native_draft_key(drv), [])
        bucket.extend(texts)
        return len(texts)

    def _on_composer_stop(self, *_) -> None:
        if self._driver is not None:
            drv = self._driver
            checkpoint = MainWindow._take_checkpoint_dispatch(self, drv)
            staged = (
                MainWindow._stage_native_wire_recovery(self, drv)
                if checkpoint is None
                else None
            )
            if staged is None:
                self._drain_queue_to_composer(
                    drv,
                    leading_texts=(
                        (checkpoint.text,) if checkpoint is not None else ()
                    ),
                )
            self._return_native_locally_buffered_user(drv)
            drv.stop()
            self._clear_busy_ui()

    def _on_emergency_stop_all(self, *_) -> None:
        """Operator kill switch for every provider session in this app."""

        manager = self._driver_manager
        drivers = manager.drivers_for_shutdown()
        if not drivers:
            self._toast("No live sessions to stop.")
            return
        has_codex = any(isinstance(drv, CodexAppServerDriver) for drv in drivers)
        for drv in drivers:
            checkpoint = MainWindow._take_checkpoint_dispatch(self, drv)
            leading = (checkpoint.text,) if checkpoint is not None else ()
            MainWindow._recover_driver_for_cancellation(
                self,
                drv,
                leading_texts=leading,
                terminal=False,
            )
        for drv in drivers:
            manager.stop_driver(drv, interrupt=True)
        if has_codex:
            # A provider interrupt is a request, not proof of termination. The
            # explicit emergency action closes the owned shared transport too,
            # killing its process group if any turn ignores the request.
            try:
                get_shared_hub().abort_transport(returncode=-1)
            except Exception as exc:
                _log.warning("emergency Codex shutdown failed: %s", exc)
        self._clear_busy_ui()
        count = len(drivers)
        self._toast(
            f"Emergency stop sent to {count} live "
            f"session{'s' if count != 1 else ''}. Unsent drafts were preserved."
        )

    def _stage_native_wire_recovery(self, drv) -> int | None:
        """Withhold wire-pending FIRST while preserving definite successors.

        Returns ``None`` when this is not a native wire-pending boundary;
        otherwise returns the number of definitely-unsent successors moved to
        the normal draft bucket. The anchor is conditional: acceptance drops
        it, definite rejection inserts FIRST, ambiguity quarantines FIRST.
        """

        if not isinstance(drv, CodexAppServerDriver):
            return None
        state = MainWindow._native_delivery_state(drv)
        if state not in (
            NATIVE_DELIVERY_WIRE,
            NATIVE_DELIVERY_ACCEPTED,
            NATIVE_DELIVERY_REJECTED,
            NATIVE_DELIVERY_UNKNOWN,
        ):
            return None
        pending_users = getattr(self, "_native_pending_user", {})
        pending = (
            pending_users.get(id(drv))
            if isinstance(pending_users, dict)
            else None
        )
        delivery = getattr(drv, "_pending_queue_delivery", None)
        queued_rows = drv.queued_messages() if hasattr(drv, "queued_messages") else []
        if pending is not None and pending[0] is drv:
            wire_text = pending[1]
            successors = drv.take_queued() if hasattr(drv, "take_queued") else []
            successor_rows = queued_rows
            queue_id = None
        elif (
            isinstance(delivery, tuple)
            and len(delivery) == 2
            and queued_rows[:1] == [delivery]
        ):
            wire_text = str(delivery[1])
            take_successors = getattr(
                drv,
                "take_queued_successors_after_pending_delivery",
                None,
            )
            successors = take_successors() if callable(take_successors) else None
            if successors is None:
                return None
            successor_rows = queued_rows[1:]
            queue_id = int(delivery[0])
        else:
            return None

        existing_anchor = MainWindow._native_recovery_anchor_for(self, drv)
        key = (
            existing_anchor.draft_key
            if existing_anchor is not None
            else MainWindow._native_draft_key(drv)
        )
        visible = not self._destroyed and self._drv_is_current(drv)
        if visible:
            for qid, _text in successor_rows:
                self._transcript.remove_queued(qid)
        drafts = getattr(self, "_native_unsent_drafts", None)
        if not isinstance(drafts, dict):
            drafts = {}
            self._native_unsent_drafts = drafts
        bucket = drafts.setdefault(key, [])
        insertion_index = (
            existing_anchor.insertion_index
            if existing_anchor is not None
            else len(bucket)
        )
        bucket.extend(successors)
        if visible:
            existing = self._composer.current_text().strip()
            if existing:
                bucket.append(existing)
            # Until turn/start resolves, every definitely-unsent word remains
            # Work-owned instead of sitting in the global composer where a
            # session switch could misattribute it to another Work.
            self._composer.set_text("")
        anchors = getattr(self, "_native_recovery_anchors", None)
        if not isinstance(anchors, dict):
            anchors = {}
            self._native_recovery_anchors = anchors
        if existing_anchor is None:
            anchors[id(drv)] = _NativeRecoveryAnchor(
                drv,
                wire_text,
                key,
                insertion_index,
                queue_id,
            )
        elif (
            existing_anchor.text != wire_text
            or existing_anchor.queue_id != queue_id
        ):
            # Ownership changed under an unresolved boundary. Refuse to move
            # the anchor: the original FIRST remains the only safe candidate
            # for authoritative acceptance/rejection.
            _log.warning("ignored conflicting native recovery anchor replacement")
        return len(successors)

    def _recover_driver_for_cancellation(
        self,
        drv,
        *,
        leading_texts: tuple[str, ...] = (),
        terminal: bool,
    ) -> int:
        """Recover only prompts that are definitely unsent, in FIFO order."""

        state = MainWindow._native_delivery_state(drv)
        if not terminal and not leading_texts and state in (
            NATIVE_DELIVERY_WIRE,
            NATIVE_DELIVERY_ACCEPTED,
            NATIVE_DELIVERY_REJECTED,
            NATIVE_DELIVERY_UNKNOWN,
        ):
            staged = MainWindow._stage_native_wire_recovery(self, drv)
            if staged is not None:
                return staged
        if terminal and state in (
            NATIVE_DELIVERY_WIRE,
            NATIVE_DELIVERY_ACCEPTED,
            NATIVE_DELIVERY_UNKNOWN,
        ):
            if state == NATIVE_DELIVERY_WIRE:
                transition = getattr(
                    drv,
                    "_transition_native_delivery_state",
                    None,
                )
                if callable(transition):
                    transition(NATIVE_DELIVERY_WIRE, NATIVE_DELIVERY_UNKNOWN)
                elif hasattr(drv, "_helios_native_delivery_state"):
                    drv._helios_native_delivery_state = NATIVE_DELIVERY_UNKNOWN
            MainWindow._quarantine_nonreplayable_native_recovery(self, drv)
        if terminal:
            uncertain_text = MainWindow._take_uncertain_pending_user(self, drv)
            uncertain_queue = MainWindow._quarantine_uncertain_queue_delivery(
                self,
                drv,
            )
            if uncertain_text is not None:
                _log.warning(
                    "quarantined provider-ambiguous prompt during terminal cleanup"
                )
            if (
                (uncertain_text is not None or uncertain_queue)
                and not self._destroyed
            ):
                self._toast(
                    "A provider prompt had unknown delivery and was not restored. "
                    "Inspect its durable recovery record before retrying."
                )

        pending_users = getattr(self, "_native_pending_user", {})
        has_native_pending = bool(
            isinstance(pending_users, dict)
            and pending_users.get(id(drv), (None,))[0] is drv
        )
        if not has_native_pending:
            MainWindow._restore_rejected_native_recovery_anchor(self, drv)

        if self._drv_is_current(drv) and not self._destroyed:
            preserved = MainWindow._preserve_driver_queue(
                self,
                drv,
                leading_texts=leading_texts,
            )
            MainWindow._return_native_pending_user(self, drv)
            MainWindow._return_identity_pending_user(self, drv)
            return preserved
        MainWindow._return_native_pending_user(self, drv)
        MainWindow._return_identity_pending_user(self, drv)
        return MainWindow._preserve_driver_queue(
            self,
            drv,
            leading_texts=leading_texts,
        )

    def _on_session_stop_requested(self, _list, session_id: str) -> None:
        """Stop a session from its sidebar row — works for background ones the
        user isn't currently viewing. Explicit user stop → interrupt=True."""
        drv = self._driver_manager.driver_for_session(session_id)
        if drv is None:
            return
        checkpoint = MainWindow._take_checkpoint_dispatch(self, drv)
        if self._drv_is_current(drv):
            staged = (
                MainWindow._stage_native_wire_recovery(self, drv)
                if checkpoint is None
                else None
            )
            if staged is None:
                self._drain_queue_to_composer(
                    drv,
                    leading_texts=(
                        (checkpoint.text,) if checkpoint is not None else ()
                    ),
                )
            self._return_native_locally_buffered_user(drv)
        else:
            if checkpoint is not None:
                MainWindow._restore_checkpoint_dispatch_text(
                    self,
                    drv,
                    checkpoint.text,
                )
            preserved = (
                MainWindow._stage_native_wire_recovery(self, drv)
                if checkpoint is None
                else None
            )
            if preserved is None:
                # Startup/Goal-local GPT FIRST is definitely unsent; Claude
                # identity FIRST is provisional. Both precede their queue.
                self._return_native_locally_buffered_user(drv)
                self._return_identity_pending_user(drv)
                preserved = MainWindow._preserve_driver_queue(self, drv)
            if preserved:
                self._toast(
                    f"Preserved {preserved} queued "
                    f"message{'s' if preserved != 1 else ''} for this session."
                )
        self._driver_manager.stop_driver(drv)
        if self._drv_is_current(drv):
            self._clear_busy_ui()

    def _clear_busy_ui(self) -> None:
        """Return the composer + toolbar to the idle state and clear the
        activity strip.

        Called from every path that ends OR abandons a turn. Previously only
        the three terminal driver callbacks (result/error/exited) reset the
        busy state, and they only fire for the *current* driver. Any path that
        detached or replaced the driver mid-turn (session switch, fresh chat,
        teardown) left `_chat_toolbar` stuck disabled — model/effort greyed out
        for the life of the process. Resetting both controls here fixes that.
        """
        self._composer.set_busy(False)
        self._chat_toolbar.set_busy(False)
        self._activity.clear()

    # ── Driver callbacks ──────────────────────────────────────────────
    # Every handler below is a "sender-guarded" callback. The first
    # parameter is the driver that emitted the signal; if it's no longer
    # the current driver (because the user switched sessions or started a
    # fresh chat while this one was finishing), drop the event on the
    # floor. The same guard is applied to `result` and `exited` so an old
    # driver's terminal events can't set self._driver = None on a freshly
    # spawned one.

    def _drv_is_current(self, drv) -> bool:
        return self._driver_manager.is_current(drv)

    def _reject_started_identity(self, drv, detail: str) -> None:
        """Fence a runtime identity before its buffered first prompt can run."""

        drv._helios_identity_rejected = True
        provider = self._driver_provider(drv)
        native_pending = getattr(self, "_native_pending_user", {})
        exec_delivery_uncertain = bool(
            provider == model_catalog.PROVIDER_OPENAI
            and isinstance(drv, CodexAppServerDriver)
            and not drv.native_transport
            and isinstance(native_pending, dict)
            and native_pending.get(id(drv), (None,))[0] is drv
        )
        try:
            was_current = self._drv_is_current(drv)
        except Exception:
            was_current = False
        identity_pending = getattr(self, "_identity_pending_user", {})
        restored_uncertain = bool(
            isinstance(identity_pending, dict)
            and identity_pending.get(id(drv), (None,))[0] is drv
        )
        _log.error("rejected provider-native session identity: %s", detail)

        # Full teardown matters for native Codex: its explicit interrupt path
        # intentionally no-ops while idle.  Forget handlers/registry state and
        # close without an interrupt marker before returning to the emitter.
        # Teardown is also the single recovery owner: it aggregates checkpoint
        # FIRST or identity-pending FIRST ahead of the driver's queued drafts.
        # Pre-draining here reversed that order for background Works.
        teardown = getattr(self, "_teardown_driver", None)
        if callable(teardown):
            teardown(drv)
        else:
            manager = self._driver_manager
            manager_teardown = getattr(manager, "teardown", None)
            if callable(manager_teardown):
                manager_teardown(drv, interrupt=False)
            else:
                forget = getattr(manager, "forget", None)
                if callable(forget):
                    forget(drv)
                stop_driver = getattr(manager, "stop_driver", None)
                if callable(stop_driver):
                    try:
                        stop_driver(drv, interrupt=False)
                    except TypeError:
                        stop_driver(drv)

        if was_current:
            toast = getattr(self, "_toast", None)
            if callable(toast):
                if (
                    provider == model_catalog.PROVIDER_ANTHROPIC
                    and restored_uncertain
                ):
                    toast(
                        "Claude stopped after an unsafe identity response. "
                        "Your draft was restored, but delivery may have begun "
                        "— check the transcript before retrying.",
                        timeout=8,
                    )
                elif exec_delivery_uncertain:
                    toast(
                        "Codex fallback stopped after an unsafe identity "
                        "response. Your draft was restored, but delivery may "
                        "have begun — check the transcript before retrying.",
                        timeout=8,
                    )
                else:
                    toast(
                        "Helios rejected an unsafe provider identity and kept "
                        "your message unsent.",
                        timeout=6,
                    )

    def _on_session_started(self, drv, session_id: str, cwd: str, model: str) -> None:
        # Register in the live-driver registry regardless of which session is
        # visible — a driver started just before the user switched away must
        # still be tracked (and reachable when they switch back) rather than
        # leaking as an untracked orphan.
        was_starting = self._driver_manager.starting is drv
        # Attach the provider-native id before the sender guard: a background
        # sibling is still a durable participant even if the user switched
        # away between spawn and its init event.
        coordinator = getattr(self, "_work_coordinator", None)
        work_id = str(getattr(drv, "_helios_work_id", "") or "")
        provider = self._driver_provider(drv)
        reported_session_id = str(session_id or "").strip()
        # A checkpoint taken on the first turn of a fresh process predates this
        # id; file it now so the very first message is rewindable too.
        _flush_pending_checkpoints(drv, reported_session_id)
        expected_resume_id = str(
            getattr(drv, "_helios_expected_resume_id", "") or ""
        ).strip()
        if not reported_session_id:
            MainWindow._reject_started_identity(
                self,
                drv,
                "runtime returned a blank native session id",
            )
            return
        if expected_resume_id and reported_session_id != expected_resume_id:
            MainWindow._reject_started_identity(
                self,
                drv,
                f"resume expected {expected_resume_id!r}, got {reported_session_id!r}",
            )
            return
        session_id = reported_session_id
        if provider not in (
            model_catalog.PROVIDER_ANTHROPIC,
            model_catalog.PROVIDER_OPENAI,
            model_catalog.PROVIDER_OPENROUTER,
        ):
            MainWindow._reject_started_identity(
                self,
                drv,
                f"session {session_id!r} came from unknown provider {provider!r}",
            )
            return
        target = getattr(self, "_next_chat", None)
        target_project = getattr(target, "project", None)
        transcript_path = (
            target_project.path / f"{session_id}.jsonl"
            if getattr(target_project, "path", None) is not None
            else None
        )
        existing = session_providers.resolve_provider(
            session_id,
            transcript_path,
            conversation_store=getattr(self, "_conversation_perms", None),
        )
        if existing.conflicted or (
            existing.known and existing.provider != provider
        ):
            MainWindow._reject_started_identity(
                self,
                drv,
                f"{provider} ownership conflicts for native session {session_id!r}",
            )
            return
        # The runtime class is authoritative for a just-created native id.
        # Persist both Claude and GPT claims; absence means unknown.
        if not session_providers.set_provider(session_id, provider):
            MainWindow._reject_started_identity(
                self,
                drv,
                f"could not persist {provider} ownership for {session_id!r}",
            )
            return
        drv._helios_identity_confirmed = True
        if coordinator is not None and session_id:
            try:
                if not work_id:
                    work = coordinator.ensure_work(
                        cwd=cwd,
                        lead_provider=provider,
                    )
                    work_id = work.work_id
                current = coordinator.participant(work_id, provider)
                tagged_generation = getattr(
                    drv, "_helios_participant_generation", None
                )
                stale_binding = (
                    tagged_generation is not None
                    and (
                        current is None
                        or current.generation != tagged_generation
                        or str(
                            getattr(drv, "_helios_participant_id", "") or ""
                        )
                        != current.participant_id
                    )
                )
                if stale_binding:
                    MainWindow._reject_started_identity(
                        self,
                        drv,
                        f"runtime identity {session_id!r} conflicts with the "
                        f"current {provider} binding for Work {work_id!r}",
                    )
                    return
                else:
                    participant = coordinator.bind_participant(
                        work_id,
                        provider,
                        native_id=session_id,
                        model=model,
                    )
                    tag_driver(drv, participant)
                    session_goals.rekey_goal(session_id, work_id)
            except Exception as exc:
                _log.warning(
                    "could not bind native session %s to Work: %s",
                    session_id,
                    exc,
                )
                MainWindow._reject_started_identity(
                    self,
                    drv,
                    f"could not bind native session {session_id!r} to its Work",
                )
                return
        if session_id:
            # Claude's first prompt was written before system/init could prove
            # the native id, but its bubble/ledger entry stayed provisional.
            # Commit both only after every identity fence above succeeded and
            # Work tagging is available.
            MainWindow._accept_identity_pending_user(self, drv)
            self._driver_manager.register_started(drv, session_id)
            permission_mode = str(
                getattr(drv, "permission_mode", "")
                or getattr(drv, "_helios_staged_permission_mode", "")
            )
            effort_key = str(
                getattr(drv, "effort_key", "")
                or getattr(drv, "_helios_staged_effort_key", "")
            )
            workflow_mode = canonical_workflow_mode(
                getattr(drv, "workflow_mode", "")
                or getattr(drv, "_helios_staged_workflow_mode", "")
            )
            durable_effort_key = (
                ""
                if provider == model_catalog.PROVIDER_OPENAI
                and effort_key == "ultra"
                else effort_key
            )
            if permission_mode in PERMISSION_MODES:
                saved = self._conversation_perms.set(
                    provider,
                    session_id,
                    permission_mode,
                    effort_key=durable_effort_key,
                    workflow_mode=workflow_mode,
                )
                if not saved:
                    drv._helios_execution_persistence_failed = True
                    _log.warning(
                        "could not persist execution settings for %s:%s",
                        provider,
                        session_id,
                    )
            if was_starting or self._drv_is_current(drv):
                self._bind_pending_goal_to_session(drv, session_id, cwd)
            if getattr(drv, "supports_native_goals", False):
                goal_key = str(getattr(drv, "_helios_work_id", "") or session_id)
                native_goal = session_goals.get_goal(goal_key)
                if native_goal is not None:
                    drv.sync_goal(native_goal)
                elif getattr(drv, "_helios_goal_session_id_hint", ""):
                    # Helios Work is canonical. A resumed Codex thread may
                    # retain an older native goal, so explicitly clear it
                    # before releasing any buffered first prompt.
                    drv.clear_native_goal()
        if not self._drv_is_current(drv):
            return
        # Keep the staged resume target synced to this driver's live session id.
        # It's only consulted when the process is gone, so a later respawn
        # (Stop → new message, idle-reap, crash, restart) resumes this exact
        # conversation instead of silently starting fresh. See stage_resume().
        if session_id:
            self._next_chat = stage_resume(
                stage_work(self._next_chat, work_id),
                session_id,
                provider,
            )
        # Cache this session's authoritative tool + MCP inventory so the
        # Settings → Tools view can show the real set (best-effort). Durable —
        # runs even during close. OpenRouter is skipped: the snapshot backs the
        # Claude built-ins page, and an OpenRouter session would overwrite
        # Claude's 16-tool inventory with its own 6 in-process tools.
        try:
            if provider == model_catalog.PROVIDER_OPENAI:
                if drv.init_mcp_servers:
                    codex_env.save_mcp_snapshot(drv.init_mcp_servers)
            elif provider != model_catalog.PROVIDER_OPENROUTER:
                claude_env.save_init_snapshot(drv.init_tools, drv.init_mcp_servers)
        except Exception:
            pass
        # Everything below is UI — a session-started event parsed from stdout
        # after window close must not touch the toolbar or schedule a sidebar
        # refresh against the finalizing tree. (register_started above already
        # ran; SessionList self-defends its live-id projection.)
        if self._destroyed:
            return
        if getattr(drv, "_helios_execution_persistence_failed", False):
            drv._helios_execution_persistence_failed = False
            self._toast(
                "Execution settings are active, but could not be saved for "
                "restart.",
                timeout=6,
            )
        self._staged_permission_mode = ""
        self._staged_effort_key = ""
        self._staged_workflow_mode = ""
        # A freshly-spawned driver is now the visible running child — surface
        # its captured execution settings only after the close-safety gate.
        self._sync_effort_sensitivity()
        self._sync_execution_control()
        self._chat_toolbar.set_context_model(model)
        MainWindow._refresh_capabilities(self)
        # The .jsonl file for this session DOES NOT EXIST YET at the moment
        # `system/init` lands on stdout — claude writes it ~100ms after init
        # is emitted (measured). Refreshing the sidebar immediately would
        # scan the directory before the file is there and the new row
        # wouldn't appear. Delay just enough to let the file land, then
        # invalidate the cache and refresh so the live row shows up.
        GLib.timeout_add(400, self._refresh_sidebar_for_live, drv, session_id)

    def _refresh_sidebar_for_live(self, drv: ClaudeCliDriver, session_id: str) -> bool:
        """One-shot scheduled refresh after session-started. Verifies the
        driver is still the live one before touching the sidebar."""
        if self._destroyed or not self._drv_is_current(drv):
            return False
        self._sync_live_ids()
        # Re-scan local disk so the freshly-written .jsonl gets a row (cheap;
        # the pool is left alone), then select it so the chat the user just
        # started is findable. Safe: _on_session_selected early-returns when
        # the id matches the running driver, so it won't tear it down.
        self._sessions.reload(preserve_selection=True, rescan_pool=False)
        self._sessions.select_session(session_id)
        return False  # one-shot

    def _on_assistant_streaming(self, drv, streaming) -> None:
        if self._destroyed:
            return
        if not self._drv_is_current(drv):
            # A background session is streaming. Before this returned
            # here, which is why a background chat rendered as one 8px dot
            # with no way to tell "reading a file" from "wedged" without
            # switching to it. Its row gets the same words the strip uses.
            self._push_background_activity(drv, *derive_state(streaming.blocks))
            return
        self._transcript.show_streaming_assistant(streaming)
        self._plan.show_streaming(streaming)
        state, detail = derive_state(streaming.blocks)
        self._remember_activity(drv, state, detail)
        self._activity.set_token_estimate(estimate_streamed_tokens(streaming.blocks))
        self._activity.set_activity(state, detail)

    def _on_turn_appended(self, drv, turn) -> None:
        coordinator = getattr(self, "_work_coordinator", None)
        already_recorded = False
        consume_recorded = getattr(drv, "_consume_recorded_contribution", None)
        if callable(consume_recorded):
            already_recorded = bool(consume_recorded(turn))
        if coordinator is not None and not already_recorded:
            coordinator.record_turn(drv, turn)
        # Ledger work above must complete even during close; only the widget
        # updates below are gated on the window still being alive.
        if self._destroyed or not self._drv_is_current(drv):
            return
        self._transcript.append_turn(turn)
        self._plan.append_turn(turn)

    def _on_turn_result(self, drv, result: dict) -> None:
        if (
            isinstance(drv, OpenRouterDriver)
            and result.get("stop_reason") in {"tool_round_limit", "tool_stalled", "output_limit"}
        ):
            # This is a local pause, not a user Stop or a Work-wide breaker.
            # Drain unsent input before accepting follow-ups; an idle driver
            # with a retained FIFO would reject every new direct send.
            MainWindow._preserve_driver_queue(self, drv)
        if self._destroyed:
            # Closing: don't touch widgets and don't post a "finished"
            # notification for a driver we're tearing down.
            return
        # The pane describes the whole worktree, so a background conversation
        # in this same folder can change what the operator is inspecting too.
        changes = getattr(self, "_changes", None)
        if changes is not None and changes.cwd == str(getattr(drv, "_cwd", "") or ""):
            changes.refresh()
        if not self._drv_is_current(drv):
            # A BACKGROUND session just finished a turn — let the user know,
            # since its dot is off-screen / not what they're watching. Unless
            # it's about to continue with a queued message: notify only when
            # it's actually waiting on the user.
            # The turn is over: whatever the row last said it was doing, it
            # is not doing it any more. Clearing here (rather than on the next
            # delta) is what keeps a finished background row from freezing on
            # its final tool for the rest of the session.
            self._push_background_activity(drv, "", "")
            if not getattr(drv, "_token_budget_exhausted", False) and not drv.queued_messages():
                self._notify_session_finished(drv)
            return
        # A completed turn is the only thing that moves OpenRouter's balance,
        # and it is the moment the panel is most likely to be read.
        self._refresh_openrouter_credits(throttle=True)
        MainWindow._refresh_capabilities(self)
        # Re-enable composer, clear activity strip.
        self._composer.set_busy(False)
        self._chat_toolbar.set_busy(False)
        self._remember_activity(drv, "", "")
        # Per-turn tokens and cost on the bubble that just finished (M-6a).
        # Empty text clears; Codex/OpenRouter results without a price are a no-op.
        set_footer = getattr(self._transcript, "set_last_turn_footer", None)
        if callable(set_footer) and not is_background_wakeup(result):
            set_footer(format_turn_footer(result))
        self._activity.clear()
        # Re-refresh sidebar — by now the .jsonl is definitely on disk and
        # has timestamps/title content the status probe can use. Belt-and-
        # braces in case the 400ms delay in session-started missed. Local
        # rescan only: no CIFS walk at the end of every turn.
        self._sessions.reload(preserve_selection=True, rescan_pool=False)
        self._composer.grab_input_focus()

    def _on_budget_exhausted(self, drv, details: dict) -> None:
        """Trip a Work-wide breaker, preserve drafts, and cancel its family."""

        # A dollar breaker must never reach the durable Work state on a
        # subscription account. There, `cost_micro_usd` is an API-equivalent
        # estimate rather than money, `budgetLimited` is terminal, and no UI
        # exists to reset it — so a notional number permanently killed a live
        # conversation. Helios no longer passes --max-budget-usd on
        # these accounts, but the CLI can still report one from the user's own
        # settings.json, so refuse the transition here too.
        if str(details.get("kind") or "") == "usd":
            from helios.backend.claude_env import is_subscription_billing

            if is_subscription_billing():
                self._toast(
                    "Claude reported a spend cap, but this account bills by "
                    "subscription — ignoring it. Check settings.json if you "
                    "did not set one."
                )
                return

        work_id = str(
            getattr(drv, "_helios_work_id", "")
            or getattr(drv, "_helios_work_id_hint", "")
            or ""
        )
        coordinator = getattr(self, "_work_coordinator", None)
        blocked = getattr(self, "_budget_blocked_work_ids", None)
        if not isinstance(blocked, set):
            blocked = set()
            self._budget_blocked_work_ids = blocked
        if work_id:
            # This in-memory latch closes the gate even if durable state is
            # temporarily unavailable. The store transition below survives a
            # process restart when storage is healthy.
            blocked.add(work_id)
        if work_id and coordinator is not None:
            try:
                coordinator.mark_budget_exhausted(
                    work_id=work_id,
                    provider=self._driver_provider(drv),
                    details=details if isinstance(details, dict) else {},
                )
            except Exception as exc:
                _log.warning("could not persist Work budget exhaustion: %s", exc)

        manager = getattr(self, "_driver_manager", None)
        family = {drv}
        drivers_for_work = getattr(manager, "drivers_for_work", None)
        if work_id and callable(drivers_for_work):
            try:
                family.update(drivers_for_work(work_id))
            except Exception as exc:
                _log.warning("could not enumerate Work driver family: %s", exc)
        for candidate in family:
            checkpoint = MainWindow._take_checkpoint_dispatch(self, candidate)
            leading = (checkpoint.text,) if checkpoint is not None else ()
            MainWindow._recover_driver_for_cancellation(
                self,
                candidate,
                leading_texts=leading,
                terminal=False,
            )
        for candidate in family:
            stop_driver = getattr(manager, "stop_driver", None)
            if callable(stop_driver):
                stop_driver(candidate, interrupt=True)
            else:
                try:
                    candidate.stop(interrupt=True)
                except Exception:
                    pass

        if self._destroyed:
            return
        if self._drv_is_current(drv):
            self._clear_busy_ui()
        if not self._drv_is_current(drv):
            self._toast(
                "A background Work reached its execution budget and was stopped."
            )

    def _on_usage_updated(self, drv, used: int, total: int) -> None:
        if self._destroyed or not self._drv_is_current(drv):
            return
        self._chat_toolbar.set_context_usage(used, total)
        self._refresh_context_breakdown(drv, used, total)

    def _on_codex_capability_drift(self, _drv, report: dict) -> None:
        """Codex sent an event this build has no handler for.

        Not gated on the driver being the visible one: the point is that the
        *installed CLI* moved, which is true regardless of which chat noticed.
        """

        if self._destroyed or not isinstance(report, dict):
            return
        self._report_cli_degradation("Codex", report)

    def _on_codex_provider_notice(self, _drv, payload: dict) -> None:
        """Project App Server's typed user notices without inventing a gap.

        Connection-level notices are delivered to every live Codex driver, so
        deduplicate those (and repeated runtime/config notices) at the window.
        Guardian warnings remain event-for-event because suppressing a later
        safety warning would be a materially different policy decision.
        """

        if self._destroyed or not isinstance(payload, dict):
            return
        method = str(payload.get("method") or "")
        message = " ".join(str(payload.get("message") or "").split())
        if not method or not message:
            return
        thread_id = str(payload.get("threadId") or "")
        key = (method, thread_id, message)
        seen = getattr(self, "_codex_provider_notices_seen", None)
        if not isinstance(seen, set):
            seen = set()
            self._codex_provider_notices_seen = seen
        if method != "guardianWarning":
            if key in seen:
                return
            if len(seen) >= 64:
                seen.clear()
            seen.add(key)

        label = {
            "configWarning": "Codex configuration warning",
            "deprecationNotice": "Codex notice",
            "guardianWarning": "Codex safety warning",
            "warning": "Codex warning",
        }.get(method, "Codex notice")
        if len(message) > 320:
            message = message[:319].rstrip() + "…"
        self._toast(f"{label}: {message}", timeout=8)

    def _report_cli_degradation(self, provider: str, report: dict) -> bool:
        """Say once, per app run, which features this CLI build cannot do.

        A dropped optional flag is otherwise invisible: argv loses an entry
        and the feature stops happening with nothing on screen to connect the
        two. Once per provider — a repeat every catalog refresh would
        be noise, and the log line above is unconditional either way.
        """

        if self._destroyed:
            return False
        degraded = [str(item) for item in (report.get("degraded") or []) if item]
        if not degraded:
            return False
        seen = getattr(self, "_cli_degradation_reported", None)
        if not isinstance(seen, set):
            seen = set()
            self._cli_degradation_reported = seen
        if provider in seen:
            return False
        seen.add(provider)
        # Codex's version is a whole user-agent string; Claude's is "2.1.245".
        version = str(report.get("version") or "")[:40].strip()
        which = f"{provider} CLI {version}".strip()
        self._toast(f"{which} cannot do: {'; '.join(degraded)}.")
        return False

    def _on_cli_capabilities(self, drv, payload: dict) -> None:
        """The CLI stated what it supports — re-render anything we were guessing.

        `supportedEffortLevels` replaces a fixed six-stop list, while the
        advertised slash-command set gates native Claude actions in the agent
        command palette. The driver caches the whole report rather than only
        those slices so later capability surfaces need no second handshake.
        """

        if self._destroyed or not self._drv_is_current(drv):
            return
        MainWindow._sync_agent_command_capabilities(self)
        if not isinstance(payload, dict) or not payload.get("models"):
            return
        MainWindow._sync_effort_sensitivity(self)

    def _on_cli_context_usage(self, drv, payload: dict) -> None:
        """Real per-category occupancy, straight from the provider.

        This answers before the first turn, which is what stops the meter
        reading 0/0 on open, and its categories are measured rather than
        estimated at 4 chars/token.

        `maxTokens` is the usable budget before autocompact fires (967,000 on
        a 1M model — the cap minus a 33,000 autocompact buffer, measured
        2026-08-07 on 2.1.224). That is the denominator a user can act on, and
        it is the one the CLI's own /context shows.
        """

        if self._destroyed or not self._drv_is_current(drv):
            return
        if not isinstance(payload, dict):
            return
        total = int(payload.get("maxTokens") or 0)
        used = int(payload.get("totalTokens") or 0)
        if total <= 0:
            return
        self._chat_toolbar.set_context_usage(used, total)
        self._chat_toolbar.set_measured_breakdown(payload)

    def _refresh_context_breakdown(self, drv, used: int, total: int) -> None:
        """Work out what is filling the window, off the main loop.

        Usage lands mid-turn now (once per API request), so this is throttled:
        the transcript is only re-read when it has actually grown, and never
        while a previous read is in flight.
        """
        session_id = str(getattr(drv, "session_id", "") or "")
        project = self._next_chat.project if self._next_chat is not None else None
        if not session_id or project is None or used <= 0 or total <= 0:
            self._chat_toolbar.set_context_breakdown(None)
            return
        path = project.path / f"{session_id}.jsonl"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return
        if self._breakdown_busy or stamp == self._breakdown_stamp:
            return
        self._breakdown_busy = True
        self._breakdown_stamp = stamp

        def work() -> None:
            try:
                turns = parse_transcript(path)
                result = context_breakdown.summarize(turns, used, total)
            except Exception as exc:  # noqa: BLE001 — a meter must never crash a turn
                _log.warning("context breakdown failed: %s", exc)
                result = None
            GLib.idle_add(self._apply_context_breakdown, drv, result)

        threading.Thread(target=work, daemon=True, name="context-breakdown").start()

    def _apply_context_breakdown(self, drv, result) -> bool:
        self._breakdown_busy = False
        if self._destroyed or not self._drv_is_current(drv):
            return False
        self._chat_toolbar.set_context_breakdown(result)
        return False

    # --- search ---

    def _open_search(self) -> None:
        if self._destroyed:
            return
        from helios.widgets.search_dialog import SearchDialog
        dlg = SearchDialog(self)
        dlg.connect("activated", self._on_search_activated)
        dlg.present()
        dlg.focus_entry()

    def _on_search_activated(self, _dlg, _project_dirname: str, session_id: str) -> None:
        """Jump to a search hit. The unified list holds every session, so
        this is a straight select — falling back to the All filter when the
        hit is hidden by the current one."""
        if not self._sessions.reveal_session(session_id):
            self._toast("That session isn't in the list anymore.")

    # --- background-session finish notifications ---

    def _notify_session_finished(self, drv: ClaudeCliDriver) -> None:
        """Post a desktop notification that a background session finished its
        turn. Respects the `notify_on_finish` preference (default on); clicking
        the notification deep-links back to that session."""
        if not self._ui_state.get("notify_on_finish", True):
            return
        app = self.get_application()
        sid = drv.session_id
        if app is None or not sid:
            return
        # Best-effort friendly label from the shared title cache.
        title = ""
        try:
            from helios.backend.process.title_generator import store as title_store
            title = title_store().get(sid) or ""
        except Exception:
            pass
        label = title or f"Session {sid[:8]}"
        provider_label = model_catalog.PROVIDER_LABELS.get(self._driver_provider(drv), "Assistant")
        note = Gio.Notification.new(f"{provider_label} finished")
        note.set_body(f"{label} is ready for your next message.")
        try:
            note.set_default_action_and_target_value(
                "app.focus-session", GLib.Variant("s", sid)
            )
        except Exception:
            pass
        app.send_notification(f"helios-finish-{sid}", note)

    def _notify_pending_question(self, drv) -> None:
        """Desktop notification that a background session is waiting on your
        answer — the reliable channel when its sidebar row is filtered out of
        view. Clicking reveals the session. Respects the notifications toggle;
        the per-session id coalesces repeats so it never stacks."""
        if self._destroyed or not self._ui_state.get("notify_on_finish", True):
            return
        app = self.get_application()
        sid = getattr(drv, "session_id", "")
        if app is None or not sid:
            return
        title = ""
        try:
            from helios.backend.process.title_generator import store as title_store
            title = title_store().get(sid) or ""
        except Exception:
            pass
        label = title or f"Session {sid[:8]}"
        provider_label = model_catalog.PROVIDER_LABELS.get(self._driver_provider(drv), "Assistant")
        note = Gio.Notification.new(f"{provider_label} needs your answer")
        note.set_body(f"{label} asked you a question.")
        try:
            note.set_default_action_and_target_value(
                "app.focus-session", GLib.Variant("s", sid)
            )
        except Exception:
            pass
        app.send_notification(f"helios-question-{sid}", note)
        # Remember it so it can be withdrawn once the question is gone.
        self._notified_question_sids.add(sid)

    def _on_focus_session_action(self, _action, param) -> None:
        sid = param.get_string() if param is not None else ""
        if not sid:
            return
        self.present()
        # reveal_session (not select_session) so a row hidden by the active
        # filter is still surfaced — clicking a notification must always land
        # on its session, even a temporary or otherwise filtered-out one.
        if not self._sessions.reveal_session(sid):
            self._toast("That session is no longer available.")

    # --- live-follow of driver-less transcripts ---

    def _stop_following(self) -> None:
        if self._follow_debounce_id:
            GLib.source_remove(self._follow_debounce_id)
            self._follow_debounce_id = 0
        if self._follow_monitor is not None:
            try:
                self._follow_monitor.cancel()
            except Exception:
                pass
            self._follow_monitor = None

    def _start_following(self, session: Session) -> None:
        """Watch `session`'s transcript and append new records as they're
        written. Only meaningful when no in-process driver is emitting events
        for it; the driver path already live-updates the view."""
        self._stop_following()
        if session is None or session.project.read_only:
            return
        try:
            gfile = Gio.File.new_for_path(str(session.path))
            monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
        except Exception:
            return
        monitor.connect("changed", self._on_follow_changed)
        self._follow_monitor = monitor

    def _on_follow_changed(self, _monitor, _file, _other, event_type) -> None:
        # A "changed" signal already queued when the window closed must not
        # re-arm the debounce against the finalizing tree.
        if self._destroyed:
            return
        # Coalesce bursts (claude writes several records per turn) into one
        # append ~300ms after the last change.
        if self._follow_debounce_id:
            GLib.source_remove(self._follow_debounce_id)
        self._follow_debounce_id = GLib.timeout_add(300, self._flush_follow)

    def _flush_follow(self) -> bool:
        self._follow_debounce_id = 0
        if self._destroyed:
            return False
        try:
            self._transcript.append_new_from_disk()
        except Exception:
            pass
        return False  # one-shot

    def _load_context_fill_async(self, session: Session) -> None:
        """Read the opened session's current context fill from its transcript
        tail (off-thread — remote sessions live on a slow CIFS mount) and show
        it on the meter, so the gauge reflects reality the instant you open a
        session instead of sitting at 0% until the next turn."""
        sid = session.session_id
        self._ctx_fill_sid = sid
        model = self._model
        self._ctx_fill_runner.submit(ContextFillRequest(session, sid, model))

    def _context_fill_work(self, request: ContextFillRequest) -> int:
        return context_fill_for(request.session)

    def _context_fill_done(
        self, request: ContextFillRequest, result: int | Exception
    ) -> None:
        used = 0 if isinstance(result, Exception) else result
        GLib.idle_add(self._apply_context_fill, request.sid, used, request.model)

    def _apply_context_fill(self, sid: str, used: int, model: str) -> bool:
        # Ignore results for a session we've since navigated away from, and
        # never stomp a live driver's authoritative usage report.
        if self._destroyed:
            return False
        if sid != getattr(self, "_ctx_fill_sid", None) or used <= 0:
            return False
        if self._driver is not None and self._driver.is_running:
            return False
        # The saved transcript drops the `[1m]` marker, so derive the window
        # from the selected model — but bump to 1M when the fill exceeds the
        # 200K base (the session was clearly a 1M-context one).
        window = context_window_for_model(model)
        if used > window:
            window = 1_000_000
        self._chat_toolbar.set_context_usage(used, window)
        return False

    # Warn once per (limit type, band) so a long turn cannot spam the same
    # notice. Bands, not a raw percentage, are what make it once-per-crossing.
    _RATE_WARN_BANDS: tuple[int, ...] = (95, 80)

    def _on_rate_limit_updated(self, drv, info: dict) -> None:
        if self._destroyed or not self._drv_is_current(drv):
            return
        self._chat_toolbar.update_rate_limit(info)
        MainWindow._warn_on_rate_limit(self, info)

    def _warn_on_rate_limit(self, info: dict) -> None:
        """Surface the REAL ceiling proactively.

        On a subscription this — not a dollar figure — is the only thing that
        actually stops work, and until now it was disclosed passively in the
        context popover, so the first sign of trouble was a turn failing. A
        blocking status always warns; otherwise the highest band crossed does.
        """

        rl_type = str(info.get("rateLimitType") or "")
        if not rl_type:
            return
        seen = getattr(self, "_rate_warned", None)
        if not isinstance(seen, set):
            seen = set()
            self._rate_warned = seen

        from helios.widgets.chat_toolbar import (
            _humanize_resets,
            rate_limit_label,
        )

        label = rate_limit_label(rl_type, info)
        resets = _humanize_resets(info.get("resetsAt"))
        suffix = f" Resets {resets}." if resets else ""
        status = str(info.get("status") or "").lower()

        if status in {"blocked", "exceeded", "rejected"}:
            key = (rl_type, "blocked")
            if key not in seen:
                seen.add(key)
                provider = str(info.get("provider") or "")
                who = {"openai": "Codex", "anthropic": "Claude"}.get(provider, "the provider")
                self._toast(f"{label} limit reached — {who} is refusing turns.{suffix}")
            return

        used = info.get("usedPercent")
        if not isinstance(used, (int, float)):
            return
        for band in MainWindow._RATE_WARN_BANDS:
            if used >= band:
                key = (rl_type, band)
                if key not in seen:
                    seen.add(key)
                    self._toast(f"{label} limit {int(used)}% used.{suffix}")
                return

    def _on_clear_budget_block(self) -> None:
        """Reopen the selected Work after the budget breaker closed it.

        The breaker stays one-way for a process respawn; a human choice is the
        renewal path. Without this, one trip abandoned the conversation — which
        is what a notional dollar figure did on 2026-08-05.
        """

        if self._destroyed:
            return
        work_id = str(self._current_work_id() or "")
        if not work_id:
            self._toast("Select a Work first — there is nothing to clear.")
            return

        blocked = getattr(self, "_budget_blocked_work_ids", None)
        latched = isinstance(blocked, set) and work_id in blocked
        coordinator = getattr(self, "_work_coordinator", None)
        cleared = False
        if coordinator is not None:
            try:
                work = coordinator.clear_budget_exhausted(
                    work_id=work_id,
                    reason="operator cleared the budget block from the menu",
                )
                cleared = getattr(work, "status", "") == "active"
            except Exception as exc:
                _log.warning("could not clear Work budget block: %s", exc)
                self._toast(f"Could not clear the budget block: {exc}")
                return

        # The in-memory latch closes the gate even when the store is healthy, so
        # it has to be released too or the Work stays blocked until restart.
        if latched:
            blocked.discard(work_id)

        if cleared or latched:
            self._toast("Budget block cleared. This Work can run again.")
        else:
            self._toast("This Work is not blocked by the budget breaker.")
        MainWindow._sync_execution_control(self)

    def _on_native_turn_status(self, drv, payload: dict) -> None:
        status = str(payload.get("status") or "") if isinstance(payload, dict) else ""
        plan = None
        if status in {"interrupted", "failed"}:
            coordinator = getattr(self, "_work_coordinator", None)
            if coordinator is not None:
                plan = coordinator.interrupt_execution_plan(drv, payload)
        if self._destroyed or not self._drv_is_current(drv):
            return
        if plan is not None:
            self._plan.show_execution_plan(plan)
            self._plan_progress.set_plan(plan)
        if status == "inProgress":
            self._plan.begin_native_turn()
            MainWindow._begin_observed_agent_scope(
                self,
                drv,
                str(payload.get("turnId") or ""),
            )

    def _on_native_plan_updated(self, drv, payload: dict) -> None:
        # ``turn/plan/updated`` is the structured authoritative plan. Item
        # plan deltas still flow through the transcript/activity stream.
        if not isinstance(payload, dict) or not (
            payload.get("source") == "turn" or isinstance(payload.get("plan"), list)
        ):
            return
        coordinator = getattr(self, "_work_coordinator", None)
        plan = (
            coordinator.record_execution_plan(drv, payload)
            if coordinator is not None
            else None
        )
        if self._destroyed or not self._drv_is_current(drv):
            return
        if plan is not None:
            self._plan.show_execution_plan(plan)
            self._plan_progress.set_plan(plan)
        else:
            # Keep live visibility if persistence is temporarily unavailable;
            # the durable chip remains unchanged rather than claiming progress.
            self._plan.show_native_plan(payload)

    def _on_native_diff_updated(self, drv, payload: dict) -> None:
        if not self._destroyed and self._drv_is_current(drv):
            self._plan.show_native_diff(payload)

    def _on_context_compacted(self, drv, info: dict) -> None:
        """Surface one provider-owned compaction boundary.

        Claude and Codex emit the same signal shape. Automatic compaction can
        arrive inside a user turn and must not clear that turn's busy state;
        Codex manual compaction is a standalone maintenance turn and is idle
        before its completion signal is emitted.
        """

        if self._destroyed or not self._drv_is_current(drv):
            return
        trigger = str((info or {}).get("trigger") or "auto")
        pre = int((info or {}).get("pre_tokens") or 0)
        post = int((info or {}).get("post_tokens") or 0)
        # Claude and Codex compact by summarising provider-side, so the older
        # turns survive in compressed form. Helios's own OpenRouter compaction
        # drops whole oldest exchanges outright and says so, because telling a
        # user their conversation "is now a summary" when it is simply gone is
        # the difference between a boundary and a false reassurance.
        summarized = (info or {}).get("summarized", True) is not False
        dropped = int((info or {}).get("dropped") or 0)
        self._chat_toolbar.note_compaction(trigger, pre)
        transcript = getattr(self, "_transcript", None)
        if transcript is not None:
            detail = f" — {pre:,} → {post:,} tokens" if pre and post else ""
            if summarized:
                fate = "Earlier turns were replaced by a summary."
            elif dropped:
                fate = (
                    f"The {dropped} oldest message(s) were dropped to fit the "
                    "context window; they are still in this transcript but the "
                    "model can no longer see them."
                )
            else:
                fate = (
                    "The oldest exchanges were dropped to fit the context "
                    "window; they are still in this transcript but the model "
                    "can no longer see them."
                )
            boundary = Turn(role="system", is_meta=True)
            boundary.add("text", f"Context compacted here{detail}. {fate}")
            append_marker = getattr(transcript, "append_meta_turn", None)
            if callable(append_marker):
                append_marker(boundary)
            else:
                transcript.append_turn(boundary)
        if trigger != "manual":
            freed = f" ({pre:,} tokens)" if pre > 0 else ""
            tail = (
                "older turns are now a summary"
                if summarized
                else "the oldest turns are no longer in the model's context"
            )
            self._toast(f"Context auto-compacted{freed} — {tail}.")
        elif not getattr(drv, "is_busy", False):
            MainWindow._clear_busy_ui(self)
            self._composer.grab_input_focus()

    def _review_current_chat(self, arguments: str = "") -> bool:
        """Dispatch a native, read-only GPT Review control action."""

        drv = getattr(self, "_driver", None)
        if not isinstance(drv, CodexAppServerDriver):
            self._toast("Native Review is available only for a connected GPT chat.")
            return False
        visible_text = f"/review {arguments.strip()}" if arguments.strip() else "/review"
        self._native_pending_user[id(drv)] = (drv, visible_text)
        admissions = getattr(self, "_provider_dispatch_in_progress", None)
        if not isinstance(admissions, set):
            admissions = set()
            self._provider_dispatch_in_progress = admissions
        admissions.add(id(drv))
        try:
            delivery = drv.request_review(arguments)
        except Exception as exc:
            # Driver state can change between the pre-dispatch check and IPC
            # delivery. An escaping exception left _native_pending_user
            # populated, so the next accepted prompt rendered as this /review
            # (a review finding). The compaction path already treats this as a
            # fallible boundary.
            _log.warning("native review request failed: %s", exc)
            pending = self._native_pending_user.get(id(drv))
            if pending == (drv, visible_text):
                self._native_pending_user.pop(id(drv), None)
            self._toast(f"Couldn't start Review: {exc}")
            return False
        finally:
            admissions.discard(id(drv))
        if delivery.rejected:
            pending = self._native_pending_user.get(id(drv))
            if pending == (drv, visible_text):
                self._native_pending_user.pop(id(drv), None)
            return False
        if delivery.uncertain:
            # The driver has already quarantined/closed the acceptance-unknown
            # request. Returning True consumes the slash action so it cannot be
            # replayed automatically with a second native Review.
            return True
        self._composer.set_busy(True)
        self._chat_toolbar.set_busy(True)
        self._activity.set_activity(STATE_REVIEWING)
        self._toast("Reviewing changes…")
        return True

    def _fork_current_chat(self) -> bool:
        """Request one persisted native GPT fork at the conversation head."""

        drv = getattr(self, "_driver", None)
        if not isinstance(drv, CodexAppServerDriver):
            self._toast("Native Fork is available only for a connected GPT chat.")
            return False
        try:
            delivery = drv.request_fork()
        except Exception as exc:
            _log.warning("native fork request failed: %s", exc)
            self._toast(f"Couldn't fork the conversation: {exc}")
            return False
        if delivery.rejected:
            return False
        if delivery.pending:
            self._composer.set_busy(True)
            self._chat_toolbar.set_busy(True)
            self._activity.set_activity(STATE_THINKING)
            self._toast("Forking conversation…")
        # UNKNOWN is consumed, never restored/replayed. The driver's error names
        # the native uncertainty and leaves the source conversation untouched.
        return True

    def _on_codex_thread_forked(self, drv, info: dict) -> None:
        """Make an accepted native fork a durable, independently bound chat."""

        payload = info if isinstance(info, dict) else {}
        source_thread_id = str(payload.get("source_thread_id") or "")
        fork_thread_id = str(payload.get("thread_id") or "")
        was_visible = bool(
            isinstance(drv, CodexAppServerDriver) and self._drv_is_current(drv)
        )
        if was_visible and not self._destroyed:
            # Fork has no normal turn/result signal. Its native request is now
            # terminal, so release the source chat's operation UI before local
            # mirror and Work registration (including every failure return).
            MainWindow._clear_busy_ui(self)
        if (
            not isinstance(drv, CodexAppServerDriver)
            or not source_thread_id
            or not fork_thread_id
            or source_thread_id != str(getattr(drv, "session_id", "") or "")
            or source_thread_id == fork_thread_id
        ):
            _log.warning("ignored invalid Codex fork result: %r", payload)
            return

        cwd = str(getattr(drv, "cwd", "") or getattr(drv, "_cwd", "") or "")
        model = str(getattr(drv, "model", "") or "")
        try:
            mirror_path = clone_transcript_for_fork(
                cwd=cwd,
                source_thread_id=source_thread_id,
                fork_thread_id=fork_thread_id,
            )
        except Exception as exc:
            _log.warning("could not mirror native Codex fork %s: %s", fork_thread_id, exc)
            if not self._destroyed:
                self._toast(
                    "Codex created the native fork, but Helios could not create "
                    "its local transcript mirror.",
                    timeout=8,
                )
            return

        coordinator = getattr(self, "_work_coordinator", None)
        source_work_id = str(
            getattr(drv, "_helios_work_id", "")
            or getattr(drv, "_helios_work_id_hint", "")
            or ""
        )
        try:
            if coordinator is None:
                raise RuntimeError("Work coordinator is unavailable")
            if not source_work_id:
                source_work = coordinator.resolve_native_work(
                    model_catalog.PROVIDER_OPENAI,
                    source_thread_id,
                    cwd=cwd,
                    model=model,
                )
                source_work_id = source_work.work_id
            coordinator.fork_work(
                source_work_id=source_work_id,
                provider=model_catalog.PROVIDER_OPENAI,
                native_id=fork_thread_id,
                model=model,
            )
        except Exception as exc:
            # This mirror was created by this exact callback and has never been
            # exposed as a usable Work. Remove only it; the native fork and the
            # source transcript remain untouched.
            try:
                mirror_path.unlink(missing_ok=True)
            except OSError:
                pass
            _log.warning("could not bind native Codex fork %s: %s", fork_thread_id, exc)
            if not self._destroyed:
                self._toast(
                    "Codex created the native fork, but Helios could not bind "
                    "its independent Work.",
                    timeout=8,
                )
            return

        source_settings = self._conversation_perms.get_settings(
            model_catalog.PROVIDER_OPENAI,
            source_thread_id,
        )
        if source_settings is not None:
            settings_saved = self._conversation_perms.set(
                model_catalog.PROVIDER_OPENAI,
                fork_thread_id,
                source_settings.permission_mode,
                effort_key=source_settings.effort_key,
                workflow_mode=source_settings.workflow_mode,
            )
        else:
            permission_mode = str(getattr(drv, "permission_mode", "") or "")
            settings_saved = self._conversation_perms.set(
                model_catalog.PROVIDER_OPENAI,
                fork_thread_id,
                permission_mode if permission_mode in PERMISSION_MODES else SAFE_FALLBACK_MODE,
                effort_key=str(getattr(drv, "effort_key", "") or ""),
                workflow_mode=canonical_workflow_mode(
                    getattr(drv, "workflow_mode", "")
                ),
            )
        provider_saved = session_providers.set_provider(
            fork_thread_id,
            model_catalog.PROVIDER_OPENAI,
        )
        if not settings_saved:
            _log.warning("could not clone execution settings to %s", fork_thread_id)
        if not provider_saved:
            # The mirror's helios-codex version marker remains a durable
            # provider-recovery source even if the separate index write fails.
            _log.warning("could not index GPT provider for %s", fork_thread_id)

        if self._destroyed:
            return
        self._sessions.reload(preserve_selection=True, rescan_pool=False)
        should_select = was_visible and not bool(payload.get("stop_requested"))
        if should_select and not self._sessions.reveal_session(fork_thread_id):
            self._toast("Fork created, but its sidebar row could not be selected.")
            return
        if should_select:
            self._toast("Conversation forked into a new independent Work.")
        else:
            self._toast("Conversation fork created in the background.")

    def _compact_current_chat(self, arguments: str = "") -> bool:
        """Run the visible provider's native context-compaction operation.

        Codex uses ``thread/compact/start``; Claude uses its advertised
        ``/compact`` command, which also takes free-text focus instructions
        (``/compact keep the migration plan``). Helios never asks a model to
        imitate either one in prompt text and never builds a competing summary.
        """

        command = next(
            (
                candidate
                for candidate in MainWindow._agent_command_capabilities(self)
                if candidate.command_id == COMPACT_CONTEXT_COMMAND
            ),
            None,
        )
        if command is None or not command.enabled:
            self._toast(
                command.unavailable_reason
                if command is not None and command.unavailable_reason
                else "Native compaction is unavailable for this conversation."
            )
            return False
        drv = self._driver
        try:
            if isinstance(drv, CodexAppServerDriver):
                accepted = drv.request_compaction()
            elif isinstance(drv, ClaudeCliDriver):
                # `with_context=False` or this is a no-op: the prompt-context
                # provider prepends the Work envelope, and a slash command
                # only counts as one when it leads the message.
                focus = str(arguments or "").strip()
                delivery = drv.send_user_text(
                    f"/compact {focus}" if focus else "/compact",
                    with_context=False,
                )
                accepted = not isinstance(delivery, MessageDelivery) or delivery.accepted
            else:
                accepted = False
        except Exception as e:
            _log.warning("could not request compaction: %s", e)
            self._toast(f"Couldn't compact: {e}")
            return False
        if not accepted:
            return False
        self._composer.set_busy(True)
        self._chat_toolbar.set_busy(True)
        self._activity.set_activity(STATE_COMPACTING)
        self._toast("Compacting…")
        return True

    def _on_native_agents_updated(self, drv, snapshot: dict) -> None:
        if self._destroyed:
            return
        if not self._drv_is_current(drv):
            # A background Work's fan-out used to be invisible (audit gap 17):
            # say on its row how many children are still running.
            live = sum(
                1
                for actor in (snapshot or {}).values()
                if isinstance(actor, dict)
                and str(actor.get("status") or "") not in {"complete", "error", "stopped"}
            )
            push = getattr(self, "_push_background_activity", None)
            if not callable(push):
                return
            if live:
                state, detail = native_activity_state({"category": "agents", "count": live})
                push(drv, state, detail)
                drv._helios_bg_fanout_shown = True
            elif getattr(drv, "_helios_bg_fanout_shown", False):
                # The caption said "N subagents running"; say what is true now.
                drv._helios_bg_fanout_shown = False
                busy = bool(getattr(drv, "is_busy", False))
                push(drv, STATE_THINKING if busy else STATE_IDLE, "")
            return
        scope = MainWindow._observed_agent_scope(self, drv)
        if scope is None:
            MainWindow._reset_observed_agent_activity(self)
            return
        if self._agent_activity.scope != scope:
            self._agent_activity.begin_scope(scope)
        projected = self._agent_activity.observe(scope, snapshot)
        install_stop = getattr(self._agent_dock, "set_stop_handler", None)
        if callable(install_stop):
            install_stop(
                self._on_agent_stop_requested
                if callable(getattr(drv, "stop_task", None))
                else None
            )
        self._agent_dock.set_snapshot(projected)
        self._plan.show_agent_activity(projected)

    def _on_agent_stop_requested(self, actor_id: str) -> None:
        """Dock stop button: stop one subagent, not the session."""
        driver = self._driver
        stop = getattr(driver, "stop_task", None)
        if self._destroyed or not callable(stop) or not getattr(driver, "is_running", False):
            return
        if not stop(actor_id):
            self._toast("That agent cannot be stopped on its own yet.", timeout=4)

    def _on_cli_commands_updated(self, drv, _commands) -> None:
        if self._destroyed or not self._drv_is_current(drv):
            return
        MainWindow._sync_composer_commands(self)

    def _sync_composer_commands(self) -> None:
        """Feed the composer's slash popover: Helios palette + the CLI's list."""
        composer = getattr(self, "_composer", None)
        setter = getattr(composer, "set_slash_commands", None)
        if not callable(setter):
            return
        try:
            from helios.backend.slash_commands import normalize_commands
        except ImportError:  # ponytail: discovery slice not merged yet
            return
        driver = getattr(self, "_driver", None)
        cli = []
        if isinstance(driver, ClaudeCliDriver):
            # `initialize.commands` includes the CLI's terminal-only names
            # (measured 2026-09-03: `color`, `doctor`), which the send path
            # correctly treats as prose. Offering them would complete a token
            # that then goes to the model as text, so filter to what the CLI
            # says it accepts here.
            cli = [
                command
                for command in (driver.cli_commands or [])
                if driver.is_slash_command("/" + str(command.get("name") or ""))
            ]
        palette = [
            command
            for command in MainWindow._agent_command_capabilities(self)
            if command.supported
        ]
        setter(normalize_commands([*palette, *cli]))

    def _on_hook_event(self, drv, payload: dict) -> None:
        # The renderer lives beside _on_driver_error (A-7 slice); forward so
        # the connect line does not depend on merge order.
        handler = getattr(self, "_on_claude_hook_event", None)
        if callable(handler):
            handler(drv, payload)

    def _on_question_cancelled(self, drv, token: str) -> None:
        """The CLI withdrew a prompt: drop it from the queue or close its dialog."""
        if self._destroyed:
            return
        # The dedup memory must forget it too: the CLI may reissue the same
        # tool_use_id after cancelling, and a remembered id is dropped unread.
        self._answered_question_ids.pop(token, None)
        self._question_queue = [
            q for q in self._question_queue if not (q[0] is drv and q[2] == token)
        ]
        if self._question_active_key == (drv, token):
            dialog = self._question_dialog
            self._question_active = False
            self._question_active_key = None
            self._question_dialog = None
            closer = getattr(dialog, "force_close", None) or getattr(
                dialog, "close", None
            )
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass
        self._sync_pending_question_indicators()
        self._pump_questions()

    def _on_driver_permission_mode_changed(self, drv, mode: str) -> None:
        """The CLI changed mode itself; mirror it and make it durable.

        Re-asserting through the toolbar path sends `set_permission_mode`
        (so the CLI and the toolbar agree even after a plain plan approval,
        where the CLI's own choice of mode is not on the wire) and saves the
        conversation's execution settings on the ACK.
        """
        if self._destroyed or not self._drv_is_current(drv):
            return
        if mode not in PERMISSION_MODES:
            return
        toolbar = getattr(self, "_chat_toolbar", None)
        if toolbar is not None:
            toolbar.set_permission_mode(mode)
        self._on_toolbar_permission_changed(toolbar, mode)

    def _on_native_goal_updated(self, drv, payload: dict) -> None:
        """Reconcile native status without echoing it back into App Server."""

        if not self._drv_is_current(drv):
            return
        if not isinstance(payload, dict) or payload.get("cleared"):
            # A native clear can come from shutdown or another client. The
            # provider-neutral Work goal remains canonical and must survive.
            self._goal_strip.clear_native_goal()
            return
        native = payload.get("goal")
        if not isinstance(native, dict):
            self._goal_strip.clear_native_goal()
            return

        native_status = str(native.get("status") or "")
        mapped_status = {
            "active": session_goals.GOAL_ACTIVE,
            "paused": session_goals.GOAL_PAUSED,
            "blocked": session_goals.GOAL_BLOCKED,
            "usageLimited": session_goals.GOAL_BLOCKED,
            "budgetLimited": session_goals.GOAL_BLOCKED,
            "complete": session_goals.GOAL_COMPLETE,
        }.get(native_status)
        work_id = str(getattr(drv, "_helios_work_id", "") or "")
        goal_key = work_id or str(getattr(drv, "session_id", "") or "")
        stored = session_goals.get_goal(goal_key) if goal_key else None
        native_objective = str(native.get("objective") or "").strip()
        if (
            stored is not None
            and mapped_status is not None
            and stored.objective.strip() == native_objective
            and stored.status != mapped_status
        ):
            stored.status = mapped_status
            session_goals.set_goal(goal_key, stored)
            stored = session_goals.get_goal(goal_key) or stored
            coordinator = getattr(self, "_work_coordinator", None)
            if coordinator is not None and work_id:
                try:
                    coordinator.record_goal(
                        work_id=work_id,
                        provider=self._driver_provider(drv),
                        goal=stored,
                    )
                except Exception as exc:
                    _log.warning(
                        "could not record native Goal status for %s: %s",
                        work_id,
                        exc,
                    )
            self._refresh_goal_strip()
        self._goal_strip.set_native_goal(payload)

    def _set_row_activity(self, drv, summary: str) -> None:
        """Write one driver's live-activity caption to its sidebar row.

        No-ops without a native session id: a driver that has not had its
        `system/init` / `thread/started` yet has no row to write to, and
        inventing a key would strand the text on a row that never appears.
        """

        sessions = getattr(self, "_sessions", None)
        if sessions is None:
            return
        session_id = str(getattr(drv, "session_id", "") or "")
        if not session_id:
            return
        sessions.set_live_activity(session_id, summary)

    def _remember_activity(self, drv, state: str, detail: str) -> str:
        """Record what a driver is doing, visible or not.

        Kept for every driver rather than only background ones, because the
        visible one becomes a background one the moment the user switches
        and its row is owed a caption at that instant.
        """

        summary = activity_summary(state, detail)
        drv._activity_summary = summary
        return summary

    def _push_background_activity(self, drv, state: str, detail: str) -> None:
        """Record a non-visible driver's activity and show it on its row."""

        self._set_row_activity(drv, self._remember_activity(drv, state, detail))

    def _on_native_activity_updated(self, drv, payload: dict) -> None:
        if self._destroyed:
            return
        state, detail = native_activity_state(payload)
        if not self._drv_is_current(drv):
            self._push_background_activity(drv, state, detail)
            return
        self._remember_activity(drv, state, detail)
        self._activity.set_activity(state, detail)

    def _on_native_mcp_status_updated(self, _drv, snapshot) -> None:
        # MCP inventory belongs to the shared App Server connection, not only
        # the currently visible thread. Keep an already-open Settings surface
        # live when a foreground or background GPT session finishes discovery.
        # Codex async discovery can land after the driver/window is torn down;
        # never touch the (finalizing) Settings dialog then.
        if self._destroyed:
            return
        dialog = self._settings_dialog
        if dialog is not None:
            dialog.set_codex_mcp_servers(snapshot)
        MainWindow._refresh_capabilities(self)

    def _on_codex_transport_status(self, drv, payload: dict) -> None:
        if self._destroyed or not self._drv_is_current(drv):
            return
        if payload.get("transport") == "exec":
            reason = str(payload.get("reason") or "native transport unavailable")
            self._toast(
                f"Codex is using the compatibility transport: {reason}",
                timeout=7,
            )

    def _on_question_asked(self, drv, input_payload: dict, tool_use_id: str) -> None:
        """Claude invoked AskUserQuestion. Enqueue a dialog and route the answer
        back to the asking driver.

        Unlike the UI-painting callbacks, this is NOT sender-guarded: a
        *background* session that asks a question would otherwise stall forever
        waiting on an answer nobody can give. We present regardless of which
        session is visible, surface which session it's for, and call
        `answer_question` on the SPECIFIC driver that asked.

        Questions are *queued* and shown one at a time: several sessions can ask
        at once, and presenting each immediately would stack modal dialogs on
        the single window.
        """
        # Fail closed after window close: a question arriving as drivers are
        # stopped has no window to present in, so drop it rather than enqueue a
        # dialog that would present against a finalizing tree. The driver is
        # being terminated regardless.
        if self._destroyed:
            return
        # Guard against duplicate emissions for the same tool_use_id.
        # Pruned FIFO so the dedup memory stays bounded for long windows.
        if tool_use_id in self._answered_question_ids:
            return
        self._answered_question_ids[tool_use_id] = None
        while len(self._answered_question_ids) > 256:
            self._answered_question_ids.pop(next(iter(self._answered_question_ids)))

        self._question_queue.append((drv, input_payload, tool_use_id))
        self._pump_questions()
        # A question from a NON-visible session was deferred to a row dot — also
        # fire a desktop notification, the reliable channel when that session's
        # row is filtered out of the sidebar (the dot alone could be invisible).
        if not self._drv_is_current(drv) and any(
            q[0] is drv and q[2] == tool_use_id for q in self._question_queue
        ):
            self._notify_pending_question(drv)

    def _pump_questions(self) -> None:
        """Present the CURRENT session's next queued question, if no dialog is
        showing. A background session's question stays queued and is surfaced as
        a subtle 'needs you' row dot instead of a focus-stealing modal; it
        presents when the user switches to that session."""
        if self._destroyed:
            return
        # Drop questions whose driver has since died (the answer is moot).
        self._question_queue = [q for q in self._question_queue if q[0].is_running]
        if self._question_active:
            # A dialog is already up — just keep the background dots current.
            self._sync_pending_question_indicators()
            return
        # Present only the VISIBLE session's question; leave background ones
        # queued. Scan the whole queue so a queued background question can never
        # head-of-line-block the current session's.
        target = next(
            (i for i, q in enumerate(self._question_queue) if self._drv_is_current(q[0])),
            None,
        )
        if target is None:
            self._sync_pending_question_indicators()
            return
        drv, input_payload, tool_use_id = self._question_queue.pop(target)
        self._question_active = True
        self._question_active_key = (drv, tool_use_id)
        # The presented question is out of the queue now, so its own row shows
        # no dot; refresh the rest.
        self._sync_pending_question_indicators()

        # Local import to avoid module-load cost when no questions are asked.
        from helios.widgets.question_dialog import present_question

        active_key = (drv, tool_use_id)

        def _finish() -> None:
            # AlertDialog.close() may deliver its response after another
            # queued dialog has already been presented. Never let the old
            # callback clear that newer dialog's state.
            if self._question_active_key != active_key:
                return
            self._question_active = False
            self._question_active_key = None
            self._question_dialog = None
            self._pump_questions()  # show the next queued question, if any

        def on_answer(text) -> None:
            if self._question_active_key != active_key:
                return
            if drv.is_running:
                drv.answer_question(tool_use_id, text)
            _finish()

        def on_dismiss() -> None:
            if self._question_active_key != active_key:
                return
            if drv.is_running:
                drv.answer_question(tool_use_id, None)
            _finish()

        dialog = present_question(
            self,
            input_payload,
            on_answer,
            on_dismiss,
        )
        # A malformed/empty payload may dismiss synchronously. Its `_finish`
        # callback can pump and install the next dialog before this call
        # returns, so never overwrite that newer dialog with the old result.
        if self._question_active_key == active_key:
            self._question_dialog = dialog

    def _sync_pending_question_indicators(self) -> None:
        """Mark each session with a still-queued question so its row shows a
        'needs you' dot. The active (being-shown) question is out of the queue,
        so its row shows no dot while its dialog is up."""
        # Late driver signals (interaction-resolved, teardown) can fire after
        # close; the durable queue cleanup still runs, but never touch the
        # finalizing session-list UI once destroyed (close-safety invariant).
        if self._destroyed:
            return
        pending = {
            q[0].session_id
            for q in self._question_queue
            if getattr(q[0], "session_id", "") and q[0].is_running
        }
        sessions = getattr(self, "_sessions", None)
        if sessions is not None:
            sessions.set_pending_questions(pending)
        # Withdraw 'needs your answer' notifications for sessions whose question
        # has since left the queue (answered / resolved / now visible), so the
        # notification never outlives the question it announced.
        stale = self._notified_question_sids - pending
        if stale:
            app = self.get_application()
            if app is not None:
                for sid in stale:
                    app.withdraw_notification(f"helios-question-{sid}")
            self._notified_question_sids -= stale

    def _on_interaction_resolved(self, drv, tool_use_id: str) -> None:
        """Remove an App Server prompt that the turn resolved elsewhere."""

        self._question_queue = [
            queued
            for queued in self._question_queue
            if not (queued[0] is drv and queued[2] == tool_use_id)
        ]
        # The queue shrank — clear any now-stale 'needs you' dot even on the
        # (common) path where the resolved prompt wasn't the active dialog.
        self._sync_pending_question_indicators()
        if self._question_active_key != (drv, tool_use_id):
            return
        dialog = self._question_dialog
        self._question_active = False
        self._question_active_key = None
        self._question_dialog = None
        # State cleared above (durable); only closing the dialog is a GTK op,
        # which the window's own close already handles once _destroyed.
        if dialog is not None and not self._destroyed:
            try:
                dialog.close()
            except Exception:
                pass
        self._pump_questions()

    def _resolve_questions_for_driver(self, drv) -> None:
        """Fail closed and remove every modal owned by a departing driver."""

        removed = [row for row in self._question_queue if row[0] is drv]
        self._question_queue = [row for row in self._question_queue if row[0] is not drv]
        for _owner, _payload, tool_use_id in removed:
            self._answered_question_ids.pop(tool_use_id, None)
            if drv.is_running:
                drv.answer_question(tool_use_id, None)
        # The departing driver's queued prompts are gone — reconcile dots even
        # when it had no *active* dialog (the early-return path below).
        self._sync_pending_question_indicators()
        active = self._question_active_key
        if active is None or active[0] is not drv:
            return
        tool_use_id = active[1]
        self._answered_question_ids.pop(tool_use_id, None)
        if drv.is_running:
            drv.answer_question(tool_use_id, None)
        dialog = self._question_dialog
        self._question_active = False
        self._question_active_key = None
        self._question_dialog = None
        # Driver failed closed above (durable); only the dialog.close() is GTK.
        if dialog is not None and not self._destroyed:
            try:
                dialog.close()
            except Exception:
                pass
        self._pump_questions()

    def _on_toolbar_model_changed(self, _toolbar, alias: str) -> None:
        # Takes effect on the next driver spawn. If a driver is running, we
        # could optionally restart it; for now we just queue.
        self._apply_model_choice(alias)

    def _on_toolbar_effort_changed(self, _toolbar, key: str) -> None:
        if self._destroyed:
            return
        if MainWindow._execution_target_lock_reason(self):
            self._sync_execution_control()
            return
        target = self._next_chat
        if target is None or target.project is None:
            self._sync_execution_control()
            return
        driver = self._driver
        live = (
            driver is not None
            and driver.is_accepting_input
            and self._driver_matches_selected_provider(driver)
            and self._driver_matches_target_binding(driver)
        )
        provider = (
            self._driver_provider(driver)
            if live
            else MainWindow._execution_target_provider(self)
        )

        if not live:
            _permission, previous_effort = MainWindow._execution_settings_for_spawn(
                self,
                target,
                provider,
            )
            session_id = MainWindow._execution_target_session_id(
                self,
                target,
                provider,
            )
            if provider == model_catalog.PROVIDER_OPENAI and key == "ultra":
                # Ultra can proactively delegate. Honor an explicit selection
                # for this one spawn, but never turn it into a sticky setting
                # for future Works. This also keeps the success toast truthful:
                # the staged value is what `_execution_effort_for_spawn` uses.
                self._staged_effort_key = key
                self._sync_execution_control()
                if previous_effort != key:
                    self._toast(
                        "Reasoning set to Ultra for this conversation "
                        "(one session only)."
                    )
                return
            if session_id:
                permission_mode = MainWindow._stored_conversation_permission(
                    self
                ) or self._effective_permission_mode(target.project.cwd)
                saved = MainWindow._save_conversation_effort(
                    self,
                    provider,
                    session_id,
                    key,
                    permission_mode=permission_mode,
                )
                if not saved:
                    self._toast("Could not save reasoning for this conversation.")
                    self._sync_execution_control()
                    return
                self._staged_effort_key = ""
            else:
                self._staged_effort_key = key
            # No toast — same reason as the permission path: the toolbar's
            # reasoning label already shows the new value.
            self._sync_execution_control()
            return

        if str(getattr(driver, "effort_key", "") or "") == key:
            session_id = str(
                getattr(driver, "session_id", "")
                or MainWindow._execution_target_session_id(self, target, provider)
            )
            if session_id:
                MainWindow._save_conversation_effort(
                    self,
                    provider,
                    session_id,
                    key,
                    permission_mode=str(
                        getattr(driver, "permission_mode", "")
                        or self._effective_permission_mode(target.project.cwd)
                    ),
                )
            self._sync_execution_control()
            return

        changes = self._effort_changes
        if driver in changes:
            self._sync_execution_control()
            return
        token = object()
        changes[driver] = token
        session_id = str(
            getattr(driver, "session_id", "")
            or MainWindow._execution_target_session_id(self, target, provider)
        )
        self._sync_execution_control()

        def applied(success: bool, detail: str = "") -> None:
            if self._effort_changes.get(driver) is not token:
                return
            del self._effort_changes[driver]
            durable_saved = True
            if success:
                durable_id = str(getattr(driver, "session_id", "") or session_id)
                if durable_id:
                    durable_saved = MainWindow._save_conversation_effort(
                        self,
                        provider,
                        durable_id,
                        key,
                        permission_mode=str(
                            getattr(driver, "permission_mode", "")
                            or self._effective_permission_mode(target.project.cwd)
                        ),
                    )
                else:
                    driver._helios_staged_effort_key = key
            restart_required = bool(
                not success
                and getattr(driver, "execution_restart_required", False)
            )
            if restart_required:
                self._teardown_driver(driver)
            if getattr(self, "_destroyed", False):
                return
            if success and durable_saved:
                pass  # see above: the toolbar label is the confirmation
            elif success:
                self._toast(
                    "Reasoning changed for the running conversation, but could "
                    "not be saved for restart.",
                    timeout=6,
                )
            else:
                message = detail or "the provider rejected the change"
                self._toast(f"Could not change reasoning: {message}", timeout=6)
            self._sync_execution_control()

        try:
            accepted = driver.set_effort(key, applied)
        except Exception as exc:
            _log.exception("reasoning change failed")
            applied(False, str(exc))
            return
        if not accepted and self._effort_changes.get(driver) is token:
            applied(False, "the conversation is not idle")

    def _on_sessions_deleted(self, _list, deleted_ids: list[str]) -> None:
        if not deleted_ids:
            return
        n = len(deleted_ids)
        self._toast(f"Deleted {n} session{'s' if n != 1 else ''}.")
        # Stop any live driver bound to a deleted session (could be background).
        for sid in deleted_ids:
            resolution = session_providers.resolve_provider(
                sid,
                conversation_store=self._conversation_perms,
            )
            if resolution.known:
                self._conversation_perms.delete(resolution.provider, sid)
            else:
                _log.warning(
                    "kept execution settings for unresolved deleted id %s",
                    sid,
                )
            coordinator = getattr(self, "_work_coordinator", None)
            detached_work_id = ""
            if coordinator is not None:
                try:
                    detached_work_id = coordinator.detach_native(sid)
                except Exception as exc:
                    _log.warning("could not retire deleted binding %s: %s", sid, exc)
            if not detached_work_id:
                # Compatibility-only legacy goals are owned by the native
                # transcript. Canonical Work goals survive participant delete.
                session_goals.clear_goal(sid)
            drv = self._driver_manager.driver_for_session(sid)
            if drv is not None:
                self._teardown_driver(drv)
        # If the user was staging a resume against a deleted session, clear it.
        self._next_chat = clear_deleted_resume(self._next_chat, set(deleted_ids))
        # Selecting a different (or no) session also resets transcript view —
        # SessionList.set_project re-selects the first row, which fires
        # session-selected and rebuilds the transcript pane.

    def _on_driver_error(self, drv, message: str) -> None:
        # Durable first: return an un-accepted prompt to its author. When the
        # window is closing this routes to the non-UI draft bucket (see
        # _return_native_pending_user), never the composer.
        # A read/process error can also arrive while the pre-turn checkpoint
        # still owns FIRST. Consume that token before clearing the busy UI;
        # otherwise a late worker/deadline callback could send after the error
        # made the window look idle.
        checkpoint = MainWindow._take_checkpoint_dispatch(self, drv)
        recovered_prompt = checkpoint is not None
        if checkpoint is not None:
            MainWindow._preserve_driver_queue(
                self,
                drv,
                leading_texts=(checkpoint.text,),
            )
        unknown_delivery = MainWindow._quarantine_nonreplayable_native_recovery(
            self,
            drv,
        )
        native_pending = getattr(self, "_native_pending_user", {})
        has_native_pending = bool(
            isinstance(native_pending, dict)
            and native_pending.get(id(drv), (None,))[0] is drv
        )
        if not has_native_pending:
            recovered_prompt = (
                MainWindow._restore_rejected_native_recovery_anchor(self, drv)
                or recovered_prompt
            )
        admission_in_progress = (
            id(drv)
            in getattr(
                self,
                "_provider_dispatch_in_progress",
                set(),
            )
            or bool(getattr(drv, "_queue_dispatch_in_progress", False))
        )
        if not drv.is_busy and not admission_in_progress:
            return_identity = getattr(
                self,
                "_return_identity_pending_user",
                None,
            )
            if self._drv_is_current(drv) and not self._destroyed:
                # Queue/current are newer than the provider-pending FIRST.
                # Recover them first so the return helpers can prepend FIRST.
                MainWindow._preserve_driver_queue(self, drv)
                recovered_prompt = (
                    self._return_native_pending_user(drv) or recovered_prompt
                )
                if callable(return_identity):
                    recovered_prompt = return_identity(drv) or recovered_prompt
            else:
                # Background recovery appends to a bucket, so FIRST must be
                # returned before its queued successors.
                recovered_prompt = (
                    self._return_native_pending_user(drv) or recovered_prompt
                )
                if callable(return_identity):
                    recovered_prompt = return_identity(drv) or recovered_prompt
                MainWindow._preserve_driver_queue(self, drv)
        # Everything below is UI (toasts + busy state). Claude deliberately
        # parses terminal stdout after stop, so this error can arrive after
        # close — do not touch the finalizing tree.
        if self._destroyed:
            return
        if not self._drv_is_current(drv):
            if unknown_delivery:
                self._toast(
                    "A background GPT prompt has unknown delivery and was not "
                    "restored. Inspect its native thread before retrying."
                )
            elif recovered_prompt:
                self._toast(
                    "A background prompt was not accepted and will be "
                    "restored when you reopen that Work."
                )
            return
        self._transcript.append_error(message)
        self._toast(message)
        if admission_in_progress:
            # The synchronous send frame still owns prompt/FIFO recovery and
            # the provider may remain live after an uncertain handoff. It will
            # clear busy state after a definite rejection, or the normal exit /
            # terminal path will clear it once ambiguity resolves.
            if drv.is_busy:
                self._composer.set_busy(True)
                self._chat_toolbar.set_busy(True)
                self._activity.set_activity(STATE_THINKING)
            return
        self._composer.set_busy(False)
        self._chat_toolbar.set_busy(False)
        self._activity.clear()

    def _on_claude_hook_event(self, drv, payload: dict) -> None:
        """Surface a hook lifecycle notice — blocked, asked, or failed — in
        the transcript. hook_started/hook_progress and a clean hook_response
        summarize to None and are dropped; see backend.hook_events.
        """
        if self._destroyed or not self._drv_is_current(drv):
            return
        from helios.backend.hook_events import summarize_hook

        notice = summarize_hook(payload)
        if notice is None:
            return
        self._transcript.append_notice(notice.title, notice.detail, notice.severity)

    def _forget_driver(self, drv: ClaudeCliDriver) -> None:
        """Remove a driver from the registry + disconnect its handlers. Used
        on exit and on hard teardown. Unbinds the visible UI if it was bound."""
        checkpoint = MainWindow._take_checkpoint_dispatch(self, drv)
        leading = (checkpoint.text,) if checkpoint is not None else ()
        MainWindow._recover_driver_for_cancellation(
            self,
            drv,
            leading_texts=leading,
            terminal=True,
        )
        self._resolve_questions_for_driver(drv)
        self._driver_manager.forget(drv)

    def _on_driver_exited(self, drv, code: int) -> None:
        was_visible = self._drv_is_current(drv)
        checkpoint = MainWindow._take_checkpoint_dispatch(self, drv)
        # Collect every ownership layer before choosing a recovery surface.
        # These states are normally mutually exclusive, but aggregating them
        # makes exit robust to a provider emitting ``exited`` between adjacent
        # GTK callbacks. FIRST must stay ahead of queue entries and any draft
        # the user typed after submitting.
        recovered: list[str] = []
        if checkpoint is not None:
            recovered.append(checkpoint.text)
        state = MainWindow._native_delivery_state(drv)
        native_pending = MainWindow._take_native_pending_user(self, drv)
        anchor = MainWindow._take_native_recovery_anchor(self, drv)
        uncertain_native = False
        anchored_preserved = 0
        if state in (
            NATIVE_DELIVERY_WIRE,
            NATIVE_DELIVERY_ACCEPTED,
            NATIVE_DELIVERY_UNKNOWN,
        ):
            delivery = getattr(drv, "_pending_queue_delivery", None)
            queue_id = anchor.queue_id if anchor is not None else None
            if queue_id is None and isinstance(delivery, tuple) and len(delivery) == 2:
                queue_id = int(delivery[0])
            quarantined_delivery = None
            quarantine = getattr(drv, "_quarantine_pending_queue_delivery", None)
            if callable(quarantine) and delivery is not None:
                quarantined_delivery = quarantine()
            if queue_id is not None:
                if (
                    quarantined_delivery is None
                    or int(quarantined_delivery[0]) != queue_id
                ):
                    if hasattr(drv, "_pending_queue_delivery"):
                        drv._pending_queue_delivery = None
                    remove_queued = getattr(drv, "remove_queued", None)
                    if callable(remove_queued):
                        remove_queued(queue_id)
                if not self._destroyed and was_visible:
                    self._transcript.remove_queued(queue_id)
            elif hasattr(drv, "_pending_queue_delivery"):
                drv._pending_queue_delivery = None
            uncertain_native = bool(
                native_pending is not None or anchor is not None or queue_id is not None
            )
            native_pending = None
            anchor = None
            if uncertain_native and not self._destroyed and was_visible:
                MainWindow._restore_native_unsent_drafts(self, drv)
        elif anchor is not None:
            if native_pending == anchor.text:
                native_pending = None
            MainWindow._restore_native_recovery_anchor_text(
                self,
                drv,
                anchor,
                force_background=self._destroyed or not was_visible,
            )
            anchored_preserved = 1
        if native_pending is not None:
            recovered.append(native_pending)
        identity_pending = MainWindow._take_identity_pending_user(self, drv)
        if identity_pending is not None:
            recovered.append(identity_pending)
        uncertain_provider = (
            MainWindow._take_uncertain_pending_user(self, drv) is not None
        )
        uncertain_queued = MainWindow._quarantine_uncertain_queue_delivery(
            self,
            drv,
        )
        queued = drv.take_queued() if hasattr(drv, "take_queued") else []
        if hasattr(drv, "_pending_queue_delivery"):
            drv._pending_queue_delivery = None
        recovered.extend(queued)
        if recovered and (self._destroyed or not was_visible):
            drafts = getattr(self, "_native_unsent_drafts", None)
            if not isinstance(drafts, dict):
                drafts = {}
                self._native_unsent_drafts = drafts
            bucket = drafts.setdefault(MainWindow._native_draft_key(drv), [])
            bucket.extend(recovered)
        # Disconnect handlers and publish the new live-id set before any UI can
        # claim the driver is gone. Recovery ownership above has been drained,
        # so the cleanup helpers inside this call are deliberate no-ops.
        self._forget_driver(drv)
        if self._destroyed:
            return
        if recovered or anchored_preserved:
            n = len(recovered) + anchored_preserved
            if was_visible:
                if queued:
                    self._transcript.clear_queued()
                existing = self._composer.current_text().strip()
                if existing:
                    recovered.append(existing)
                self._composer.set_text("\n\n".join(recovered))
                self._toast(
                    f"Session ended — {n} unsent message{'s' if n != 1 else ''} "
                    "returned to the composer."
                )
            else:
                self._toast(
                    f"A background session ended with {n} unsent "
                    f"message{'s' if n != 1 else ''} preserved."
                )
        if uncertain_native or uncertain_provider or uncertain_queued:
            self._toast(
                "A provider prompt had unknown delivery and was not restored. "
                "Inspect its durable recovery record before retrying."
            )
        if not was_visible:
            # A background session ended, or the window is closing — sidebar
            # dots refresh on their own tick; nothing to do to the visible UI.
            return
        MainWindow._reset_observed_agent_activity(self)
        self._composer.set_busy(False)
        self._chat_toolbar.set_busy(False)
        self._activity.clear()
        # The visible driver is gone; stop advertising its live state and
        # restore the selected conversation's durable execution settings.
        self._sync_execution_control()
        if code != 0:
            name = getattr(drv, "display_name", "claude")
            self._toast(f"{name} exited with code {code}")

    def _teardown_driver(self, driver: ClaudeCliDriver | None = None) -> None:
        """Hard stop: disconnect handlers AND kill the subprocess. Used when a
        session is deleted out from under its driver, or on app close.
        Defaults to the visible driver."""
        old = driver if driver is not None else self._driver
        if old is None:
            return
        was_visible = self._drv_is_current(old)
        checkpoint = MainWindow._take_checkpoint_dispatch(self, old)
        leading = (checkpoint.text,) if checkpoint is not None else ()
        MainWindow._recover_driver_for_cancellation(
            self,
            old,
            leading_texts=leading,
            terminal=True,
        )
        self._resolve_questions_for_driver(old)
        # interrupt=False: the user deleted the session / is closing — they
        # did NOT ask to interrupt, so don't let claude stamp
        # "[Request interrupted by user]" into the JSONL.
        self._driver_manager.teardown(old, interrupt=False)
        if was_visible:
            if not self._destroyed:
                MainWindow._reset_observed_agent_activity(self)
            self._clear_busy_ui()
            # Hard teardown suppresses the normal exit handler, so refresh here
            # too — a killed Bypass child must not linger as the effective mode.
            if not self._destroyed:
                self._sync_execution_control()

    # --- toasts + motion ---

    def _sync_motion(self, settings: Gtk.Settings) -> None:
        """Apply `.reduced-motion` to the window when system animations
        are disabled, so CSS keyframes can opt out."""
        try:
            enabled = settings.get_property("gtk-enable-animations")
        except Exception:
            enabled = True
        if enabled:
            self.remove_css_class("reduced-motion")
        else:
            self.add_css_class("reduced-motion")

    def _toast(self, text: str, *, timeout: int = 4) -> None:
        if self._destroyed:
            return
        # One toast at a time. Adw.ToastOverlay QUEUES: without this, five
        # quick changes sit over the composer for 5 x timeout seconds and the
        # last one shows long after the action that caused it. Dismissing the
        # outstanding toast keeps the banner honest (it always describes the
        # most recent event) and bounds the obstruction at one timeout.
        if self._live_toast is not None:
            self._live_toast.dismiss()
        toast = Adw.Toast.new(text)
        toast.set_timeout(timeout)
        toast.connect("dismissed", self._on_toast_dismissed)
        self._live_toast = toast
        self._toast_overlay.add_toast(toast)

    def _on_toast_dismissed(self, toast) -> None:
        if self._live_toast is toast:
            self._live_toast = None

    # --- session hand-off ---

    def _on_handoff_requested(self, _pane, session) -> None:
        if self._destroyed:
            return
        present_handoff_dialog(
            self,
            session,
            on_written=self._on_handoff_written,
            on_error=lambda msg: self._toast(f"Hand-off failed: {msg}", timeout=6),
        )

    def _on_handoff_written(self, key: str) -> None:
        if self._destroyed:
            return
        self._toast(f"Handed off → {key}")
        # Show the new entry at the top of the pane right away.
        self._shared.refresh()

    # --- preferences ---

    def _show_preferences(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.present(self)
            return
        dlg = SettingsDialog(
            self._permission_mode,
            self._model,
            on_codex_credentials_changed=self._on_codex_credentials_changed,
            on_openrouter_credentials_changed=self._on_openrouter_credentials_changed,
        )
        self._settings_dialog = dlg
        dlg.connect("closed", self._on_settings_closed)
        dlg.connect("apply", self._on_prefs_apply)
        # Pool-visibility toggle reshapes the session list right away.
        dlg.connect(
            "pool-visibility-changed",
            lambda *_: self._sessions.reload(preserve_selection=True),
        )
        dlg.present(self)

    def _on_settings_closed(self, dlg, *_args) -> None:
        if self._settings_dialog is dlg:
            self._settings_dialog = None
        if self._destroyed:
            return
        # The OpenRouter shortlist is edited in Settings and read by the model
        # picker — rebuild so a just-ticked model is there when you open it.
        self._chat_toolbar.set_choices(list(self._catalog_entries_by_id.values()))

    # --- checkpoints / rewind ---

    def _checkpoint_cwd(self) -> str:
        target = self._next_chat
        project = target.project if target is not None else None
        return str(getattr(project, "cwd", "") or "")

    def _capture_checkpoint(self, drv, text: str, dispatch) -> None:
        """Snapshot the working tree, THEN release the turn.

        git work happens off the main loop so the UI never freezes, but
        ``dispatch`` is called only once the snapshot has been written — and
        always exactly once, including on failure. A checkpoint that races the
        edits it is supposed to precede would silently capture a half-applied
        turn.
        """
        cwd = self._checkpoint_cwd()
        session_id = str(getattr(drv, "session_id", "") or "")
        generation = MainWindow._begin_checkpoint_dispatch(self, drv, text)
        if not cwd:
            if MainWindow._take_checkpoint_dispatch(
                self,
                drv,
                generation,
            ) is not None:
                dispatch()
            return

        def work() -> None:
            point = None
            try:
                point = checkpoints.snapshot(cwd, text)
            except Exception as exc:  # noqa: BLE001 — a send must never be lost
                _log.warning("checkpoint snapshot raised: %s", exc)
            # One idle callback for both outcomes so `dispatch` cannot be
            # skipped by an early return: no repo, git missing, or a raise all
            # still send the message, just without an undo point.
            GLib.idle_add(
                self._checkpoint_captured,
                drv,
                generation,
                session_id,
                point,
                dispatch,
            )

        threading.Thread(target=work, daemon=True, name="checkpoint").start()
        GLib.timeout_add(
            _CHECKPOINT_DEADLINE_MS,
            self._checkpoint_deadline,
            drv,
            generation,
            dispatch,
        )

    def _checkpoint_captured(
        self,
        drv,
        generation: int,
        session_id: str,
        point,
        dispatch,
    ) -> bool:
        # Worker, deadline, Stop and teardown all compete for the same token on
        # the GTK thread. Only the exact current generation may store or send.
        if MainWindow._take_checkpoint_dispatch(self, drv, generation) is None:
            if point is not None:
                _log.info("discarding checkpoint from a cancelled/stale dispatch")
            return False
        if self._destroyed:
            return False
        if point is not None:
            self._store_checkpoint(drv, session_id, point)
        dispatch()
        return False

    def _checkpoint_deadline(self, drv, generation: int, dispatch) -> bool:
        """Release the turn when the snapshot is taking too long.

        Correctness for the checkpoint must not become a hostage situation for
        the send: a slow clean filter, a network filesystem, or a wedged git
        would otherwise stall the message behind git's own 30s-per-command
        timeout. Past the deadline the turn goes out with no undo point, and
        the user is told so rather than left wondering.
        """
        if self._destroyed or MainWindow._take_checkpoint_dispatch(
            self,
            drv,
            generation,
        ) is None:
            return False
        _log.warning("checkpoint capture exceeded its deadline; sending without one")
        self._toast(
            "Sent without a checkpoint — the snapshot was taking too long. "
            "Rewind is unavailable for this message.",
            timeout=6,
        )
        dispatch()
        return False

    def _store_checkpoint(self, drv, session_id: str, point) -> bool:
        if self._destroyed:
            return False
        sid = session_id or str(getattr(drv, "session_id", "") or "")
        if sid:
            checkpoints.record(sid, point)
        else:
            # No id yet: hold it on the driver so session-started can file it.
            pending = getattr(drv, "_helios_pending_checkpoints", None)
            if pending is None:
                pending = []
                drv._helios_pending_checkpoints = pending
            pending.append(point)
        return False

    def _show_rewind(self) -> None:
        if self._destroyed:
            return
        from helios.widgets.rewind_dialog import RewindDialog

        drv = self._driver
        checkpoint_pending = MainWindow._checkpoint_dispatch_pending_for(self, drv)
        if self._agent_is_writing(drv) or checkpoint_pending:
            # A rewind and a running turn both write the same files. Whichever
            # lands second wins, so the result is neither the checkpoint nor
            # the turn — it is a mix that never existed. Refuse rather than
            # race, and say why.
            self._toast(
                "Stop the current response before rewinding — it is still "
                "writing files.",
                timeout=6,
            )
            return
        if self._restore_in_flight:
            self._toast("A restore is already running.")
            return
        session_id = str(getattr(drv, "session_id", "") or "")
        points = checkpoints.load(session_id) if session_id else []
        # `changes_since` takes a fresh snapshot and shells out to git several
        # times; on a large repo that is seconds. Hand the dialog an async
        # loader so neither opening it nor selecting a checkpoint can freeze
        # the window.
        dialog = RewindDialog(points, load_changes=self._load_changes_async)
        dialog.connect("restore-requested", self._on_restore_requested)
        dialog.present(self)

    def _load_changes_async(self, point, on_ready) -> None:
        """Compute a checkpoint's change list off the main loop."""

        def work() -> None:
            try:
                changes = checkpoints.changes_since(point)
            except Exception as exc:  # noqa: BLE001 — never crash the dialog
                _log.warning("checkpoint diff failed: %s", exc)
                changes = []
            GLib.idle_add(on_ready, changes)

        threading.Thread(target=work, daemon=True, name="checkpoint-diff").start()

    @staticmethod
    def _agent_is_writing(drv) -> bool:
        """Whether a turn is currently in flight and could touch the worktree."""
        return drv is not None and bool(getattr(drv, "is_busy", False))

    def _on_restore_requested(self, _dialog, point, paths) -> None:
        if self._destroyed:
            return
        # Re-checked here, not only when the dialog opened: the agent may have
        # started a queued turn while the user was reading the change list.
        checkpoint_pending = MainWindow._checkpoint_dispatch_pending_for(
            self,
            self._driver,
        )
        if self._agent_is_writing(self._driver) or checkpoint_pending:
            self._toast(
                "Restore cancelled — a response started while the dialog was "
                "open and is writing files.",
                timeout=6,
            )
            return
        if self._restore_in_flight:
            self._toast("A restore is already running.")
            return
        self._restore_in_flight = True

        def work() -> None:
            result = None
            error = ""
            try:
                result = checkpoints.restore(point, list(paths))
            except Exception as exc:  # noqa: BLE001 — never die silently
                # The dialog has already closed. Without this the thread dies
                # and the user is left with no idea whether files moved.
                _log.exception("checkpoint restore failed")
                error = str(exc)
            finally:
                GLib.idle_add(self._report_restore, result, error)

        threading.Thread(target=work, daemon=True, name="checkpoint-restore").start()

    def _report_restore(self, result, error: str = "") -> bool:
        # Cleared unconditionally, before any early return: leaving the flag
        # set would wedge the action for the rest of the session.
        self._restore_in_flight = False
        if self._destroyed:
            return False
        if result is None:
            self._toast(
                f"Restore failed: {error or 'unknown error'}. Your files were "
                "not fully changed — check the log.",
                timeout=8,
            )
            return False
        touched = len(result.restored) + len(result.removed)
        if result.failed:
            self._toast(
                f"Restored {touched} file(s); {len(result.failed)} could not be "
                "changed.",
                timeout=6,
            )
        elif result.kept:
            # Never silent: a refused deletion means the checkpoint and the
            # disk disagree about what predates the turn, and the user needs
            # to know a file they may have expected gone is still there.
            self._toast(
                f"Restored {touched} file(s). Kept {len(result.kept)} that "
                "existed before this checkpoint.",
                timeout=6,
            )
        elif touched:
            note = (
                f" Removed files were moved to {result.trash_dir}."
                if result.trash_dir
                else ""
            )
            self._toast(
                f"Restored {touched} file(s) from the checkpoint.{note}",
                timeout=8 if note else 4,
            )
        else:
            self._toast("Nothing to restore.")
        return False

    def _on_codex_credentials_changed(self) -> None:
        """Refresh app-wide GPT capabilities after durable credential update."""
        if self._destroyed:
            return
        self._refresh_model_catalog(force=True)

    def _on_openrouter_credentials_changed(self) -> None:
        """Refresh app-wide OpenRouter capabilities after key change."""
        if self._destroyed:
            return
        self._refresh_model_catalog(force=True)
        self._sync_new_chat_actions()
        self._sync_openrouter_toggle_sensitivity()
        self._refresh_openrouter_credits()

    def _on_prefs_apply(self, dlg, permission_mode: str, model: str) -> None:
        # Retired or corrupt values cannot become executable through a stale
        # dialog or direct callback; the current picker exposes only valid
        # scoped modes.
        permission_mode = sanitize_global_default(permission_mode)
        self._permission_mode = permission_mode
        self._ui_state.set("permission_mode", permission_mode)
        # Retain the marker for older settings migrations. It never widens a
        # mode rejected by sanitize_global_default/resolve_startup_default.
        self._ui_state.set("permission_mode_confirmed", True)
        # A changed global fallback immediately affects any selected
        # conversation that has no explicit conversation-owned mode.
        self._sync_execution_control()
        # Keep toolbar label + provider toggle + per-provider memory in sync;
        # quiet=True because the toast below already covers it.
        self._apply_model_choice(model, quiet=True)
        self._toast(
            f"Chat defaults saved — {MODE_LABELS.get(permission_mode, permission_mode)} "
            f"permissions, model {model or 'default'}."
        )

    # --- close ---

    def _on_close_request(self, *_args) -> bool:
        # Tear down timers/threads BEFORE we start stopping drivers: a periodic
        # timer or a daemon thread's idle_add firing against the finalizing
        # widget tree is a classic GTK shutdown crash. The _destroyed flag also
        # makes every guarded idle callback a no-op from here on.
        if self._destroyed:
            return False
        self._destroyed = True
        changes = getattr(self, "_changes", None)
        if changes is not None:
            changes.close()
        # Invalidate every pre-provider dispatch before any worker/deadline idle
        # can land. Closing is not consent to send; the unsent text stays in the
        # same in-memory recovery bucket used by other background teardown.
        drivers_for_shutdown = getattr(
            self._driver_manager,
            "drivers_for_shutdown",
            None,
        )
        if callable(drivers_for_shutdown):
            for drv in drivers_for_shutdown():
                checkpoint = MainWindow._take_checkpoint_dispatch(self, drv)
                MainWindow._recover_driver_for_cancellation(
                    self,
                    drv,
                    leading_texts=(
                        (checkpoint.text,) if checkpoint is not None else ()
                    ),
                    terminal=True,
                )
        for attr in (
            "_reap_timer_id",
            "_archive_timer_id",
            "_startup_archive_timer_id",
            "_orphan_sweep_timer_id",
            "_router_dispatch_timer_id",
            "_catalog_tick_id",
            "_follow_debounce_id",
        ):
            tid = getattr(self, attr, None)
            if tid:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    pass
                setattr(self, attr, 0)
        # Cancel the live-follow GFileMonitor (MainWindow owns it) so a queued
        # "changed" signal can't re-arm a debounce against the closing window.
        try:
            self._stop_following()
        except Exception:
            pass
        # Stop the context-fill runner's delivery immediately (join=False: its
        # daemon thread dies with the process; don't stall the close on it).
        try:
            self._ctx_fill_runner.shutdown(join=False)
        except Exception:
            pass
        try:
            self._sessions.shutdown()
        except Exception:
            pass
        try:
            self._transcript.shutdown()
        except Exception:
            pass
        try:
            self._chat_toolbar.shutdown()
        except Exception:
            pass
        try:
            self._composer.shutdown()
        except Exception:
            pass
        try:
            self._context.shutdown()
        except Exception:
            pass
        try:
            self._shared.shutdown()
        except Exception:
            pass
        try:
            self._activity.shutdown()
        except Exception:
            pass
        try:
            self._agent_dock.shutdown()
        except Exception:
            pass
        palette = getattr(self, "_command_palette", None)
        self._command_palette = None
        if palette is not None:
            try:
                palette.close()
            except Exception:
                pass
        # Panes fast-close (join=False) so several loader joins can't stack into
        # a multi-second stall on quit; delivery stops immediately either way,
        # and every late idle/callback is already guarded by _destroyed.
        try:
            self._plan.shutdown(join=False)
        except Exception:
            pass
        try:
            self._missions.shutdown(join=False)
        except Exception:
            pass
        # GoalStrip owns no timers/threads, but a late native goal-status event
        # persists to disk and then updates it — refuse that widget update.
        try:
            self._goal_strip.shutdown()
        except Exception:
            pass
        try:
            self._plan_progress.shutdown()
        except Exception:
            pass
        # Remember the window size + splitter positions for next launch.
        self._save_window_layout()
        # Closing the window stops EVERY live session, but the user didn't
        # press Stop — so terminate (interrupt=False), never SIGINT. SIGINT
        # would stamp "[Request interrupted by user]" into every session that
        # happened to be running at quit (including background ones the user
        # never looked at). interrupt=False closes stdin + SIGTERMs the whole
        # process group, delivered synchronously here before teardown.
        self._driver_manager.stop_all(interrupt=False)
        # Driver stop/exits are asynchronous and their final callbacks may
        # still append canonical events. Keep the lightweight SQLite handle
        # alive until process teardown rather than closing it underneath them.
        return False
