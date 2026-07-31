"""Temperature sensor — bearing and spindle, in Celsius.

Heat is the second degradation signal, and it corroborates the first: friction from a
failing bearing appears as a slow temperature rise, so vibration and temperature climbing
together is a far stronger indication than either alone. Only the bearing carries a hard
limit — it is the reading that precedes a seizure.
"""

from __future__ import annotations

import random
from typing import Mapping

from factoryfleet_agent.config import SensorConfig
from factoryfleet_agent.sensors.base import Clock, SensorCondition, register, utc_now
from factoryfleet_agent.sensors.simulation import SimulatedSensor, Waveform


@register("temperature")
class TemperatureSensor(SimulatedSensor):
    """Reports bearing and spindle temperature."""

    def __init__(
        self,
        config: SensorConfig,
        clock: Clock = utc_now,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(config, clock, rng)
        self._hard_limit = config.required_float("hard_limit_bearing_c")
        noise = config.required_float("noise_c")
        drift = float(config.option("drift_c_per_hour", 0.0))
        self._bearing = Waveform(
            baseline=config.required_float("baseline_bearing_c"),
            noise=noise,
            drift_per_hour=drift,
        )
        # The spindle does not drift: it is the control reading. If both climb the machine is
        # heating up; if only the bearing climbs, the bearing is the problem.
        self._spindle = Waveform(
            baseline=config.required_float("baseline_spindle_c"),
            noise=noise,
        )

    def read(self) -> Mapping[str, float]:
        elapsed_hours = self.elapsed_hours()
        return {
            "bearing_c": round(self._bearing.at(elapsed_hours, self._rng), 2),
            "spindle_c": round(self._spindle.at(elapsed_hours, self._rng), 2),
        }

    def evaluate(self, metrics: Mapping[str, float]) -> SensorCondition:
        if metrics["bearing_c"] > self._hard_limit:
            return SensorCondition.CRITICAL
        return SensorCondition.OK
