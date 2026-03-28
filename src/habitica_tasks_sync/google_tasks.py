"""Google Tasks v1 client wrapper.

Uses the official `google-api-python-client`. Auth credentials are loaded
from a token JSON produced once by `auth_helper.py` on a machine with a
browser. The container only needs the token file plus the OAuth client
secret, not interactive auth.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import GoogleTask, date_to_google_due

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/tasks"]


class GoogleAuthError(RuntimeError):
    """Token file missing, expired without refresh, or scopes mismatch."""


class GoogleTasksClient:
    def __init__(self, credentials_file: Path, token_file: Path) -> None:
        self._credentials_file = credentials_file
        self._token_file = token_file
        self._lock = threading.RLock()
        self._creds: Credentials = self._load_credentials()
        self._service = build(
            "tasks",
            "v1",
            credentials=self._creds,
            cache_discovery=False,
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._service.close()
            except Exception:
                pass

    def __enter__(self) -> "GoogleTasksClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- credentials ----------------------------------------------------

    def _load_credentials(self) -> Credentials:
        if not self._token_file.exists():
            raise GoogleAuthError(
                f"Google token file not found at {self._token_file}. "
                f"Run `habitica-tasks-sync-auth --client {self._credentials_file} --token {self._token_file}` "
                f"on a machine with a browser to mint one."
            )
        try:
            creds = Credentials.from_authorized_user_file(str(self._token_file), SCOPES)
        except (ValueError, json.JSONDecodeError) as exc:
            raise GoogleAuthError(f"Token file at {self._token_file} is malformed: {exc}") from exc

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self._persist_credentials(creds)
                except RefreshError as exc:
                    raise GoogleAuthError(
                        f"Failed to refresh Google token: {exc}. Re-run the auth helper."
                    ) from exc
            else:
                raise GoogleAuthError(
                    "Google credentials invalid and no refresh token available. Re-run the auth helper."
                )
        return creds

    def _persist_credentials(self, creds: Credentials) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_file.with_suffix(self._token_file.suffix + ".tmp")
        tmp.write_text(creds.to_json(), encoding="utf-8")
        tmp.replace(self._token_file)
        try:
            self._token_file.chmod(0o600)
        except OSError:
            pass

    def _ensure_fresh(self) -> None:
        with self._lock:
            if self._creds.expired and self._creds.refresh_token:
                self._creds.refresh(Request())
                self._persist_credentials(self._creds)

    # --- tasklists ------------------------------------------------------

    def list_tasklists(self) -> list[dict[str, Any]]:
        self._ensure_fresh()
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            resp = self._call(
                self._service.tasklists().list(maxResults=100, pageToken=page_token)
            )
            out.extend(resp.get("items", []) or [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    def get_tasklist(self, tasklist_id: str) -> dict[str, Any] | None:
        self._ensure_fresh()
        try:
            return self._call(self._service.tasklists().get(tasklist=tasklist_id))
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise

    def create_tasklist(self, title: str) -> dict[str, Any]:
        self._ensure_fresh()
        return self._call(self._service.tasklists().insert(body={"title": title}))

    def resolve_tasklist(self, *, tasklist_id: str | None, title: str | None) -> str:
        """Resolve a tasklist to its ID, creating one with `title` if needed.

        Precedence: explicit `tasklist_id` > matching `title` > newly created
        list using `title` > the user's default ("@default").
        """

        if tasklist_id:
            return tasklist_id
        if title:
            for tl in self.list_tasklists():
                if (tl.get("title") or "").strip() == title.strip():
                    return tl["id"]
            log.info("creating google tasklist %r (none matched)", title)
            return self.create_tasklist(title)["id"]
        return "@default"

    # --- tasks ----------------------------------------------------------

    def list_tasks(
        self,
        tasklist: str,
        *,
        updated_min: str | None = None,
        show_completed: bool = True,
        show_deleted: bool = True,
        show_hidden: bool = True,
    ) -> list[GoogleTask]:
        self._ensure_fresh()
        out: list[GoogleTask] = []
        page_token: str | None = None
        while True:
            req = self._service.tasks().list(
                tasklist=tasklist,
                maxResults=100,
                pageToken=page_token,
                showCompleted=show_completed,
                showDeleted=show_deleted,
                showHidden=show_hidden,
                updatedMin=updated_min,
            )
            resp = self._call(req)
            for item in resp.get("items", []) or []:
                out.append(GoogleTask.from_api(item))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    def get_task(self, tasklist: str, task_id: str) -> GoogleTask | None:
        self._ensure_fresh()
        try:
            data = self._call(self._service.tasks().get(tasklist=tasklist, task=task_id))
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise
        return GoogleTask.from_api(data)

    def insert_task(
        self,
        tasklist: str,
        *,
        title: str,
        notes: str = "",
        due_date_iso: str | None = None,
        completed: bool = False,
    ) -> GoogleTask:
        self._ensure_fresh()
        body: dict[str, Any] = {
            "title": title or "(untitled)",
            "notes": notes or "",
            "status": "completed" if completed else "needsAction",
        }
        if due_date_iso:
            body["due"] = date_to_google_due(due_date_iso)
        if completed:
            body["completed"] = _now_rfc3339()
        data = self._call(self._service.tasks().insert(tasklist=tasklist, body=body))
        return GoogleTask.from_api(data)

    def patch_task(
        self,
        tasklist: str,
        task_id: str,
        *,
        title: str | None = None,
        notes: str | None = None,
        due_date_iso: str | None = None,
        clear_due: bool = False,
        completed: bool | None = None,
    ) -> GoogleTask:
        self._ensure_fresh()
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if notes is not None:
            body["notes"] = notes
        if clear_due:
            body["due"] = None
        elif due_date_iso is not None:
            body["due"] = date_to_google_due(due_date_iso)
        if completed is True:
            body["status"] = "completed"
            body["completed"] = _now_rfc3339()
        elif completed is False:
            body["status"] = "needsAction"
            body["completed"] = None
        if not body:
            current = self.get_task(tasklist, task_id)
            if current is None:
                raise HttpError(_FakeResp(404), b"")
            return current
        data = self._call(self._service.tasks().patch(tasklist=tasklist, task=task_id, body=body))
        return GoogleTask.from_api(data)

    def delete_task(self, tasklist: str, task_id: str) -> bool:
        self._ensure_fresh()
        try:
            self._call(self._service.tasks().delete(tasklist=tasklist, task=task_id))
            return True
        except HttpError as exc:
            if exc.resp.status == 404:
                return False
            raise

    # --- helpers --------------------------------------------------------

    def _call(self, request: Any) -> Any:
        """Execute a discovery `request`, retrying on 429/5xx with backoff."""

        delay = 1.0
        for attempt in range(6):
            try:
                return request.execute()
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                if status in (429, 500, 502, 503, 504) and attempt < 5:
                    retry_after = float(exc.resp.get("retry-after", "0") or 0) or delay
                    log.warning("google tasks %s; retrying in %.1fs", status, retry_after)
                    time.sleep(retry_after)
                    delay = min(delay * 2, 30)
                    continue
                raise
        raise RuntimeError("unreachable: google retry loop exited without raising")


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def overlap_window(now: datetime, *, minutes: int = 5) -> str:
    """Return an `updatedMin` value `minutes` before `now` to absorb clock skew."""

    cursor = now.astimezone(timezone.utc) - timedelta(minutes=minutes)
    return cursor.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _FakeResp:
    """Stand-in for googleapiclient's response object when raising HttpError manually."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Not Found" if status == 404 else "Error"

    def get(self, key: str, default: Any = None) -> Any:
        return default
