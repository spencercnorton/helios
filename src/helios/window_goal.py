"""Goal Mode + header Work-indicator methods, split out of main_window.

Behaviour-preserving mixin: every method still runs on the composed
MainWindow instance, so shared state (self._driver, self._goal_strip,
self._work_coordinator, self._shared, ...) and cross-cluster method calls
resolve exactly as before. This module owns only the Goal-Mode and header
Work-indicator surface of the window.
"""

from __future__ import annotations

from dataclasses import replace

from helios.backend import session_goals, session_handoff
from helios.backend.session_goals import GoalState
from helios.backend.process.message_queue import RequiredPromptContextError
from helios.log import get_logger
from helios.widgets.goal_strip import present_goal_dialog

_log = get_logger("window")


class GoalWorkMixin:
    """Goal Mode + header Work-indicator controller for MainWindow."""

    def _current_work_id(self) -> str:
        if self._driver is not None:
            work_id = str(getattr(self._driver, "_helios_work_id", "") or "")
            if work_id:
                return work_id
        if self._next_chat is not None and self._next_chat.work_id:
            return self._next_chat.work_id
        return ""

    def _goal_work_id(self) -> str:
        """Goal key: canonical Work id, with legacy native-id fallback."""

        work_id = self._current_work_id()
        if work_id:
            return work_id
        if self._driver is not None and self._driver.session_id:
            return self._driver.session_id
        if self._next_chat is not None and self._next_chat.resume_id:
            return self._next_chat.resume_id
        return ""

    def _goal_session_id(self) -> str:
        """Compatibility alias retained for older window-level tests/plugins."""

        return self._goal_work_id()

    def _visible_goal(self) -> GoalState | None:
        work_id = self._goal_work_id()
        if work_id:
            return session_goals.get_goal(work_id)
        return self._pending_goal

    def _refresh_goal_strip(self) -> None:
        self._goal_strip.set_goal(self._visible_goal())

    def _refresh_execution_plan(self) -> None:
        """Project the Work's durable model plan into both visible surfaces."""

        plan = None
        coordinator = getattr(self, "_work_coordinator", None)
        work_id = GoalWorkMixin._current_work_id(self)
        if coordinator is not None and work_id:
            try:
                plan = coordinator.store.get_execution_plan(work_id)
            except Exception as exc:  # noqa: BLE001 — plan UI is non-critical
                _log.warning("could not read execution plan for %s: %s", work_id, exc)
        progress = getattr(self, "_plan_progress", None)
        set_progress = getattr(progress, "set_plan", None)
        if callable(set_progress):
            set_progress(plan)
        pane = getattr(self, "_plan", None)
        show_plan = getattr(pane, "show_execution_plan", None)
        if callable(show_plan):
            show_plan(plan)

    def _open_execution_plan(self) -> None:
        if self._destroyed:
            return
        self._show_context = True
        self._outer.set_show_sidebar(True)
        self._context_btn.set_active(True)
        self._right_stack.set_visible_child_name("plan")

    def _update_work_indicator(self) -> None:
        """Show the current Work's participants + lead in the header when it
        is a real (multi-provider) tandem Work; hidden otherwise. Read-only."""
        indicator = getattr(self, "_work_indicator", None)
        if indicator is None:
            return
        coordinator = getattr(self, "_work_coordinator", None)
        work_id = self._current_work_id()
        work = None
        participants: list = []
        # ponytail: cheap indexed reads on each execution-sync; cache only if
        # it ever shows up in a profile.
        if coordinator is not None and work_id:
            try:
                work = coordinator.store.get_work(work_id)
                participants = coordinator.store.list_participants(work_id)
            except Exception as exc:  # noqa: BLE001
                _log.warning("could not read Work for the indicator: %s", exc)
                work = None
        if work is None:
            indicator.update("", "", [])
            return
        by_provider = {}
        for participant in participants:
            provider = getattr(participant, "provider", "")
            if not provider:
                continue
            current = by_provider.get(provider)
            if current is None or (
                participant.role == "lead" and current.role != "lead"
            ):
                by_provider[provider] = participant
        # Offer click-to-hand-off in the popover when a hand-off-able session
        # is open — labelled with the other model, matching the dialog default.
        target = self._shared.get_handoff_target()
        handoff_label = None
        if target is not None:
            try:
                recipient = session_handoff.default_recipient_provider(
                    target.session_id, target.path
                )
                handoff_label = session_handoff.provider_label(recipient)
            except Exception as exc:  # noqa: BLE001 — must never break the header
                _log.warning("could not resolve hand-off recipient: %s", exc)
        indicator.update(
            work.mode,
            work.lead_provider,
            list(by_provider.values()),
            handoff_label=handoff_label,
            on_handoff=self._request_handoff_from_header,
        )

    def _request_handoff_from_header(self) -> None:
        """Header Work-indicator hand-off: same flow as the Shared Context pane,
        targeting whatever session that pane currently points at."""
        if self._destroyed:
            return
        session = self._shared.get_handoff_target()
        if session is not None:
            self._on_handoff_requested(None, session)

    def _goal_context_for_driver(self, drv, text: str):
        """Prepare Goal + Work delta context for either provider backend."""

        work_id = str(
            getattr(drv, "_helios_work_id", "")
            or getattr(drv, "_helios_work_id_hint", "")
            or ""
        )
        goal_key = work_id or drv.session_id or getattr(
            drv, "_helios_goal_session_id_hint", ""
        )
        goal = session_goals.get_goal(goal_key) if goal_key else None
        if goal is None and (drv is self._driver or drv is self._driver_manager.starting):
            goal = self._pending_goal
        supports_native_goal = bool(getattr(drv, "supports_native_goals", False))
        native_goal_synced = supports_native_goal and bool(
            getattr(drv, "native_goal_synced", False)
        )
        include_goal_envelope = not (
            supports_native_goal and (goal is None or native_goal_synced)
        )
        # App Server's native goal synchronizes objective/status/budget only.
        # Keep that single objective authority while supplying the accepted
        # definition-of-done and checklist through Helios' prompt context.
        include_goal_supplement = native_goal_synced and goal is not None

        def with_goal_context(value: str) -> str:
            if include_goal_envelope:
                return session_goals.wrap_user_prompt(value, goal)
            if include_goal_supplement:
                return session_goals.wrap_goal_supplement(value, goal)
            return value

        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is None or not work_id:
            return with_goal_context(text)
        provider = str(
            getattr(drv, "_helios_participant_provider", "")
            or self._driver_provider(drv)
        )
        try:
            return coordinator.prepare_prompt(
                work_id=work_id,
                provider=provider,
                text=text,
                goal=goal,
                include_goal_envelope=include_goal_envelope,
                include_goal_supplement=include_goal_supplement,
            )
        except Exception as exc:
            _log.warning("could not prepare Work context for %s: %s", work_id, exc)
            try:
                work = coordinator.store.get_work(work_id)
            except Exception:
                work = None
            # A tandem packet is part of the accepted provider contract. Work
            # absence or an unreadable record is equally unverifiable; never
            # send a raw/goal-only prompt and silently erase peer context.
            if work is None or getattr(work, "mode", "") != "single":
                raise RequiredPromptContextError() from exc
            try:
                evidence = coordinator.record_context_degradation(
                    work_id=work_id,
                    provider=provider,
                )
            except Exception as audit_exc:
                raise RequiredPromptContextError() from audit_exc
            toast = getattr(self, "_toast", None)
            if evidence is None or not callable(toast):
                raise RequiredPromptContextError() from exc
            try:
                toast(
                    "Shared Work context was unavailable. This single-provider "
                    "turn is continuing with Goal context only.",
                    timeout=6,
                )
            except Exception as toast_exc:
                raise RequiredPromptContextError() from toast_exc
            return with_goal_context(text)

    def _goal_with_current_metadata(self, goal: GoalState) -> GoalState:
        # replace(): these helpers only re-stamp cwd/provider, so enumerating
        # every other field just creates a place for a new one to be dropped.
        return replace(
            goal,
            items=list(goal.items),
            cwd=self._current_project_cwd(),
            provider=self._selected_provider(),
        )

    def _goal_with_driver_metadata(
        self,
        goal: GoalState,
        drv,
        cwd: str,
    ) -> GoalState:
        return replace(
            goal,
            items=list(goal.items),
            cwd=cwd,
            provider=self._driver_provider(drv),
        )

    def _edit_goal(self) -> None:
        if self._next_chat is not None and self._next_chat.project.read_only:
            self._toast("Read-only session is view-only.")
            return
        present_goal_dialog(self, self._visible_goal(), self._save_current_goal)

    def _save_current_goal(self, goal: GoalState) -> None:
        goal = self._goal_with_current_metadata(goal)
        work_id = self._current_work_id() or self._ensure_target_work()
        goal_key = work_id or self._goal_work_id()
        if goal_key:
            session_goals.set_goal(goal_key, goal)
            coordinator = getattr(self, "_work_coordinator", None)
            if coordinator is not None and work_id:
                try:
                    coordinator.record_goal(
                        work_id=work_id,
                        provider=self._selected_provider(),
                        goal=goal,
                    )
                except Exception as exc:
                    _log.warning("could not record Goal event for %s: %s", work_id, exc)
        else:
            self._pending_goal = goal
        drv = self._driver
        if (
            drv is not None
            and getattr(drv, "supports_native_goals", False)
            and (not work_id or getattr(drv, "_helios_work_id", "") == work_id)
        ):
            drv.sync_goal(goal)
        self._refresh_goal_strip()

    def _set_goal_status(self, status: session_goals.GoalStatus) -> None:
        goal = self._visible_goal()
        if goal is None:
            return
        goal.status = status
        self._save_current_goal(goal)

    def _clear_goal(self) -> None:
        goal_key = self._goal_work_id()
        if goal_key:
            session_goals.clear_goal(goal_key)
            work_id = self._current_work_id()
            coordinator = getattr(self, "_work_coordinator", None)
            if coordinator is not None and work_id:
                try:
                    coordinator.clear_goal(
                        work_id=work_id,
                        provider=self._selected_provider(),
                    )
                except Exception as exc:
                    _log.warning("could not clear canonical Goal for %s: %s", work_id, exc)
        else:
            self._pending_goal = None
        drv = self._driver
        if drv is not None and getattr(drv, "supports_native_goals", False):
            drv.clear_native_goal()
        self._refresh_goal_strip()

    def _bind_pending_goal_to_session(self, drv, session_id: str, cwd: str) -> None:
        work_id = str(getattr(drv, "_helios_work_id", "") or "")
        if work_id and session_id:
            session_goals.rekey_goal(session_id, work_id)
        goal_key = work_id or session_id
        if not goal_key or self._pending_goal is None:
            return
        goal = self._goal_with_driver_metadata(self._pending_goal, drv, cwd)
        session_goals.set_goal(goal_key, goal)
        coordinator = getattr(self, "_work_coordinator", None)
        if coordinator is not None and work_id:
            try:
                coordinator.record_goal(
                    work_id=work_id,
                    provider=self._driver_provider(drv),
                    goal=goal,
                )
            except Exception as exc:
                _log.warning("could not record pending Goal for %s: %s", work_id, exc)
        self._pending_goal = None
        if self._drv_is_current(drv):
            self._refresh_goal_strip()
