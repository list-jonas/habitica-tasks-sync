from __future__ import annotations

from pathlib import Path

from habitica_tasks_sync.db import StateStore, TaskMapping


def _mapping(**overrides) -> TaskMapping:
    base = dict(
        pair_name="alice",
        habitica_id="h1",
        google_id="g1",
        google_tasklist="tl1",
        habitica_hash="hh",
        google_hash="gh",
        habitica_updated="2026-05-16T10:00:00.000Z",
        google_updated="2026-05-16T10:00:00.000Z",
        last_synced_at="2026-05-16T10:00:01.000Z",
    )
    base.update(overrides)
    return TaskMapping(**base)


def test_upsert_and_lookup(tmp_path: Path):
    store = StateStore(tmp_path / "db.sqlite3")
    store.upsert_mapping(_mapping())
    found = store.get_by_habitica("alice", "h1")
    assert found is not None
    assert found.google_id == "g1"
    assert store.get_by_google("alice", "g1") == found


def test_upsert_updates_existing(tmp_path: Path):
    store = StateStore(tmp_path / "db.sqlite3")
    store.upsert_mapping(_mapping())
    store.upsert_mapping(_mapping(habitica_hash="changed"))
    found = store.get_by_habitica("alice", "h1")
    assert found is not None and found.habitica_hash == "changed"


def test_list_for_pair_isolation(tmp_path: Path):
    store = StateStore(tmp_path / "db.sqlite3")
    store.upsert_mapping(_mapping(pair_name="alice", habitica_id="h1", google_id="g1"))
    store.upsert_mapping(_mapping(pair_name="bob", habitica_id="h2", google_id="g2"))
    alice = store.list_for_pair("alice")
    bob = store.list_for_pair("bob")
    assert {m.habitica_id for m in alice} == {"h1"}
    assert {m.habitica_id for m in bob} == {"h2"}


def test_atomic_remove_with_tombstone(tmp_path: Path):
    store = StateStore(tmp_path / "db.sqlite3")
    store.upsert_mapping(_mapping())
    store.remove_mapping_with_tombstone(
        "alice", habitica_id="h1", google_id="g1",
        when_iso="2026-05-16T10:05:00.000Z",
    )
    assert store.get_by_habitica("alice", "h1") is None
    # Both sides get tombstoned so a return on either side is blocked.
    assert store.has_tombstone("alice", "google", "g1")
    assert store.has_tombstone("alice", "habitica", "h1")


def test_tombstone_prune(tmp_path: Path):
    store = StateStore(tmp_path / "db.sqlite3")
    store.add_tombstone("alice", "google", "g1", "2025-01-01T00:00:00.000Z")
    store.add_tombstone("alice", "google", "g2", "2026-05-16T00:00:00.000Z")
    pruned = store.prune_tombstones_older_than("2026-01-01T00:00:00.000Z")
    assert pruned == 1
    assert not store.has_tombstone("alice", "google", "g1")
    assert store.has_tombstone("alice", "google", "g2")


def test_sync_state_round_trip(tmp_path: Path):
    store = StateStore(tmp_path / "db.sqlite3")
    assert store.get_last_google_sync("alice") is None
    store.set_last_google_sync("alice", "2026-05-16T10:00:00.000Z")
    assert store.get_last_google_sync("alice") == "2026-05-16T10:00:00.000Z"


def test_invalid_tombstone_side(tmp_path: Path):
    import pytest
    store = StateStore(tmp_path / "db.sqlite3")
    with pytest.raises(ValueError):
        store.add_tombstone("alice", "bogus", "x", "2026-05-16T10:00:00.000Z")
