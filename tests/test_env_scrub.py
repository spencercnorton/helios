"""Tests for the scoped child-environment scrub (GTK-free).

No real secret values appear here: the source dicts use fabricated
credential-shaped NAMES with inert placeholder values, and assertions check
names only.
"""

from __future__ import annotations

from helios.backend.process.env_scrub import (
    AGENT_SOCKET_ENV,
    CLAUDE_AUTH_ENV,
    CODEX_EXEC_ENV,
    HELIOS_INTERNAL_ENV,
    POLICY_KEEP,
    names_to_scrub,
    scrub_helios_env,
    scrubbed_child_env,
)


class FakeLauncher:
    """Records unsetenv() calls like Gio.SubprocessLauncher would receive."""

    def __init__(self) -> None:
        self.unset: list[str] = []

    def unsetenv(self, name: str) -> None:
        self.unset.append(name)


# A parent environment shaped like a real workstation: both providers' auth,
# benign vars, a spread of unrelated credentials, and the two agent sockets.
# Values are inert placeholders — the scrub is name-based and never reads them.
_SAMPLE_ENV = {
    # Anthropic auth
    "ANTHROPIC_API_KEY": "x",
    "ANTHROPIC_AUTH_TOKEN": "x",
    "CLAUDE_CODE_OAUTH_TOKEN": "x",
    # OpenAI/Codex auth
    "OPENAI_API_KEY": "x",
    "CODEX_API_KEY": "x",
    "CODEX_ACCESS_TOKEN": "x",
    # benign, must survive
    "PATH": "/usr/bin",
    "HOME": "/home/user",
    "CLAUDE_HOME": "/home/user/.claude",
    "CODEX_HOME": "/home/user/.codex",
    "LANG": "en_US.UTF-8",
    "XAUTHORITY": "/run/user/1000/.mutter-Xwaylandauth",
    "GIT_PAGER": "cat",
    "PWD": "/home/user/repo",  # bare PWD/OLDPWD must survive the _PWD marker
    "OLDPWD": "/home/user",
    "CLASSPATH": "/opt/java",  # must survive: no _PASS substring
    "BYPASS_CACHE": "1",  # must survive: BYPASS has no underscore before PASS
    # unrelated credentials + name families the heuristic must catch
    "TANDEM_TELEMETRY_SECRET": "x",
    "AWS_SECRET_ACCESS_KEY": "x",
    "AWS_ACCESS_KEY_ID": "x",
    "GITHUB_TOKEN": "x",
    "GITLAB_PAT": "x",
    "GH_PAT": "x",
    "DOCKER_AUTH_CONFIG": "x",
    "GIT_ASKPASS": "/usr/lib/git-core/git-askpass",
    "SUDO_ASKPASS": "/usr/bin/ssh-askpass",
    "MY_JWT": "x",
    "SENTRY_AUTH_TOKEN": "x",
    "MY_SERVICE_APIKEY": "x",
    "DB_PASSWORD": "x",
    "REDIS_PASS": "x",
    "SMTP_PASS": "x",
    "MYSQL_PWD": "x",
    "GPG_PASSPHRASE": "x",
    "SSH_KEY_PASSPHRASE": "x",
    "STRIPE_KEY": "x",
    "SIGNING_KEY": "x",
    "GNOME_KEYRING_CONTROL": "/run/user/1000/keyring",
    # SSH agent socket — RETAINED by explicit policy (agents ssh/push).
    "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.sock",
    # gpg agent socket — dropped as hygiene (not a GPG boundary).
    "GPG_AGENT_INFO": "/run/user/1000/gnupg/S.gpg-agent",
    # Helios-internal — always dropped
    "APOLLO_SCRATCHPAD_KEY": "x",
}

_UNRELATED_CREDENTIALS = {
    "TANDEM_TELEMETRY_SECRET",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "GITHUB_TOKEN",
    "GITLAB_PAT",
    "GH_PAT",
    "DOCKER_AUTH_CONFIG",
    "GIT_ASKPASS",
    "SUDO_ASKPASS",
    "MY_JWT",
    "SENTRY_AUTH_TOKEN",
    "MY_SERVICE_APIKEY",
    "DB_PASSWORD",
    "REDIS_PASS",
    "SMTP_PASS",
    "MYSQL_PWD",
    "GPG_PASSPHRASE",
    "SSH_KEY_PASSPHRASE",
    "STRIPE_KEY",
    "SIGNING_KEY",
    "GNOME_KEYRING_CONTROL",
}

_BENIGN = {"PATH", "HOME", "CLAUDE_HOME", "CODEX_HOME", "LANG",
           "XAUTHORITY", "GIT_PAGER", "PWD", "OLDPWD",
           "CLASSPATH", "BYPASS_CACHE"}


# Every keep variant a real spawn uses: no-key (native Codex App Server /
# discovery / login / status / version, and --help/--version probes), Claude
# model spawns, and the codex-exec-only key.
_ALL_KEEPS = ((), CLAUDE_AUTH_ENV, CODEX_EXEC_ENV)


def test_native_codex_and_its_tools_get_no_env_key():
    """App Server / discovery / login / status / version forward keep=() — so
    neither OpenAI nor Codex nor Anthropic keys reach native Codex or the tool
    children that inherit its env."""
    env = scrubbed_child_env(_SAMPLE_ENV)  # keep=() — the native path
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert "CODEX_ACCESS_TOKEN" not in env
    assert env.keys().isdisjoint(CLAUDE_AUTH_ENV)


def test_codex_exec_gets_only_codex_api_key():
    env = scrubbed_child_env(_SAMPLE_ENV, keep=CODEX_EXEC_ENV)
    assert "CODEX_API_KEY" in env
    assert "OPENAI_API_KEY" not in env  # ignored by the pinned CLI; never forwarded
    assert "CODEX_ACCESS_TOKEN" not in env  # persisted-auth-only Helios boundary
    assert env.keys().isdisjoint(CLAUDE_AUTH_ENV)


def test_claude_child_gets_only_documented_claude_auth():
    env = scrubbed_child_env(_SAMPLE_ENV, keep=CLAUDE_AUTH_ENV)
    assert CLAUDE_AUTH_ENV <= env.keys()  # its own env auth survives
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_API_KEY" not in env
    assert "CODEX_ACCESS_TOKEN" not in env


def test_bare_openai_key_is_never_forwarded_to_any_child():
    for keep in _ALL_KEEPS:
        assert "OPENAI_API_KEY" not in scrubbed_child_env(_SAMPLE_ENV, keep=keep)


def test_unrelated_credential_families_are_dropped():
    # Every scoped variant drops all unrelated credentials, incl. PAT / askpass
    # / auth-config / jwt / *_PWD / *_PASS / passphrase families.
    for keep in _ALL_KEEPS:
        env = scrubbed_child_env(_SAMPLE_ENV, keep=keep)
        assert _UNRELATED_CREDENTIALS.isdisjoint(env.keys())


def test_benign_vars_survive():
    for keep in _ALL_KEEPS:
        env = scrubbed_child_env(_SAMPLE_ENV, keep=keep)
        assert _BENIGN <= env.keys()


def test_ssh_agent_socket_retained_by_policy_everywhere():
    for keep in _ALL_KEEPS:
        env = scrubbed_child_env(_SAMPLE_ENV, keep=keep)
        assert env.get("SSH_AUTH_SOCK") == _SAMPLE_ENV["SSH_AUTH_SOCK"]
    assert POLICY_KEEP == frozenset({"SSH_AUTH_SOCK"})


def test_gpg_agent_socket_dropped_as_hygiene():
    env = scrubbed_child_env(_SAMPLE_ENV, keep=CODEX_EXEC_ENV)
    assert "GPG_AGENT_INFO" not in env
    assert "GPG_AGENT_INFO" in AGENT_SOCKET_ENV


def test_helios_internal_always_dropped():
    env = scrubbed_child_env(_SAMPLE_ENV, keep=CLAUDE_AUTH_ENV)
    assert env.keys().isdisjoint(HELIOS_INTERNAL_ENV)


def test_names_to_scrub_is_name_only_and_deterministic():
    names = names_to_scrub(_SAMPLE_ENV.keys(), keep=CLAUDE_AUTH_ENV)
    assert names == sorted(names)
    assert _UNRELATED_CREDENTIALS <= set(names)
    assert set(names).isdisjoint(CLAUDE_AUTH_ENV)  # kept provider not in drops
    assert "SSH_AUTH_SOCK" not in names


def test_launcher_path_accepts_and_honours_keep():
    keep_launcher = FakeLauncher()
    scrub_helios_env(keep_launcher, keep=CLAUDE_AUTH_ENV)
    unset = set(keep_launcher.unset)
    assert set(HELIOS_INTERNAL_ENV) <= unset
    assert set(AGENT_SOCKET_ENV) <= unset
    assert unset.isdisjoint(CLAUDE_AUTH_ENV)  # keep not unset
    assert "SSH_AUTH_SOCK" not in unset  # POLICY_KEEP not unset


def test_auth_sets_never_overlap_cross_provider():
    # Cross-provider isolation depends on these never overlapping.
    assert CLAUDE_AUTH_ENV.isdisjoint(CODEX_EXEC_ENV)


# --- operator-granted Infisical workload identity ------------------


def test_infisical_identity_reaches_a_scrubbed_child_when_granted():
    """The whole point of the grant: the interactive agent can authenticate."""
    from helios.backend.process.env_scrub import (
        CLAUDE_AUTH_ENV,
        INFISICAL_WORKLOAD_ENV,
        names_to_scrub,
    )

    names = [
        "INFISICAL_CLIENT_ID",
        "INFISICAL_CLIENT_SECRET",
        "GITLAB_TOKEN",
        "JIRA_API_KEY",
    ]
    dropped = set(
        names_to_scrub(names, keep=CLAUDE_AUTH_ENV | INFISICAL_WORKLOAD_ENV)
    )

    assert "INFISICAL_CLIENT_SECRET" not in dropped
    assert "INFISICAL_CLIENT_ID" not in dropped
    # One bootstrap credential, not a pile: everything derivable from Infisical
    # stays scrubbed, so widening the grant has to be a deliberate edit.
    assert "GITLAB_TOKEN" in dropped
    assert "JIRA_API_KEY" in dropped


def test_infisical_identity_is_scrubbed_for_housekeeping_and_codex_spawns():
    """Title-gen, archival, and probes share CLAUDE_AUTH_ENV but must not hold
    a secrets-manager credential; a Codex child must never see it either."""
    from helios.backend.process.env_scrub import (
        CLAUDE_AUTH_ENV,
        CODEX_EXEC_ENV,
        names_to_scrub,
    )

    names = ["INFISICAL_CLIENT_SECRET"]

    assert "INFISICAL_CLIENT_SECRET" in names_to_scrub(names, keep=CLAUDE_AUTH_ENV)
    assert "INFISICAL_CLIENT_SECRET" in names_to_scrub(names, keep=CODEX_EXEC_ENV)
    assert "INFISICAL_CLIENT_SECRET" in names_to_scrub(names)


# --- operator-granted Norvi Tracker token (2026-08-22) ----------------------


def test_tracker_token_reaches_a_granted_session_but_nothing_else():
    """A session can run `norvi-work`; it still gets no other credential."""
    from helios.backend.process.env_scrub import (
        CLAUDE_AUTH_ENV,
        NORVI_TRACKER_ENV,
        names_to_scrub,
    )

    names = ["NORVI_TRACKER_API_TOKEN", "GITLAB_TOKEN", "JIRA_API_KEY"]
    dropped = set(names_to_scrub(names, keep=CLAUDE_AUTH_ENV | NORVI_TRACKER_ENV))

    assert "NORVI_TRACKER_API_TOKEN" not in dropped
    assert {"GITLAB_TOKEN", "JIRA_API_KEY"} <= dropped


def test_tracker_token_is_scrubbed_for_housekeeping_spawns():
    """Title-gen, archival and the probes have no work to document."""
    from helios.backend.process.env_scrub import (
        CLAUDE_AUTH_ENV,
        CODEX_EXEC_ENV,
        names_to_scrub,
    )

    names = ["NORVI_TRACKER_API_TOKEN"]

    assert "NORVI_TRACKER_API_TOKEN" in names_to_scrub(names, keep=CLAUDE_AUTH_ENV)
    assert "NORVI_TRACKER_API_TOKEN" in names_to_scrub(names, keep=CODEX_EXEC_ENV)
    assert "NORVI_TRACKER_API_TOKEN" in names_to_scrub(names)


def _files_naming(symbol_pattern: str) -> list[str]:
    """Modules that reference a grant constant, excluding env_scrub itself.

    Matches the NAME anywhere rather than a `keep=...` shape. The older regex
    keyed off `keep=\\S*NAME` and `NAME\\s*)`, so wrapping one call site across
    two lines silently emptied the result and the test passed by finding
    nothing at all — a detector for scope creep must not be defeatable by
    `ruff format`.
    """
    import re
    from pathlib import Path

    import helios

    src_root = Path(helios.__file__).parent
    return sorted(
        p.relative_to(src_root).as_posix()
        for p in src_root.rglob("*.py")
        if p.name != "env_scrub.py" and re.search(symbol_pattern, p.read_text())
    )


def test_only_the_interactive_claude_driver_grants_the_identity():
    """Pin the scope at the call site, not just in the constant."""
    granting = _files_naming(r"\bINFISICAL_(?:WORKLOAD_|CONFIG_)?ENV\b")

    assert granting == ["backend/process/cli_driver.py"], granting


def test_only_interactive_sessions_get_the_tracker_token():
    """One grant per interactive provider — and nothing else.

    Housekeeping spawns (title generation, session archival, auth/mcp/version
    probes) have no work to document, so they must never hold a tracker
    credential a tool call could read.
    """
    granting = _files_naming(r"\bNORVI_TRACKER_ENV\b")

    assert granting == [
        "backend/process/cli_driver.py",  # Claude
        "backend/process/codex_app_server.py",  # GPT, and its tool children
        "backend/process/openrouter_tools.py",  # OpenRouter's Bash tool
    ], granting
