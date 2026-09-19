"""No test run may touch the developer's real Helios state.

On 2026-07-28 a plain `python3 -m pytest` on the workstation overwrote the
running app's `~/.helios/openrouter-models.json`, cutting its live catalog from
367 models to 1. `conftest.isolate_state_dir` was supposed to prevent exactly
that, and could not: nine modules computed `Path.home() / ".helios" / ...` at
**import** time, so their constants were bound to the real path before any
fixture existed.

conftest now quarantines HOME before the first helios import. These tests are
the tripwire: they fail if a module reintroduces the pattern, or if the
quarantine itself stops working.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import helios
from conftest import QUARANTINE, REAL_HOME


def _helios_modules():
    """Every importable helios module, skipping ones needing absent deps."""
    for info in pkgutil.walk_packages(helios.__path__, prefix="helios."):
        try:
            yield importlib.import_module(info.name)
        except Exception:
            # GTK-dependent modules on the slim image, optional deps, etc.
            continue


def _paths_in(module):
    for name in dir(module):
        if name.startswith("__"):
            continue
        try:
            value = getattr(module, name)
        except Exception:
            continue
        if isinstance(value, Path):
            yield name, value


def _outside_real_home(path: Path) -> bool:
    text = str(path)
    return text != str(REAL_HOME) and not text.startswith(str(REAL_HOME) + "/")


def test_quarantine_is_actually_in_effect():
    # Two layers, and the test asserts what both exist for: nothing resolves
    # into the real home. conftest quarantines HOME at import time (covering
    # collection-time imports); isolate_state_dir then narrows it further to a
    # per-test tmp_path, which is why Path.home() here is the fixture's dir
    # rather than the quarantine.
    assert QUARANTINE != REAL_HOME
    assert _outside_real_home(QUARANTINE)
    assert _outside_real_home(Path.home())


def test_no_module_holds_a_path_inside_the_real_home():
    """The regression guard.

    A module-level `Path.home() / ".helios" / ...` binds before any fixture can
    redirect it — so if one is pointing into the real home, this run is one
    `_write_json` away from clobbering live state.
    """
    offenders: list[str] = []
    real = str(REAL_HOME)
    for module in _helios_modules():
        for name, value in _paths_in(module):
            text = str(value)
            if text == real or text.startswith(real + "/"):
                offenders.append(f"{module.__name__}.{name} = {text}")
    assert not offenders, (
        "module-level paths resolve into the real home directory, so a test "
        "run can overwrite live state. Resolve them at call time via "
        "helios.paths.state_dir(). Offenders:\n  " + "\n  ".join(sorted(offenders))
    )


@pytest.mark.parametrize(
    ("module_name", "attr"),
    [
        ("helios.backend.openrouter.catalog", "CACHE_PATH"),
        ("helios.backend.openrouter.key", "KEY_PATH"),
        ("helios.backend.ui_state", "_DEFAULT_PATH"),
        ("helios.backend.model_catalog", "_CATALOG_CACHE"),
        ("helios.backend.project_names", "_DEFAULT_PATH"),
        ("helios.backend.session_archiver", "ARCHIVE_ROOT"),
        ("helios.backend.projects", "HELIOS_ARCHIVE_ROOT"),
        ("helios.backend.memory_io", "_BACKUPS_ROOT"),
        ("helios.backend.title_store", "_CACHE_PATH"),
    ],
)
def test_each_known_import_time_path_landed_in_the_quarantine(module_name, attr):
    """The specific nine from an earlier fix, named so a regression says which broke."""
    module = pytest.importorskip(module_name)
    value = getattr(module, attr, None)
    if value is None:
        pytest.skip(f"{module_name}.{attr} no longer exists (resolved lazily?)")
    assert _outside_real_home(value), (
        f"{module_name}.{attr} = {value} resolves into the real home"
    )


def test_the_specific_file_that_was_destroyed_is_not_reachable():
    """openrouter-models.json, by name — this is the one that actually died."""
    from helios.backend.openrouter import catalog

    assert _outside_real_home(catalog.CACHE_PATH)
    assert catalog.CACHE_PATH.name == "openrouter-models.json"
