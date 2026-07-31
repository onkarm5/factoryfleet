"""Wires the subsystems together and owns their lifecycle.

The shape is two independent loops over one store. Sampling writes to the outbox; publishing
drains it. Nothing connects them but the database, which is what makes an outage boring: the
sampler neither knows nor cares that the publisher is failing, so the machine keeps being
measured and the readings keep accumulating until the link returns.

Startup order matters in one respect. Registration is queued before sampling begins, so the
backend learns what the machine is before it starts receiving readings from it. It is queued
rather than sent, so an agent starting during an outage still announces itself eventually
instead of never.
"""

from __future__ import annotations

import logging
from typing import Sequence

from factoryfleet_agent import wire
from factoryfleet_agent.config import AgentConfig
from factoryfleet_agent.runner import SamplingRunner
from factoryfleet_agent.scheduler import Scheduler
from factoryfleet_agent.sensors import create_all
from factoryfleet_agent.sensors.base import Sensor
from factoryfleet_agent.store import Store
from factoryfleet_agent.timeutil import Clock, utc_now
from factoryfleet_agent.transport.broker import BrokerClient, MqttBrokerClient
from factoryfleet_agent.transport.publisher import Publisher

logger = logging.getLogger(__name__)

#: How long shutdown waits for a final drain before giving up and leaving it to the outbox.
SHUTDOWN_DRAIN_SECONDS = 5.0


class Agent:
    """One machine's agent: samples it, and reports it to the cloud."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        clock: Clock = utc_now,
        client: BrokerClient | None = None,
        version: str = "0.2.0",
    ) -> None:
        self._config = config
        self._version = version
        self._store = Store(config.database_path, clock=clock)
        self._sensors: Sequence[Sensor] = create_all(config.enabled_sensors, clock)
        self._runner = SamplingRunner(config.asset, self._sensors, self._store, clock)
        self._client = client if client is not None else MqttBrokerClient(config.broker)
        self._publisher = Publisher(
            self._store,
            self._client,
            batch_size=config.publish_batch_size,
            clock=clock,
        )
        self._sampler = Scheduler(
            interval_seconds=config.sample_interval_seconds,
            task=self._runner.sample_once,
            name="sampler",
            on_error=self._on_sampler_error,
        )
        self._publisher_loop = Scheduler(
            interval_seconds=config.publish_interval_seconds,
            task=self._publisher.drain_once,
            name="publisher",
            on_error=self._on_publisher_error,
        )
        self._running = False

    @property
    def store(self) -> Store:
        return self._store

    @property
    def runner(self) -> SamplingRunner:
        return self._runner

    @property
    def publisher(self) -> Publisher:
        return self._publisher

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        asset = self._config.asset
        logger.info(
            "Starting agent %s for %s/%s (%s), %d sensors: %s",
            self._version,
            asset.site_id,
            asset.asset_id,
            asset.machine_type,
            len(self._sensors),
            ", ".join(sensor.sensor_id for sensor in self._sensors),
        )
        pending = self._store.pending_count()
        if pending:
            # Not a warning: this is the outbox doing its job across a restart.
            logger.info("Recovered %d unsent entries from the previous run", pending)

        self._client.start()
        self._announce()
        self._sampler.start()
        self._publisher_loop.start()
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        logger.info("Stopping agent")
        # Sampler first: no point taking readings that will not be published, and stopping it
        # keeps the outbox from growing while shutdown is in progress.
        sampler_stopped = self._sampler.stop()
        publisher_stopped = self._publisher_loop.stop()
        threads_stopped = sampler_stopped and publisher_stopped
        if not threads_stopped:
            logger.warning(
                "Loops did not stop in time (sampler=%s, publisher=%s)",
                sampler_stopped,
                publisher_stopped,
            )

        # One last drain, so a clean shutdown does not leave readings on disk that the broker
        # was available to accept. Bounded, because refusing to exit is worse than a delay:
        # anything unsent is already durable and goes out on the next start.
        try:
            self._publisher.drain_once()
        except Exception as error:  # noqa: BLE001 — shutdown must complete regardless
            logger.warning("Final drain failed: %s", error)

        remaining = self._store.pending_count()
        if remaining:
            logger.info("%d entries still queued; they will be sent on next start", remaining)

        self._client.stop()
        if threads_stopped:
            self._store.close()
        else:
            # Closing under a thread that is still sampling would turn a slow shutdown into
            # an error inside that thread. Leaking the handle until the process exits is the
            # lesser problem, and the data is already committed.
            logger.warning("Leaving the store open because a loop is still running")
        self._running = False
        logger.info(
            "Stopped after %d sampling cycles and %d published entries",
            self._runner.cycles,
            self._publisher.published,
        )

    def __enter__(self) -> "Agent":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _announce(self) -> None:
        """Queues this asset's registration, sending it now if the broker is reachable."""
        payload = self._runner.registration_payload(self._version)
        if self._publisher.publish_now(wire.registration_topic(self._config.asset), payload):
            logger.info("Registered with the backend")
        else:
            logger.info("Registration queued; it will be sent when the broker is reachable")

    def _on_sampler_error(self, error: BaseException) -> None:
        # Individual sensor failures never reach here — they become ERROR readings. This is
        # the store or the batch itself failing, which means the machine is now unmonitored.
        logger.error("Sampling cycle failed: %s", error, exc_info=error)

    def _on_publisher_error(self, error: BaseException) -> None:
        logger.error("Publish cycle failed: %s", error, exc_info=error)
