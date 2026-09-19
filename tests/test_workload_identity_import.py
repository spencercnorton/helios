"""Adopting the Infisical machine identity at startup.

Helios's driver already forwards INFISICAL_WORKLOAD_ENV to interactive Claude
sessions — but it can only forward what this process has, and the launch path
that the desktop entry uses does not inherit the systemd user environment.
"""

from __future__ import annotations

import subprocess

from helios.backend.process import env_scrub


def _fake_systemctl(monkeypatch, stdout: str, *, boom: Exception | None = None):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if boom is not None:
            raise boom
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(env_scrub.subprocess, "run", run)
    return calls


def test_adopts_both_names_when_the_process_has_neither(monkeypatch) -> None:
    _fake_systemctl(
        monkeypatch,
        "LANG=en_GB.UTF-8\n"
        "INFISICAL_CLIENT_ID=abc-123\n"
        "INFISICAL_CLIENT_SECRET=s3cr3t\n",
    )
    env: dict[str, str] = {}

    adopted = env_scrub.import_workload_identity(env)

    assert adopted == ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET")
    assert env["INFISICAL_CLIENT_ID"] == "abc-123"
    assert env["INFISICAL_CLIENT_SECRET"] == "s3cr3t"


def test_an_explicit_value_is_never_overwritten(monkeypatch) -> None:
    _fake_systemctl(
        monkeypatch,
        "INFISICAL_CLIENT_ID=from-systemd\n"
        "INFISICAL_CLIENT_SECRET=from-systemd\n",
    )
    env = {"INFISICAL_CLIENT_ID": "explicit"}

    adopted = env_scrub.import_workload_identity(env)

    assert env["INFISICAL_CLIENT_ID"] == "explicit"
    assert adopted == ("INFISICAL_CLIENT_SECRET",)


def test_systemd_shell_quoting_is_unquoted_not_taken_literally(monkeypatch) -> None:
    """A literal quote in a credential would fail auth in a confusing way."""

    _fake_systemctl(
        monkeypatch,
        "INFISICAL_CLIENT_ID='quoted-id'\n"
        'INFISICAL_CLIENT_SECRET="has space"\n',
    )
    env: dict[str, str] = {}

    env_scrub.import_workload_identity(env)

    assert env["INFISICAL_CLIENT_ID"] == "quoted-id"
    assert env["INFISICAL_CLIENT_SECRET"] == "has space"


def test_only_the_allow_listed_names_can_be_adopted(monkeypatch) -> None:
    """A polluted user environment must not smuggle anything in through this."""

    _fake_systemctl(
        monkeypatch,
        "INFISICAL_CLIENT_ID=abc\n"
        "INFISICAL_CLIENT_SECRET=def\n"
        "AWS_SECRET_ACCESS_KEY=nope\n"
        "GITLAB_TOKEN=nope\n"
        "PATH=/nope\n",
    )
    env: dict[str, str] = {}

    env_scrub.import_workload_identity(env)

    assert set(env) == {"INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET"}


def test_nothing_runs_when_the_identity_is_already_present(monkeypatch) -> None:
    calls = _fake_systemctl(monkeypatch, "")
    env = {
        "INFISICAL_CLIENT_ID": "a",
        "INFISICAL_CLIENT_SECRET": "b",
        "INFISICAL_API_URL": "http://192.0.2.1:8080/api",
        "NORVI_TRACKER_API_TOKEN": "c",
    }

    assert env_scrub.import_workload_identity(env) == ()
    assert calls == [], "must not shell out when there is nothing to fill in"


def test_a_missing_systemctl_degrades_quietly(monkeypatch) -> None:
    _fake_systemctl(monkeypatch, "", boom=OSError("no systemctl"))
    env: dict[str, str] = {}

    assert env_scrub.import_workload_identity(env) == ()
    assert env == {}


def test_the_self_hosted_url_is_adopted_too(monkeypatch) -> None:
    """Without it the CLI silently targets Infisical Cloud, not our server."""

    _fake_systemctl(
        monkeypatch,
        "INFISICAL_CLIENT_ID=abc\n"
        "INFISICAL_CLIENT_SECRET=def\n"
        "INFISICAL_API_URL=http://192.0.2.1:8080/api\n",
    )
    env: dict[str, str] = {}

    adopted = env_scrub.import_workload_identity(env)

    assert "INFISICAL_API_URL" in adopted
    assert env["INFISICAL_API_URL"] == "http://192.0.2.1:8080/api"


def test_the_url_reaches_a_claude_child() -> None:
    """The identity is useless if the session cannot tell which Infisical."""

    env = {
        "INFISICAL_CLIENT_ID": "abc",
        "INFISICAL_CLIENT_SECRET": "def",
        "INFISICAL_API_URL": "http://192.0.2.1:8080/api",
    }

    child = env_scrub.scrubbed_child_env(env, keep=env_scrub.INFISICAL_ENV)

    assert child["INFISICAL_API_URL"] == "http://192.0.2.1:8080/api"


def test_the_url_is_config_not_credential() -> None:
    """Keeps the trust grant honest: only the pair is 'the secret'."""

    assert "INFISICAL_API_URL" not in env_scrub.INFISICAL_WORKLOAD_ENV
    assert env_scrub.INFISICAL_ENV == (
        env_scrub.INFISICAL_WORKLOAD_ENV | env_scrub.INFISICAL_CONFIG_ENV
    )


def test_the_adopted_identity_survives_the_scrub_into_a_claude_child() -> None:
    """End to end: adoption is pointless if the scrub then strips it.

    INFISICAL_CLIENT_SECRET matches the `SECRET` credential marker, so it is
    only present because the driver's keep-list names it.
    """

    env = {
        "INFISICAL_CLIENT_ID": "abc",
        "INFISICAL_CLIENT_SECRET": "def",
        "AWS_SECRET_ACCESS_KEY": "nope",
    }

    child = env_scrub.scrubbed_child_env(env, keep=env_scrub.INFISICAL_WORKLOAD_ENV)

    assert child["INFISICAL_CLIENT_ID"] == "abc"
    assert child["INFISICAL_CLIENT_SECRET"] == "def"
    assert "AWS_SECRET_ACCESS_KEY" not in child


def test_without_the_keep_list_the_secret_is_still_scrubbed() -> None:
    """Pins that the keep-list is what carries it, not a hole in the scrub."""

    env = {"INFISICAL_CLIENT_SECRET": "def"}

    assert "INFISICAL_CLIENT_SECRET" not in env_scrub.scrubbed_child_env(env)
