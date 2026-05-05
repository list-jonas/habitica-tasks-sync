from __future__ import annotations

from habitica_tasks_sync.models import (
    CanonicalTask,
    ChecklistItem,
    GoogleTask,
    HabiticaTask,
    _iso_to_date,
    date_to_google_due,
    date_to_habitica_due,
)


def test_iso_to_date_takes_prefix_verbatim():
    assert _iso_to_date("2026-05-16T07:00:00.000Z") == "2026-05-16"
    assert _iso_to_date("2026-05-16") == "2026-05-16"
    assert _iso_to_date(None) is None
    assert _iso_to_date("") is None
    assert _iso_to_date("garbage") is None


def test_date_helpers_round_trip():
    assert date_to_google_due("2026-05-16") == "2026-05-16T00:00:00.000Z"
    assert date_to_habitica_due("2026-05-16") == "2026-05-16T00:00:00.000Z"
    assert date_to_google_due(None) is None
    assert date_to_habitica_due(None) is None


def test_habitica_from_api_captures_challenge():
    raw = {
        "id": "abc",
        "text": "Buy milk",
        "type": "todo",
        "completed": False,
        "dateCompleted": None,
        "date": None,
        "challenge": {"id": "ch1"},
        "group": {},
    }
    h = HabiticaTask.from_api(raw)
    assert h.is_managed_externally is True
    assert h.challenge_id == "ch1"


def test_habitica_to_canonical():
    h = HabiticaTask(
        id="x", text="t", notes="n", type="todo",
        completed=True, date_completed="2026-05-16T10:00:00Z",
        due_date="2026-05-20T00:00:00Z",
        checklist=[{"id": "c1", "text": "sub", "completed": False}],
    )
    c = h.to_canonical()
    assert c.title == "t"
    assert c.notes == "n"
    assert c.completed is True
    assert c.due_date == "2026-05-20"
    assert c.checklist == (ChecklistItem(text="sub", completed=False),)


def test_google_to_canonical():
    g = GoogleTask(
        id="g1", etag="e", title="T", notes="N",
        status="completed", due="2026-05-16T00:00:00.000Z",
        completed="2026-05-16T10:00:00Z", updated="2026-05-16T11:00:00Z",
    )
    c = g.to_canonical()
    assert c.title == "T"
    assert c.completed is True
    assert c.due_date == "2026-05-16"


def test_content_hash_stable():
    a = CanonicalTask(title="x", notes="n", completed=False, due_date="2026-05-16", completed_at=None)
    b = CanonicalTask(title="x", notes="n", completed=False, due_date="2026-05-16", completed_at=None)
    c = CanonicalTask(title="x", notes="DIFF", completed=False, due_date="2026-05-16", completed_at=None)
    assert a.content_hash() == b.content_hash()
    assert a.content_hash() != c.content_hash()
