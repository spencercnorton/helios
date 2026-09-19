import json

import pytest

from helios.backend import project_perms
from helios.backend.project_perms import (
    AUTONOMY_MODE,
    GLOBAL_DEFAULT_MODES,
    PERMISSION_MODE_DESCRIPTORS,
    PERMISSION_MODES,
    SAFE_FALLBACK_MODE,
    canonical_cwd,
    codex_permission_profile,
    effective_execution_mode,
    legacy_permission,
    more_restrictive_mode,
    reload_legacy_permissions,
    resolve_startup_default,
    sanitize_global_default,
)


def test_permission_descriptors_have_unique_keys_and_cover_all_modes():
    keys = [mode.key for mode in PERMISSION_MODE_DESCRIPTORS]

    assert tuple(keys) == PERMISSION_MODES
    assert len(keys) == len(set(keys)) == 6
    assert AUTONOMY_MODE in keys


def test_openrouter_descriptions_disclose_its_approval_scope():
    auto = project_perms.permission_description("auto", provider="openrouter")
    assert "edit project files freely" in auto
    assert "external tools need approval" in auto
    assert "refuse edits" in project_perms.permission_description("dontAsk", provider="openrouter")
    for descriptor in PERMISSION_MODE_DESCRIPTORS:
        assert project_perms.permission_description(descriptor.key, provider="anthropic") == descriptor.description
        assert project_perms.permission_description(descriptor.key, provider="openai") == descriptor.description


def test_bypass_is_the_only_full_access_codex_profile():
    """Full access is available only through the explicit Bypass choice."""
    ungated = [
        mode.key
        for mode in PERMISSION_MODE_DESCRIPTORS
        if mode.codex_approval_policy == "never"
        and mode.codex_sandbox == "danger-full-access"
    ]
    assert ungated == [AUTONOMY_MODE]

    assert codex_permission_profile(AUTONOMY_MODE) == (
        "never",
        "danger-full-access",
        "user",
        True,
    )


def test_codex_permission_profiles_preserve_selected_trust_level():
    assert codex_permission_profile("bypassPermissions") == (
        "never",
        "danger-full-access",
        "user",
        True,
    )
    # Accept edits must never be stricter than Ask: Codex `untrusted` prompts
    # per command, which is the opposite of what the picker order promises.
    # Egress follows the picker order: Ask stays gated, the two looser modes
    # get the network so `git fetch`/`pip install` stop raising an approval
    # each, and Plan/Never ask stay closed.
    assert codex_permission_profile("acceptEdits") == (
        "on-request",
        "workspace-write",
        "user",
        True,
    )
    assert codex_permission_profile("auto") == (
        "on-request",
        "workspace-write",
        "user",
        True,
    )
    assert codex_permission_profile("dontAsk") == (
        "never",
        "workspace-write",
        "user",
        False,
    )
    assert codex_permission_profile("plan") == ("never", "read-only", "user", False)
    assert codex_permission_profile("default") == (
        "on-request",
        "workspace-write",
        "user",
        False,
    )


def test_unknown_permission_profile_fails_closed_to_scoped_ask():
    assert codex_permission_profile("future-mode") == (
        "on-request",
        "workspace-write",
        "user",
        False,
    )


# --- safe fallback for unconfigured conversations --------------------------


def test_safe_fallback_is_a_real_mode_and_not_bypass():
    assert SAFE_FALLBACK_MODE in PERMISSION_MODES
    assert SAFE_FALLBACK_MODE != "bypassPermissions"


def test_safe_fallback_is_not_approval_never_plus_danger():
    """Acceptance: an unconfigured workspace is never never+danger-full-access."""
    approval, sandbox, _reviewer, network = codex_permission_profile(SAFE_FALLBACK_MODE)
    assert network is False
    assert (approval, sandbox) != ("never", "danger-full-access")
    assert approval != "never"
    assert sandbox != "danger-full-access"


# --- contract: autonomy is never inherited from global state ----------------


def test_global_default_modes_include_bypass_as_an_explicit_choice():
    assert AUTONOMY_MODE in GLOBAL_DEFAULT_MODES
    assert set(GLOBAL_DEFAULT_MODES) == set(PERMISSION_MODES)


def test_sanitize_global_default_preserves_valid_and_clamps_invalid():
    assert sanitize_global_default(AUTONOMY_MODE) == AUTONOMY_MODE
    assert sanitize_global_default("plan") == "plan"
    assert sanitize_global_default("not-a-mode") == SAFE_FALLBACK_MODE


def test_bypass_is_selectable_but_never_the_failsafe():
    """Full access must always be an explicit choice, never what garbage
    decays into."""
    assert sanitize_global_default(AUTONOMY_MODE) == AUTONOMY_MODE
    assert sanitize_global_default("not-a-mode") == SAFE_FALLBACK_MODE
    assert sanitize_global_default("") == SAFE_FALLBACK_MODE
    assert SAFE_FALLBACK_MODE != AUTONOMY_MODE


def test_resolve_startup_default_requires_confirmation_for_bypass():
    """An upgrade must not silently inherit full access: a persisted Bypass
    stays clamped until the user reconfirms it in Settings."""
    assert resolve_startup_default(AUTONOMY_MODE, confirmed=False) == SAFE_FALLBACK_MODE
    assert resolve_startup_default(AUTONOMY_MODE, confirmed=True) == AUTONOMY_MODE
    assert resolve_startup_default("plan", confirmed=False) == "plan"
    assert resolve_startup_default("not-a-mode", confirmed=True) == SAFE_FALLBACK_MODE


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openrouter"])
def test_home_is_forced_to_plan_below_the_ui(provider):
    home = project_perms.PROTECTED_HOME_CWD

    assert effective_execution_mode("auto", home, provider=provider) == "plan"
    assert (
        effective_execution_mode("auto", f"{home}/project", provider=provider)
        == "auto"
    )
    # HOME outranks Bypass: full access still cannot execute in $HOME itself.
    assert (
        effective_execution_mode(AUTONOMY_MODE, home, provider=provider) == "plan"
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openrouter"])
def test_supported_providers_offer_and_execute_bypass(provider):
    assert project_perms.provider_allows_mode(provider, AUTONOMY_MODE)
    assert AUTONOMY_MODE in project_perms.modes_for_provider(provider)
    assert effective_execution_mode(AUTONOMY_MODE, "/repo", provider=provider) == AUTONOMY_MODE
    for mode in PERMISSION_MODES:
        assert project_perms.provider_allows_mode(provider, mode)


def test_unknown_provider_narrows_bypass_to_ask_at_the_chokepoint():
    assert effective_execution_mode(
        AUTONOMY_MODE, "/repo", provider="unknown-future-provider"
    ) == SAFE_FALLBACK_MODE


def test_other_codex_permission_profiles_remain_sandboxed():
    for mode in (*PERMISSION_MODES, "manual", "not-a-mode", ""):
        approval, sandbox, reviewer, _net = project_perms.codex_permission_profile(mode)
        assert (sandbox == "danger-full-access") == (mode == AUTONOMY_MODE)
        if mode == AUTONOMY_MODE:
            assert approval == "never"
        assert reviewer == "user"
    for junk in ("manual", "not-a-mode", ""):
        assert project_perms.codex_permission_profile(
            junk
        ) == project_perms.codex_permission_profile(SAFE_FALLBACK_MODE)


# --- canonical workspace keys / alias migration (BLOCKER 7) -----------------


def test_canonical_cwd_normalizes_trailing_slash_and_dotdot():
    assert canonical_cwd("") == ""
    assert canonical_cwd("/a/b/") == "/a/b"
    assert canonical_cwd("/a/b/../b") == "/a/b"


def test_obsolete_permission_badge_projection_api_is_removed():
    assert not hasattr(project_perms, "badge_projection")
    assert not hasattr(project_perms, "driver_badge_projection")


def test_obsolete_workspace_permission_store_is_removed():
    assert not hasattr(project_perms, "ProjectPermsStore")
    assert not hasattr(project_perms, "effective_mode")
    assert not hasattr(project_perms, "store")


def test_legacy_restrictive_workspace_policy_survives_as_visible_fallback(tmp_path):
    path = tmp_path / "project-perms.json"
    path.write_text(json.dumps({"/repo": "plan"}), encoding="utf-8")
    reload_legacy_permissions()

    legacy = legacy_permission("/repo")

    assert legacy is not None
    assert legacy.mode == "plan"
    assert legacy.original_mode == "plan"
    assert legacy.bypass_retired is False


def test_legacy_bypass_is_retired_to_ask_until_conversation_confirmation(tmp_path):
    path = tmp_path / "project-perms.json"
    path.write_text(
        json.dumps({"/repo": "bypassPermissions"}),
        encoding="utf-8",
    )
    reload_legacy_permissions()

    legacy = legacy_permission("/repo")

    assert legacy is not None
    assert legacy.mode == SAFE_FALLBACK_MODE
    assert legacy.bypass_retired is True


def test_malformed_legacy_file_fails_closed_for_every_workspace(tmp_path):
    (tmp_path / "project-perms.json").write_text("not-json\n", encoding="utf-8")
    reload_legacy_permissions()

    legacy = legacy_permission("/any/workspace")

    assert legacy is not None
    assert legacy.invalid is True
    assert legacy.mode == "plan"


def test_non_mapping_legacy_file_fails_closed_for_every_workspace(tmp_path):
    (tmp_path / "project-perms.json").write_text(
        json.dumps(["not", "a", "workspace-map"]),
        encoding="utf-8",
    )
    reload_legacy_permissions()

    legacy = legacy_permission("/any/workspace")

    assert legacy is not None
    assert legacy.invalid is True
    assert legacy.mode == "plan"


def test_unreadable_legacy_file_fails_closed_for_every_workspace(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "project-perms.json").write_text("{}", encoding="utf-8")
    reload_legacy_permissions()

    def _raise_permission_error(*_args, **_kwargs):
        raise PermissionError("not readable")

    monkeypatch.setattr(project_perms.Path, "read_text", _raise_permission_error)

    legacy = legacy_permission("/any/workspace")

    assert legacy is not None
    assert legacy.invalid is True
    assert legacy.mode == "plan"


def test_invalid_legacy_entry_only_locks_its_canonical_workspace(tmp_path):
    (tmp_path / "project-perms.json").write_text(
        json.dumps(
            {
                "/restricted/../restricted": "future-mode",
                "/valid": "dontAsk",
            }
        ),
        encoding="utf-8",
    )
    reload_legacy_permissions()

    invalid = legacy_permission("/restricted")
    valid = legacy_permission("/valid")

    assert invalid is not None
    assert invalid.invalid is True
    assert invalid.mode == "plan"
    assert valid is not None
    assert valid.invalid is False
    assert valid.mode == "dontAsk"
    assert legacy_permission("/unrelated") is None


def test_invalid_legacy_alias_overrides_valid_alias_for_same_workspace(tmp_path):
    (tmp_path / "project-perms.json").write_text(
        json.dumps(
            {
                "/repo": "auto",
                "/repo/../repo": "future-mode",
            }
        ),
        encoding="utf-8",
    )
    reload_legacy_permissions()

    legacy = legacy_permission("/repo")

    assert legacy is not None
    assert legacy.invalid is True
    assert legacy.mode == "plan"


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("plan", "auto", "plan"),
        ("dontAsk", "acceptEdits", "dontAsk"),
        ("default", "auto", "default"),
        ("acceptEdits", "auto", "acceptEdits"),
        ("auto", "plan", "plan"),
        # Bypass is the least restrictive mode, so the projection always picks
        # the other one — it can never widen an existing policy to full access.
        ("bypassPermissions", "auto", "auto"),
        ("bypassPermissions", "plan", "plan"),
    ],
)
def test_upgrade_fallback_never_widens_either_policy(first, second, expected):
    assert more_restrictive_mode(first, second) == expected
