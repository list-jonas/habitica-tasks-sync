"""Long-running entry point. Loops forever, syncing each configured pair."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

from .config import AppConfig, SyncPair, load_config
from .db import StateStore
from .google_tasks import GoogleAuthError, GoogleTasksClient
from .habitica import HabiticaClient, HabiticaError
from .logging_setup import setup_logging
from .sync import SyncEngine

log = logging.getLogger(__name__)


class _Shutdown:
    """Graceful shutdown signal that survives signal-handler reentrancy."""

    def __init__(self) -> None:
        self._evt = threading.Event()

    def request(self, signum: int = 0, _frame: object | None = None) -> None:
        if not self._evt.is_set():
            log.info("shutdown requested (signal=%s)", signum or "n/a")
        self._evt.set()

    def wait(self, seconds: float) -> bool:
        return self._evt.wait(timeout=seconds)

    def is_set(self) -> bool:
        return self._evt.is_set()


def run(config: AppConfig, stop: _Shutdown | None = None) -> int:
    setup_logging(config.log_level)
    stop = stop or _Shutdown()

    store = StateStore(config.db_path)
    log.info(
        "starting habitica-tasks-sync: %d pair(s), every %ds, db=%s",
        len(config.pairs), config.sync_interval_seconds, config.db_path,
    )

    while not stop.is_set():
        cycle_started = time.monotonic()
        for pair in config.pairs:
            if stop.is_set():
                break
            try:
                _run_pair(pair, store, config)
            except (HabiticaError, GoogleAuthError) as exc:
                log.error("[%s] sync aborted: %s", pair.name, exc)
                if config.fail_fast:
                    store.close()
                    return 2
            except Exception:  # noqa: BLE001
                log.exception("[%s] sync crashed", pair.name)
                if config.fail_fast:
                    store.close()
                    return 3

        elapsed = time.monotonic() - cycle_started
        wait = max(1.0, config.sync_interval_seconds - elapsed)
        log.debug("cycle took %.1fs; sleeping %.1fs", elapsed, wait)
        if stop.wait(wait):
            break

    store.close()
    log.info("stopped cleanly")
    return 0


def _run_pair(pair: SyncPair, store: StateStore, config: AppConfig) -> None:
    with HabiticaClient(
        user_id=pair.habitica.user_id,
        api_token=pair.habitica.api_token,
        client_uuid=config.user_agent_id,
        app_name=config.user_agent_app,
        timeout=config.http_timeout_seconds,
    ) as h, GoogleTasksClient(
        credentials_file=pair.google.credentials_file,
        token_file=pair.google.token_file,
    ) as g:
        engine = SyncEngine(
            pair=pair,
            habitica=h,
            google=g,
            store=store,
            delete_propagation=config.delete_propagation,
        )
        engine.run_once()


def _install_signal_handlers(stop: _Shutdown) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop.request)
        except (ValueError, OSError):
            # Not on the main thread (e.g. tests) — ignore.
            pass


def _resolve_config_path(argv: list[str]) -> Path:
    if len(argv) > 1:
        return Path(argv[1])
    env = os.environ.get("HABITICA_SYNC_CONFIG")
    if env:
        return Path(env)
    return Path("/etc/habitica-tasks-sync/config.yaml")


def main(argv: Iterable[str] | None = None) -> int:
    args = list(argv or sys.argv)
    config_path = _resolve_config_path(args)
    try:
        config = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to load config from {config_path}: {exc}", file=sys.stderr)
        return 1

    stop = _Shutdown()
    _install_signal_handlers(stop)
    return run(config, stop)


if __name__ == "__main__":
    raise SystemExit(main())
