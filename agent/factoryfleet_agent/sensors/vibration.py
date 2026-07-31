"""Vibration sensor — RMS velocity and peak, the classic rotating-machinery health signal.

Bearing wear, shaft misalignment, and loosening mounts all show up as rising RMS velocity
long before anything seizes. The crest factor (peak over RMS) is reported alongside because
it moves for a different reason: impact-type faults such as a spalled bearing race spike the
peak while barely moving the RMS.
"""

from __future__ import annotations

import random
from typing import Mapping

from factoryfleet_agent.config import SensorConfig
from factoryfleet_agent.sensors.base import Clock, SensorCondition, register, utc_now
from factoryfleet_agent.sensors.simulation import SimulatedSensor, Waveform


@register("vibration")
class VibrationSensor(SimulatedSensor):
    """Reports RMS and peak velocity in mm/s."""

    def __init__(
        self,
        config: SensorConfig,
        clock: Clock = utc_now,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(config, clock, rng)
        # Read at construction so a misconfigured sensor fails at agent startup rather than
        # producing ERROR readings once an hour for the rest of its life.
        self._hard_limit = config.required_float("hard_limit_rms_mm_s")
        self._peak_factor = config.required_float("peak_factor")
        self._waveform = Waveform(
            baseline=config.required_float("baseline_rms_mm_s"),
            noise=config.required_float("noise_mm_s"),
            drift_per_hour=float(config.option("drift_mm_s_per_hour", 0.0)),
        )

    def read(self) -> Mapping[str, float]:
        rms = self._waveform.at(self.elapsed_hours(), self._rng)
        return {
            "rms_mm_s": round(rms, 3),
            "peak_mm_s": round(rms * self._peak_factor, 3),
        }

    def evaluate(self, metrics: Mapping[str, float]) -> SensorCondition:
        if metrics["rms_mm_s"] > self._hard_limit:
            return SensorCondition.CRITICAL
        return SensorCondition.OK
