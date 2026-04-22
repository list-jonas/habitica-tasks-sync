"""Container healthcheck.

Healthy ⇔ the SQLite state DB exists AND has been touched (any pair has
recorded a successful sync) within the past 3 × sync_interval. This
catches the daemon being silently stuck on a cycle, not just the file
existing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

from .config import load_config


def _resolve_config() -> Path:
    return Path(os.environ.get("HABITICA_SYNC_CONFIG", "/etc/habitica-tasks-sync/config.yaml"))


def main() -> int:
    cfg_path = _resolve_config()
    if not cfg_path.exists():
        print(f"healthcheck: config missing at {cfg_path}", file=sys.stderr)
        return 1
    try:
        config = load_config(cfg_path)
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck: config invalid: {exc}", file=sys.stderr)
        return 1

    db = config.db_path
    if not db.exists():
        # Pre-first-cycle: tolerate during start-period.
        print(f"healthcheck: db {db} not yet created", file=sys.stderr)
        return 1

    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT MAX(last_run_at) AS last_run FROM sync_state"
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        print(f"healthcheck: db query failed: {exc}", file=sys.stderr)
        return 1

    last = row["last_run"] if row else None
    if not last:
        print("healthcheck: no sync has completed yet", file=sys.stderr)
        return 1

    # Treat a sync as fresh if it ran within the past 3 intervals. The factor
    # of 3 absorbs jitter and slow cycles without missing genuine stalls.
    threshold = config.sync_interval_seconds * 3
    last_epoch = _parse_epoch(last)
    if last_epoch is None:
        print(f"healthcheck: cannot parse last_run_at={last!r}", file=sys.stderr)
        return 1
    if (time.time() - last_epoch) > threshold:
        print(
            f"healthcheck: last sync at {last} is older than {threshold}s",
            file=sys.stderr,
        )
        return 1
    return 0


def _parse_epoch(iso: str) -> float | None:
    # Handle both "Z" and "+00:00" suffixes.
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


if __name__ == "__main__":
    raise SystemExit(main())
