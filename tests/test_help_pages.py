"""One command, two bounded and independently rate-limited transmissions."""

import asyncio

import pytest

from bot.guard import Verdict
from bot.service import Decision
from tests.test_queue import until


@pytest.mark.parametrize("queued", [False, True])
async def test_second_page_waits_for_sender_token_without_losing_fifo(harness, clock, queued):
    h = harness(queue_max_pending=10 if queued else 0, global_burst=5,
                sender_burst=1, sender_rate_per_min=1)
    h.service.queue_tick_s = 0.001
    help_task = asyncio.create_task(h.say("Alice: /help"))
    await until(lambda: len(h.sent) == 1)
    assert h.limiter.snapshot()["global_tokens"] == 4
    other = asyncio.create_task(h.say("Bob: hello"))
    try:
        if queued:
            await until(lambda: h.service.stats.queue_depth == 1)
        else:
            assert await other is Decision.DROP_RATE_LIMITED
        clock.advance(59)
        await asyncio.sleep(0.01)
        assert len(h.sent) == 1 and not h.backend.calls
        clock.advance(1)
        assert await asyncio.wait_for(help_task, 1) is Decision.ANSWERED_HELP
        assert [text for _, text in h.sent[:2]] == list(h.cfg.help_pages)
        if queued:
            assert await asyncio.wait_for(other, 1) is Decision.ANSWERED
    finally:
        await h.service.stop()


async def test_second_page_obeys_utilization_pause(harness, clock):
    h = harness(queue_max_pending=10)
    h.service.queue_tick_s = 0.001
    task = asyncio.create_task(h.say("Alice: /help"))
    try:
        await until(lambda: len(h.sent) == 1)
        h.limiter.set_global_factor(0)
        clock.advance(60)
        await asyncio.sleep(0.01)
        assert len(h.sent) == 1 and not task.done()
        h.limiter.set_global_factor(1)
        clock.advance(60)
        assert await asyncio.wait_for(task, 1) is Decision.ANSWERED_HELP
        assert len(h.sent) == 2
    finally:
        await h.service.stop()


async def test_second_page_gate_block_spends_no_first_token(harness):
    class Gate:
        def check(self, text):
            return Verdict("2/2" in text, 1.0, (), text)

    h = harness(gate=Gate())
    assert await h.say("Alice: /help") is Decision.DROP_INJECTION
    assert not h.sent and not h.backend.calls
    assert h.limiter.snapshot()["global_tokens"] == 1


async def test_second_page_rechecked_after_first_page(harness, clock, monkeypatch):
    h = harness()
    h.service.queue_tick_s = 0.001
    task = asyncio.create_task(h.say("Alice: /help"))
    try:
        await until(lambda: len(h.sent) == 1)

        def broken(*args, **kwargs):
            raise RuntimeError("detector unavailable")

        monkeypatch.setattr("bot.guard.detect_prompt_injection", broken)
        clock.advance(60)
        assert await asyncio.wait_for(task, 1) is Decision.DROP_INJECTION
        assert len(h.sent) == 1
        assert h.limiter.snapshot()["global_tokens"] == 1
    finally:
        await h.service.stop()


async def test_second_page_wait_is_bounded(harness, clock):
    h = harness(queue_wait_s=10)
    h.service.queue_tick_s = 0.001
    task = asyncio.create_task(h.say("Alice: /help"))
    try:
        await until(lambda: len(h.sent) == 1)
        clock.advance(11)
        assert await asyncio.wait_for(task, 1) is Decision.DROP_QUEUE_EXPIRED
        assert len(h.sent) == 1 and not h.service._requests
        assert h.inbound_records()[-1]["pages_sent"] == 1
    finally:
        await h.service.stop()


async def test_shutdown_cancels_pending_second_page(harness):
    h = harness()
    task = asyncio.create_task(h.say("Alice: /help"))
    await until(lambda: len(h.sent) == 1)
    await h.service.stop()
    assert task.cancelled()
    assert len(h.sent) == 1 and not h.service._requests


async def test_first_page_send_failure_stops_the_pair(harness):
    h = harness(global_burst=2, sender_burst=2)
    h.mc.commands.raise_on_send = RuntimeError("radio disconnected")
    assert await h.say("Alice: /help") is Decision.DROP_SEND_FAILED
    assert not h.sent and not h.backend.calls
    assert h.service.stats.send_errors == 1
    assert h.inbound_records()[-1]["pages_sent"] == 0
    # The uncertain attempt is charged, but page two was never reserved.
    assert h.limiter.snapshot()["global_tokens"] == 1
