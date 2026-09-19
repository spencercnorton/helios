"""Shared-context injection and plan mode for OpenRouter sessions.

Two ways OpenRouter mode behaved as a second-class provider: the model never
saw the project's CLAUDE.md/AGENTS.md that Claude and Codex both get, and plan
mode was enforcement-only — every mutating tool denied, with no way for the
model to say what it *would* have done.
"""

from __future__ import annotations

import pytest

from helios.backend.process import openrouter_tools as t


class TestExitPlanMode:
    def test_plan_mode_permits_the_only_tool_that_leaves_it(self):
        """Without this, plan mode denies everything and the model is stuck
        with no way to hand a plan back."""
        assert t.decide("ExitPlanMode", "plan") == "auto"

    @pytest.mark.parametrize("mode", [
        "plan", "dontAsk", "default", "auto", "acceptEdits", "bypassPermissions",
    ])
    def test_it_is_allowed_in_every_mode(self, mode):
        """It mutates nothing, so no mode has cause to refuse it."""
        assert t.decide("ExitPlanMode", mode) == "auto"

    def test_plan_mode_still_denies_everything_that_mutates(self):
        for tool in ("Write", "Edit", "Bash"):
            assert t.decide(tool, "plan") == "deny"

    def test_it_is_advertised_to_the_model(self):
        names = [s["function"]["name"] for s in t.TOOL_SCHEMAS]
        assert "ExitPlanMode" in names

    def test_it_changes_nothing_on_disk(self, tmp_path):
        before = list(tmp_path.iterdir())
        content, is_error = t.execute_tool(
            "ExitPlanMode", {"plan": [{"step": "do the thing"}]}, cwd=str(tmp_path))
        assert not is_error
        assert list(tmp_path.iterdir()) == before

    def test_it_tells_the_model_to_stop(self):
        """A plan the model then ignores is worse than no plan."""
        content, _ = t.execute_tool(
            "ExitPlanMode", {"plan": [{"step": "x"}]}, cwd="/tmp")
        assert "wait" in content.lower()
        assert "do not continue" in content.lower()

    def test_an_empty_plan_is_refused(self):
        content, is_error = t.execute_tool("ExitPlanMode", {"plan": []}, cwd="/tmp")
        assert is_error
        content, is_error = t.execute_tool("ExitPlanMode", {}, cwd="/tmp")
        assert is_error


class TestPlanNormalization:
    def test_steps_start_pending(self):
        """Nothing has been done yet — that is the point of proposing."""
        out = t.normalize_plan({"plan": [{"step": "a"}, {"step": "b"}]})
        assert [s["status"] for s in out["plan"]] == ["pending", "pending"]

    def test_bare_strings_are_accepted(self):
        out = t.normalize_plan({"plan": ["read", "fix"]})
        assert [s["step"] for s in out["plan"]] == ["read", "fix"]

    def test_the_payload_matches_the_codex_plan_shape(self):
        """Rendered through the same PlanPane path rather than growing a second
        plan format."""
        from helios.backend.plan_summary import summarize_native_plan

        out = t.normalize_plan({"plan": [{"step": "a"}], "explanation": "why"})
        summary = summarize_native_plan(out["plan"], out["explanation"])
        assert [s.text for s in summary.steps] == ["a"]

    def test_junk_entries_are_dropped_not_rendered(self):
        out = t.normalize_plan({"plan": [None, 7, {"nope": 1}, {"step": "  "}, {"step": "ok"}]})
        assert [s["step"] for s in out["plan"]] == ["ok"]

    def test_it_is_bounded(self):
        out = t.normalize_plan({
            "plan": [{"step": "s"} for _ in range(500)],
            "explanation": "x" * 99_999,
        })
        assert len(out["plan"]) <= 50
        assert len(out["explanation"]) <= 4000

    def test_never_raises_on_garbage(self):
        for junk in ({}, {"plan": None}, {"plan": "steps"}, {"plan": 7}):
            assert isinstance(t.normalize_plan(junk)["plan"], list)


class TestSharedContextInjection:
    def test_provider_drivers_have_no_hidden_first_turn_context_state(self):
        # Constructing the driver needs GTK. Everything else in this module is
        # GTK-free and must keep running on the slim CI lane, so the skip is
        # scoped to this test rather than the file.
        pytest.importorskip("gi")
        from helios.backend.process.openrouter_driver import OpenRouterDriver

        fresh = OpenRouterDriver(cwd="/tmp", model="v/m")
        resumed = OpenRouterDriver(cwd="/tmp", model="v/m", resume_session_id="abc")
        assert not hasattr(fresh, "_inject_context_next")
        assert not hasattr(resumed, "_inject_context_next")

    def test_the_user_request_survives_injection(self, tmp_path):
        """Whatever context is prepended, the thing the user actually typed
        must still reach the model intact and last."""
        from helios.backend import codex_context

        out = codex_context.inject_shared_context(str(tmp_path), "find the bug")
        assert out.endswith("find the bug")

    def test_provider_drivers_send_native_user_text_without_claude_context(self):
        import inspect

        # Both driver modules import gi at module scope.
        pytest.importorskip("gi")
        from helios.backend import codex_context
        from helios.backend.process import codex_driver, openrouter_driver

        assert "codex_context.inject_shared_context" not in inspect.getsource(
            openrouter_driver.OpenRouterDriver.send_user_text
        )
        assert "codex_context.inject_shared_context" not in inspect.getsource(
            codex_driver
        )
        assert callable(codex_context.inject_shared_context)
