"""Drains the outbox to the broker.

The counterpart to the store: the store guarantees a reading survives, and this decides when
it is safe to stop keeping it. Two rules make that safe.

*Confirmed, then forgotten.* An entry is marked published only after the broker acknowledges
it. Marking on handoff instead would turn at-least-once into at-most-once, and the loss would
be silent — the outbox would look drained whether or not anything arrived.

*Stop at the first failure.* When a send fails the drain ends rather than skipping ahead. The
failed entry stays at the head of the queue, so readings reach the cloud in the order the
machine produced them, and a batch is never quietly overtaken by a later one.
"""

from __future__ import annotations

import logging

from factoryfleet_agent import wire
from factoryfleet_agent.store import Store
from factoryfleet_agent.timeutil import Clock, utc_now
from factoryfleet_agent.transport.broker import BrokerClient

logger = logging.getLogger(__name__)


class Publisher:
    """Sends as much of the outbox as the broker will confirm, oldest first."""

    def __init__(
        self,
        store: Store,
        client: BrokerClient,
        *,
        batch_size: int = 50,
        retain_published_seconds: float = 3600.0,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._client = client
        self._batch_size = batch_size
        self._retain_published_seconds = retain_published_seconds
        self._clock = clock
        self._published = 0

    @property
    def published(self) -> int:
        """Messages confirmed by the broker over this agent's lifetime."""
        return self._published

    def drain_once(self) -> int:
        """Publishes up to ``batch_size`` entries, returning how many were confirmed."""
        if not self._client.is_connected():
            pending = self._store.pending_count()
            if pending:
                logger.debug("Broker unreachable; %d entries waiting", pending)
            return 0

        entries = self._store.pending(self._batch_size)
        if not entries:
            self._prune()
            return 0

        confirmed = 0
        for entry in entries:
            # sentAt is stamped now, not when the batch was queued, so a batch buffered
            # through an outage reports honestly when it actually left.
            payload = wire.stamp_sent(entry.payload, self._clock())
            if not self._client.publish(entry.topic, payload):
                logger.warning(
                    "Broker did not confirm entry %d; stopping this drain with %d entries left",
                    entry.id,
                    len(entries) - confirmed,
                )
                break
            # One at a time: a crash here costs a resend of a single confirmed message, not
            # the loss of every other entry in the batch.
            self._store.mark_published([entry.id])
            confirmed += 1

        self._published += confirmed
        if confirmed:
            logger.info(
                "Published %d entries; %d still queued", confirmed, self._store.pending_count()
            )
        self._prune()
        return confirmed

    def publish_now(self, topic: str, payload: dict) -> bool:
        """Queues a payload and tries to send it immediately.

        Used for registration at startup. It still goes through the outbox rather than
        straight to the broker, so an agent starting while the network is down announces
        itself once the link returns instead of never.
        """
        self._store.enqueue(topic, payload)
        return self.drain_once() > 0

    def _prune(self) -> None:
        """Discards confirmed entries past their retention window."""
        removed = self._store.prune_published(self._retain_published_seconds)
        if removed:
            logger.debug("Pruned %d confirmed outbox entries", removed)
