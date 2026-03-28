"""SQLite-backed mapping store.

Tracks the relationship between Habitica task IDs and Google Tasks IDs for
each sync pair, the most recently observed `updatedAt` timestamps from each
side, the canonical content hash, and a tombstone log so deletes can
propagate without resurrecting tasks on the next pull.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS task_map (
    pair_name        TEXT NOT NULL,
    habitica_id      TEXT NOT NULL,
    google_id        TEXT NOT NULL,
    google_tasklist  TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    habitica_updated TEXT NOT NULL,
    google_updated   TEXT NOT NULL,
    last_synced_at   TEXT NOT NULL,
    PRIMARY KEY (pair_name, habitica_id),
    UNIQUE (pair_name, google_id)
);

CREATE INDEX IF NOT EXISTS idx_task_map_pair ON task_map(pair_name);

CREATE TABLE IF NOT EXISTS sync_state (
    pair_name        TEXT PRIMARY KEY,
    last_google_sync TEXT,
    last_run_at      TEXT
);

CREATE TABLE IF NOT EXISTS tombstones (
    pair_name        TEXT NOT NULL,
    side             TEXT NOT NULL,         -- 'habitica' | 'google'
    foreign_id       TEXT NOT NULL,         -- the ID on the OTHER side that we deleted
    deleted_at       TEXT NOT NULL,
    PRIMARY KEY (pair_name, side, foreign_id)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""

CURRENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TaskMapping:
    pair_name: str
    habitica_id: str
    google_id: str
    google_tasklist: str
    content_hash: str
    habitica_updated: str
    google_updated: str
    last_synced_at: str


class StateStore:
    """Thread-safe wrapper around a SQLite database file.

    Sync runs are serialized per-pair upstream, but the same DB may also be
    read by health checks or future tooling, so we keep a re-entrant lock and
    use `check_same_thread=False`.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions explicitly
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # --- schema ---------------------------------------------------------

    def _init_schema(self) -> None:
        with self.transaction() as c:
            c.executescript(SCHEMA)
            row = c.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                c.execute("INSERT INTO schema_version(version) VALUES (?)", (CURRENT_SCHEMA_VERSION,))

    # --- mappings -------------------------------------------------------

    def upsert_mapping(self, m: TaskMapping) -> None:
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO task_map(
                    pair_name, habitica_id, google_id, google_tasklist,
                    content_hash, habitica_updated, google_updated, last_synced_at
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(pair_name, habitica_id) DO UPDATE SET
                    google_id        = excluded.google_id,
                    google_tasklist  = excluded.google_tasklist,
                    content_hash     = excluded.content_hash,
                    habitica_updated = excluded.habitica_updated,
                    google_updated   = excluded.google_updated,
                    last_synced_at   = excluded.last_synced_at
                """,
                (
                    m.pair_name, m.habitica_id, m.google_id, m.google_tasklist,
                    m.content_hash, m.habitica_updated, m.google_updated, m.last_synced_at,
                ),
            )

    def get_by_habitica(self, pair_name: str, habitica_id: str) -> TaskMapping | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_map WHERE pair_name=? AND habitica_id=?",
                (pair_name, habitica_id),
            ).fetchone()
        return _row_to_mapping(row)

    def get_by_google(self, pair_name: str, google_id: str) -> TaskMapping | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM task_map WHERE pair_name=? AND google_id=?",
                (pair_name, google_id),
            ).fetchone()
        return _row_to_mapping(row)

    def list_for_pair(self, pair_name: str) -> list[TaskMapping]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_map WHERE pair_name=?",
                (pair_name,),
            ).fetchall()
        return [m for m in (_row_to_mapping(r) for r in rows) if m is not None]

    def delete_mapping(self, pair_name: str, habitica_id: str) -> None:
        with self.transaction() as c:
            c.execute(
                "DELETE FROM task_map WHERE pair_name=? AND habitica_id=?",
                (pair_name, habitica_id),
            )

    # --- tombstones -----------------------------------------------------

    def add_tombstone(self, pair_name: str, side: str, foreign_id: str, when_iso: str) -> None:
        if side not in ("habitica", "google"):
            raise ValueError(f"invalid tombstone side: {side!r}")
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO tombstones(pair_name, side, foreign_id, deleted_at)
                VALUES (?,?,?,?)
                ON CONFLICT(pair_name, side, foreign_id) DO UPDATE SET deleted_at = excluded.deleted_at
                """,
                (pair_name, side, foreign_id, when_iso),
            )

    def has_tombstone(self, pair_name: str, side: str, foreign_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tombstones WHERE pair_name=? AND side=? AND foreign_id=?",
                (pair_name, side, foreign_id),
            ).fetchone()
        return row is not None

    def prune_tombstones_older_than(self, cutoff_iso: str) -> int:
        with self.transaction() as c:
            cur = c.execute("DELETE FROM tombstones WHERE deleted_at < ?", (cutoff_iso,))
            return cur.rowcount or 0

    # --- sync state -----------------------------------------------------

    def get_last_google_sync(self, pair_name: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_google_sync FROM sync_state WHERE pair_name=?",
                (pair_name,),
            ).fetchone()
        return row["last_google_sync"] if row else None

    def set_last_google_sync(self, pair_name: str, when_iso: str) -> None:
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO sync_state(pair_name, last_google_sync, last_run_at)
                VALUES(?,?,?)
                ON CONFLICT(pair_name) DO UPDATE SET
                    last_google_sync = excluded.last_google_sync,
                    last_run_at      = excluded.last_run_at
                """,
                (pair_name, when_iso, when_iso),
            )


def _row_to_mapping(row: sqlite3.Row | None) -> TaskMapping | None:
    if row is None:
        return None
    return TaskMapping(
        pair_name=row["pair_name"],
        habitica_id=row["habitica_id"],
        google_id=row["google_id"],
        google_tasklist=row["google_tasklist"],
        content_hash=row["content_hash"],
        habitica_updated=row["habitica_updated"],
        google_updated=row["google_updated"],
        last_synced_at=row["last_synced_at"],
    )
