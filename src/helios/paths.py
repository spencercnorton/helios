"""Filesystem paths owned by Helios itself.

Centralised here so every state file (`work.db`, `router.db`,
`estate-mcp.json`, …) resolves through one function and tests or
sandboxes can redirect the whole tree with ``HELIOS_STATE_DIR``.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path


def state_dir() -> Path:
    """Directory for Helios-owned state files (default ``~/.helios``)."""
    override = os.environ.get("HELIOS_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".helios"


class _BoundStatePath(type(Path())):
    """Path value produced dynamically from ``state_dir()``."""

    _is_bound_state_path = True


class _StateModule(types.ModuleType):
    """Module type that resolves bound state-path attributes via ``state_dir()``."""

    def __getattribute__(self, name: str):
        d = object.__getattribute__(self, "__dict__")
        specs = d.get("_state_specs")
        if specs and name in specs and name not in d:
            parts = specs[name]
            return _BoundStatePath(state_dir().joinpath(*parts))
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value: object) -> None:
        d = object.__getattribute__(self, "__dict__")
        specs = d.get("_state_specs")
        if specs and name in specs and getattr(value, "_is_bound_state_path", False):
            d.pop(name, None)
            return
        super().__setattr__(name, value)

    def __dir__(self) -> list[str]:
        d = object.__getattribute__(self, "__dict__")
        specs = d.get("_state_specs") or {}
        return sorted(set(super().__dir__()) | set(specs.keys()))

    def __delattr__(self, name: str) -> None:
        d = object.__getattribute__(self, "__dict__")
        if name in d:
            del d[name]
        else:
            super().__delattr__(name)


def bind_state_paths(
    module_name: str,
    **specs: str | tuple[str, ...],
) -> types.ModuleType:
    """Bind dynamic ``state_dir()``-relative ``Path`` attributes on a module.

    Attribute reads resolve ``state_dir()`` at access time unless overridden via
    ``setattr`` / ``monkeypatch.setattr`` / ``unittest.mock.patch.object``.
    """
    mod = sys.modules[module_name]
    if not isinstance(mod, _StateModule):
        mod.__class__ = _StateModule
    normalized: dict[str, tuple[str, ...]] = dict(mod.__dict__.get("_state_specs") or {})
    for attr, rel in specs.items():
        normalized[attr] = (rel,) if isinstance(rel, str) else tuple(rel)
        mod.__dict__.pop(attr, None)
    mod.__dict__["_state_specs"] = normalized
    return mod
