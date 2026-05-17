"""Bidirectional sync engine for one Habitica<->Google pair.

Sync algorithm (per pair, per cycle):

1. Fetch all Habitica todos (active + last 30 completed) — Habitica has no
   incremental endpoint, so a full pull is unavoidable.

2. Fetch Google tasks across every configured tasklist since
   `last_google_sync - overlap` with showDeleted/showHidden/showCompleted=true.

3. Detect deletions:
   - Habitica: any mapping whose habitica_id is no longer present in the
     active+completed lists (and is older than the most recent run) is
     treated as deleted on Habitica's side. Deletes are propagated to
     Google.
   - Google: any task returned with `deleted=true` is propagated to
     Habitica as a delete.

4. For each surviving Habitica task with a mapping, compare its canonical
   content hash and updatedAt against the stored values to decide if a push
   to Google is needed. If the Habitica task's tags now point at a
   different configured list than the mapping records, migrate the task
   to the new list (delete old, create new).

5. For each surviving Google task with a mapping, compare similarly to
   decide if a push to Habitica is needed.

6. Conflict (both sides changed since last sync): the side with the later
   `updatedAt` wins. The other side is overwritten.

7. Unmapped tasks on either side become creations on the other side; the
   resulting mapping is persisted. Routing is by tag:

   - Habitica → Google: pick the configured tasklist whose tag matches
     one of the task's tags; otherwise the default (first configured)
     tasklist, with that list's tag added back to the Habitica task.
   - Google → Habitica: read the source tasklist, look up its tag,
     create the Habitica task with that tag attached.

Tombstones prevent a deleted task on side A — still visible on side B
because side B hasn't pulled yet — from being recreated on side A on the
next cycle.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .config import SyncPair, TasklistConfig
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
    moved_in_google: int = 0
    conflicts_resolved: int = 0
    errors: int = 0

    def summary(self) -> str:
        return (
            f"[{self.pair}] "
            f"google: +{self.created_in_google}/~{self.updated_in_google}"
            f"/-{self.deleted_in_google}/>{self.moved_in_google} | "
            f"habitica: +{self.created_in_habitica}/~{self.updated_in_habitica}/-{self.deleted_in_habitica} | "
            f"conflicts: {self.conflicts_resolved} | errors: {self.errors}"
        )


@dataclass(frozen=True)
class TasklistRouting:
    """Resolved tasklist + tag identifiers for one pair.

    Computed once per `run_once()` so each downstream helper can answer
    "which Google list does this Habitica task belong in?" / "which tag
    represents this Google list?" in O(1) without extra API round trips.
    """

    tasklists: tuple[TasklistConfig, ...]
    tasklist_ids: tuple[str, ...]
    tag_id_by_name: dict[str, str]         # configured tag name → Habitica tag ID
    tag_name_by_tasklist: dict[str, str]   # Google tasklist ID → tag name
    tasklist_by_tag_id: dict[str, str]     # Habitica tag ID → Google tasklist ID

    @property
    def is_multi_list(self) -> bool:
        return len(self.tasklists) > 1

    @property
    def default_tasklist_id(self) -> str:
        return self.tasklist_ids[0]

    def tag_id_for_tasklist(self, tasklist_id: str) -> str | None:
        name = self.tag_name_by_tasklist.get(tasklist_id)
        if not name:
            return None
        return self.tag_id_by_name.get(name)

    def configured_tasklist(self, tasklist_id: str) -> bool:
        return tasklist_id in self.tasklist_ids


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
        self._routing: TasklistRouting | None = None

    # --- public ---------------------------------------------------------

    def run_once(self) -> SyncStats:
        stats = SyncStats(pair=self.pair.name)
        routing = self._resolve_routing()
        log.info(
            "[%s] sync start (tasklists=%d, multi=%s)",
            self.pair.name, len(routing.tasklist_ids), routing.is_multi_list,
        )

        habitica_tasks = self._fetch_habitica()
        existing_mappings = {m.habitica_id: m for m in self.store.list_for_pair(self.pair.name)}
        # If we have no mappings at all (first sync, or DB lost) we need a
        # FULL Google fetch — otherwise the title-adoption pool only sees
        # tasks updated within the cursor window and will create
        # duplicates for older Google tasks.
        force_full_google = not existing_mappings
        google_tasks, new_cursor = self._fetch_google(routing, force_full=force_full_google)

        h_by_id = {t.id: t for t in habitica_tasks}
        g_by_id = {t.id: t for t in google_tasks}

        # 1) Propagate Habitica deletions → Google.
        if self.delete_propagation:
            self._propagate_habitica_deletions(existing_mappings, h_by_id, routing, stats)

        # 2) Propagate Google deletions → Habitica.
        if self.delete_propagation:
            self._propagate_google_deletions(existing_mappings, g_by_id, stats)

        # Refresh mappings after deletions.
        existing_mappings = {m.habitica_id: m for m in self.store.list_for_pair(self.pair.name)}

        # 3) Sync content changes for already-mapped tasks (handles conflicts and moves).
        self._sync_existing_mappings(
            existing_mappings, h_by_id, g_by_id, routing, stats
        )

        # 4) Create missing counterparts in both directions.
        existing_mappings = {m.habitica_id: m for m in self.store.list_for_pair(self.pair.name)}
        self._create_missing_in_google(
            h_by_id, existing_mappings, routing, stats, g_by_id=g_by_id,
        )

        existing_mappings = {m.habitica_id: m for m in self.store.list_for_pair(self.pair.name)}
        self._create_missing_in_habitica(
            g_by_id, existing_mappings, routing, stats, h_by_id=h_by_id,
        )

        self.store.set_last_google_sync(self.pair.name, new_cursor)
        for tlid in routing.tasklist_ids:
            self.store.set_last_tasklist_sync(self.pair.name, tlid, new_cursor)

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.tombstone_ttl_days)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        pruned = self.store.prune_tombstones_older_than(cutoff)
        if pruned:
            log.debug("[%s] pruned %d expired tombstones", self.pair.name, pruned)

        log.info(stats.summary())
        return stats

    # --- routing --------------------------------------------------------

    def _resolve_routing(self) -> TasklistRouting:
        if self._routing is not None:
            return self._routing

        configs = self.pair.google.tasklists
        ids = tuple(
            self.g.resolve_tasklist(tasklist_id=c.tasklist_id, title=c.tasklist_title)
            for c in configs
        )

        tag_id_by_name: dict[str, str] = {}
        tag_name_by_tasklist: dict[str, str] = {}
        tasklist_by_tag_id: dict[str, str] = {}

        configs_with_tag = [(c, tlid) for c, tlid in zip(configs, ids) if c.tag]
        if configs_with_tag:
            # Single tag list fetch covers all configured tags.
            existing = {(t.get("name") or "").strip().casefold(): t for t in self.h.list_tags()}
            for c, tlid in configs_with_tag:
                tag_name = c.tag or ""
                key = tag_name.strip().casefold()
                tag_obj = existing.get(key)
                if tag_obj is None:
                    log.info("[%s] creating habitica tag %r for tasklist %s",
                             self.pair.name, tag_name, c.display_name())
                    tag_obj = self.h.create_tag(tag_name)
                    if isinstance(tag_obj, dict) and tag_obj.get("name"):
                        existing[(tag_obj["name"] or "").strip().casefold()] = tag_obj
                tag_id = (tag_obj or {}).get("id")
                if not tag_id:
                    raise RuntimeError(
                        f"could not resolve habitica tag {tag_name!r}: missing id in response"
                    )
                tag_id_by_name[tag_name] = tag_id
                tag_name_by_tasklist[tlid] = tag_name
                tasklist_by_tag_id[tag_id] = tlid

        self._routing = TasklistRouting(
            tasklists=configs,
            tasklist_ids=ids,
            tag_id_by_name=tag_id_by_name,
            tag_name_by_tasklist=tag_name_by_tasklist,
            tasklist_by_tag_id=tasklist_by_tag_id,
        )
        return self._routing

    # --- fetch ----------------------------------------------------------

    def _fetch_habitica(self) -> list[HabiticaTask]:
        # Skip challenge/group tasks: they're owned by the challenge/group,
        # not the user, so PUT/DELETE will return 401 with
        # `challengeTasksNoUserDelete` etc. Treating them as out-of-scope is
        # simpler than special-casing each mutation.
        return [
            t for t in self.h.list_todos(include_completed=True)
            if t.type == "todo" and not t.is_managed_externally
        ]

    def _fetch_google(self, routing: TasklistRouting, *, force_full: bool = False) -> tuple[list[GoogleTask], str]:
        """Pull tasks across every configured tasklist using a per-list cursor.

        A tasklist with no per-list cursor row gets a full pull so a
        newly-added list (or a first-ever sync) doesn't drop the
        pre-existing tasks. The cursor is advanced by run_once after a
        successful cycle, not here.
        """

        new_cursor = overlap_window(datetime.now(timezone.utc), minutes=0)
        all_tasks: list[GoogleTask] = []
        for tlid in routing.tasklist_ids:
            last = None if force_full else self.store.get_last_tasklist_sync(self.pair.name, tlid)
            cursor_iso: str | None = None
            if last:
                try:
                    anchor = datetime.fromisoformat(last.replace("Z", "+00:00"))
                except ValueError:
                    anchor = datetime.now(timezone.utc) - timedelta(days=7)
                cursor = (anchor - timedelta(minutes=5)).astimezone(timezone.utc)
                cursor_iso = cursor.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            else:
                log.info("[%s] no cursor for tasklist %s — doing full pull", self.pair.name, tlid)
            tasks = self.g.list_tasks(
                tlid,
                updated_min=cursor_iso,
                show_completed=True,
                show_deleted=True,
                show_hidden=True,
            )
            for t in tasks:
                if not t.tasklist_id:
                    t.tasklist_id = tlid
            all_tasks.extend(tasks)
        return all_tasks, new_cursor

    # --- deletions ------------------------------------------------------

    def _propagate_habitica_deletions(
        self,
        mappings: dict[str, TaskMapping],
        h_by_id: dict[str, HabiticaTask],
        routing: TasklistRouting,
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
            target_tasklist = mapping.google_tasklist or routing.default_tasklist_id
            try:
                self.g.delete_task(target_tasklist, mapping.google_id)
                self.store.remove_mapping_with_tombstone(
                    self.pair.name,
                    habitica_id=habitica_id,
                    google_id=mapping.google_id,
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
                    habitica_id=habitica_id,
                    google_id=google_id,
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
        routing: TasklistRouting,
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

            # In multi-list mode, check whether the Habitica task's tags
            # now point at a different configured list than the mapping
            # records. If so, migrate before doing the regular sync.
            if routing.is_multi_list:
                intended = self._intended_tasklist_for_habitica(h, routing)
                if intended is not None and intended != (mapping.google_tasklist or routing.default_tasklist_id):
                    try:
                        new_g = self._migrate_google_to_list(h, g, mapping, intended, stats)
                        if new_g is not None:
                            g = new_g
                            mapping = self.store.get_by_habitica(self.pair.name, h.id) or mapping
                    except Exception as exc:  # noqa: BLE001
                        stats.errors += 1
                        log.exception("[%s] failed to migrate task %s to %s: %s",
                                      self.pair.name, h.id, intended, exc)
                        continue

            h_can = h.to_canonical()
            g_can = g.to_canonical()
            h_changed = (h.updated_at != mapping.habitica_updated) or (h_can.content_hash() != mapping.habitica_hash)
            g_changed = (g.updated != mapping.google_updated) or (g_can.content_hash() != mapping.google_hash)

            if not h_changed and not g_changed:
                continue

            target_tasklist = mapping.google_tasklist or routing.default_tasklist_id
            try:
                if h_changed and not g_changed:
                    self._push_habitica_to_google(h, g, mapping, target_tasklist, stats)
                elif g_changed and not h_changed:
                    self._push_google_to_habitica(g, h, mapping, target_tasklist, routing, stats)
                else:
                    # Both changed — last writer wins.
                    h_ts = _parse_iso(h.updated_at)
                    g_ts = _parse_iso(g.updated)
                    stats.conflicts_resolved += 1
                    if g_ts >= h_ts:
                        log.info("[%s] conflict on %s ↔ %s: google wins (%s vs %s)",
                                 self.pair.name, h.id, g.id, g.updated, h.updated_at)
                        self._push_google_to_habitica(g, h, mapping, target_tasklist, routing, stats)
                    else:
                        log.info("[%s] conflict on %s ↔ %s: habitica wins (%s vs %s)",
                                 self.pair.name, h.id, g.id, h.updated_at, g.updated)
                        self._push_habitica_to_google(h, g, mapping, target_tasklist, stats)
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
            tasklist_id,
            g.id,
            title=_truncate(canonical.title, GOOGLE_TITLE_MAX),
            notes=notes,
            due_date_iso=canonical.due_date,
            clear_due=canonical.due_date is None and g.due is not None,
            completed=completed_arg,
        )
        stats.updated_in_google += 1
        self._record_mapping(h, new_g, tasklist_id)

    def _push_google_to_habitica(
        self,
        g: GoogleTask,
        h: HabiticaTask,
        mapping: TaskMapping,
        tasklist_id: str,
        routing: TasklistRouting,
        stats: SyncStats,
    ) -> None:
        canonical = g.to_canonical(checklist=h.to_canonical().checklist)
        notes = _strip_checklist_artifact(canonical.notes)
        self.h.update_todo(
            h.id,
            text=canonical.title,
            notes=notes,
            due_date_iso=canonical.due_date,
            clear_due=canonical.due_date is None,
        )
        if canonical.completed != h.completed:
            self.h.score_todo(h.id, complete=canonical.completed)
        # If the Habitica task is missing the tag for its source list, attach it.
        # _ensure_habitica_has_tasklist_tag re-fetches when it adds a tag.
        if routing.is_multi_list:
            self._ensure_habitica_has_tasklist_tag(h, tasklist_id, routing)
        refreshed = self.h.get_todo(h.id) or h
        stats.updated_in_habitica += 1
        self._record_mapping(refreshed, g, tasklist_id)

    # --- migrations -----------------------------------------------------

    def _migrate_google_to_list(
        self,
        h: HabiticaTask,
        g: GoogleTask,
        mapping: TaskMapping,
        new_tasklist_id: str,
        stats: SyncStats,
    ) -> GoogleTask | None:
        """Move a Google task to a different list by recreating it.

        Google Tasks has no cross-list move; the only safe path is
        insert-into-new → update mapping → delete-old, in that order, so a
        failure on the second step leaves the user with a duplicate (which
        the user can resolve) rather than a missing task.
        """

        old_tasklist = mapping.google_tasklist or g.tasklist_id
        if not old_tasklist:
            return None
        canonical = h.to_canonical()
        new_g = self.g.insert_task(
            new_tasklist_id,
            title=_truncate(canonical.title, GOOGLE_TITLE_MAX),
            notes=_merge_notes_for_google(canonical),
            due_date_iso=canonical.due_date,
            completed=canonical.completed,
        )
        self._record_mapping(h, new_g, new_tasklist_id)
        try:
            self.g.delete_task(old_tasklist, g.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] migrated %s ↔ %s to %s but failed to delete old task %s: %s",
                        self.pair.name, h.id, new_g.id, new_tasklist_id, g.id, exc)
        # Tombstone the OLD google id so the next cycle (still seeing it via
        # cursor overlap or because the delete returned 404) doesn't try to
        # recreate it on Habitica.
        self.store.add_tombstone(self.pair.name, "google", g.id, _now_iso())
        stats.moved_in_google += 1
        log.info("[%s] moved google task: %s → %s for habitica %s (new id %s)",
                 self.pair.name, old_tasklist, new_tasklist_id, h.id, new_g.id)
        return new_g

    # --- creations ------------------------------------------------------

    def _create_missing_in_google(
        self,
        h_by_id: dict[str, HabiticaTask],
        mappings: dict[str, TaskMapping],
        routing: TasklistRouting,
        stats: SyncStats,
        g_by_id: dict[str, GoogleTask] | None = None,
    ) -> None:
        # Adoption pool: unmapped, undeleted Google tasks bucketed by their
        # source tasklist so we can match within the same list only.
        mapped_google_ids = {m.google_id for m in mappings.values()}
        adoption_pool: dict[tuple[str, str], GoogleTask] = {}
        if g_by_id:
            for g in g_by_id.values():
                if g.deleted or g.id in mapped_google_ids:
                    continue
                key = (g.tasklist_id or routing.default_tasklist_id, _title_key(g.title))
                adoption_pool.setdefault(key, g)

        for h in h_by_id.values():
            if h.id in mappings:
                continue
            if self.store.has_tombstone(self.pair.name, "habitica", h.id):
                continue
            try:
                canonical = h.to_canonical()
                # Determine target list. In single-list mode this is always
                # the only configured list. In multi-list mode we route by
                # the task's tags, falling back to the default list (and
                # tagging the Habitica task accordingly so future cycles
                # are deterministic).
                target_tasklist = self._intended_tasklist_for_habitica(h, routing) or routing.default_tasklist_id

                # Attach the routing tag FIRST so the mapping we record
                # below reflects Habitica's post-tag `updatedAt`. If we
                # tagged after recording, the next cycle would see h.updated_at
                # drift and re-push without any real content change.
                if routing.is_multi_list:
                    h = self._ensure_habitica_has_tasklist_tag(h, target_tasklist, routing)
                    h_by_id[h.id] = h
                    canonical = h.to_canonical()

                adopted = adoption_pool.pop((target_tasklist, _title_key(canonical.title)), None)
                if adopted is not None:
                    log.info("[%s] adopted existing google task %s for habitica %s (title match %r, list=%s)",
                             self.pair.name, adopted.id, h.id, canonical.title, target_tasklist)
                    self._record_mapping(h, adopted, target_tasklist)
                    continue

                new_g = self.g.insert_task(
                    target_tasklist,
                    title=_truncate(canonical.title, GOOGLE_TITLE_MAX),
                    notes=_merge_notes_for_google(canonical),
                    due_date_iso=canonical.due_date,
                    completed=canonical.completed,
                )
                stats.created_in_google += 1
                self._record_mapping(h, new_g, target_tasklist)
                log.info("[%s] created google task %s for habitica %s (list=%s)",
                         self.pair.name, new_g.id, h.id, target_tasklist)
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                log.exception("[%s] failed to create google task for habitica %s: %s",
                              self.pair.name, h.id, exc)

    def _create_missing_in_habitica(
        self,
        g_by_id: dict[str, GoogleTask],
        mappings: dict[str, TaskMapping],
        routing: TasklistRouting,
        stats: SyncStats,
        h_by_id: dict[str, HabiticaTask] | None = None,
    ) -> None:
        mapped_google_ids = {m.google_id for m in mappings.values()}
        mapped_habitica_ids = {m.habitica_id for m in mappings.values()}

        # Adoption pool: unmapped Habitica tasks bucketed by the tasklist
        # they "would" land in if we created them on Google now. The
        # tag-derived bucket lets us pair like-with-like across sides.
        adoption_pool: dict[tuple[str, str], HabiticaTask] = {}
        if h_by_id:
            for h in h_by_id.values():
                if h.id in mapped_habitica_ids:
                    continue
                target = self._intended_tasklist_for_habitica(h, routing) or routing.default_tasklist_id
                adoption_pool.setdefault((target, _title_key(h.text)), h)

        for g in g_by_id.values():
            if g.id in mapped_google_ids or g.deleted:
                continue
            if self.store.has_tombstone(self.pair.name, "google", g.id):
                continue
            try:
                canonical = g.to_canonical()
                source_tasklist = g.tasklist_id or routing.default_tasklist_id
                tag_id = routing.tag_id_for_tasklist(source_tasklist)
                tag_name = routing.tag_name_by_tasklist.get(source_tasklist)

                adopted = adoption_pool.pop((source_tasklist, _title_key(canonical.title)), None)
                if adopted is not None:
                    log.info("[%s] adopted existing habitica task %s for google %s (title match %r, list=%s)",
                             self.pair.name, adopted.id, g.id, canonical.title, source_tasklist)
                    # Tag first so the recorded mapping matches Habitica's
                    # post-tag updatedAt.
                    if tag_id and tag_id not in adopted.tags:
                        adopted = self._ensure_habitica_has_tasklist_tag(adopted, source_tasklist, routing)
                    self._record_mapping(adopted, g, source_tasklist)
                    continue

                new_h = self.h.create_todo(
                    text=canonical.title,
                    notes=canonical.notes,
                    due_date_iso=canonical.due_date,
                    tags=[tag_id] if tag_id else (),
                )
                if canonical.completed:
                    try:
                        self.h.score_todo(new_h.id, complete=True)
                        new_h = self.h.get_todo(new_h.id) or new_h
                    except HabiticaError as exc:
                        log.warning("[%s] could not score new habitica task complete: %s",
                                    self.pair.name, exc)
                stats.created_in_habitica += 1
                self._record_mapping(new_h, g, source_tasklist)
                log.info("[%s] created habitica task %s for google %s (list=%s, tag=%s)",
                         self.pair.name, new_h.id, g.id, source_tasklist, tag_name)
            except Exception as exc:  # noqa: BLE001
                stats.errors += 1
                log.exception("[%s] failed to create habitica task for google %s: %s",
                              self.pair.name, g.id, exc)

    # --- routing helpers -----------------------------------------------

    def _intended_tasklist_for_habitica(
        self, h: HabiticaTask, routing: TasklistRouting
    ) -> str | None:
        """Pick the configured tasklist a Habitica task should live in.

        Returns the first match on the task's tag list (preserving the
        order Habitica returned), or None if the task has no tag that
        maps to a configured tasklist.
        """

        for tag_id in h.tags:
            target = routing.tasklist_by_tag_id.get(tag_id)
            if target is not None:
                return target
        return None

    def _ensure_habitica_has_tasklist_tag(
        self, h: HabiticaTask, tasklist_id: str, routing: TasklistRouting
    ) -> HabiticaTask:
        """Attach the tag for `tasklist_id` to `h` if missing.

        Returns the refreshed Habitica task (Habitica bumps `updatedAt`
        on tag changes; callers MUST use the returned task when
        recording a mapping so the saved `habitica_updated` matches what
        the next cycle will read back).
        """

        tag_id = routing.tag_id_for_tasklist(tasklist_id)
        if not tag_id or tag_id in h.tags:
            return h
        try:
            self.h.add_tag_to_task(h.id, tag_id)
        except HabiticaError as exc:
            log.warning("[%s] could not attach tasklist tag to habitica %s: %s",
                        self.pair.name, h.id, exc)
            return h
        refreshed = self.h.get_todo(h.id)
        return refreshed if refreshed is not None else h

    # --- mapping persistence -------------------------------------------

    def _record_mapping(self, h: HabiticaTask, g: GoogleTask, g_tasklist: str) -> None:
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
__all__ = ["SyncEngine", "SyncStats", "TasklistRouting"]
