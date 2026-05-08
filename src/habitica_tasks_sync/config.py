"""Load and validate the YAML config that drives the sync service.

Supports multiple `pairs`, where each pair couples one Habitica account
with one Google Tasks account + tasklist. Two pairs ⇒ two independent
people syncing through the same container.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ConfigError(ValueError):
    """Raised for any configuration problem the user can fix."""


@dataclass(frozen=True)
class HabiticaCreds:
    user_id: str
    api_token: str

    def __post_init__(self) -> None:
        if not UUID_RE.match(self.user_id):
            raise ConfigError(f"habitica.user_id is not a valid UUID: {self.user_id!r}")
        if not UUID_RE.match(self.api_token):
            raise ConfigError("habitica.api_token must be a UUID; reset it in Habitica settings if needed")


@dataclass(frozen=True)
class GoogleCreds:
    """Credential paths for a single Google account.

    `credentials_file` holds the OAuth client (downloaded from Cloud Console).
    `token_file` is produced by `habitica-tasks-sync-auth` and refreshed in place.
    """

    credentials_file: Path
    token_file: Path
    tasklist_id: str | None  # if None: use the user's default ("@default")
    tasklist_title: str | None  # if set, auto-resolve / auto-create by title

    def resolved_tasklist(self) -> str:
        return self.tasklist_id or "@default"


@dataclass(frozen=True)
class SyncPair:
    name: str
    habitica: HabiticaCreds
    google: GoogleCreds


@dataclass(frozen=True)
class AppConfig:
    sync_interval_seconds: int
    pairs: tuple[SyncPair, ...]
    db_path: Path
    user_agent_id: str  # required by Habitica `x-client` header
    user_agent_app: str
    log_level: str = "INFO"
    delete_propagation: bool = True
    initial_full_sync: bool = True
    http_timeout_seconds: float = 30.0
    fail_fast: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def load_config(path: str | os.PathLike[str]) -> AppConfig:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")

    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {cfg_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("Top-level config must be a mapping")

    raw = _interpolate_env(raw)

    try:
        interval = int(raw.get("sync_interval_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"sync_interval_seconds must be an integer: {exc}") from exc
    if interval < 30:
        raise ConfigError("sync_interval_seconds must be >= 30 to respect Habitica rate limits")

    db_path = Path(raw.get("db_path", "./data/sync.sqlite3")).expanduser()

    ua = raw.get("user_agent") or {}
    ua_id = str(ua.get("uuid") or "").strip()
    ua_app = str(ua.get("app_name") or "habitica-tasks-sync").strip()
    if not ua_id or not UUID_RE.match(ua_id):
        raise ConfigError(
            "user_agent.uuid is required and must be your developer Habitica User ID (UUID). "
            "Habitica requires the x-client header to identify third-party apps."
        )

    pairs_raw = raw.get("pairs")
    if not pairs_raw or not isinstance(pairs_raw, list):
        raise ConfigError("`pairs` must be a non-empty list")

    pairs: list[SyncPair] = []
    seen_names: set[str] = set()
    for i, p in enumerate(pairs_raw):
        if not isinstance(p, dict):
            raise ConfigError(f"pairs[{i}] must be a mapping")
        name = str(p.get("name") or "").strip()
        if not name:
            raise ConfigError(f"pairs[{i}].name is required")
        if name in seen_names:
            raise ConfigError(f"duplicate pair name: {name!r}")
        seen_names.add(name)

        h = p.get("habitica") or {}
        habitica = HabiticaCreds(
            user_id=str(h.get("user_id", "")).strip(),
            api_token=str(h.get("api_token", "")).strip(),
        )

        g = p.get("google") or {}
        creds_raw = str(g.get("credentials_file", "")).strip()
        token_raw = str(g.get("token_file", "")).strip()
        # Guard before wrapping in `Path` — `Path("")` becomes `Path(".")`,
        # which is truthy and would hide a missing value until the OAuth
        # flow blows up at runtime.
        if not creds_raw:
            raise ConfigError(f"pairs[{i}].google.credentials_file is required")
        if not token_raw:
            raise ConfigError(f"pairs[{i}].google.token_file is required")
        creds_file = Path(creds_raw).expanduser()
        token_file = Path(token_raw).expanduser()

        tasklist_id = g.get("tasklist_id")
        tasklist_title = g.get("tasklist_title")
        if tasklist_id is not None:
            tasklist_id = str(tasklist_id)
        if tasklist_title is not None:
            tasklist_title = str(tasklist_title)

        pairs.append(
            SyncPair(
                name=name,
                habitica=habitica,
                google=GoogleCreds(
                    credentials_file=creds_file,
                    token_file=token_file,
                    tasklist_id=tasklist_id,
                    tasklist_title=tasklist_title,
                ),
            )
        )

    return AppConfig(
        sync_interval_seconds=interval,
        pairs=tuple(pairs),
        db_path=db_path,
        user_agent_id=ua_id,
        user_agent_app=ua_app,
        log_level=str(raw.get("log_level", "INFO")).upper(),
        delete_propagation=bool(raw.get("delete_propagation", True)),
        initial_full_sync=bool(raw.get("initial_full_sync", True)),
        http_timeout_seconds=_to_float(raw.get("http_timeout_seconds", 30.0), "http_timeout_seconds"),
        fail_fast=bool(raw.get("fail_fast", False)),
    )


def _to_float(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} must be a number: {exc}") from exc


_ENV_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")


def _interpolate_env(value: Any) -> Any:
    """Replace `${VAR}` and `${VAR:-default}` placeholders in any nested string.

    A `${VAR}` reference without a default that resolves to an unset env
    var raises immediately so missing secrets surface at startup instead
    of as opaque downstream errors (empty UUIDs, paths that became `.`).
    Use `${VAR:-}` to opt into "may be empty".
    """

    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is None:
                raise ConfigError(
                    f"Environment variable {name!r} is referenced in the config "
                    f"but is not set. Use ${{{name}:-default}} to provide a fallback."
                )
            return default

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value
