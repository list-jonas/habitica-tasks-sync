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

from habitica_tasks_sync.config import GoogleCreds, HabiticaCreds, SyncPair, TasklistConfig
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
    _tags: dict[str, dict[str, Any]] = field(default_factory=dict)  # tag_id -> {id,name}

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
        tags: Iterable[str] = (),
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
            "tags": [t for t in tags if t],
        }
        self._store[new_id] = raw
        return HabiticaTask.from_api(raw)

    def create_todos(self, items: list[dict[str, Any]]) -> list[HabiticaTask]:
        return [
            self.create_todo(
                text=it.get("text", ""),
                notes=it.get("notes", ""),
                due_date_iso=it.get("due_date_iso"),
                checklist=it.get("checklist") or (),
                tags=it.get("tags") or (),
            )
            for it in items
        ]

    # Tag CRUD --------------------------------------------------------

    def list_tags(self) -> list[dict[str, Any]]:
        return list(self._tags.values())

    def create_tag(self, name: str) -> dict[str, Any]:
        new_id = f"tag-{uuid.uuid4().hex[:8]}"
        obj = {"id": new_id, "name": name}
        self._tags[new_id] = obj
        return obj

    def add_tag_to_task(self, task_id: str, tag_id: str) -> None:
        raw = self._store.get(task_id)
        if raw is None:
            return
        tags = raw.setdefault("tags", [])
        if tag_id not in tags:
            tags.append(tag_id)
        raw["updatedAt"] = self.clock.tick()

    def remove_tag_from_task(self, task_id: str, tag_id: str) -> None:
        raw = self._store.get(task_id)
        if raw is None:
            return
        tags = raw.get("tags", [])
        if tag_id in tags:
            tags.remove(tag_id)
            raw["updatedAt"] = self.clock.tick()

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
    # tasklist_id -> { task_id -> raw }
    _store: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    # tasklist_title -> tasklist_id (for resolve_tasklist)
    _titles: dict[str, str] = field(default_factory=dict)

    def _bucket(self, tasklist: str) -> dict[str, dict[str, Any]]:
        return self._store.setdefault(tasklist, {})

    @property
    def _all(self) -> dict[str, dict[str, Any]]:
        """Flat (task_id -> raw) view across every tasklist for test convenience."""

        flat: dict[str, dict[str, Any]] = {}
        for bucket in self._store.values():
            flat.update(bucket)
        return flat

    def _find(self, task_id: str) -> tuple[str, dict[str, Any]] | None:
        for tlid, bucket in self._store.items():
            raw = bucket.get(task_id)
            if raw is not None:
                return tlid, raw
        return None

    # API surface used by SyncEngine -------------------------------------

    def resolve_tasklist(self, *, tasklist_id: str | None, title: str | None) -> str:
        if tasklist_id:
            self._bucket(tasklist_id)
            return tasklist_id
        if title:
            existing = self._titles.get(title)
            if existing:
                return existing
            new_id = f"tl-{uuid.uuid4().hex[:6]}"
            self._titles[title] = new_id
            self._bucket(new_id)
            return new_id
        self._bucket("@default")
        return "@default"

    def list_tasks(self, tasklist: str, *, updated_min=None, **_) -> list[GoogleTask]:
        out: list[GoogleTask] = []
        for raw in self._bucket(tasklist).values():
            if updated_min and raw["updated"] < updated_min:
                continue
            out.append(GoogleTask.from_api(raw, tasklist_id=tasklist))
        return out

    def get_task(self, tasklist: str, task_id: str) -> GoogleTask | None:
        raw = self._bucket(tasklist).get(task_id)
        return GoogleTask.from_api(raw, tasklist_id=tasklist) if raw else None

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
        self._bucket(tasklist)[new_id] = raw
        return GoogleTask.from_api(raw, tasklist_id=tasklist)

    def patch_task(
        self, tasklist: str, task_id: str, *, title: str | None = None,
        notes: str | None = None, due_date_iso: str | None = None,
        clear_due: bool = False, completed: bool | None = None,
    ) -> GoogleTask:
        raw = self._bucket(tasklist)[task_id]
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
        return GoogleTask.from_api(raw, tasklist_id=tasklist)

    def delete_task(self, tasklist: str, task_id: str) -> bool:
        raw = self._bucket(tasklist).get(task_id)
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
            tasklists=(TasklistConfig(tasklist_id="tl1", tasklist_title=None, tag=None),),
        ),
    )


def _engine(tmp_path: Path):
    clock = _Clock()
    h = StubHabitica(clock=clock)
    g = StubGoogle(clock=clock)
    store = StateStore(tmp_path / "db.sqlite3")
    eng = SyncEngine(_pair(), h, g, store)
    return eng, h, g, store, clock


def _multi_pair(tasklists: tuple[TasklistConfig, ...]) -> SyncPair:
    return SyncPair(
        name="alice",
        habitica=HabiticaCreds(
            user_id="11111111-2222-3333-4444-555555555555",
            api_token="99999999-2222-3333-4444-555555555555",
        ),
        google=GoogleCreds(
            credentials_file=Path("/tmp/c.json"),
            token_file=Path("/tmp/t.json"),
            tasklists=tasklists,
        ),
    )


def _multi_engine(tmp_path: Path, tasklists: tuple[TasklistConfig, ...]):
    clock = _Clock()
    h = StubHabitica(clock=clock)
    g = StubGoogle(clock=clock)
    store = StateStore(tmp_path / "db.sqlite3")
    eng = SyncEngine(_multi_pair(tasklists), h, g, store)
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
    google_task = next(iter(g._all.values()))
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
    assert all(raw["deleted"] for raw in g._all.values()) or not g._all


def test_tombstone_blocks_recreate_after_habitica_delete(tmp_path: Path):
    eng, h, g, store, _ = _engine(tmp_path)
    ht = h.create_todo(text="One-shot")
    eng.run_once()
    google_id = next(iter(g._all))
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
    g._bucket("tl1")[gt.id]["deleted"] = False
    eng.run_once()
    assert len(h._store) == 0


def test_conflict_resolution_last_writer_wins(tmp_path: Path):
    eng, h, g, _, clock = _engine(tmp_path)
    ht = h.create_todo(text="Original")
    eng.run_once()
    google_id = next(iter(g._all))

    # Habitica edit first, then Google edit (Google wins by timestamp).
    h.update_todo(ht.id, text="From Habitica")
    g.patch_task("tl1", google_id, title="From Google")
    stats = eng.run_once()
    assert stats.conflicts_resolved == 1
    assert h._store[ht.id]["text"] == "From Google"
    assert g._all[google_id]["title"] == "From Google"


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
    g_task = next(iter(g._all.values()))
    assert "— Checklist —" in g_task["notes"]
    assert "[x] Tickets" in g_task["notes"]
    assert "[ ] Passport" in g_task["notes"]


def test_notes_truncated_to_google_limit(tmp_path: Path):
    from habitica_tasks_sync.sync import GOOGLE_NOTES_MAX
    eng, h, g, _, _ = _engine(tmp_path)
    long_notes = "x" * (GOOGLE_NOTES_MAX + 5000)
    h.create_todo(text="big", notes=long_notes)
    eng.run_once()
    g_task = next(iter(g._all.values()))
    assert len(g_task["notes"]) <= GOOGLE_NOTES_MAX


def test_title_truncated_to_google_limit(tmp_path: Path):
    from habitica_tasks_sync.sync import GOOGLE_TITLE_MAX
    eng, h, g, _, _ = _engine(tmp_path)
    h.create_todo(text="x" * (GOOGLE_TITLE_MAX + 100))
    eng.run_once()
    g_task = next(iter(g._all.values()))
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
    g_raw = next(iter(g._all.values()))
    assert g_raw.get("deleted", False) is False


# --- multi-list / tag routing -------------------------------------------


_PERSONAL = TasklistConfig(tasklist_id="tl-personal", tasklist_title=None, tag="personal")
_WORK = TasklistConfig(tasklist_id="tl-work", tasklist_title=None, tag="work")


def _tag_id(h: StubHabitica, name: str) -> str:
    for t in h._tags.values():
        if t["name"] == name:
            return t["id"]
    raise KeyError(name)


def test_multi_list_creates_tags_on_first_sync(tmp_path: Path):
    eng, h, g, _, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    eng.run_once()
    names = {t["name"] for t in h._tags.values()}
    assert names == {"personal", "work"}


def test_google_task_creates_habitica_with_source_tag(tmp_path: Path):
    eng, h, g, store, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    g.insert_task("tl-work", title="Quarterly review")
    eng.run_once()
    habitica_task = next(iter(h._store.values()))
    work_tag = _tag_id(h, "work")
    assert work_tag in habitica_task["tags"]
    mapping = store.list_for_pair("alice")[0]
    assert mapping.google_tasklist == "tl-work"


def test_habitica_task_routed_by_existing_tag(tmp_path: Path):
    eng, h, g, store, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    # Seed the work tag first so the Habitica task can carry it on creation.
    eng._resolve_routing()
    work_tag = _tag_id(h, "work")
    h.create_todo(text="Pay quarterly tax", tags=[work_tag])
    eng.run_once()
    work_bucket = g._bucket("tl-work")
    personal_bucket = g._bucket("tl-personal")
    assert len(work_bucket) == 1
    assert len(personal_bucket) == 0
    mapping = store.list_for_pair("alice")[0]
    assert mapping.google_tasklist == "tl-work"


def test_habitica_task_without_tag_routes_to_default_and_gets_tagged(tmp_path: Path):
    eng, h, g, _, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    ht = h.create_todo(text="Buy milk")
    eng.run_once()
    # Default list = first configured (personal). Task should appear there
    # and the personal tag should be back-attached to the Habitica task.
    assert len(g._bucket("tl-personal")) == 1
    assert len(g._bucket("tl-work")) == 0
    personal_tag = _tag_id(h, "personal")
    assert personal_tag in h._store[ht.id]["tags"]


def test_move_carries_content_changes(tmp_path: Path):
    """Title and tag changing in the same cycle should land both in the
    target list with the new title and not leave a stale copy behind."""
    eng, h, g, store, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    ht = h.create_todo(text="Old title")
    eng.run_once()
    work_tag = _tag_id(h, "work")

    raw = h._store[ht.id]
    raw["text"] = "New title"
    raw["tags"] = [work_tag]
    raw["updatedAt"] = h.clock.tick()

    stats = eng.run_once()
    assert stats.moved_in_google == 1
    work_live = [r for r in g._bucket("tl-work").values() if not r.get("deleted")]
    assert len(work_live) == 1
    assert work_live[0]["title"] == "New title"
    # The mapping should now reference the new google task in tl-work.
    mapping = store.list_for_pair("alice")[0]
    assert mapping.google_tasklist == "tl-work"
    assert mapping.google_id == work_live[0]["id"]
    # Next cycle should be a no-op.
    stats = eng.run_once()
    assert stats.moved_in_google == 0
    assert stats.updated_in_google == 0
    assert stats.updated_in_habitica == 0


def test_move_is_idempotent_next_cycle(tmp_path: Path):
    """A move should leave the mapping consistent with what the next
    cycle reads back — no phantom updatedAt drift causing a second
    sync to redo work."""
    eng, h, g, _, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    ht = h.create_todo(text="Plan")
    eng.run_once()  # in personal
    work_tag = _tag_id(h, "work")

    raw = h._store[ht.id]
    raw["tags"] = [work_tag]
    raw["updatedAt"] = h.clock.tick()

    eng.run_once()  # triggers the move
    stats = eng.run_once()  # should be a no-op
    assert stats.moved_in_google == 0
    assert stats.updated_in_google == 0
    assert stats.updated_in_habitica == 0
    assert stats.created_in_google == 0
    assert stats.created_in_habitica == 0


def test_move_drops_old_list_tag(tmp_path: Path):
    """A move triggered by adding a new tag should also strip the old
    list's tag, otherwise routing is non-deterministic next time tags
    are reordered."""
    eng, h, g, store, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    ht = h.create_todo(text="Project plan")
    eng.run_once()  # routed to personal; personal tag now attached
    personal_tag = _tag_id(h, "personal")
    work_tag = _tag_id(h, "work")
    assert personal_tag in h._store[ht.id]["tags"]

    # User adds the work tag WITHOUT removing personal.
    raw = h._store[ht.id]
    raw["tags"] = [work_tag, personal_tag]
    raw["updatedAt"] = h.clock.tick()

    stats = eng.run_once()
    assert stats.moved_in_google == 1
    # Old (personal) tag stripped after the move.
    assert personal_tag not in h._store[ht.id]["tags"]
    assert work_tag in h._store[ht.id]["tags"]


def test_move_between_lists_when_tag_changes(tmp_path: Path):
    eng, h, g, store, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    ht = h.create_todo(text="Project plan")
    eng.run_once()  # lands in personal (default)
    assert len(g._bucket("tl-personal")) == 1
    personal_tag = _tag_id(h, "personal")
    work_tag = _tag_id(h, "work")

    # User retags the task in Habitica: drop personal, add work.
    raw = h._store[ht.id]
    raw["tags"] = [work_tag]
    raw["updatedAt"] = h.clock.tick()

    stats = eng.run_once()
    assert stats.moved_in_google == 1
    # Old list now has a deleted-marker entry; new list has a live task.
    personal_live = [r for r in g._bucket("tl-personal").values() if not r.get("deleted")]
    assert personal_live == []
    work_live = [r for r in g._bucket("tl-work").values() if not r.get("deleted")]
    assert len(work_live) == 1
    mapping = store.list_for_pair("alice")[0]
    assert mapping.google_tasklist == "tl-work"


def test_multi_list_adoption_only_within_same_list(tmp_path: Path):
    eng, h, g, store, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    # Make sure tags exist + cache them.
    eng._resolve_routing()
    work_tag = _tag_id(h, "work")
    # Habitica side: task tagged work.
    h.create_todo(text="Refactor", tags=[work_tag])
    # Google side: title-matching task BUT in personal list.
    g.insert_task("tl-personal", title="refactor")
    eng.run_once()
    # Should NOT have adopted; instead two separate mappings exist.
    mappings = store.list_for_pair("alice")
    assert len(mappings) == 2
    # Habitica gained a personal-task pulled from Google.
    assert any(_tag_id(h, "personal") in t["tags"] for t in h._store.values())


def test_multi_list_idempotent_second_cycle(tmp_path: Path):
    eng, h, g, _, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    g.insert_task("tl-work", title="Item A")
    h.create_todo(text="Item B")  # untagged → default (personal)
    eng.run_once()
    stats = eng.run_once()
    assert stats.created_in_google == 0
    assert stats.created_in_habitica == 0
    assert stats.updated_in_google == 0
    assert stats.updated_in_habitica == 0
    assert stats.moved_in_google == 0


def test_multi_list_delete_propagates_with_correct_tasklist(tmp_path: Path):
    eng, h, g, store, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    gt = g.insert_task("tl-work", title="Doomed")
    eng.run_once()
    habitica_id = next(iter(h._store))
    h.delete_todo(habitica_id)
    eng.run_once()
    # The Google task should be marked deleted in the WORK list (its source).
    assert g._bucket("tl-work")[gt.id]["deleted"] is True
    assert store.has_tombstone("alice", "google", gt.id)


def test_newly_added_tasklist_does_full_initial_pull(tmp_path: Path):
    """Adding a list mid-life must not silently skip its existing tasks.

    The per-pair cursor used to mean a newly-configured list was filtered
    out from its very first cycle. Per-tasklist cursors fix that: a list
    we've never synced has no entry in sync_state_tasklist, so the engine
    treats it as a full pull.
    """

    eng, h, g, store, clock = _multi_engine(tmp_path, (_PERSONAL,))
    # Seed Personal and run a cycle so the pair has a non-trivial cursor.
    g.insert_task("tl-personal", title="Existing personal")
    eng.run_once()
    assert len(h._store) == 1

    # Add an OLD task to a list the engine has never seen before, with a
    # backdated `updated` so the legacy per-pair cursor would skip it.
    work_bucket = g._bucket("tl-work")
    work_bucket["g-old"] = {
        "id": "g-old", "etag": "e", "title": "Old work task", "notes": "",
        "status": "needsAction", "due": None, "completed": None,
        "updated": "2000-01-01T00:00:00.000Z", "deleted": False, "hidden": False,
    }
    # Reconstruct engine to mimic a config change + restart with WORK added.
    eng = SyncEngine(_multi_pair((_PERSONAL, _WORK)), h, g, store)
    eng.run_once()
    # We should have pulled "Old work task" into Habitica.
    titles = {t["text"] for t in h._store.values()}
    assert "Old work task" in titles


def test_initial_sync_uses_bulk_create_for_habitica(tmp_path: Path):
    """A large G→H first sync should issue exactly one bulk create
    instead of one create per task — otherwise the Habitica rate
    limiter would dominate the cycle."""
    eng, h, g, _, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    for i in range(30):
        g.insert_task("tl-work", title=f"Task {i}")

    # Verify the engine batches the creates into bulk calls. The stub's
    # create_todos delegates to create_todo internally; spy only on the
    # bulk entrypoint so we count what the ENGINE invoked, not what the
    # stub did downstream.
    bulk_calls: list[int] = []
    real_bulk = h.create_todos
    def spy_bulk(items):
        bulk_calls.append(len(items))
        return real_bulk(items)
    h.create_todos = spy_bulk  # type: ignore[assignment]

    eng.run_once()
    # One bulk call covering all 30 tasks (well under the 100-per-chunk cap).
    assert bulk_calls == [30]
    assert len(h._store) == 30


def test_legacy_pair_cursor_bootstraps_known_tasklist(tmp_path: Path):
    """Upgrading from the pair-level cursor world: a list that already
    has mappings should reuse the legacy pair cursor rather than doing
    a wasteful full pull."""

    eng, h, g, store, _ = _multi_engine(tmp_path, (_PERSONAL,))
    g.insert_task("tl-personal", title="Existing")
    eng.run_once()
    # Simulate the upgrade: nuke per-tasklist cursors, keep the pair-level one.
    store._conn.execute("DELETE FROM sync_state_tasklist")
    pair_cursor = store.get_last_google_sync("alice")
    assert pair_cursor is not None

    # Now run another cycle — the bootstrap fallback should kick in and
    # use the pair cursor for tl-personal (it has mappings).
    stats = eng.run_once()
    # No work needed since nothing changed.
    assert stats.created_in_google == 0
    assert stats.created_in_habitica == 0


def test_multi_list_reuses_existing_habitica_tag(tmp_path: Path):
    """If the user already has a tag of the right name, we shouldn't create a duplicate."""
    eng, h, g, _, _ = _multi_engine(tmp_path, (_PERSONAL, _WORK))
    # Pre-create the tag (different id casing variants).
    existing = h.create_tag("Personal")  # different case
    eng.run_once()
    names = sorted(t["name"] for t in h._tags.values())
    # Case-insensitive match: should not create a second "personal" tag.
    assert names.count("Personal") + names.count("personal") == 1
    assert "work" in names
