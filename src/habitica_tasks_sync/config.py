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
class TasklistConfig:
    """One Google Tasks list paired with an optional Habitica tag.

    - `tasklist_id`: explicit Google list ID. Highest precedence.
    - `tasklist_title`: looked up by title; created if missing.
    - `tag`: Habitica tag name used to route tasks between this list and
      its Habitica side. Required in multi-list mode (where `tag` is the
      only way to know which list a Habitica task belongs to); optional
      in single-list mode (no routing needed).
    """

    tasklist_id: str | None
    tasklist_title: str | None
    tag: str | None  # Habitica tag name; mandatory if more than one list configured

    def display_name(self) -> str:
        return self.tasklist_title or self.tasklist_id or "@default"


@dataclass(frozen=True)
class GoogleCreds:
    """Credential paths for a single Google account.

    `credentials_file` holds the OAuth client (downloaded from Cloud Console).
    `token_file` is produced by `habitica-tasks-sync-auth` and refreshed in place.

    `tasklists` is always at least one entry. The first entry is the
    "default" list — new Habitica tasks that don't carry any
    list-tag end up there. In single-list mode the field is just a
    one-element tuple and the tag is unused.
    """

    credentials_file: Path
    token_file: Path
    tasklists: tuple[TasklistConfig, ...]

    @property
    def is_multi_list(self) -> bool:
        return len(self.tasklists) > 1

    @property
    def default_tasklist(self) -> TasklistConfig:
        return self.tasklists[0]

    # Compat shims for any caller still expecting single-list fields.
    @property
    def tasklist_id(self) -> str | None:
        return self.default_tasklist.tasklist_id

    @property
    def tasklist_title(self) -> str | None:
        return self.default_tasklist.tasklist_title

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

        tasklists = _parse_tasklists(g, pair_index=i)

        pairs.append(
            SyncPair(
                name=name,
                habitica=habitica,
                google=GoogleCreds(
                    credentials_file=creds_file,
                    token_file=token_file,
                    tasklists=tasklists,
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


def _parse_tasklists(g: dict[str, Any], *, pair_index: int) -> tuple[TasklistConfig, ...]:
    """Parse the per-pair google.tasklists list (preferred) or fall back
    to the legacy single-list fields `tasklist_id` / `tasklist_title`.

    Validation:
    - At least one tasklist must be specified.
    - Mixing the legacy fields with `tasklists` raises so the user can't
      end up wondering which one was used.
    - In multi-list mode every entry must have a non-empty `tag` so the
      sync engine can route tasks unambiguously.
    - No two entries may share the same tag (case-insensitive) or the
      same explicit `tasklist_id`/`tasklist_title`.
    """

    multi = g.get("tasklists")
    legacy_id = g.get("tasklist_id")
    legacy_title = g.get("tasklist_title")
    legacy_tag = g.get("tag")
    has_legacy = legacy_id is not None or legacy_title is not None

    if multi is not None and has_legacy:
        raise ConfigError(
            f"pairs[{pair_index}].google: use either `tasklists` (multi-list) "
            f"or `tasklist_id`/`tasklist_title` (legacy single-list), not both."
        )

    if multi is not None:
        if not isinstance(multi, list) or not multi:
            raise ConfigError(f"pairs[{pair_index}].google.tasklists must be a non-empty list")
        entries: list[TasklistConfig] = []
        for j, raw in enumerate(multi):
            if not isinstance(raw, dict):
                raise ConfigError(f"pairs[{pair_index}].google.tasklists[{j}] must be a mapping")
            tid = raw.get("tasklist_id") or raw.get("id")
            ttitle = raw.get("tasklist_title") or raw.get("title")
            tid_s = str(tid).strip() if tid is not None else None
            ttitle_s = str(ttitle).strip() if ttitle is not None else None
            if not tid_s and not ttitle_s:
                raise ConfigError(
                    f"pairs[{pair_index}].google.tasklists[{j}] must set `title` or `tasklist_id`"
                )

            # Tag handling has three states:
            #   - `tag:` key absent → fall back to title (in multi-list mode)
            #   - `tag: <name>` → use the given name verbatim
            #   - `tag: null` or `tag: ""` → opt OUT of tagging; this list
            #     becomes the "untagged sink" for tasks without any
            #     routing tag. Only allowed on the first entry.
            tag_key_set = "tag" in raw
            tag_raw = raw.get("tag")
            tag_s: str | None
            if tag_key_set:
                if tag_raw is None or str(tag_raw).strip() == "":
                    tag_s = None  # explicit untagged
                else:
                    tag_s = str(tag_raw).strip()
            elif len(multi) > 1 and ttitle_s:
                # Convenience: fall back to title for multi-list entries
                # that didn't bother setting `tag`. Preserves the old
                # "tag defaults to title" behaviour.
                tag_s = ttitle_s
            else:
                tag_s = None
            entries.append(TasklistConfig(tasklist_id=tid_s, tasklist_title=ttitle_s, tag=tag_s))

        if len(entries) > 1:
            untagged_positions = [j for j, e in enumerate(entries) if not e.tag]
            if len(untagged_positions) > 1:
                raise ConfigError(
                    f"pairs[{pair_index}].google.tasklists: at most one entry may be "
                    f"untagged (opt out with `tag: null`); got "
                    f"{len(untagged_positions)} untagged entries."
                )
            if untagged_positions and untagged_positions[0] != 0:
                raise ConfigError(
                    f"pairs[{pair_index}].google.tasklists: the untagged tasklist "
                    f"must be the first entry — it acts as the default sink for "
                    f"tasks without a routing tag."
                )
        # Uniqueness checks.
        seen_tags: set[str] = set()
        seen_ids: set[str] = set()
        seen_titles: set[str] = set()
        for j, e in enumerate(entries):
            if e.tag:
                key = e.tag.casefold()
                if key in seen_tags:
                    raise ConfigError(
                        f"pairs[{pair_index}].google.tasklists: duplicate tag {e.tag!r}"
                    )
                seen_tags.add(key)
            if e.tasklist_id:
                if e.tasklist_id in seen_ids:
                    raise ConfigError(
                        f"pairs[{pair_index}].google.tasklists: duplicate tasklist_id "
                        f"{e.tasklist_id!r}"
                    )
                seen_ids.add(e.tasklist_id)
            if e.tasklist_title:
                key = e.tasklist_title.casefold()
                if key in seen_titles:
                    raise ConfigError(
                        f"pairs[{pair_index}].google.tasklists: duplicate tasklist_title "
                        f"{e.tasklist_title!r}"
                    )
                seen_titles.add(key)
        return tuple(entries)

    # Legacy single-list path.
    tid_s = str(legacy_id).strip() if legacy_id is not None else None
    ttitle_s = str(legacy_title).strip() if legacy_title is not None else None
    tag_s = str(legacy_tag).strip() if legacy_tag is not None else None
    return (TasklistConfig(tasklist_id=tid_s, tasklist_title=ttitle_s, tag=tag_s),)


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
