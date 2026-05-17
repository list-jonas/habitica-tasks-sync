"""Habitica client unit tests covering the rate limiter + helpers."""

from __future__ import annotations

import time

from habitica_tasks_sync.habitica import _RateLimiter, _parse_rate_limit_reset


def test_rate_limiter_no_sleep_under_budget(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    rl = _RateLimiter(max_per_window=5, window_seconds=60.0)
    for _ in range(5):
        rl.acquire()
    assert slept == []


def test_rate_limiter_sleeps_when_window_full(monkeypatch):
    slept: list[float] = []
    # Freeze monotonic so the 6th acquire is guaranteed to be inside the window.
    fake_t = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_t[0])
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    rl = _RateLimiter(max_per_window=3, window_seconds=60.0)
    rl.acquire()
    fake_t[0] += 5
    rl.acquire()
    fake_t[0] += 5
    rl.acquire()
    fake_t[0] += 5  # 15s after first ts, well inside the 60s window
    rl.acquire()
    # We expect one sleep of roughly 60 - 15 = 45s.
    assert len(slept) == 1
    assert 44.0 < slept[0] < 46.0


def test_rate_limit_reset_parses_epoch_seconds():
    soon = time.time() + 30
    assert 29 < _parse_rate_limit_reset(str(soon)) < 31


def test_rate_limit_reset_parses_epoch_millis():
    soon_ms = (time.time() + 30) * 1000
    parsed = _parse_rate_limit_reset(str(soon_ms))
    assert parsed is not None and 29 < parsed < 31


def test_rate_limit_reset_parses_seconds_from_now():
    assert _parse_rate_limit_reset("30") == 30.0


def test_rate_limit_reset_rejects_bogus():
    assert _parse_rate_limit_reset(None) is None
    assert _parse_rate_limit_reset("") is None
    assert _parse_rate_limit_reset("garbage") is None
    # Already in the past (≥ 120s ago).
    past = time.time() - 1000
    assert _parse_rate_limit_reset(str(past)) is None
