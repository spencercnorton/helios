"""Session deletion keeps provider identity until synchronous observers run."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from helios.backend import model_catalog, session_providers
from helios.widgets.session_list import SessionList


def test_delete_observer_sees_provider_before_index_is_forgotten(tmp_path):
    transcript = tmp_path / "thread-openai.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    session_providers.set_provider(
        "thread-openai",
        model_catalog.PROVIDER_OPENAI,
    )
    observed: list[str] = []
    owner = SimpleNamespace(
        emit=lambda _signal, ids: observed.extend(
            session_providers.provider_for(session_id) for session_id in ids
        ),
        _set_select_mode=lambda _active: None,
        reload=lambda **_kwargs: None,
    )
    session = SimpleNamespace(
        path=transcript,
        session_id="thread-openai",
        display_title="GPT chat",
        project=object(),
    )

    SessionList._on_delete_response(owner, None, "delete", [session])

    assert observed == [model_catalog.PROVIDER_OPENAI]
    assert session_providers.provider_for("thread-openai") == ""
    assert not transcript.exists()
