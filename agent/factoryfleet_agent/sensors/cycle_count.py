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

#: Shortest window from which a rate is estimated directly. One second of a machine running at
#: production speed is enough cycles to divide by; a millisecond is not.
MIN_RATE_WINDOW_MINUTES = 1.0 / SECONDS_PER_MINUTE


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

        uptime_seconds = self.elapsed_seconds()
        return {
            "cycles_total": self._cycles_total,
            "cycles_per_minute": round(self._rate(produced, minutes, uptime_seconds), 2),
            "uptime_seconds": round(uptime_seconds, 1),
        }

    def _rate(self, produced: float, minutes: float, uptime_seconds: float) -> float:
        """Cycles per minute, estimated over a window long enough for the answer to mean something.

        A rate is a count divided by a window, so as the window shrinks the division amplifies
        whatever noise the count carries. On the first sample the window is effectively zero
        and the result is meaningless — this used to report 470 cycles/min for a machine
        holding a steady 42, alongside a total of 0.

        Below the minimum window it falls back to the long-run average over total runtime,
        which is a real measurement rather than an invented one, and which is 0 on the very
        first sample where nothing has been produced yet.
        """
        if minutes >= MIN_RATE_WINDOW_MINUTES:
            return produced / minutes
        uptime_minutes = uptime_seconds / SECONDS_PER_MINUTE
        if uptime_minutes > 0:
            return self._cycles_total / uptime_minutes
        return 0.0
