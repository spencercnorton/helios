from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


INSTALLER = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "install-helios-router-service"
)
SERVICE_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "helios-router.service.in"
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_staged_release_is_traversable_but_not_writable_by_service_user(
    tmp_path,
):
    release = tmp_path / "release"
    python = release / "venv" / "bin" / "python"
    data = release / "source" / "src" / "helios" / "__init__.py"
    python.parent.mkdir(parents=True)
    data.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    data.write_text('__version__ = "0.41.0"\n')

    os.chmod(release, 0o700)
    os.chmod(release / "venv", 0o700)
    os.chmod(release / "venv" / "bin", 0o700)
    os.chmod(python, 0o700)
    os.chmod(release / "source", 0o700)
    os.chmod(release / "source" / "src", 0o700)
    os.chmod(release / "source" / "src" / "helios", 0o700)
    os.chmod(data, 0o600)

    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; harden_release_tree "$2"',
            "router-installer-test",
            str(INSTALLER),
            str(release),
        ],
        check=True,
    )

    for directory in (
        release,
        release / "venv",
        release / "venv" / "bin",
        release / "source",
        release / "source" / "src",
        release / "source" / "src" / "helios",
    ):
        mode = _mode(directory)
        assert mode & 0o005 == 0o005
        assert mode & 0o022 == 0

    assert _mode(python) & 0o005 == 0o005
    assert _mode(python) & 0o022 == 0
    assert _mode(data) & 0o004 == 0o004
    assert _mode(data) & 0o023 == 0


# deploy/ is not part of the public source tree.
@pytest.mark.skipif(not SERVICE_TEMPLATE.exists(), reason="deploy/helios-router.service.in is not part of this tree")
def test_router_service_uses_credential_group_as_primary():
    template = SERVICE_TEMPLATE.read_text()

    assert "\nGroup=helios-router\n" in template
    assert "\nSupplementaryGroups=@SOCKET_GROUP@\n" in template
    assert "\nGroup=@SOCKET_GROUP@\n" not in template
    assert "\nSupplementaryGroups=helios-router\n" not in template
