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

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> HabiticaTask:
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

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> GoogleTask:
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
    """Normalize any ISO 8601 timestamp (or None) to a YYYY-MM-DD string."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Some Habitica due dates arrive as "YYYY-MM-DD".
        try:
            parsed = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()


def date_to_google_due(date_iso: str | None) -> str | None:
    """Convert YYYY-MM-DD into the RFC3339 form Google Tasks accepts."""

    if not date_iso:
        return None
    return f"{date_iso}T00:00:00.000Z"


def date_to_habitica_due(date_iso: str | None) -> str | None:
    if not date_iso:
        return None
    return f"{date_iso}T00:00:00.000Z"
