"""The sampling cycle: read every sensor, save the machine's state, queue a batch.

Deliberately knows nothing about the broker. Its whole job ends at the outbox, which is what
lets sampling continue at its own pace while the network is unavailable — the runner cannot
be blocked by a connection it never holds.
"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from factoryfleet_agent import wire
from factoryfleet_agent.config import AssetIdentity
from factoryfleet_agent.sensors.base import Reading, Sensor, SensorCondition
from factoryfleet_agent.store import Store
from factoryfleet_agent.timeutil import Clock, utc_now

logger = logging.getLogger(__name__)


class SamplingRunner:
    """Runs one sampling cycle per call, driven by a scheduler."""

    def __init__(
        self,
        identity: AssetIdentity,
        sensors: Sequence[Sensor],
        store: Store,
        clock: Clock = utc_now,
    ) -> None:
        self._identity = identity
        self._sensors = tuple(sensors)
        self._store = store
        self._clock = clock
        self._topic = wire.telemetry_topic(identity)
        self._cycles = 0

    @property
    def cycles(self) -> int:
        return self._cycles

    @property
    def sensors(self) -> tuple[Sensor, ...]:
        return self._sensors

    def sample_once(self) -> tuple[Reading, ...]:
        """Samples every sensor, then commits state and one telemetry batch together.

        Sensor failures are data, not errors: :meth:`Sensor.sample` renders a failed read as
        an ``ERROR`` reading, so a seized sensor is reported rather than swallowed, and its
        healthy neighbours still get through. The batch is published even when every reading
        failed — an operator needs to know the sensors are down, and silence cannot say that.
        """
        readings = tuple(sensor.sample() for sensor in self._sensors)
        if not readings:
            return ()

        batched_at = self._clock()
        payload = wire.telemetry_batch(self._identity, readings, batched_at)

        # One transaction: recorded state that the outbox has no matching batch for would
        # mislead whoever later asks what this machine reported and when.
        with self._store.transaction():
            self._store.record_all(readings)
            self._store.enqueue(self._topic, payload)

        self._cycles += 1
        _log_cycle(readings)
        return readings

    def registration_payload(self, agent_version: str) -> dict:
        """What this asset declares about itself on startup."""
        return wire.registration(
            self._identity,
            [sensor.sensor_id for sensor in self._sensors],
            agent_version,
            self._clock(),
        )


def _log_cycle(readings: Iterable[Reading]) -> None:
    """Logs a cycle at a level matching the worst thing in it."""
    readings = tuple(readings)
    failed = [r.sensor_id for r in readings if r.condition is SensorCondition.ERROR]
    critical = [r.sensor_id for r in readings if r.condition is SensorCondition.CRITICAL]

    if critical:
        logger.warning(
            "Sampled %d sensors; %s past hard limits", len(readings), ", ".join(critical)
        )
    elif failed:
        logger.warning(
            "Sampled %d sensors; %s could not be read", len(readings), ", ".join(failed)
        )
    else:
        logger.info("Sampled %d sensors, all within limits", len(readings))
