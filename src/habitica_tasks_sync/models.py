"""Internal task representation used to mediate between Habitica and Google Tasks.

The two systems disagree on many fields (Habitica has priority/checklist/tags,
Google has parent/position/etag). The canonical model below holds the subset
both can faithfully round-trip plus a content hash used for change detection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Source(str, Enum):
    HABITICA = "habitica"
    GOOGLE = "google"


@dataclass(frozen=True)
class CanonicalTask:
    """Source-agnostic representation of a single todo item.

    `due_date` is a date-only ISO string (YYYY-MM-DD) because Google Tasks
    discards time-of-day. Habitica accepts datetimes; we normalize to midnight UTC.
    """

    title: str
    notes: str
    completed: bool
    due_date: str | None  # YYYY-MM-DD or None
    completed_at: str | None  # RFC3339 or None
    checklist: tuple[ChecklistItem, ...] = ()

    def content_hash(self) -> str:
        payload = {
            "title": self.title,
            "notes": self.notes,
            "completed": self.completed,
            "due_date": self.due_date,
            "checklist": [
                {"text": c.text, "completed": c.completed} for c in self.checklist
            ],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChecklistItem:
    text: str
    completed: bool


@dataclass
class HabiticaTask:
    """Raw shape of a Habitica `todo` task as returned by the API."""

    id: str
    text: str
    notes: str
    type: str
    completed: bool
    date_completed: str | None  # ISO 8601
    due_date: str | None  # ISO 8601 or None
    checklist: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    priority: float = 1.0
    alias: str | None = None
    challenge_id: str | None = None  # set if task belongs to a challenge
    group_id: str | None = None  # set if task is a group/party task
    tags: list[str] = field(default_factory=list)  # Habitica tag IDs

    @property
    def is_managed_externally(self) -> bool:
        """True if the task is owned by a challenge or group and shouldn't be mutated."""

        return bool(self.challenge_id or self.group_id)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> HabiticaTask:
        challenge = data.get("challenge") or {}
        group = data.get("group") or {}
        return cls(
            id=data.get("id") or data.get("_id", ""),
            text=data.get("text", "") or "",
            notes=data.get("notes", "") or "",
            type=data.get("type", "todo"),
            completed=bool(data.get("completed", False)),
            date_completed=data.get("dateCompleted"),
            due_date=data.get("date"),
            checklist=list(data.get("checklist") or []),
            created_at=data.get("createdAt", "") or "",
            updated_at=data.get("updatedAt", "") or "",
            priority=float(data.get("priority", 1.0) or 1.0),
            alias=data.get("alias"),
            challenge_id=(challenge.get("id") if isinstance(challenge, dict) else None),
            group_id=(group.get("id") if isinstance(group, dict) else None),
            tags=[str(t) for t in (data.get("tags") or []) if t],
        )

    def to_canonical(self) -> CanonicalTask:
        items = tuple(
            ChecklistItem(text=c.get("text", "") or "", completed=bool(c.get("completed", False)))
            for c in self.checklist
        )
        return CanonicalTask(
            title=self.text,
            notes=self.notes,
            completed=self.completed,
            due_date=_iso_to_date(self.due_date),
            completed_at=self.date_completed,
            checklist=items,
        )


@dataclass
class GoogleTask:
    """Raw shape of a Google Tasks task."""

    id: str
    etag: str
    title: str
    notes: str
    status: str  # "needsAction" | "completed"
    due: str | None  # RFC3339, date-only semantics
    completed: str | None  # RFC3339 or None
    updated: str  # RFC3339
    deleted: bool = False
    hidden: bool = False
    parent: str | None = None
    position: str = ""
    # Populated by the client / sync engine; not part of the Google API
    # response. Lets the engine route updates and deletes back to the
    # right list when more than one is configured.
    tasklist_id: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any], *, tasklist_id: str = "") -> GoogleTask:
        return cls(
            id=data.get("id", ""),
            etag=data.get("etag", "") or "",
            title=data.get("title", "") or "",
            notes=data.get("notes", "") or "",
            status=data.get("status", "needsAction") or "needsAction",
            due=data.get("due"),
            completed=data.get("completed"),
            updated=data.get("updated", "") or "",
            deleted=bool(data.get("deleted", False)),
            hidden=bool(data.get("hidden", False)),
            parent=data.get("parent"),
            position=data.get("position", "") or "",
            tasklist_id=tasklist_id,
        )

    def to_canonical(self, checklist: tuple[ChecklistItem, ...] = ()) -> CanonicalTask:
        return CanonicalTask(
            title=self.title,
            notes=self.notes,
            completed=self.status == "completed",
            due_date=_iso_to_date(self.due),
            completed_at=self.completed,
            checklist=checklist,
        )


def _iso_to_date(value: str | None) -> str | None:
    """Normalize any ISO 8601 timestamp (or None) to a YYYY-MM-DD string.

    We deliberately take the date prefix as-is rather than converting
    through UTC. Both Habitica and Google represent due dates as midnight
    in *some* timezone (Google strictly UTC, Habitica varies); converting
    again can shift the visible date by ±1 day for users east/west of UTC.
    The user's UI shows the date prefix unmodified, so mirror that.
    """

    if not value:
        return None
    head = value[:10]
    # Cheap shape validation; reject e.g. "garbage" or non-ISO dates.
    try:
        datetime.strptime(head, "%Y-%m-%d")
    except ValueError:
        return None
    return head


def date_to_google_due(date_iso: str | None) -> str | None:
    """Convert YYYY-MM-DD into the RFC3339 form Google Tasks accepts."""

    if not date_iso:
        return None
    return f"{date_iso}T00:00:00.000Z"


def date_to_habitica_due(date_iso: str | None) -> str | None:
    if not date_iso:
        return None
    return f"{date_iso}T00:00:00.000Z"
