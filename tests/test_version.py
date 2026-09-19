"""The UI/App Server identity, package metadata and CHANGELOG must agree."""

from pathlib import Path
import re
import tomllib

import pytest

import helios

_ROOT = Path(__file__).resolve().parents[1]
_CHANGELOG = _ROOT / "CHANGELOG.md"
# The public source tree ships without CHANGELOG.md (its history names private
# infrastructure); these two checks are about the private release process.
_needs_changelog = pytest.mark.skipif(not _CHANGELOG.exists(), reason="CHANGELOG.md is not part of this tree")


def _package_version() -> str:
    pyproject = _ROOT / "pyproject.toml"
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


def test_runtime_and_package_versions_match() -> None:
    assert helios.__version__ == _package_version()


@_needs_changelog
def test_changelog_leads_with_the_shipping_version() -> None:
    """v0.68.4 shipped with both version files still reading 0.68.3.

    The two files agreed with each other, so the check above passed, and the
    CHANGELOG — the one artifact that already said 0.68.4 — was not compared to
    anything. A release whose tag, changelog and `__version__` disagree is the
    exact thing the tag sweep cannot reason about later.
    """

    changelog = _CHANGELOG.read_text(encoding="utf-8")
    versions = re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\]", changelog)

    assert versions, "CHANGELOG has no released version headings"
    assert versions[0] == _package_version(), (
        f"CHANGELOG leads with {versions[0]} but the package is "
        f"{_package_version()} — bump both or neither"
    )


@_needs_changelog
def test_changelog_versions_are_ordered_newest_first() -> None:
    changelog = _CHANGELOG.read_text(encoding="utf-8")
    versions = [
        tuple(int(p) for p in v.split("."))
        for v in re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\]", changelog)
    ]

    assert versions == sorted(versions, reverse=True), (
        "CHANGELOG entries are out of order — a rebase that resolves a "
        "conflict by keeping both sides can interleave them"
    )
