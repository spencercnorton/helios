"""Tests for the TitleStore singleton + persistence.

`helios.backend.title_store` is pure stdlib — no gi — so these run in the slim
CI lane. They are also the regression proof that splitting the store out of
`backend/process/title_generator.py` changed no behaviour.
"""

from __future__ import annotations

import json
import threading

import pytest

from helios.backend import title_store as ts


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Reset the module singleton and point the cache at tmp_path."""
    monkeypatch.setattr(ts, "_store_singleton", None)
    monkeypatch.setattr(ts, "_CACHE_PATH", tmp_path / "title-cache.json")
    return tmp_path


def test_store_returns_singleton(fresh_store):
    a = ts.store()
    b = ts.store()
    assert a is b


def test_store_is_threadsafe_singleton(fresh_store):
    """Concurrent first-touch from many threads must yield exactly one
    instance (double-checked locking); also proves store() doesn't deadlock
    on the cache lock."""
    instances = []
    barrier = threading.Barrier(16)

    def grab() -> None:
        barrier.wait()
        instances.append(ts.store())

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(instances) == 16
    assert len({id(x) for x in instances}) == 1


def test_set_get_roundtrip_and_persist(fresh_store):
    s = ts.store()
    s.set("sess-1", "My Title")
    assert s.get("sess-1") == "My Title"
    # Persisted to disk...
    cache = fresh_store / "title-cache.json"
    assert json.loads(cache.read_text())["sess-1"] == "My Title"


def test_new_instance_reads_persisted_cache(fresh_store, monkeypatch):
    ts.store().set("sess-2", "Persisted")
    # Drop the singleton; a fresh one must load what was written.
    monkeypatch.setattr(ts, "_store_singleton", None)
    assert ts.store().get("sess-2") == "Persisted"


def test_blank_title_is_ignored(fresh_store):
    s = ts.store()
    s.set("sess-3", "   ")
    assert s.get("sess-3") is None


def test_set_if_absent_only_writes_when_empty(fresh_store):
    """Generation uses set_if_absent so it can't clobber a manual rename that
    landed while it was in flight; rename uses the forcing set."""
    s = ts.store()
    # Empty slot: writes and reports True.
    assert s.set_if_absent("sess-4", "Generated") is True
    assert s.get("sess-4") == "Generated"
    # Occupied slot (e.g. a manual rename): refuses and reports False.
    s.set("sess-4", "Manual Rename")
    assert s.set_if_absent("sess-4", "Late Generated") is False
    assert s.get("sess-4") == "Manual Rename"
    # Blank is always a no-op.
    assert s.set_if_absent("sess-5", "  ") is False
    assert s.get("sess-5") is None
