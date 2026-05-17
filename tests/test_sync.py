"""End-to-end sync engine tests using in-memory stub clients.

The stubs faithfully simulate the parts of Habitica/Google Tasks that the
engine touches: stable IDs, monotonic updated timestamps, completion
semantics, and deletion behavior. They deliberately do NOT exercise
network/HTTP — that's the clients' job — but they DO catch interactions
between the sync algorithm and the state store, which is where most
sync-engine bugs hide.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pytest

from habitica_tasks_sync.config import GoogleCreds, HabiticaCreds, SyncPair
from habitica_tasks_sync.db import StateStore
from habitica_tasks_sync.models import GoogleTask, HabiticaTask
from habitica_tasks_sync.sync import SyncEngine, _strip_checklist_artifact, _title_key


# --- stubs ---------------------------------------------------------------


class _Clock:
    """Monotonically advancing UTC clock for deterministic 'updated' values.

    Anchored far in the future so the sync engine's real-time `updatedMin`
    cursor never filters stub tasks out on the second cycle.
    """

    def __init__(self) -> None:
        self._t = datetime(2099, 1, 1, tzinfo=timezone.utc)

    def tick(self, seconds: float = 1.0) -> str:
        self._t += timedelta(seconds=seconds)
        return self._t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class StubHabitica:
    clock: _Clock = field(default_factory=_Clock)
    _store: dict[str, dict[str, Any]] = field(default_factory=dict)

    # API surface used by SyncEngine -------------------------------------

    def list_todos(self, *, include_completed: bool = True) -> list[HabiticaTask]:
        out: list[HabiticaTask] = []
        for t in self._store.values():
            if not include_completed and t["completed"]:
                continue
            out.append(HabiticaTask.from_api(t))
        return out

    def get_todo(self, task_id: str) -> HabiticaTask | None:
        raw = self._store.get(task_id)
        return HabiticaTask.from_api(raw) if raw else None

    def create_todo(
        self, *, text: str, notes: str = "", due_date_iso: str | None = None,
        checklist: Iterable[dict[str, Any]] = (), alias: str | None = None,
    ) -> HabiticaTask:
        new_id = f"h-{uuid.uuid4().hex[:8]}"
        now = self.clock.tick()
        raw = {
            "id": new_id, "_id": new_id, "type": "todo",
            "text": text or "(untitled)", "notes": notes or "",
            "completed": False, "dateCompleted": None,
            "date": f"{due_date_iso}T00:00:00.000Z" if due_date_iso else None,
            "checklist": list(checklist),
            "createdAt": now, "updatedAt": now,
        }
        self._store[new_id] = raw
        return HabiticaTask.from_api(raw)

    def update_todo(
        self, task_id: str, *, text: str | None = None, notes: str | None = None,
        due_date_iso: str | None = None, clear_due: bool = False,
    ) -> HabiticaTask:
        raw = self._store[task_id]
        if text is not None:
            raw["text"] = text
        if notes is not None:
            raw["notes"] = notes
        if clear_due:
            raw["date"] = None
        elif due_date_iso is not None:
            raw["date"] = f"{due_date_iso}T00:00:00.000Z"
        raw["updatedAt"] = self.clock.tick()
        return HabiticaTask.from_api(raw)

    def score_todo(self, task_id: str, *, complete: bool) -> None:
        raw = self._store[task_id]
        raw["completed"] = complete
        raw["dateCompleted"] = self.clock.tick() if complete else None
        raw["updatedAt"] = self.clock.tick()

    def delete_todo(self, task_id: str) -> bool:
        return self._store.pop(task_id, None) is not None


@dataclass
class StubGoogle:
    clock: _Clock = field(default_factory=_Clock)
    _store: dict[str, dict[str, Any]] = field(default_factory=dict)

    # API surface used by SyncEngine -------------------------------------

    def resolve_tasklist(self, *, tasklist_id: str | None, title: str | None) -> str:
        return tasklist_id or "default"

    def list_tasks(self, tasklist: str, *, updated_min=None, **_) -> list[GoogleTask]:
        out: list[GoogleTask] = []
        for raw in self._store.values():
            if updated_min and raw["updated"] < updated_min:
                continue
            out.append(GoogleTask.from_api(raw))
        return out

    def get_task(self, tasklist: str, task_id: str) -> GoogleTask | None:
        raw = self._store.get(task_id)
        return GoogleTask.from_api(raw) if raw else None

    def insert_task(
        self, tasklist: str, *, title: str, notes: str = "",
        due_date_iso: str | None = None, completed: bool = False,
    ) -> GoogleTask:
        new_id = f"g-{uuid.uuid4().hex[:8]}"
        now = self.clock.tick()
        raw = {
            "id": new_id, "etag": "etag",
            "title": title or "(untitled)", "notes": notes or "",
            "status": "completed" if completed else "needsAction",
            "due": f"{due_date_iso}T00:00:00.000Z" if due_date_iso else None,
            "completed": now if completed else None,
            "updated": now, "deleted": False, "hidden": False,
        }
        self._store[new_id] = raw
        return GoogleTask.from_api(raw)

    def patch_task(
        self, tasklist: str, task_id: str, *, title: str | None = None,
        notes: str | None = None, due_date_iso: str | None = None,
        clear_due: bool = False, completed: bool | None = None,
    ) -> GoogleTask:
        raw = self._store[task_id]
        if title is not None:
            raw["title"] = title
        if notes is not None:
            raw["notes"] = notes
        if clear_due:
            raw["due"] = None
        elif due_date_iso is not None:
            raw["due"] = f"{due_date_iso}T00:00:00.000Z"
        if completed is True:
            raw["status"] = "completed"
            raw["completed"] = self.clock.tick()
        elif completed is False:
            raw["status"] = "needsAction"
            raw["completed"] = None
        raw["updated"] = self.clock.tick()
        return GoogleTask.from_api(raw)

    def delete_task(self, tasklist: str, task_id: str) -> bool:
        raw = self._store.get(task_id)
        if raw is None:
            return False
        raw["deleted"] = True
        raw["updated"] = self.clock.tick()
        return True


# --- helpers -------------------------------------------------------------


def _pair() -> SyncPair:
    return SyncPair(
        name="alice",
        habitica=HabiticaCreds(
            user_id="11111111-2222-3333-4444-555555555555",
            api_token="99999999-2222-3333-4444-555555555555",
        ),
        google=GoogleCreds(
            credentials_file=Path("/tmp/c.json"),
            token_file=Path("/tmp/t.json"),
            tasklist_id="tl1",
            tasklist_title=None,
        ),
    )


def _engine(tmp_path: Path):
    clock = _Clock()
    h = StubHabitica(clock=clock)
    g = StubGoogle(clock=clock)
    store = StateStore(tmp_path / "db.sqlite3")
    eng = SyncEngine(_pair(), h, g, store)
    return eng, h, g, store, clock


# --- tests ---------------------------------------------------------------


def test_first_sync_creates_in_both_directions(tmp_path: Path):
    eng, h, g, store, clock = _engine(tmp_path)
    h.create_todo(text="From Habitica")
    g.insert_task("tl1", title="From Google")
    stats = eng.run_once()
    assert stats.created_in_google == 1
    assert stats.created_in_habitica == 1
    assert len(store.list_for_pair("alice")) == 2


def test_first_sync_adopts_matching_titles(tmp_path: Path):
    eng, h, g, store, _ = _engine(tmp_path)
    h.create_todo(text="Buy Milk")
    g.insert_task("tl1", title="buy milk  ")  # same after normalize
    stats = eng.run_once()
    assert stats.created_in_google == 0
    assert stats.created_in_habitica == 0
    # Single mapping, not two duplicates.
    assert len(store.list_for_pair("alice")) == 1


def test_completion_propagates_habitica_to_google(tmp_path: Path):
    eng, h, g, _, _ = _engine(tmp_path)
    ht = h.create_todo(text="Task")
    eng.run_once()  # creates google twin
    h.score_todo(ht.id, complete=True)
    eng.run_once()
    google_task = next(iter(g._store.values()))
    assert google_task["status"] == "completed"


def test_completion_propagates_google_to_habitica(tmp_path: Path):
    eng, h, g, _, _ = _engine(tmp_path)
    gt = g.insert_task("tl1", title="Task")
    eng.run_once()
    g.patch_task("tl1", gt.id, completed=True)
    eng.run_once()
    habitica_task = next(iter(h._store.values()))
    assert habitica_task["completed"] is True


def test_delete_propagates_both_ways(tmp_path: Path):
    eng, h, g, store, _ = _engine(tmp_path)
    ht = h.create_todo(text="A")
    gt = g.insert_task("tl1", title="B")
    eng.run_once()
    assert len(store.list_for_pair("alice")) == 2

    h.delete_todo(ht.id)
    g.delete_task("tl1", gt.id)
    eng.run_once()
    # Both mappings cleared, both sides empty.
    assert len(store.list_for_pair("alice")) == 0
    assert all(raw["deleted"] for raw in g._store.values()) or not g._store


def test_tombstone_blocks_recreate_after_habitica_delete(tmp_path: Path):
    eng, h, g, store, _ = _engine(tmp_path)
    ht = h.create_todo(text="One-shot")
    eng.run_once()
    google_id = next(iter(g._store))
    # User deletes on Habitica -> we delete on Google -> paired tombstones.
    h.delete_todo(ht.id)
    eng.run_once()
    assert store.has_tombstone("alice", "google", google_id)
    assert store.has_tombstone("alice", "habitica", ht.id)
    # Google still returns the deleted task in its list with deleted=true;
    # the create path should NOT resurrect it as a new Habitica task.
    eng.run_once()
    assert len(h._store) == 0


def test_tombstone_blocks_recreate_after_google_delete(tmp_path: Path):
    eng, h, g, store, _ = _engine(tmp_path)
    gt = g.insert_task("tl1", title="One-shot")
    eng.run_once()
    habitica_id = next(iter(h._store))
    # User deletes on Google -> we delete on Habitica -> paired tombstones.
    g.delete_task("tl1", gt.id)
    eng.run_once()
    assert store.has_tombstone("alice", "habitica", habitica_id)
    assert store.has_tombstone("alice", "google", gt.id)
    # Defensive scenario: Google flips `deleted` back to false (e.g. user
    # restores from trash). The google-side tombstone should still block
    # resurrection on Habitica.
    g._store[gt.id]["deleted"] = False
    eng.run_once()
    assert len(h._store) == 0


def test_conflict_resolution_last_writer_wins(tmp_path: Path):
    eng, h, g, _, clock = _engine(tmp_path)
    ht = h.create_todo(text="Original")
    eng.run_once()
    google_id = next(iter(g._store))

    # Habitica edit first, then Google edit (Google wins by timestamp).
    h.update_todo(ht.id, text="From Habitica")
    g.patch_task("tl1", google_id, title="From Google")
    stats = eng.run_once()
    assert stats.conflicts_resolved == 1
    assert h._store[ht.id]["text"] == "From Google"
    assert g._store[google_id]["title"] == "From Google"


def test_idempotent_when_no_changes(tmp_path: Path):
    eng, h, g, _, _ = _engine(tmp_path)
    h.create_todo(text="X")
    g.insert_task("tl1", title="Y")
    eng.run_once()
    stats = eng.run_once()
    # Second sync should be a no-op.
    assert stats.created_in_google == 0
    assert stats.created_in_habitica == 0
    assert stats.updated_in_google == 0
    assert stats.updated_in_habitica == 0
    assert stats.deleted_in_google == 0
    assert stats.deleted_in_habitica == 0


def test_checklist_flattens_to_google_notes(tmp_path: Path):
    eng, h, g, _, _ = _engine(tmp_path)
    h.create_todo(
        text="Trip",
        notes="Pack early",
        checklist=[{"id": "1", "text": "Tickets", "completed": True},
                   {"id": "2", "text": "Passport", "completed": False}],
    )
    eng.run_once()
    g_task = next(iter(g._store.values()))
    assert "— Checklist —" in g_task["notes"]
    assert "[x] Tickets" in g_task["notes"]
    assert "[ ] Passport" in g_task["notes"]


def test_notes_truncated_to_google_limit(tmp_path: Path):
    from habitica_tasks_sync.sync import GOOGLE_NOTES_MAX
    eng, h, g, _, _ = _engine(tmp_path)
    long_notes = "x" * (GOOGLE_NOTES_MAX + 5000)
    h.create_todo(text="big", notes=long_notes)
    eng.run_once()
    g_task = next(iter(g._store.values()))
    assert len(g_task["notes"]) <= GOOGLE_NOTES_MAX


def test_title_truncated_to_google_limit(tmp_path: Path):
    from habitica_tasks_sync.sync import GOOGLE_TITLE_MAX
    eng, h, g, _, _ = _engine(tmp_path)
    h.create_todo(text="x" * (GOOGLE_TITLE_MAX + 100))
    eng.run_once()
    g_task = next(iter(g._store.values()))
    assert len(g_task["title"]) <= GOOGLE_TITLE_MAX


def test_strip_checklist_artifact():
    raw = "Pack early\n\n— Checklist —\n[x] Tickets\n[ ] Passport"
    assert _strip_checklist_artifact(raw) == "Pack early"
    assert _strip_checklist_artifact("no artifact") == "no artifact"
    assert _strip_checklist_artifact("") == ""


def test_title_key_normalization():
    assert _title_key("Hello World") == _title_key("  hello   world  ")
    assert _title_key("A") != _title_key("B")
    # Empty strings don't collide with each other.
    assert _title_key("") != _title_key("")


def test_habitica_completed_beyond_30_cap_not_treated_as_delete(tmp_path: Path):
    """The 30-recent cap is enforced via include_completed=False on list_todos.

    We simulate the cap by hiding a completed task from list_todos, then
    rely on get_todo (by ID) still returning it. The engine should NOT
    delete the corresponding Google task.
    """
    eng, h, g, _, _ = _engine(tmp_path)
    ht = h.create_todo(text="Old completed")
    eng.run_once()  # establishes mapping
    h.score_todo(ht.id, complete=True)
    eng.run_once()  # propagates completion

    # Patch the stub to mimic Habitica's cap: completed task drops from list.
    real_list = h.list_todos
    def list_no_completed(*, include_completed=True):
        return [t for t in real_list(include_completed=True) if not t.completed]
    h.list_todos = list_no_completed  # type: ignore

    stats = eng.run_once()
    # No deletion attempted because get_todo still returns the task.
    assert stats.deleted_in_google == 0
    g_raw = next(iter(g._store.values()))
    assert g_raw.get("deleted", False) is False
