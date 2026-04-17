"""Thin Habitica REST client scoped to the endpoints the sync uses.

Habitica's v3 API:
- Base URL  https://habitica.com/api/v3
- Auth      x-api-user (UUID), x-api-key (UUID)
- Identify  x-client (developer-UUID-AppName)  — required for third-party tools
- Limit     30 requests / 60 s per (user, IP)

Only `todo` tasks are touched. Habits, dailies and rewards are intentionally
ignored: they map awkwardly onto Google Tasks (which only knows todos).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .models import HabiticaTask, date_to_habitica_due

log = logging.getLogger(__name__)

BASE_URL = "https://habitica.com/api/v3"


class HabiticaError(RuntimeError):
    """Any non-recoverable error coming back from Habitica."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HabiticaRateLimitError(HabiticaError):
    """429 from Habitica. Honor `Retry-After` if present."""

    def __init__(self, message: str, retry_after: float, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


_RETRYABLE_NETWORK = (
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
)


def _habitica_wait_strategy(retry_state: Any) -> float:
    """Honor `Retry-After` from 429s; otherwise exponential jitter backoff."""

    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, HabiticaRateLimitError):
        # Cap to 60 s so a misbehaving server can't hang us indefinitely.
        return min(max(exc.retry_after, 1.0), 60.0)
    base = wait_exponential_jitter(initial=1, max=30)
    return base(retry_state)


class HabiticaClient:
    def __init__(
        self,
        user_id: str,
        api_token: str,
        *,
        client_uuid: str,
        app_name: str,
        timeout: float = 30.0,
        base_url: str = BASE_URL,
    ) -> None:
        if not user_id or not api_token:
            raise HabiticaError("Habitica user_id and api_token are required")
        self._user_id = user_id
        # `x-client` is parsed by Habitica as `<UUID>-<AppName>` on the
        # FIRST hyphen after the UUID; spaces or commas in the app name
        # have triggered 400s in past Habitica versions, so sanitize.
        safe_app = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in app_name) or "habitica-tasks-sync"
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "x-api-user": user_id,
                "x-api-key": api_token,
                "x-client": f"{client_uuid}-{safe_app}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"{safe_app}/0.1 (+https://github.com/)",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "HabiticaClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def user_id(self) -> str:
        return self._user_id

    # --- low-level ------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((HabiticaRateLimitError, *_RETRYABLE_NETWORK)),
        wait=_habitica_wait_strategy,
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _request(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self._http.request(method, path, json=json, params=params)
        except _RETRYABLE_NETWORK as exc:
            log.warning("habitica network error on %s %s: %s", method, path, exc)
            raise

        if resp.status_code == 429:
            # Honor Retry-After when present; default to 5 s. We only stash
            # the value on the exception — the actual sleep happens in the
            # custom wait strategy so we don't double-sleep.
            retry_after = float(resp.headers.get("Retry-After", "5") or 5)
            log.warning("habitica rate limited; will sleep %.1fs before retry", retry_after)
            raise HabiticaRateLimitError(
                "Habitica rate limit exceeded",
                retry_after=retry_after,
                status=429,
                body=_safe_json(resp),
            )

        if resp.status_code >= 500:
            # 5xx is transient — let tenacity retry by raising a network-class error.
            log.warning("habitica %s on %s %s", resp.status_code, method, path)
            raise httpx.RemoteProtocolError(f"Habitica {resp.status_code}")

        if resp.status_code >= 400:
            body = _safe_json(resp)
            msg = (body or {}).get("message") or (body or {}).get("error") or resp.text
            raise HabiticaError(
                f"Habitica {method} {path} failed: {resp.status_code}: {msg}",
                status=resp.status_code,
                body=body,
            )

        if resp.status_code == 204 or not resp.content:
            return None
        body = resp.json()
        if isinstance(body, dict) and body.get("success") is False:
            raise HabiticaError(
                f"Habitica {method} {path}: {body.get('message') or body.get('error')}",
                status=resp.status_code,
                body=body,
            )
        return body.get("data") if isinstance(body, dict) else body

    # --- todos ----------------------------------------------------------

    def list_todos(self, *, include_completed: bool = True) -> list[HabiticaTask]:
        """Return active todos, plus the last 30 completed todos if requested."""

        active = self._request("GET", "/tasks/user", params={"type": "todos"}) or []
        tasks = [HabiticaTask.from_api(t) for t in active]
        if include_completed:
            done = self._request("GET", "/tasks/user", params={"type": "completedTodos"}) or []
            tasks.extend(HabiticaTask.from_api(t) for t in done)
        return tasks

    def get_todo(self, task_id: str) -> HabiticaTask | None:
        try:
            data = self._request("GET", f"/tasks/{task_id}")
        except HabiticaError as exc:
            if exc.status == 404:
                return None
            raise
        if data is None:
            return None
        return HabiticaTask.from_api(data)

    def create_todo(
        self,
        *,
        text: str,
        notes: str = "",
        due_date_iso: str | None = None,
        checklist: Iterable[dict[str, Any]] = (),
        alias: str | None = None,
    ) -> HabiticaTask:
        body: dict[str, Any] = {"type": "todo", "text": text or "(untitled)", "notes": notes or ""}
        if due_date_iso:
            body["date"] = date_to_habitica_due(due_date_iso)
        cl = [_sanitize_checklist_item(c) for c in checklist]
        if cl:
            body["checklist"] = cl
        if alias:
            body["alias"] = alias
        data = self._request("POST", "/tasks/user", json=body)
        return HabiticaTask.from_api(data)

    def update_todo(
        self,
        task_id: str,
        *,
        text: str | None = None,
        notes: str | None = None,
        due_date_iso: str | None = None,
        clear_due: bool = False,
    ) -> HabiticaTask:
        body: dict[str, Any] = {}
        if text is not None:
            body["text"] = text
        if notes is not None:
            body["notes"] = notes
        if clear_due:
            body["date"] = None
        elif due_date_iso is not None:
            body["date"] = date_to_habitica_due(due_date_iso)
        if not body:
            current = self.get_todo(task_id)
            if current is None:
                raise HabiticaError(f"task {task_id} not found", status=404)
            return current
        data = self._request("PUT", f"/tasks/{task_id}", json=body)
        return HabiticaTask.from_api(data)

    def score_todo(self, task_id: str, *, complete: bool) -> None:
        direction = "up" if complete else "down"
        self._request("POST", f"/tasks/{task_id}/score/{direction}")

    def delete_todo(self, task_id: str) -> bool:
        try:
            self._request("DELETE", f"/tasks/{task_id}")
            return True
        except HabiticaError as exc:
            if exc.status == 404:
                return False
            raise

    # --- checklist ------------------------------------------------------

    def add_checklist_item(self, task_id: str, *, text: str, completed: bool = False) -> HabiticaTask:
        data = self._request(
            "POST",
            f"/tasks/{task_id}/checklist",
            json={"text": text, "completed": completed},
        )
        return HabiticaTask.from_api(data)

    def update_checklist_item(
        self, task_id: str, item_id: str, *, text: str | None = None, completed: bool | None = None
    ) -> HabiticaTask:
        body: dict[str, Any] = {}
        if text is not None:
            body["text"] = text
        if completed is not None:
            body["completed"] = completed
        if not body:
            return self.get_todo(task_id) or HabiticaTask(
                id=task_id, text="", notes="", type="todo",
                completed=False, date_completed=None, due_date=None,
            )
        data = self._request("PUT", f"/tasks/{task_id}/checklist/{item_id}", json=body)
        return HabiticaTask.from_api(data)

    def delete_checklist_item(self, task_id: str, item_id: str) -> HabiticaTask | None:
        try:
            data = self._request("DELETE", f"/tasks/{task_id}/checklist/{item_id}")
        except HabiticaError as exc:
            if exc.status == 404:
                return None
            raise
        if data is None:
            return None
        return HabiticaTask.from_api(data)


def _sanitize_checklist_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": str(item.get("text", "") or ""),
        "completed": bool(item.get("completed", False)),
    }


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None
