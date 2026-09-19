from __future__ import annotations

import types

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

from helios.backend import model_catalog  # noqa: E402
from helios.widgets.work_indicator import WorkIndicator, provider_label  # noqa: E402


def _p(provider: str, role: str):
    return types.SimpleNamespace(provider=provider, role=role, status="active")


def test_provider_label_maps_known_providers():
    assert provider_label(model_catalog.PROVIDER_ANTHROPIC) == "Claude"
    assert provider_label(model_catalog.PROVIDER_OPENAI) == "GPT"
    assert provider_label("") == "Unknown"


def test_hidden_for_solo_single_provider_work():
    ind = WorkIndicator()
    ind.update(
        "solo",
        model_catalog.PROVIDER_ANTHROPIC,
        [_p(model_catalog.PROVIDER_ANTHROPIC, "lead")],
    )
    assert ind.get_visible() is False


def test_visible_for_multi_provider_tandem_with_lead_named():
    ind = WorkIndicator()
    ind.update(
        "tandem",
        model_catalog.PROVIDER_ANTHROPIC,
        [
            _p(model_catalog.PROVIDER_ANTHROPIC, "lead"),
            _p(model_catalog.PROVIDER_OPENAI, "partner"),
        ],
    )
    assert ind.get_visible() is True
    assert "lead: Claude" in (ind.get_tooltip_text() or "")


def _tandem(ind, **kw):
    ind.update(
        "tandem",
        model_catalog.PROVIDER_ANTHROPIC,
        [
            _p(model_catalog.PROVIDER_ANTHROPIC, "lead"),
            _p(model_catalog.PROVIDER_OPENAI, "partner"),
        ],
        **kw,
    )


def test_handoff_action_pins_and_fires_callback():
    fired = []
    ind = WorkIndicator()
    _tandem(ind, handoff_label="GPT", on_handoff=lambda: fired.append(True))
    assert ind._on_handoff is not None
    ind._on_handoff_clicked(None)  # same path as the popover button
    assert fired == [True]


def test_handoff_action_absent_without_target():
    ind = WorkIndicator()
    _tandem(ind)  # no handoff_label / on_handoff → falls back to the hint
    assert ind._on_handoff is None
    ind._on_handoff_clicked(None)  # no-op, must not raise
