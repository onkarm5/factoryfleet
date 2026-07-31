"""Production counter — cycles completed, current rate, and agent uptime.

Not a health signal on its own; it is the context the others are read against. Vibration at
6 mm/s means one thing on a machine that has run four hundred cycles and something else on
one that has run four million. The rate is reported per reading rather than only as a total
so a stall is visible immediately instead of having to be inferred from a flat total.

No hard limit: a counter cannot breach one. A rate that drops to zero on a machine that
should be running is a real problem, but recognising that needs the machine's history, which
is the backend's baseline analysis rather than a fixed threshold here.
"""

from __future__ import annotations

import math
import random
from typing import Mapping

from factoryfleet_agent.config import SensorConfig
from factoryfleet_agent.sensors.base import Clock, register, utc_now
from factoryfleet_agent.sensors.simulation import SimulatedSensor

SECONDS_PER_MINUTE = 60.0


@register("cycle_count")
class CycleCountSensor(SimulatedSensor):
    """Reports a monotonically increasing cycle total, the observed rate, and uptime."""

    def __init__(
        self,
        config: SensorConfig,
        clock: Clock = utc_now,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(config, clock, rng)
        self._nominal_rate = config.required_float("cycles_per_minute")
        self._jitter = float(config.option("jitter_cycles", 0.0))
        self._cycles_total = 0
        self._last_sampled_at = self._started_at

    def read(self) -> Mapping[str, float]:
        # Called under the sensor lock, so this mutation is serialised against commands.
        now = self._clock()
        minutes = max(0.0, (now - self._last_sampled_at).total_seconds() / SECONDS_PER_MINUTE)
        self._last_sampled_at = now

        produced = self._nominal_rate * minutes
        if produced > 0 and self._jitter > 0:
            # Jitter is quoted per minute and accumulates like a random walk, so its spread
            # grows with the square root of the interval. Adding it flat would swamp a short
            # interval: ±3 cycles on the ~7 produced in ten seconds would swing the reported
            # rate between 20 and 60 cycles/min on a machine holding a steady 42.
            produced += self._rng.gauss(0.0, self._jitter * math.sqrt(minutes))
        # A physical counter only ever counts up; jitter must not be able to reverse it.
        produced = max(0.0, produced)
        self._cycles_total += int(round(produced))

        observed_rate = produced / minutes if minutes > 0 else 0.0
        return {
            "cycles_total": self._cycles_total,
            "cycles_per_minute": round(observed_rate, 2),
            "uptime_seconds": round(self.elapsed_seconds(), 1),
        }
