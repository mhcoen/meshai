"""Channel-utilization monitor: policy, sampling, limiter coupling, error handling."""

import asyncio
import io

import pytest
from meshcore import EventType
from meshcore.events import Event

from bot.jsonlog import EventLog
from bot.ratelimit import RateLimiter
from bot.utilization import UtilizationMonitor, next_level
from tests.conftest import FakeClock, FakeMeshCore

LOW, HIGH = 0.05, 0.15


# ----------------------------------------------------------------------------- policy


@pytest.mark.parametrize(
    "duty,current,expected",
    [
        (0.00, "full", "full"),
        (0.049, "full", "full"),
        (0.05, "full", "half"),      # tighten at the threshold
        (0.10, "full", "half"),
        (0.15, "full", "paused"),    # tighten straight past half
        (0.50, "half", "paused"),
        (0.10, "half", "half"),      # in band: hold
        (0.045, "half", "half"),     # below low but above 80% of it: hold (hysteresis)
        (0.039, "half", "full"),     # below 80% of low: relax
        (0.13, "paused", "paused"),  # below high but above 80% of it: hold
        (0.119, "paused", "half"),   # relax one step only, even if far below
        (0.00, "paused", "half"),
    ],
)
def test_next_level(duty, current, expected):
    assert next_level(duty, current, LOW, HIGH) == expected


# ----------------------------------------------------------------------------- sampling


class StatsMeshCore(FakeMeshCore):
    """FakeMeshCore plus scripted radio/packet counters."""

    def __init__(self):
        super().__init__()
        self.tx_air = 0
        self.rx_air = 0
        self.recv = 0
        self.sent = 0
        self.fail_radio = False
        self.commands.get_stats_radio = self._get_stats_radio
        self.commands.get_stats_packets = self._get_stats_packets

    async def _get_stats_radio(self):
        if self.fail_radio:
            return Event(EventType.ERROR, {"reason": "timeout"})
        return Event(
            EventType.STATS_RADIO,
            {"noise_floor": -110, "last_rssi": -85, "last_snr": 7.5, "tx_air_secs": self.tx_air, "rx_air_secs": self.rx_air},
        )

    async def _get_stats_packets(self):
        return Event(
            EventType.STATS_PACKETS,
            {"recv": self.recv, "sent": self.sent, "flood_tx": 0, "direct_tx": 0, "flood_rx": 0, "direct_rx": 0},
        )


def make_monitor(clock, window_s=20.0, poll_s=10.0):
    """Default window of 20 s means the policy acts once 10 s of data are in hand."""
    mc = StatsMeshCore()
    limiter = RateLimiter(2.0, 1, 2.0, 1, clock=clock)
    records = []
    log = EventLog(stream=io.StringIO())
    log.subscribe(records.append)
    mon = UtilizationMonitor(mc, limiter, log, poll_s=poll_s, window_s=window_s, duty_low=LOW, duty_high=HIGH, clock=clock)
    return mc, limiter, mon, records


async def test_first_sample_has_nothing_to_compare():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    assert await mon.sample() is None
    assert mon.level == "full" and limiter.global_factor == 1.0


async def test_duty_cycle_is_received_airtime_only_and_tx_is_reported():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    await mon.sample()
    clock.advance(10)
    mc.tx_air, mc.rx_air, mc.recv, mc.sent = 1, 2, 4, 1  # 2 s heard, 1 s sent, in 10 s
    u = await mon.sample()
    assert u.duty == pytest.approx(0.2)
    assert u.tx_duty == pytest.approx(0.1)
    assert u.packets_per_min == pytest.approx(30.0)
    assert u.window_s == pytest.approx(10.0)
    assert (u.noise_floor, u.last_rssi, u.last_snr) == (-110, -85, 7.5)
    assert u.level == "paused" and u.factor == 0.0
    assert limiter.global_factor == 0.0
    rec = [r for r in records if r["event"] == "utilization"][-1]
    assert rec["level"] == "paused" and rec["level_changed"] is True and rec["tx_duty"] == 0.1
    assert any(r["event"] == "rate_level" and (r["old"], r["new"]) == ("full", "paused") for r in records)


async def test_own_transmissions_never_throttle_the_bot():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    await mon.sample()
    clock.advance(10)
    mc.tx_air = 5  # 50% of the window is our own TX; nothing heard
    u = await mon.sample()
    assert u.duty == 0.0 and u.tx_duty == pytest.approx(0.5)
    assert u.level == "full" and limiter.global_factor == 1.0


def make_budget_monitor(clock, budget=0.02, window_s=20.0):
    mc = StatsMeshCore()
    limiter = RateLimiter(4.0, 1, 4.0, 1, clock=clock)
    records = []
    log = EventLog(stream=io.StringIO())
    log.subscribe(records.append)
    mon = UtilizationMonitor(mc, limiter, log, poll_s=10.0, window_s=window_s, duty_low=LOW, duty_high=HIGH,
                             clock=clock, tx_budget=budget)
    return mc, limiter, mon, records


async def test_own_transmit_budget_halves_the_rate_when_exceeded():
    clock = FakeClock()
    mc, limiter, mon, records = make_budget_monitor(clock, budget=0.05)
    await mon.sample()
    clock.advance(20)
    mc.tx_air = 1  # 1 s of our own airtime in 20 s = 5%: at the budget
    u = await mon.sample()
    assert u.tx_duty == pytest.approx(0.05)
    assert u.level == "half" and u.reason == "tx"
    assert limiter.global_factor == 0.5
    assert [r for r in records if r["event"] == "rate_level"][-1]["reason"] == "tx"


async def test_own_transmit_at_twice_the_budget_pauses():
    clock = FakeClock()
    mc, limiter, mon, records = make_budget_monitor(clock, budget=0.05)
    await mon.sample()
    clock.advance(20)
    mc.tx_air = 2  # 10% = 2 x budget
    u = await mon.sample()
    assert u.level == "paused" and u.reason == "tx"
    assert limiter.global_factor == 0.0


async def test_the_more_restrictive_policy_wins():
    clock = FakeClock()
    mc, limiter, mon, records = make_budget_monitor(clock, budget=0.05)
    await mon.sample()
    clock.advance(20)
    mc.rx_air = 4  # 20% received: paused by rx
    mc.tx_air = 1  # 5% own: half by tx
    u = await mon.sample()
    assert u.level == "paused" and u.reason == "rx"


async def test_budget_relaxes_with_the_same_hysteresis():
    clock = FakeClock()
    mc, limiter, mon, records = make_budget_monitor(clock, budget=0.05, window_s=10.0)
    await mon.sample()
    clock.advance(10)
    mc.tx_air = 1  # 10% -> paused
    assert (await mon.sample()).level == "paused"
    clock.advance(10)  # window slides past the burst
    assert (await mon.sample()).level == "half"
    clock.advance(10)
    assert (await mon.sample()).level == "full"
    assert limiter.global_factor == 1.0


async def test_no_action_until_half_the_window_is_in_hand():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, window_s=60.0)  # acts only from 30 s of data
    await mon.sample()
    clock.advance(10)
    mc.rx_air = 2  # 20% over 10 s: would pause, but the window is too short to trust
    u = await mon.sample()
    assert u.duty == pytest.approx(0.2)
    assert u.level == "full" and limiter.global_factor == 1.0
    clock.advance(10)
    assert (await mon.sample()).level == "full"  # 20 s: still waiting
    clock.advance(10)
    u = await mon.sample()  # 30 s of data: 2 s / 30 s = 6.7% -> half
    assert u.level == "half" and limiter.global_factor == 0.5


async def test_window_slides_and_old_samples_drop_out():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, window_s=20.0, poll_s=10.0)
    await mon.sample()
    clock.advance(10)
    mc.rx_air = 5  # a 5 s burst: 50% over the first 10 s
    assert (await mon.sample()).level == "paused"
    for _ in range(5):  # quiet for 50 s; the burst leaves the 20 s window
        clock.advance(10)
        u = await mon.sample()
    assert u.duty == pytest.approx(0.0)
    assert u.window_s <= 20.0


async def test_relaxes_one_level_per_poll_with_hysteresis():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, window_s=10.0)
    await mon.sample()
    clock.advance(10)
    mc.rx_air = 2  # 20% -> paused
    assert (await mon.sample()).level == "paused"
    clock.advance(10)  # window now covers only the last 10 s, with zero new airtime
    assert (await mon.sample()).level == "half"  # one step only
    assert limiter.global_factor == 0.5
    clock.advance(10)
    assert (await mon.sample()).level == "full"
    assert limiter.global_factor == 1.0


async def test_paused_level_actually_stops_replies_and_resumes(clock):
    mc, limiter, mon, records = make_monitor(clock)
    assert limiter.allow("alice").allowed  # spend the single burst token
    await mon.sample()
    clock.advance(10)
    mc.rx_air = 3  # 30%: paused
    await mon.sample()
    clock.advance(600)
    assert not limiter.allow("bob").allowed  # no refill while paused, even after 10 minutes
    await mon.sample()  # after a gap longer than the window there is nothing to compare: stays paused
    assert mon.level == "paused"
    clock.advance(10)
    await mon.sample()  # 10 quiet seconds on record: relax one step
    assert mon.level == "half"
    clock.advance(60)
    assert limiter.allow("bob").allowed  # half rate: 1/min, so a token after 60 s


async def test_counter_reset_restarts_the_window():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    mc.rx_air = 100
    await mon.sample()
    clock.advance(10)
    mc.rx_air = 1  # radio rebooted; counters went backwards
    u = await mon.sample()
    assert u is None  # window restarted, nothing to compare yet
    assert mon.level == "full"


async def test_stats_error_keeps_last_level_and_logs():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock)
    await mon.sample()
    clock.advance(10)
    mc.rx_air = 2
    await mon.sample()
    assert mon.level == "paused"
    mc.fail_radio = True
    clock.advance(10)
    assert (await mon.sample()).level == "paused"  # unchanged
    assert mon.errors == 1
    assert [r for r in records if r["event"] == "utilization_error"][0]["command"] == "get_stats_radio"


async def test_exception_in_poll_does_not_kill_the_loop():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, poll_s=0.001)

    async def boom():
        raise RuntimeError("serial hiccup")

    mc.commands.get_stats_radio = boom
    mon.start()
    await asyncio.sleep(0.02)
    await mon.stop()
    assert mon.errors >= 1
    assert any(r["event"] == "utilization_error" and "serial hiccup" in r["error"] for r in records)
    assert mon.level == "full" and limiter.global_factor == 1.0  # stop() restores full rate


async def test_start_and_stop_are_idempotent():
    clock = FakeClock()
    mc, limiter, mon, records = make_monitor(clock, poll_s=0.001)
    mon.start()
    task = mon._task
    mon.start()
    assert mon._task is task
    await mon.stop()
    await mon.stop()
    assert mon._task is None
