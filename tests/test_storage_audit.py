"""Regression tests from the storage audit. Each is xfail(strict) until the defect is fixed."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from bot.history import History, HistoryEntry
from tests.conftest import FakeClock
from tests.test_queue import until


@pytest.mark.xfail(strict=True, reason="History.snapshot drops live lines stamped after a backward wall-clock step")
def test_backward_wall_clock_step_keeps_live_history():
    clock = FakeClock()
    history = History(20, max_age_s=3600, clock=clock)
    history.append(HistoryEntry("Alice", "first"))
    clock.advance(1)
    history.append(HistoryEntry("Alice", "second"))
    clock.advance(-2)  # an NTP correction after sleep, or a manual clock change
    assert [e.text for e in history.entries()] == ["first", "second"]


@pytest.mark.xfail(strict=True, reason="_save_state only catches StateError; anything else ends the periodic saver silently")
async def test_periodic_saver_survives_an_unexpected_exception(harness, tmp_path, monkeypatch):
    h = harness(state_db=str(tmp_path / "state.sqlite3"), fortune_enabled=False, state_save_interval_s=0.01)
    h.service._announce_startup = AsyncMock()
    await h.service.start()
    store = h.service._state_store
    real_save = store.save
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unexpected failure inside save")
        return real_save(*args)

    monkeypatch.setattr(store, "save", flaky)
    try:
        await h.say("Alice: Hi")
        await until(lambda: len(calls) >= 1)
        await asyncio.sleep(0.05)
        assert not h.service._state_task.done()
        assert any(r["event"] == "state_error" for r in h.records)
    finally:
        await h.service.stop()
