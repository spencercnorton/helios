"""Every driver Helios spawns defines the signals the window wires for all.

v0.69.0 added "capabilities-updated" and "context-usage" to the *common* half
of `connect_handlers()`, but only ClaudeCliDriver defines them. GObject raises
`TypeError: unknown signal name` on connect, so the exception landed inside
`_on_composer_send` -> `_ensure_driver` -> `DriverManager.start_new`: every GPT
and OpenRouter send died at spawn with nothing at all shown in the UI.

Nothing caught it because the common list was a bare literal inside a closure.
It is now `COMMON_DRIVER_SIGNALS`, and this test is what makes adding a
provider-specific signal to it fail loudly here instead of silently in
production.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from gi.repository import GObject  # noqa: E402

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402
from helios.backend.process.codex_app_driver import (  # noqa: E402
    CodexAppServerDriver,
)
from helios.backend.process.codex_driver import CodexCliDriver  # noqa: E402
from helios.backend.process.driver_manager import (  # noqa: E402
    COMMON_DRIVER_SIGNALS,
)
from helios.backend.process.openrouter_driver import (  # noqa: E402
    OpenRouterDriver,
)

DRIVERS = (
    ClaudeCliDriver,
    CodexCliDriver,
    CodexAppServerDriver,
    OpenRouterDriver,
)


@pytest.mark.parametrize("driver_cls", DRIVERS, ids=lambda c: c.__name__)
def test_every_driver_defines_the_common_signal_contract(driver_cls) -> None:
    missing = [
        name
        for name in COMMON_DRIVER_SIGNALS
        if not GObject.signal_lookup(name, driver_cls)
    ]
    assert not missing, (
        f"{driver_cls.__name__} does not define {missing}. Either the signal "
        "is provider-specific — wire it in an isinstance branch of "
        "connect_handlers() — or every driver must emit it."
    )
