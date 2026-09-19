"""reveal_session filter-fallback: a background session must be surfaceable
even when filtered out — including a temporary (`_tmp_`) session, which lives
only under FILTER_TEMP. We bind the real method to a tiny fake (it's pure
filter logic over select_session/_set_filter), so no real list is built.
"""

from __future__ import annotations

import types

import pytest

pytest.importorskip("gi")

from helios.widgets.session_list import (  # noqa: E402
    FILTER_ALL,
    FILTER_TEMP,
    SessionList,
)


def _fake(current: str, ids: list[str], present_under: str):
    """A fake list where select_session(sid) succeeds only while the active
    filter equals `present_under`."""
    f = types.SimpleNamespace(_filter=current, _filter_ids=ids)
    f.select_session = lambda _sid: f._filter == present_under
    f._set_filter = lambda x: setattr(f, "_filter", x)
    f.reveal_session = types.MethodType(SessionList.reveal_session, f)
    return f


def test_reveal_falls_back_to_temp():
    f = _fake(FILTER_ALL, [FILTER_ALL, FILTER_TEMP], present_under=FILTER_TEMP)
    assert f.reveal_session("s") is True
    assert f._filter == FILTER_TEMP  # ALL misses temp; TEMP finds it


def test_reveal_finds_in_all():
    f = _fake("cwd:/x", [FILTER_ALL, FILTER_TEMP, "cwd:/x"], present_under=FILTER_ALL)
    assert f.reveal_session("s") is True
    assert f._filter == FILTER_ALL


def test_reveal_restores_filter_when_not_found():
    f = _fake("cwd:/x", [FILTER_ALL, FILTER_TEMP, "cwd:/x"], present_under="never")
    assert f.reveal_session("s") is False
    assert f._filter == "cwd:/x"  # not stranded on ALL/TEMP after a failed reveal


def test_set_filter_preserves_selection():
    # Probing filters must NOT auto-select row 0 (preserve_selection=True),
    # else it fires a spurious session-selected and could present the wrong
    # session's queued question.
    renders: list[bool] = []
    f = types.SimpleNamespace(
        _filter="all",
        _filter_ids=[FILTER_ALL, FILTER_TEMP],
        _filter_updating=False,
        _filter_dd=types.SimpleNamespace(set_selected=lambda _i: None),
        _ui_state=types.SimpleNamespace(set=lambda _k, _v: None),
    )
    f._render = lambda *, preserve_selection: renders.append(preserve_selection)
    f._set_filter = types.MethodType(SessionList._set_filter, f)
    f._set_filter(FILTER_TEMP)
    assert renders == [True]
