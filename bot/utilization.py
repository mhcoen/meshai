"""Channel-utilization monitor: polls the radio's airtime and packet counters and scales
the global reply rate down when the channel is busy.

Signals, all from the companion via the library:
  get_stats_radio   -> tx_air_secs, rx_air_secs (cumulative, integer seconds), noise_floor,
                       last_rssi, last_snr
  get_stats_packets -> recv, sent, flood_rx, flood_tx, direct_rx, direct_tx (cumulative)

Duty cycle over the window = (delta tx_air + delta rx_air) / elapsed. Airtime is what this
radio heard or sent, so a busy channel it cannot hear does not register; that is inherent
to a node-local measurement. Counters are integer seconds, so with a 60 s window the
resolution is about 1.7 percentage points.

Policy with hysteresis:
  duty <  duty_low                -> "full"   factor 1.0
  duty_low <= duty < duty_high    -> "half"   factor 0.5
  duty >= duty_high               -> "paused" factor 0.0
Tightening happens as soon as a threshold is crossed. Relaxing happens one level per
poll and only once duty has fallen below 80% of the threshold that level guards, so a
channel hovering at a threshold does not flap.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meshcore import EventType

from bot.jsonlog import EventLog
from bot.ratelimit import RateLimiter

LEVELS = ("full", "half", "paused")
FACTORS = {"full": 1.0, "half": 0.5, "paused": 0.0}
RELAX_MARGIN = 0.8


@dataclass(frozen=True)
class Sample:
    t: float
    tx_air: int
    rx_air: int
    recv: int
    sent: int
    noise_floor: int | None
    last_rssi: int | None
    last_snr: float | None


@dataclass(frozen=True)
class Utilization:
    duty: float
    packets_per_min: float
    window_s: float
    noise_floor: int | None
    last_rssi: int | None
    last_snr: float | None
    level: str
    factor: float


def next_level(duty: float, current: str, duty_low: float, duty_high: float) -> str:
    """Pure policy step: where to go from ``current`` given this window's duty cycle."""
    target = "paused" if duty >= duty_high else "half" if duty >= duty_low else "full"
    cur_i, tgt_i = LEVELS.index(current), LEVELS.index(target)
    if tgt_i >= cur_i:
        return target  # tighten immediately (or stay)
    guard = duty_high if current == "paused" else duty_low
    if duty < guard * RELAX_MARGIN:
        return LEVELS[cur_i - 1]  # relax one step
    return current


class UtilizationMonitor:
    def __init__(
        self,
        meshcore: Any,
        limiter: RateLimiter,
        log: EventLog,
        poll_s: float,
        window_s: float,
        duty_low: float,
        duty_high: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.mc = meshcore
        self.limiter = limiter
        self.log = log
        self.poll_s = poll_s
        self.window_s = window_s
        self.duty_low = duty_low
        self.duty_high = duty_high
        self._clock = clock
        self._samples: deque[Sample] = deque()
        self.level = "full"
        self.current: Utilization | None = None
        self.polls = 0
        self.errors = 0
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="utilization-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._set_level("full", duty=0.0)

    async def _run(self) -> None:
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let the monitor kill the bot
                self.errors += 1
                self.log.emit("utilization_error", error=f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self.poll_s)

    # ------------------------------------------------------------------ sampling

    async def sample(self) -> Utilization | None:
        """Poll both counters once, update the window, apply the policy. Returns the reading."""
        self.polls += 1
        radio = await self.mc.commands.get_stats_radio()
        packets = await self.mc.commands.get_stats_packets()
        for name, res in (("get_stats_radio", radio), ("get_stats_packets", packets)):
            if res is None or res.type == EventType.ERROR:
                self.errors += 1
                self.log.emit("utilization_error", command=name, error=str(getattr(res, "payload", None)))
                return self.current
        r, p = radio.payload or {}, packets.payload or {}
        sample = Sample(
            t=self._clock(),
            tx_air=int(r.get("tx_air_secs", 0)),
            rx_air=int(r.get("rx_air_secs", 0)),
            recv=int(p.get("recv", 0)),
            sent=int(p.get("sent", 0)),
            noise_floor=r.get("noise_floor"),
            last_rssi=r.get("last_rssi"),
            last_snr=r.get("last_snr"),
        )
        if self._samples and (
            sample.tx_air < self._samples[-1].tx_air
            or sample.rx_air < self._samples[-1].rx_air
            or sample.recv < self._samples[-1].recv
        ):
            self._samples.clear()  # the radio's counters reset (reboot); start the window over
        self._samples.append(sample)
        while len(self._samples) > 1 and sample.t - self._samples[0].t > self.window_s:
            self._samples.popleft()

        oldest = self._samples[0]
        elapsed = sample.t - oldest.t
        if elapsed <= 0:
            return self.current  # first sample: nothing to compare yet
        air = (sample.tx_air - oldest.tx_air) + (sample.rx_air - oldest.rx_air)
        duty = min(1.0, max(0.0, air / elapsed))
        ppm = ((sample.recv - oldest.recv) + (sample.sent - oldest.sent)) / elapsed * 60.0

        level = next_level(duty, self.level, self.duty_low, self.duty_high)
        changed = level != self.level
        self._set_level(level, duty)
        self.current = Utilization(
            duty=duty,
            packets_per_min=ppm,
            window_s=elapsed,
            noise_floor=sample.noise_floor,
            last_rssi=sample.last_rssi,
            last_snr=sample.last_snr,
            level=level,
            factor=FACTORS[level],
        )
        self.log.emit(
            "utilization",
            duty=round(duty, 4),
            packets_per_min=round(ppm, 2),
            window_s=round(elapsed, 1),
            noise_floor=sample.noise_floor,
            last_rssi=sample.last_rssi,
            last_snr=sample.last_snr,
            level=level,
            factor=FACTORS[level],
            level_changed=changed,
        )
        return self.current

    def _set_level(self, level: str, duty: float) -> None:
        if level != self.level:
            self.log.emit("rate_level", old=self.level, new=level, duty=round(duty, 4))
        self.level = level
        self.limiter.set_global_factor(FACTORS[level])
