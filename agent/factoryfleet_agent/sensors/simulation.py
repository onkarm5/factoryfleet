"""Shared machinery for sensors that model a machine rather than read hardware.

The fleet has to be demonstrable without a plant floor attached, so the built-in sensors
simulate. What they simulate matters: a flat random number would make the backend's
baseline detection look like it works when it does not. Each simulated reading is therefore
a machine-specific baseline, plus cycle-to-cycle noise, plus optional progressive drift.

Drift is the interesting knob. Left at zero a machine reads healthy forever. Turned up, its
readings climb slowly out of their own normal range while staying well inside any absolute
limit — exactly the failure that a fleet-wide threshold misses and a per-machine baseline
catches.

A sensor talking to real hardware subclasses :class:`~factoryfleet_agent.sensors.base.Sensor`
directly and ignores everything here.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from factoryfleet_agent.config import SensorConfig
from factoryfleet_agent.sensors.base import Clock, Sensor, utc_now

SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True)
class Waveform:
    """One simulated quantity: where it sits, how much it wobbles, where it is heading."""

    baseline: float
    noise: float
    drift_per_hour: float = 0.0
    #: Lower bound applied after noise. Physical quantities like vibration and temperature
    #: cannot go negative, and a stray negative reading would corrupt a baseline.
    floor: float | None = 0.0

    def at(self, elapsed_hours: float, rng: random.Random) -> float:
        value = self.baseline + self.drift_per_hour * elapsed_hours
        if self.noise > 0:
            value += rng.gauss(0.0, self.noise)
        if self.floor is not None:
            value = max(self.floor, value)
        return value


class SimulatedSensor(Sensor):
    """Base for the built-in simulated sensors.

    Adds a seedable random source, so a test can assert on exact readings, and a runtime
    origin, so drift can be expressed per hour of operation rather than per reading. Per
    hour is the useful unit: it makes the simulation independent of how often the scheduler
    happens to sample.
    """

    def __init__(
        self,
        config: SensorConfig,
        clock: Clock = utc_now,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(config, clock)
        self._rng = rng if rng is not None else random.Random()
        self._started_at: datetime = clock()

    @property
    def started_at(self) -> datetime:
        return self._started_at

    def elapsed_seconds(self) -> float:
        """Seconds of runtime since this sensor was constructed, never negative."""
        return max(0.0, (self._clock() - self._started_at).total_seconds())

    def elapsed_hours(self) -> float:
        return self.elapsed_seconds() / SECONDS_PER_HOUR
