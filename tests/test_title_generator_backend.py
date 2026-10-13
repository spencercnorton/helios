"""Title-generation backend selection.

These construct a real `TitleGenerator` (a GObject), so unlike the TitleStore
tests they need the gi/Gtk typelib — gtk_tests lane only.
"""

from __future__ import annotations

from types import SimpleNamespace
import io
import json

import pytest

pytest.importorskip("gi")

from helios.backend.process import title_generator as tg  # noqa: E402
from helios.backend.process import local_engine  # noqa: E402
from helios.backend import ollama_titles  # noqa: E402


def test_title_generator_sends_think_false_for_short_titles(monkeypatch):
    calls = []

    def urlopen(request, *, timeout):
        payload = json.loads(request.data)
        calls.append(payload)
        # A thinking-by-default server can spend the entire 32-token budget
        # on hidden reasoning unless the actual title request disables it.
        answer = "Review capacity routing" if payload.get("think") is False else ""
        return io.BytesIO(json.dumps({"response": answer}).encode())

    monkeypatch.setattr(ollama_titles.urllib.request, "urlopen", urlopen)
    assert tg._ollama_generate("http://server", "qwen3.6:35b", "Synthetic title") == "Review capacity routing"
    assert len(calls) == 1


@pytest.fixture
def stub_store(monkeypatch):
    """Keep _spawn's store lookup off the real ~/.helios cache."""
    monkeypatch.setattr(tg, "store", lambda: SimpleNamespace(get=lambda _session_id: None))


def test_title_generation_defaults_to_first_message_without_model_call(monkeypatch):
    generator = tg.TitleGenerator()
    selected_defaults: list[tuple[str, str]] = []

    class Ui:
        def get(self, key, default=None):
            selected_defaults.append((key, default))
            return default

    monkeypatch.setattr(tg, "ui_state_store", lambda: Ui())
    monkeypatch.setattr(
        tg,
        "store",
        lambda: SimpleNamespace(get=lambda _session_id: None),
    )
    monkeypatch.setattr(tg, "_extract_seed", lambda _session: ("Fix auth", "Done"))
    monkeypatch.setattr(
        generator,
        "_spawn_builtin",
        lambda *_a: pytest.fail("default backend should not spawn builtin local engine"),
    )
    monkeypatch.setattr(
        generator,
        "_spawn_ollama",
        lambda *_a: pytest.fail("default backend should not spawn ollama"),
    )
    monkeypatch.setattr(
        tg,
        "find_claude_binary",
        lambda: pytest.fail("default backend should not spawn Claude"),
    )

    generator._spawn(SimpleNamespace(session_id="session-1"))

    assert ("title_backend", "first_message") in selected_defaults
    assert generator._in_flight == ""


def test_explicit_builtin_backend_spawns_local_engine(stub_store, monkeypatch):
    generator = tg.TitleGenerator()

    class Ui:
        def get(self, key, default=None):
            return "builtin" if key == "title_backend" else default

    monkeypatch.setattr(tg, "ui_state_store", lambda: Ui())
    monkeypatch.setattr(tg, "_extract_seed", lambda _session: ("Fix auth", "Done"))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        generator,
        "_spawn_builtin",
        lambda session_id, prompt: calls.append((session_id, prompt)),
    )

    generator._spawn(SimpleNamespace(session_id="session-builtin"))

    assert calls and calls[0][0] == "session-builtin"


@pytest.mark.parametrize("invalid_backend", ["", "future", None, 7])
def test_invalid_title_backend_never_opts_into_cloud_or_downloads(
    stub_store, monkeypatch, invalid_backend
):
    generator = tg.TitleGenerator()

    class Ui:
        def get(self, _key, _default=None):
            return invalid_backend

    monkeypatch.setattr(tg, "ui_state_store", lambda: Ui())
    monkeypatch.setattr(tg, "_extract_seed", lambda _session: ("Fix auth", "Done"))
    monkeypatch.setattr(
        generator,
        "_spawn_builtin",
        lambda *_a: pytest.fail("invalid state attempted a builtin model call"),
    )
    monkeypatch.setattr(
        tg,
        "find_claude_binary",
        lambda: pytest.fail("invalid state attempted a Claude cloud call"),
    )

    generator._spawn(SimpleNamespace(session_id="session-invalid"))

    assert generator._in_flight == ""


def test_local_engine_uses_state_dir_and_refuses_unverified_binary_download(tmp_path, monkeypatch):
    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(local_engine.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="llama-cli not found"):
        local_engine.ensure_binaries()
