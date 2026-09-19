from __future__ import annotations

from helios.backend import codex_context


def test_non_claude_context_never_copies_provider_role_files(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_text(
        "Claude builds; GPT audits. Crossing providers is free capacity.",
        encoding="utf-8",
    )

    assert codex_context.shared_context_for_cwd(str(cwd)) == ""


def test_fallback_prompt_keeps_user_request_separate(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_text("Claude builds; GPT audits.", encoding="utf-8")

    prompt = codex_context.inject_shared_context(str(cwd), "do the task")

    assert "Claude builds" not in prompt
    assert prompt.endswith("User request:\ndo the task")


def test_fresh_gpt_prompt_includes_compact_presentation_policy(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()

    prompt = codex_context.inject_shared_context(str(cwd), "do the task")

    # The Helios-owned policy is present even when this project has no shared
    # context files; the driver's first-turn gate is what makes it fresh-only.
    assert "one sentence per meaningful milestone" in prompt
    assert "do not use progress updates to preview or repeat claims" in prompt
    assert "Keep the final response concise by default" in prompt
    assert "material safety warnings" in prompt
    assert prompt.endswith("User request:\ndo the task")


def test_codex_developer_policy_is_provider_neutral():
    policy = codex_context.CODEX_DEVELOPER_INSTRUCTIONS
    assert "AGENTS.md" in policy
    assert "Do not infer provider roles" in policy
    assert "Claude builds" not in policy
    assert "GPT audits" not in policy


def test_codex_developer_policy_requires_evidence_backed_multi_step_plans():
    policy = codex_context.CODEX_DEVELOPER_INSTRUCTIONS

    assert "create a concrete native execution plan" in policy
    assert "before the first write" in policy
    assert "2–8 outcome-based tasks" in policy
    assert "mark a task complete only after evidence" in policy
    assert "never silently drop an unfinished task" in policy
    assert "does not replace" in policy


def test_codex_policy_asks_only_at_material_decision_boundaries():
    policy = codex_context.CODEX_DEVELOPER_INSTRUCTIONS

    assert "Continue reversible, in-scope local work without asking" in policy
    assert "materially change acceptance criteria or architecture" in policy
    assert "external, destructive, or irreversible effect" in policy
    assert "missing authority or credentials" in policy
    assert "Do not ask merely for confirmation" in policy
    assert "blocked plan task" in policy
