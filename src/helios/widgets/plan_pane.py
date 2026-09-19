from __future__ import annotations

import shlex
from typing import NamedTuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from helios.backend.agent_activity import (
    AgentActivity,
    AgentActivitySnapshot,
    AgentObservedStatus,
    agent_status_label,
)
from helios.backend.latest_worker import LatestTaskRunner
from helios.backend.execution_plan import ExecutionPlan
from helios.backend.plan_summary import (
    PlanPhase,
    PlanStep,
    empty_summary,
    summarize_execution_plan,
    summarize_native_plan,
    summarize_streaming,
    summarize_turns,
)
from helios.backend.session_insights import SessionInsight, insights_for_turns
from helios.backend.transcript import Turn, iter_transcript


class _PlanLoadRequest(NamedTuple):
    session: object
    token: int
    session_id: str


def _turns_payload(turns: list[Turn]) -> tuple[list, list, list]:
    """Flatten a turn window while ignoring record boundaries and metadata.

    Claude persists one assistant record per content block while the live driver
    emits one combined Turn. Codex mirrors the combined Turn but assigns a fresh
    UUID/timestamp and may elide large tool payloads. Comparing these aggregate
    lanes lets both providers recognize the same local contribution.
    """
    content = []
    tool_uses = []
    tool_results = []
    for turn in turns:
        owner = (turn.role, turn.is_sidechain, turn.is_meta)
        content.extend((*owner, span.kind, span.text) for span in turn.content)
        tool_uses.extend(
            (*owner, tool.name, tool.id, tool.input if not tool.id else None)
            for tool in turn.tool_uses
        )
        tool_results.extend(
            (
                *owner,
                result.tool_use_id,
                result.is_error,
                result.content if not result.tool_use_id else None,
            )
            for result in turn.tool_results
        )
    return content, tool_uses, tool_results


def _merge_loaded_turns(
    loaded: list[Turn], pending: list[Turn]
) -> tuple[list[Turn], list[Turn]]:
    """Append only the local suffix not already represented at the loaded tail."""
    for overlap in range(len(pending), 0, -1):
        pending_payload = _turns_payload(pending[:overlap])
        target_counts = tuple(len(lane) for lane in pending_payload)
        suffix_counts = [0, 0, 0]
        for start in range(len(loaded) - 1, -1, -1):
            turn_payload = _turns_payload([loaded[start]])
            for index, lane in enumerate(turn_payload):
                suffix_counts[index] += len(lane)
            if any(
                count > target
                for count, target in zip(suffix_counts, target_counts, strict=True)
            ):
                break
            if (
                tuple(suffix_counts) == target_counts
                and _turns_payload(loaded[start:]) == pending_payload
            ):
                remaining = pending[overlap:]
                return [*loaded, *remaining], list(remaining)
    return [*loaded, *pending], list(pending)


class PlanPane(Gtk.Box):
    """Right-side plan and progress surface for long coding sessions."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("helios-plan-pane")
        self.set_size_request(280, -1)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        header.set_margin_top(12)
        header.set_margin_bottom(8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        title = Gtk.Label(label="Plan", xalign=0)
        title.add_css_class("title-4")
        self._subtitle = Gtk.Label(label="No active session", xalign=0)
        self._subtitle.add_css_class("dim-label")
        self._subtitle.add_css_class("caption")
        self._subtitle.set_wrap(True)
        self._progress = Gtk.Label(xalign=0)
        self._progress.add_css_class("caption-heading")
        self._progress.add_css_class("helios-execution-plan-progress")
        self._progress.set_visible(False)
        header.append(title)
        header.append(self._subtitle)
        header.append(self._progress)
        self.append(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        self.append(scroller)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(14)
        body.set_margin_end(14)
        body.set_margin_bottom(16)
        scroller.set_child(body)

        self._phase_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.append(self._phase_box)

        self._detail = Gtk.Label(xalign=0)
        self._detail.add_css_class("helios-plan-detail")
        self._detail.add_css_class("dim-label")
        self._detail.set_wrap(True)
        body.append(self._detail)

        health_title = Gtk.Label(label="Quality signals", xalign=0)
        health_title.add_css_class("caption-heading")
        health_title.add_css_class("dim-label")
        body.append(health_title)

        health_scope = Gtk.Label(
            label="Current request · conversation evidence",
            xalign=0,
        )
        health_scope.add_css_class("caption")
        health_scope.add_css_class("dim-label")
        health_scope.set_wrap(True)
        body.append(health_scope)

        self._health_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(self._health_box)

        steps_title = Gtk.Label(label="Steps", xalign=0)
        steps_title.add_css_class("caption-heading")
        steps_title.add_css_class("dim-label")
        body.append(steps_title)

        self._steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(self._steps_box)

        self._changes_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self._changes_section.set_visible(False)
        self._changes_title = Gtk.Label(label="Changes", xalign=0)
        self._changes_title.add_css_class("caption-heading")
        self._changes_title.add_css_class("dim-label")
        self._changes_section.append(self._changes_title)

        self._changes_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._changes_summary = Gtk.Label(xalign=0)
        self._changes_summary.add_css_class("caption")
        self._changes_summary.add_css_class("dim-label")
        self._changes_summary.set_hexpand(True)
        self._changes_summary.set_wrap(True)
        self._changes_box.append(self._changes_summary)
        self._changes_button = Gtk.Button(label="View diff")
        self._changes_button.add_css_class("flat")
        self._changes_button.set_valign(Gtk.Align.CENTER)
        self._changes_button.connect("clicked", self._open_diff_dialog)
        self._changes_box.append(self._changes_button)
        self._changes_section.append(self._changes_box)
        body.append(self._changes_section)

        self._agents_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self._agents_section.set_visible(False)
        self._agents_title = Gtk.Label(label="Observed agents", xalign=0)
        self._agents_title.add_css_class("caption-heading")
        self._agents_title.add_css_class("dim-label")
        self._agents_section.append(self._agents_title)

        self._agents_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self._agents_section.append(self._agents_box)
        body.append(self._agents_section)

        self._token = 0
        self._destroyed = False
        self._session_id = ""
        self._session_scope = ""
        self._turns: list[Turn] = []
        self._pending_turns: list[Turn] = []
        self._insights: list[SessionInsight] = []
        self._native_plan_summary = None
        self._execution_plan_summary = None
        self._step_rows: dict[str, _StepRow] = {}
        self._steps_keyed = False
        self._native_diff = ""
        self._diff_window: Gtk.Window | None = None
        self._session_loader = LatestTaskRunner[
            _PlanLoadRequest, tuple[object, list[SessionInsight], list]
        ](
            work=self._load_session_state,
            deliver=self._deliver_session_state,
            name="helios-plan-load",
        )
        self._render(empty_summary(), [])

    def shutdown(self, *, join: bool = True) -> None:
        """Stop the background session loader and close the diff window before
        the widget tree finalizes. Mirrors the other panes' teardown so a
        daemon thread's idle_add can't fire against a finalizing widget on
        window close. Idempotent.

        Once shut down, every public mutator below is a no-op: late provider
        events (a driver stopped by window close still emits terminal
        streaming/turn/native callbacks) must never mutate the finalizing pane.
        ``join=False`` closes without blocking on the loader thread — used on
        window teardown so several panes' closes don't stack 2s joins."""
        self._destroyed = True
        self._session_loader.shutdown(join=join)
        if self._diff_window is not None:
            self._diff_window.close()
            self._diff_window = None

    def clear(self) -> None:
        if self._destroyed:
            return
        self._session_id = ""
        self._session_scope = ""
        self._turns = []
        self._pending_turns = []
        self._insights = []
        self._clear_native_state()
        self._subtitle.set_label("No active session")
        self._render(empty_summary(), [])

    def set_live_pending(self, provider_label: str) -> None:
        if self._destroyed:
            return
        self._session_id = ""
        self._session_scope = ""
        self._turns = []
        self._pending_turns = []
        self._insights = []
        self._clear_native_state()
        self._subtitle.set_label(f"{provider_label} is preparing a new session")
        self._render(empty_summary(), [])

    def set_session(self, session) -> None:
        if self._destroyed:
            return
        if session is None:
            self.clear()
            return
        session_id = session.session_id
        if session_id != self._session_id:
            self._clear_native_state()
            # Never expose the prior session as history for a newly selected
            # session while its asynchronous transcript load is in flight.
            self._turns = []
            self._pending_turns = []
            self._insights = []
        self._session_id = session_id
        self._session_scope = str(getattr(session.project, "cwd", "") or "")
        self._subtitle.set_label(_session_label(session))
        self._token += 1
        token = self._token
        self._session_loader.submit(
            _PlanLoadRequest(
                session=session, token=token, session_id=session.session_id
            )
        )

    def _load_session_state(self, request: _PlanLoadRequest):
        turns = list(iter_transcript(request.session.path))
        return (
            summarize_turns(turns),
            insights_for_turns(turns, default_scope=request.session.project.cwd),
            turns,
        )

    def _deliver_session_state(self, request: _PlanLoadRequest, result) -> None:
        if self._destroyed:
            return
        if isinstance(result, Exception):
            summary, insights, turns = empty_summary(), [], []
        else:
            summary, insights, turns = result
        GLib.idle_add(
            self._apply_session_state,
            summary,
            insights,
            turns,
            request.token,
            request.session_id,
        )

    def show_streaming(self, streaming) -> None:
        if self._destroyed:
            return
        authoritative = self._authoritative_summary()
        if authoritative is not None:
            self._render(authoritative, self._insights, live=True)
            return
        self._render(
            summarize_streaming(streaming, self._turns),
            self._insights,
            live=True,
        )

    def append_turn(self, turn) -> None:
        if self._destroyed:
            return
        self._turns.append(turn)
        # Keep locally observed turns as a suffix to merge with an outstanding
        # disk snapshot. The selection token remains stable so that snapshot can
        # restore earlier history instead of being discarded by this append.
        self._pending_turns.append(turn)
        summary = summarize_turns(self._turns)
        self._insights = insights_for_turns(
            self._turns, default_scope=self._session_scope
        )
        self._render(self._authoritative_summary() or summary, self._insights)

    def begin_native_turn(self) -> None:
        """Discard per-turn native state before a new App Server turn starts."""
        if self._destroyed:
            return
        self._native_plan_summary = None
        self.show_native_diff("")
        self.show_native_agents({})
        summary = summarize_turns(self._turns) if self._turns else empty_summary()
        self._render(
            self._execution_plan_summary or summary,
            self._insights,
            live=True,
        )

    def show_native_plan(self, payload: dict) -> None:
        """Render an authoritative Codex ``turn/plan/updated`` payload."""
        if self._destroyed or not isinstance(payload, dict):
            return
        summary = summarize_native_plan(
            payload.get("plan"),
            str(payload.get("explanation") or ""),
        )
        self._native_plan_summary = summary
        self._render(summary, self._insights, live=True)

    def show_execution_plan(self, plan: ExecutionPlan | None) -> None:
        """Render the latest durable Work plan across selection and restart."""

        if self._destroyed:
            return
        self._native_plan_summary = None
        self._execution_plan_summary = (
            summarize_execution_plan(plan) if plan is not None else None
        )
        summary = self._execution_plan_summary
        if summary is None:
            summary = summarize_turns(self._turns) if self._turns else empty_summary()
        self._render(summary, self._insights)

    def show_native_diff(self, payload) -> None:
        """Replace the current turn's aggregate diff with the latest snapshot."""
        if self._destroyed:
            return
        diff = _native_diff_text(payload)
        self._native_diff = diff
        visible = bool(diff.strip())
        self._changes_section.set_visible(visible)
        self._changes_summary.set_label(_diff_summary(diff) if visible else "")
        self._set_diff_child()

    def show_native_agents(self, snapshot) -> None:
        """Render the latest keyed App Server subagent status snapshot."""
        if self._destroyed:
            return
        states = _native_agent_states(snapshot)
        _clear_box(self._agents_box)
        for agent_id, state in states:
            self._agents_box.append(_AgentRow(agent_id, state))
        visible = bool(states)
        self._agents_section.set_visible(visible)

    def show_agent_activity(self, snapshot: AgentActivitySnapshot) -> None:
        """Render the provider-neutral model shared with the Agent Dock."""

        if self._destroyed or not isinstance(snapshot, AgentActivitySnapshot):
            return
        _clear_box(self._agents_box)
        for activity in snapshot.activities:
            self._agents_box.append(_ObservedAgentRow(activity))
        self._agents_section.set_visible(bool(snapshot.activities))

    def _clear_native_state(self) -> None:
        self._native_plan_summary = None
        self._execution_plan_summary = None
        self.show_native_diff("")
        self.show_native_agents({})
        if self._diff_window is not None:
            self._diff_window.close()

    def _set_diff_child(self) -> None:
        """Replace the dialog's body with a fresh highlighted diff view.

        Rebuilt rather than updated in place: the diff changes once per turn
        and CodeBlock has no setter."""
        if self._diff_window is None:
            return
        scroller = self._diff_window.get_child()
        if isinstance(scroller, Gtk.ScrolledWindow):
            from helios.widgets.code_block import CodeBlock

            scroller.set_child(CodeBlock(self._native_diff, "diff"))

    def _open_diff_dialog(self, _button: Gtk.Button) -> None:
        if self._diff_window is None:
            window = Gtk.Window(title="Turn changes")
            window.set_default_size(1000, 720)
            window.set_modal(True)
            root = self.get_root()
            if isinstance(root, Gtk.Window):
                window.set_transient_for(root)

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroller.set_hexpand(True)
            scroller.set_vexpand(True)
            window.set_child(scroller)
            window.connect("close-request", self._on_diff_window_closed)
            self._diff_window = window
        self._set_diff_child()
        self._diff_window.present()

    def _on_diff_window_closed(self, _window: Gtk.Window) -> bool:
        self._diff_window = None
        return False

    def _apply_session_state(
        self, summary, insights, turns, token: int, session_id: str
    ) -> bool:
        if self._destroyed:
            return False
        if token == self._token and session_id == self._session_id:
            merged, remaining = _merge_loaded_turns(turns, self._pending_turns)
            self._turns = merged
            self._pending_turns = remaining
            if remaining:
                summary = summarize_turns(merged)
                insights = insights_for_turns(merged, default_scope=self._session_scope)
            self._insights = insights
            self._render(self._authoritative_summary() or summary, insights)
        return False

    def _authoritative_summary(self):
        return self._native_plan_summary or self._execution_plan_summary

    def _render(self, summary, insights, *, live: bool = False) -> None:
        _clear_box(self._phase_box)
        _clear_box(self._health_box)

        for phase in summary.phases:
            self._phase_box.append(_PhaseRow(phase))

        if summary.total_count or summary.revision:
            noun = "task" if summary.total_count == 1 else "tasks"
            label = f"{summary.completed_count}/{summary.total_count} {noun} complete"
            if summary.revision:
                label += f" · revision {summary.revision}"
            self._progress.set_label(label)
            self._progress.set_visible(True)
        else:
            self._progress.set_label("")
            self._progress.set_visible(False)

        detail = summary.active_detail
        if not detail and summary.source == "empty":
            detail = "Open a session or start a chat to see the agent's plan."
        elif not detail and live:
            detail = "Live reasoning in progress."
        self._detail.set_label(detail)
        self._detail.set_visible(bool(detail))

        if insights:
            for insight in insights:
                self._health_box.append(_InsightRow(insight))
        else:
            empty_health = Gtk.Label(
                label="Current checks appear as work progresses.",
                xalign=0,
            )
            empty_health.add_css_class("dim-label")
            empty_health.add_css_class("caption")
            empty_health.set_wrap(True)
            self._health_box.append(empty_health)

        self._render_steps(summary.steps[:10])

    def _render_steps(self, steps: list[PlanStep]) -> None:
        keyed = bool(steps) and all(step.task_id for step in steps)
        if not keyed:
            self._steps_keyed = False
            self._step_rows.clear()
            _clear_box(self._steps_box)
            if steps:
                for step in steps:
                    self._steps_box.append(_StepRow(step))
            else:
                empty = Gtk.Label(
                    label="Plan steps will appear when the agent creates them.",
                    xalign=0,
                )
                empty.add_css_class("dim-label")
                empty.add_css_class("caption")
                empty.set_wrap(True)
                self._steps_box.append(empty)
            return

        if not self._steps_keyed:
            self._step_rows.clear()
            _clear_box(self._steps_box)
            self._steps_keyed = True

        wanted = {step.task_id for step in steps}
        for task_id, row in list(self._step_rows.items()):
            if task_id not in wanted:
                self._steps_box.remove(row)
                del self._step_rows[task_id]

        previous = None
        for step in steps:
            row = self._step_rows.get(step.task_id)
            if row is None:
                row = _StepRow(step)
                self._step_rows[step.task_id] = row
                self._steps_box.append(row)
            else:
                row.update(step)
            self._steps_box.reorder_child_after(row, previous)
            previous = row


class _PhaseRow(Gtk.Box):
    def __init__(self, phase: PlanPhase) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.add_css_class("helios-plan-phase")
        self.add_css_class(f"helios-plan-{phase.status}")

        marker = Gtk.Box()
        marker.add_css_class("helios-plan-marker")
        marker.add_css_class(f"helios-plan-marker-{phase.status}")
        marker.set_valign(Gtk.Align.CENTER)
        self.append(marker)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_hexpand(True)
        title = Gtk.Label(label=phase.title, xalign=0)
        title.add_css_class("caption-heading")
        desc = Gtk.Label(label=phase.description, xalign=0)
        desc.add_css_class("caption")
        desc.add_css_class("dim-label")
        desc.set_wrap(True)
        text.append(title)
        text.append(desc)
        self.append(text)


class _StepRow(Gtk.Box):
    def __init__(self, step: PlanStep) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("helios-plan-step")
        self._status = ""
        self._icon = Gtk.Image()
        self._icon.set_pixel_size(14)
        self._icon.add_css_class("dim-label")
        self._icon.set_valign(Gtk.Align.START)
        self.append(self._icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        self._label = Gtk.Label(xalign=0)
        self._label.set_wrap(True)
        self._label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.append(self._label)
        self._detail = Gtk.Label(xalign=0)
        self._detail.add_css_class("caption")
        self._detail.add_css_class("dim-label")
        self._detail.set_wrap(True)
        self._detail.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.append(self._detail)
        self.append(text)
        self.update(step)

    def update(self, step: PlanStep) -> None:
        if self._status:
            self.remove_css_class(f"helios-plan-step-{self._status}")
        self._status = step.status
        self.add_css_class(f"helios-plan-step-{step.status}")
        self._icon.set_from_icon_name(_icon_for_status(step.status))
        self._label.set_label(step.text)
        self._detail.set_label(step.detail)
        self._detail.set_visible(bool(step.detail))


class _InsightRow(Gtk.Box):
    def __init__(self, insight: SessionInsight) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("helios-plan-insight")
        self.add_css_class(f"helios-plan-insight-{insight.status}")
        marker = Gtk.Box()
        marker.add_css_class("helios-plan-insight-marker")
        marker.add_css_class(f"helios-plan-insight-marker-{insight.status}")
        marker.set_valign(Gtk.Align.START)
        self.append(marker)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(label=insight.title, xalign=0)
        title.add_css_class("caption-heading")
        detail = Gtk.Label(label=insight.detail, xalign=0)
        detail.add_css_class("caption")
        detail.add_css_class("dim-label")
        detail.set_wrap(True)
        text.append(title)
        text.append(detail)
        for item in insight.evidence[:4]:
            evidence = Gtk.Label(label=f"- {item}", xalign=0)
            evidence.add_css_class("caption")
            evidence.add_css_class("dim-label")
            evidence.set_wrap(True)
            text.append(evidence)
        self.append(text)


class _AgentRow(Gtk.Box):
    def __init__(self, agent_id: str, state: dict) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        status = str(state.get("status") or "unknown")
        icon = Gtk.Image.new_from_icon_name(_agent_icon_for_status(status))
        icon.set_pixel_size(13)
        icon.add_css_class("dim-label")
        self.append(icon)

        label = str(state.get("name") or agent_id)
        agent = Gtk.Label(label=label, xalign=0)
        agent.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        agent.set_hexpand(True)
        agent.set_tooltip_text(agent_id)
        self.append(agent)

        status_label = Gtk.Label(label=_agent_status_label(status), xalign=1)
        status_label.add_css_class("caption")
        status_label.add_css_class("dim-label")
        message = state.get("message")
        if message:
            status_label.set_tooltip_text(str(message))
        self.append(status_label)


class _ObservedAgentRow(Gtk.Box):
    def __init__(self, activity: AgentActivity) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        icon = Gtk.Image.new_from_icon_name(_observed_agent_icon(activity.status))
        icon.set_pixel_size(13)
        icon.add_css_class("dim-label")
        self.append(icon)

        agent = Gtk.Label(label=activity.name, xalign=0)
        agent.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        agent.set_hexpand(True)
        agent.set_tooltip_text(activity.actor_id)
        self.append(agent)

        status = Gtk.Label(label=agent_status_label(activity.status), xalign=1)
        status.add_css_class("caption")
        status.add_css_class("dim-label")
        if activity.detail:
            status.set_tooltip_text(activity.detail)
        self.append(status)


def _native_diff_text(payload) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("diff", "aggregateDiff", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return ""


def _diff_summary(diff: str) -> str:
    files: list[str] = []
    additions = 0
    deletions = 0
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            try:
                path = shlex.split(line)[-1]
            except (ValueError, IndexError):
                path = line.rsplit(" ", 1)[-1]
            path = _trim_diff_prefix(path)
            if path and path not in files:
                files.append(path)
        elif line.startswith("+++ "):
            path = _trim_diff_prefix(line[4:].strip())
            if path and path not in files:
                files.append(path)
        elif line.startswith("--- "):
            continue
        elif line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1

    if not files:
        subject = "Aggregate diff"
    elif len(files) == 1:
        subject = files[0]
    elif len(files) == 2:
        subject = f"{files[0]}, {files[1]}"
    else:
        subject = f"{files[0]}, {files[1]} + {len(files) - 2} more"
    count = "1 file" if len(files) == 1 else f"{len(files)} files"
    return f"{subject} · {count} · +{additions} −{deletions}"


def _trim_diff_prefix(path: str) -> str:
    path = path.strip('"')
    if path == "/dev/null":
        return ""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _native_agent_states(snapshot) -> list[tuple[str, dict]]:
    container = snapshot
    if isinstance(snapshot, dict):
        item = snapshot.get("item")
        if isinstance(item, dict) and "agentsStates" in item:
            container = item.get("agentsStates")
        else:
            for key in ("agentsStates", "agents", "states"):
                if key in snapshot:
                    container = snapshot.get(key)
                    break

    states: list[tuple[str, dict]] = []
    if isinstance(container, dict):
        for agent_id, raw_state in container.items():
            if isinstance(raw_state, dict):
                state = dict(raw_state)
            elif isinstance(raw_state, str):
                state = {"status": raw_state}
            else:
                continue
            states.append((str(agent_id), state))
    elif isinstance(container, list):
        for index, raw_state in enumerate(container):
            if not isinstance(raw_state, dict):
                continue
            agent_id = (
                raw_state.get("threadId")
                or raw_state.get("id")
                or raw_state.get("name")
                or f"agent-{index + 1}"
            )
            states.append((str(agent_id), dict(raw_state)))
    states.sort(key=lambda entry: entry[0])
    return states


def _agent_status_label(status: str) -> str:
    return {
        "pendingInit": "Starting",
        "inProgress": "Working",
        "running": "Working",
        "completed": "Complete",
        "interrupted": "Interrupted",
        "errored": "Error",
        "failed": "Error",
        "shutdown": "Stopped",
        "notFound": "Not found",
    }.get(status, status or "Unknown")


def _agent_icon_for_status(status: str) -> str:
    if status == "completed":
        return "object-select-symbolic"
    if status in {"running", "inProgress"}:
        return "media-playback-start-symbolic"
    if status in {"errored", "failed", "notFound"}:
        return "dialog-warning-symbolic"
    if status in {"interrupted", "shutdown"}:
        return "media-playback-stop-symbolic"
    return "content-loading-symbolic"


def _observed_agent_icon(status: AgentObservedStatus) -> str:
    return {
        AgentObservedStatus.STARTING: "content-loading-symbolic",
        AgentObservedStatus.RUNNING: "media-playback-start-symbolic",
        AgentObservedStatus.NEEDS_INPUT: "dialog-warning-symbolic",
        AgentObservedStatus.COMPLETED: "object-select-symbolic",
        AgentObservedStatus.FAILED: "dialog-error-symbolic",
        AgentObservedStatus.STOPPED: "media-playback-stop-symbolic",
        AgentObservedStatus.UNKNOWN: "view-more-symbolic",
    }[status]


def _icon_for_status(status: str) -> str:
    if status == "done":
        return "object-select-symbolic"
    if status == "active":
        return "media-playback-start-symbolic"
    if status == "blocked":
        return "dialog-warning-symbolic"
    if status == "interrupted":
        return "media-playback-stop-symbolic"
    if status == "dropped":
        return "list-remove-symbolic"
    return "radio-symbolic"


def _clear_box(box: Gtk.Box) -> None:
    while (child := box.get_first_child()) is not None:
        box.remove(child)


def _session_label(session) -> str:
    sid = (getattr(session, "session_id", "") or "")[:8]
    origin = getattr(getattr(session, "project", None), "origin", "")
    if origin and origin != "local":
        return f"Session {sid} from {origin}"
    return f"Session {sid}" if sid else "Current session"
