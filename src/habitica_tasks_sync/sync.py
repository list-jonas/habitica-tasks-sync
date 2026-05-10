"""Bidirectional sync engine for one Habitica<->Google pair.

Sync algorithm (per pair, per cycle):

1. Fetch all Habitica todos (active + last 30 completed) — Habitica has no
   incremental endpoint, so a full pull is unavoidable.

2. Fetch Google tasks since `last_google_sync - overlap` with
   showDeleted/showHidden/showCompleted=true. Google supports a real
   `updatedMin` cursor.

3. Detect deletions:
   - Habitica: any mapping whose habitica_id is no longer present in the
     active+completed lists (and is older than the most recent run) is
     treated as deleted on Habitica's side. Deletes are propagated to
     Google.
   - Google: any task returned with `deleted=true` is propagated to
     Habitica as a delete.

4. For each surviving Habitica task with a mapping, compare its canonical
   content hash and updatedAt against the stored values to decide if a push
   to Google is needed.

5. For each surviving Google task with a mapping, compare similarly to
   decide if a push to Habitica is needed.

6. Conflict (both sides changed since last sync): the side with the later
   `updatedAt` wins. The other side is overwritten.

7. Unmapped tasks on either side become creations on the other side; the
   resulting mapping is persisted.

Tombstones prevent a deleted task on side A — still visible on side B
because side B hasn't pulled yet — from being recreated on side A on the
next cycle.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import SyncPair
from .db import StateStore, TaskMapping
from .google_tasks import GoogleTasksClient, overlap_window
from .habitica import HabiticaClient, HabiticaError
from .models import (
    CanonicalTask,
    GoogleTask,
    HabiticaTask,
    Source,
)

log = logging.getLogger(__name__)


@dataclass
class SyncStats:
    pair: str
    created_in_google: int = 0
    updated_in_google: int = 0
    deleted_in_google: int = 0
    created_in_habitica: int = 0
    updated_in_habitica: int = 0
    deleted_in_habitica: int = 0
    conflicts_resolved: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"[{self.pair}] "
            f"google: +{self.created_in_google}/~{self.updated_in_google}/-{self.deleted_in_google} | "
            f"habitica: +{self.created_in_habitica}/~{self.updated_in_habitica}/-{self.deleted_in_habitica} | "
            f"conflicts: {self.conflicts_resolved} | errors: {self.errors}"
        )


class SyncEngine:
    def __init__(
        self,
        pair: SyncPair,
        habitica: HabiticaClient,
        google: GoogleTasksClient,
        store: StateStore,
        *,
        delete_propagation: bool = True,
        tombstone_ttl_days: int = 30,
    ) -> None:
        self.pair = pair
        self.h = habitica
        self.g = google
        self.store = store
        self.delete_propagation = delete_propagation
        self.tombstone_ttl_days = tombstone_ttl_days
        self._tasklist_id: str | None = None

    # --- public ---------------------------------------------------------

    def run_once(self) -> SyncStats:
        stats = SyncStats(pair=self.pair.name)
        tasklist_id = self._resolve_tasklist()
        log.info("[%s] sync start (tasklist=%s)", self.pair.name, tasklist_id)

        habitica_tasks = self._fetch_habitica()
        # If the mapping table is empty (first sync, or DB lost) we need a
        # FULL Google fetch — otherwise the title-adoption pool only sees
        # tasks updated within the cursor window and will create
        # duplicates for older Google tasks.
        existing_mappings = {m.habitica_id: m for m in self.store.list_for_pair(self.pair.name)}
        force_full_google = not existing_mappings
        google_tasks, new_cursor = self._fetch_google(tasklist_id, force_full=force_full_google)

        h_by_id = {t.id: t for t in habitica_tasks}
        g_by_id = {t.id: t for t in google_tasks}

        # 1) Propagate Habitica deletions → Google.
        if self.delete_propagation:
            self._propagate_habitica_deletions(existing_mappings, h_by_id, tasklist_id, stats)

        # 2) Propagate Google deletions → Habitica.
        if self.delete_propagation:
            self._propagate_google_deletions(existing_mappings, g_by_id, stats)

        # Refresh mappings after deletions.
        existing_mappings = {m.habitica_id: m for m in self.store.list_for_pair(self.pair.name)}

        # 3) Sync content changes for already-mapped tasks (handles conflicts).
        self._sync_existing_mappings(
            existing_mappings, h_by_id, g_by_id, tasklist_id, stats
        )

        # 4) Create missing counterparts in both directions. Pass the
        # opposite-side index so unmapped tasks with matching titles can be
        # adopted instead of duplicated (relevant on first sync).
        self._create_missing_in_google(
            h_by_id, existing_mappings, tasklist_id, stats, g_by_id=g_by_id,
        )

        # Re-read mappings so the next direction sees what we just wrote.
        existing_mappings = {m.habitica_id: m for m in self.store.list_for_pair(self.pair.name)}
        self._create_missing_in_habitica(
            g_by_id, existing_mappings, tasklist_id, stats, h_by_id=h_by_id,
        )

        self.store.set_last_google_sync(self.pair.name, new_cursor)

        # Use the same RFC3339 format as `_now_iso()` so lexicographic
        # comparison of timestamps in SQLite is correct (mixing `+00:00`
        # and `.000Z` suffixes produces wrong-order results).
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.tombstone_ttl_days)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        pruned = self.store.prune_tombstones_older_than(cutoff)
        if pruned:
            log.debug("[%s] pruned %d expired tombstones", self.pair.name, pruned)

        log.info(stats.summary())
        return stats

    # --- fetch ----------------------------------------------------------

    def _resolve_tasklist(self) -> str:
        if self._tasklist_id is None:
            self._tasklist_id = self.g.resolve_tasklist(
                tasklist_id=self.pair.google.tasklist_id,
                title=self.pair.google.tasklist_title,
            )
        return self._tasklist_id

    def _fetch_habitica(self) -> list[HabiticaTask]:
        # Skip challenge/group tasks: they're owned by the challenge/group,
        # not the user, so PUT/DELETE will return 401 with
        # `challengeTasksNoUserDelete` etc. Treating them as out-of-scope is
        # simpler than special-casing each mutation.
        return [
            t for t in self.h.list_todos(include_completed=True)
            if t.type == "todo" and not t.is_managed_externally
        ]

    def _fetch_google(self, tasklist_id: str, *, force_full: bool = False) -> tuple[list[GoogleTask], str]:
        last = None if force_full else self.store.get_last_google_sync(self.pair.name)
        # Always overlap the cursor by a few minutes to absorb clock skew between
        # Google's servers and ours. Duplicates are deduped by ID downstream.
        cursor_iso: str | None = None
        if last:
            try:
                anchor = datetime.fromisoformat(last.replace("Z", "+00:00"))
            except ValueError:
                anchor = datetime.now(timezone.utc) - timedelta(days=7)
            cursor = (anchor - timedelta(minutes=5)).astimezone(timezone.utc)
            cursor_iso = cursor.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        tasks = self.g.list_tasks(
            tasklist_id,
            updated_min=cursor_iso,
            show_completed=True,
            show_deleted=True,
            show_hidden=True,
        )
        new_cursor = overlap_window(datetime.now(timezone.utc), minutes=0)
        return tasks, new_cursor

    # --- deletions ------------------------------------------------------

    def _propagate_habitica_deletions(
        self,
        mappings: dict[str, TaskMapping],
        h_by_id: dict[str, HabiticaTask],
        tasklist_id: str,
        stats: SyncStats,
    ) -> None:
        for habitica_id, mapping in list(mappings.items()):
            if habitica_id in h_by_id:
                continue
            # Habitica's `completedTodos` list is capped at the 30 most
            # recent entries. A mapped task that's missing from the list
            # could be (a) genuinely deleted, or (b) just an older
            # completed todo. Verify by direct GET before propagating a
            # delete — if the task still exists, treat it as visible and
            # let the regular sync path handle any state changes.
            actual = self.h.get_todo(habitica_id)
            if actual is not None and actual.type == "todo" and not actual.is_managed_externally:
                h_by_id[habitica_id] = actual
                continue
            try:
                self.g.delete_task(mapping.google_tasklist or tasklist_id, mapping.google_id)
                # Mapping removal + tombstone in one transaction so a
                # crash here can't leave us with a deleted-on-Google task
                # that gets recreated next cycle (no tombstone) or a
                # stale mapping that re-attempts the delete forever.
                self.store.remove_mapping_with_tombstone(
                    self.pair.name,
                    habitica_id,
                    tombstone_side="google",
                    tombstone_id=mapping.google_id,
                    when_iso=_now_iso(),
                )
                stats.deleted_in_google += 1
                log.info("[%s] deleted google task %s (was habitica %s)",
                         self.pair.name, mapping.google_id, habitica_id)
            except Exception as exc:  # noqa: BLE001 - log and continue
                stats.errors += 1
                log.exception("[%s] failed to delete google task %s: %s",
                              self.pair.name, mapping.google_id, exc)

    def _propagate_google_deletions(
        self,
        mappings: dict[str, TaskMapping],
        g_by_id: dict[str, GoogleTask],
        stats: SyncStats,
    ) -> None:
        google_to_habitica = {m.google_id: m.habitica_id for m in mappings.values()}
        for google_id, habitica_id in list(google_to_habitica.items()):
            gtask = g_by_id.get(google_id)
            if gtask is None or not gtask.deleted:
                continue
            try:
                deleted = self.h.delete_todo(habitica_id)
                if deleted:
                    stats.deleted_in_habitica += 1
                self.store.remove_mapping_with_tombstone(
                    self.pair.name,
                    habitica_id,
                    tombstone_side="habitica",
                    tombstone_id=habitica_id,
                    when_iso=_now_iso(),
                )
                log.info("[%s] deleted habitica task %s (was google %s)",
                         self.pair.name, habitica_id, google_id)
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                log.exception("[%s] failed to delete habitica task %s: %s",
                              self.pair.name, habitica_id, exc)

    # --- content sync ---------------------------------------------------

    def _sync_existing_mappings(
        self,
        mappings: dict[str, TaskMapping],
        h_by_id: dict[str, HabiticaTask],
        g_by_id: dict[str, GoogleTask],
        tasklist_id: str,
        stats: SyncStats,
    ) -> None:
        for habitica_id, mapping in mappings.items():
            h = h_by_id.get(habitica_id)
            g = g_by_id.get(mapping.google_id)
            if h is None or g is None:
                # Either side missing — handled by deletion / creation paths.
                continue
            if g.deleted:
                continue

            h_can = h.to_canonical()
            g_can = g.to_canonical()
            h_changed = (h.updated_at != mapping.habitica_updated) or (h_can.content_hash() != mapping.habitica_hash)
            g_changed = (g.updated != mapping.google_updated) or (g_can.content_hash() != mapping.google_hash)

            if not h_changed and not g_changed:
                continue

            try:
                if h_changed and not g_changed:
                    self._push_habitica_to_google(h, g, mapping, tasklist_id, stats)
                elif g_changed and not h_changed:
                    self._push_google_to_habitica(g, h, mapping, tasklist_id, stats)
                else:
                    # Both changed — last writer wins.
                    h_ts = _parse_iso(h.updated_at)
                    g_ts = _parse_iso(g.updated)
                    stats.conflicts_resolved += 1
                    if g_ts >= h_ts:
                        log.info("[%s] conflict on %s ↔ %s: google wins (%s vs %s)",
                                 self.pair.name, h.id, g.id, g.updated, h.updated_at)
                        self._push_google_to_habitica(g, h, mapping, tasklist_id, stats)
                    else:
                        log.info("[%s] conflict on %s ↔ %s: habitica wins (%s vs %s)",
                                 self.pair.name, h.id, g.id, h.updated_at, g.updated)
                        self._push_habitica_to_google(h, g, mapping, tasklist_id, stats)
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                log.exception("[%s] failed to sync mapping %s ↔ %s: %s",
                              self.pair.name, h.id, g.id, exc)

    def _push_habitica_to_google(
        self,
        h: HabiticaTask,
        g: GoogleTask,
        mapping: TaskMapping,
        tasklist_id: str,
        stats: SyncStats,
    ) -> None:
        canonical = h.to_canonical()
        notes = _merge_notes_for_google(canonical)
        completed_arg: bool | None = None
        if canonical.completed != (g.status == "completed"):
            completed_arg = canonical.completed

        new_g = self.g.patch_task(
            mapping.google_tasklist or tasklist_id,
            g.id,
            title=_truncate(canonical.title, GOOGLE_TITLE_MAX),
            notes=notes,
            due_date_iso=canonical.due_date,
            clear_due=canonical.due_date is None and g.due is not None,
            completed=completed_arg,
        )
        stats.updated_in_google += 1
        # `new_g` reflects what Google actually stored (date-only `due`,
        # truncations, etc.) — hash from that, not from our send-side view.
        self._record_mapping(h, new_g, mapping.google_tasklist or tasklist_id)

    def _push_google_to_habitica(
        self,
        g: GoogleTask,
        h: HabiticaTask,
        mapping: TaskMapping,
        tasklist_id: str,
        stats: SyncStats,
    ) -> None:
        canonical = g.to_canonical(checklist=h.to_canonical().checklist)
        notes = _strip_checklist_artifact(canonical.notes)
        # Title/notes/due go through PUT; completion is its own endpoint.
        self.h.update_todo(
            h.id,
            text=canonical.title,
            notes=notes,
            due_date_iso=canonical.due_date,
            clear_due=canonical.due_date is None,
        )
        if canonical.completed != h.completed:
            self.h.score_todo(h.id, complete=canonical.completed)
        # Re-fetch to capture fresh updatedAt + computed completion timestamp.
        refreshed = self.h.get_todo(h.id) or h
        stats.updated_in_habitica += 1
        self._record_mapping(refreshed, g, mapping.google_tasklist or tasklist_id)

    # --- creations ------------------------------------------------------

    def _create_missing_in_google(
        self,
        h_by_id: dict[str, HabiticaTask],
        mappings: dict[str, TaskMapping],
        tasklist_id: str,
        stats: SyncStats,
        g_by_id: dict[str, GoogleTask] | None = None,
    ) -> None:
        # Index unmapped, undeleted Google tasks by normalized title so we
        # can adopt an existing match instead of creating a duplicate.
        # Only used on first sync (or when an old mapping was lost) — once
        # mapped, the ID handles linkage.
        mapped_google_ids = {m.google_id for m in mappings.values()}
        adoption_pool: dict[str, GoogleTask] = {}
        if g_by_id:
            for g in g_by_id.values():
                if g.deleted or g.id in mapped_google_ids:
                    continue
                key = _title_key(g.title)
                # First seen wins (stable when iteration order is stable).
                adoption_pool.setdefault(key, g)

        for h in h_by_id.values():
            if h.id in mappings:
                continue
            if self.store.has_tombstone(self.pair.name, "habitica", h.id):
                # We previously deleted this task on Habitica's side; do not
                # recreate it on Google. The Habitica copy is a resurrection
                # the user did manually — accept it but no longer treat it as
                # a sync target until the user re-edits it.
                continue
            try:
                canonical = h.to_canonical()
                key = _title_key(canonical.title)
                adopted = adoption_pool.pop(key, None)
                if adopted is not None:
                    log.info("[%s] adopted existing google task %s for habitica %s (title match %r)",
                             self.pair.name, adopted.id, h.id, canonical.title)
                    self._record_mapping(h, adopted, tasklist_id)
                    continue
                new_g = self.g.insert_task(
                    tasklist_id,
                    title=_truncate(canonical.title, GOOGLE_TITLE_MAX),
                    notes=_merge_notes_for_google(canonical),
                    due_date_iso=canonical.due_date,
                    completed=canonical.completed,
                )
                stats.created_in_google += 1
                self._record_mapping(h, new_g, tasklist_id)
                log.info("[%s] created google task %s for habitica %s",
                         self.pair.name, new_g.id, h.id)
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                log.exception("[%s] failed to create google task for habitica %s: %s",
                              self.pair.name, h.id, exc)

    def _create_missing_in_habitica(
        self,
        g_by_id: dict[str, GoogleTask],
        mappings: dict[str, TaskMapping],
        tasklist_id: str,
        stats: SyncStats,
        h_by_id: dict[str, HabiticaTask] | None = None,
    ) -> None:
        mapped_google_ids = {m.google_id for m in mappings.values()}
        mapped_habitica_ids = {m.habitica_id for m in mappings.values()}

        adoption_pool: dict[str, HabiticaTask] = {}
        if h_by_id:
            for h in h_by_id.values():
                if h.id in mapped_habitica_ids:
                    continue
                key = _title_key(h.text)
                adoption_pool.setdefault(key, h)

        for g in g_by_id.values():
            if g.id in mapped_google_ids or g.deleted:
                continue
            if self.store.has_tombstone(self.pair.name, "google", g.id):
                continue
            try:
                canonical = g.to_canonical()
                key = _title_key(canonical.title)
                adopted = adoption_pool.pop(key, None)
                if adopted is not None:
                    log.info("[%s] adopted existing habitica task %s for google %s (title match %r)",
                             self.pair.name, adopted.id, g.id, canonical.title)
                    self._record_mapping(adopted, g, g_tasklist=tasklist_id)
                    continue
                new_h = self.h.create_todo(
                    text=canonical.title,
                    notes=canonical.notes,
                    due_date_iso=canonical.due_date,
                )
                if canonical.completed:
                    try:
                        self.h.score_todo(new_h.id, complete=True)
                        new_h = self.h.get_todo(new_h.id) or new_h
                    except HabiticaError as exc:
                        log.warning("[%s] could not score new habitica task complete: %s",
                                    self.pair.name, exc)
                stats.created_in_habitica += 1
                self._record_mapping(new_h, g, g_tasklist=tasklist_id)
                log.info("[%s] created habitica task %s for google %s",
                         self.pair.name, new_h.id, g.id)
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                log.exception("[%s] failed to create habitica task for google %s: %s",
                              self.pair.name, g.id, exc)

    # --- mapping persistence -------------------------------------------

    def _record_mapping(self, h: HabiticaTask, g: GoogleTask, g_tasklist: str) -> None:
        # Hash each side from its own observed state. The two hashes can
        # legitimately differ (e.g. Habitica's checklist gets flattened
        # into Google's notes) — what matters is that each side's hash
        # matches what's actually stored on that side, so we can detect
        # subsequent edits without false positives every cycle.
        self.store.upsert_mapping(
            TaskMapping(
                pair_name=self.pair.name,
                habitica_id=h.id,
                google_id=g.id,
                google_tasklist=g_tasklist,
                habitica_hash=h.to_canonical().content_hash(),
                google_hash=g.to_canonical().content_hash(),
                habitica_updated=h.updated_at,
                google_updated=g.updated,
                last_synced_at=_now_iso(),
            )
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_iso(value: str) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


_EMPTY_KEY_COUNTER = itertools.count()


def _title_key(title: str) -> str:
    """Normalize a title for cross-side adoption matching on first sync.

    Empty/whitespace titles are excluded from matching by returning a
    globally unique sentinel each call (so no two empty-title tasks ever
    adopt each other). Otherwise: trim, collapse internal whitespace,
    lower.
    """

    cleaned = " ".join((title or "").split()).strip().lower()
    if not cleaned:
        return f"\x00empty\x00{next(_EMPTY_KEY_COUNTER)}"
    return cleaned


_CHECKLIST_HEADER = "— Checklist —"

# Per Google Tasks API: title ≤ 1024, notes ≤ 8192. Habitica's limits are
# higher (or unenforced), so the binding side is always Google.
GOOGLE_TITLE_MAX = 1024
GOOGLE_NOTES_MAX = 8192
_TRUNCATION_SUFFIX = "… [truncated]"


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    head = limit - len(_TRUNCATION_SUFFIX)
    if head <= 0:
        return value[:limit]
    return value[:head] + _TRUNCATION_SUFFIX


def _merge_notes_for_google(canonical: CanonicalTask) -> str:
    """Append checklist text to notes since Google Tasks has no checklist concept.

    Habitica → Google loses the structured checklist; we surface it as a
    bullet list at the bottom of `notes` so the user still sees it in
    Google. Round-tripping back is best-effort: if the user edits the notes
    and the bullets get garbled, Habitica's checklist is the source of truth.

    The combined output is truncated to Google's 8192-char notes limit so
    we never get a 400 for an over-long body.
    """

    base = _strip_checklist_artifact(canonical.notes or "").rstrip()
    if not canonical.checklist:
        return _truncate(base, GOOGLE_NOTES_MAX)
    bullets = "\n".join(
        f"[{'x' if c.completed else ' '}] {c.text}" for c in canonical.checklist
    )
    sep = "\n\n" if base else ""
    return _truncate(f"{base}{sep}{_CHECKLIST_HEADER}\n{bullets}", GOOGLE_NOTES_MAX)


def _strip_checklist_artifact(notes: str) -> str:
    """Remove the trailing `— Checklist —` block we appended for Google.

    When pushing Google → Habitica we don't want the bullets to land in
    Habitica's notes (Habitica has a real checklist field). The block is
    always at the end and starts with our sentinel header.
    """

    if not notes:
        return ""
    idx = notes.rfind(_CHECKLIST_HEADER)
    if idx == -1:
        return notes
    return notes[:idx].rstrip()


# Re-export for tests / external introspection.
__all__ = ["SyncEngine", "SyncStats"]
