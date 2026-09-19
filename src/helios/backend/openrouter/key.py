"""User-level OpenRouter API key for the desktop chat driver.

This is deliberately separate from the root-owned broker credential at
``/etc/helios-router/openrouter.key``: the Smart Routing broker boundary is
unchanged. The chat driver's key is a dedicated consumer credential owned by
the desktop user, stored owner-only under ``~/.helios/`` — the same on-disk
precedent as the other Helios state files (0600, atomic replace).

GTK-free so Settings and tests can use it anywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from helios.log import get_logger

_log = get_logger("openrouter-key")

KEY_PATH = Path.home() / ".helios" / "openrouter.key"

__all__ = ["KEY_PATH", "KeyValidationError", "delete_key", "load_key", "save_key", "validate_key"]


class KeyValidationError(ValueError):
    """The offered key cannot be a usable OpenRouter credential."""


def validate_key(key: str) -> str:
    """Return the stripped key or raise KeyValidationError.

    Same shape rules as the broker gateway: minimum length, no surrounding
    whitespace ambiguity, and no characters that could smuggle headers.
    """
    stripped = (key or "").strip()
    if (
        len(stripped) < 16
        or any(character in stripped for character in (" ", "\t", "\r", "\n", "\x00"))
    ):
        raise KeyValidationError("not a usable OpenRouter API key")
    return stripped


def load_key() -> str:
    """The saved key, or "" when none/unreadable. Never raises."""
    try:
        return KEY_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_key(key: str) -> None:
    """Validate and persist the key atomically with owner-only permissions."""
    stripped = validate_key(key)
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = KEY_PATH.with_suffix(".key.tmp")
    tmp.write_text(stripped + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(KEY_PATH)
    _log.info("OpenRouter user key saved (%d chars)", len(stripped))


def delete_key() -> None:
    try:
        KEY_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        _log.warning("could not delete %s: %s", KEY_PATH, e)
