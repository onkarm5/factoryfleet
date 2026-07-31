"""Topics and payload shapes the agent speaks.

Kept in one module because the runner builds payloads, the publisher stamps and sends them,
and startup announces the asset — three callers that must agree exactly. The backend parses
what this file produces, so any change here is a contract change.

Three timestamps travel with a batch, and the distinction between them is the point:

``capturedAt``
    When the sensor was read. Set by the sensor.

``batchedAt``
    When the agent formed the batch and committed it to the outbox. Set by the runner.

``sentAt``
    When the batch actually left the agent. Set by the publisher at publish time, not at
    enqueue time, because a batch may sit in the outbox for hours while a link is down.

Their differences are diagnostic rather than decorative: ``sentAt`` minus ``batchedAt`` is
how long the agent was unable to reach the broker, which is otherwise invisible from the
cloud — buffered telemetry arriving late is indistinguishable from telemetry produced late.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from factoryfleet_agent.config import AssetIdentity
from factoryfleet_agent.sensors.base import Reading
from factoryfleet_agent.timeutil import iso


def telemetry_topic(identity: AssetIdentity) -> str:
    return f"{identity.topic_prefix}/telemetry"


def registration_topic(identity: AssetIdentity) -> str:
    return f"{identity.topic_prefix}/registration"


def telemetry_batch(
    identity: AssetIdentity,
    readings: Iterable[Reading],
    batched_at: datetime,
) -> dict[str, Any]:
    """One publishable batch of readings.

    The asset and site are repeated in the body even though they are already in the topic.
    Once messages are routed through a queue the topic is no longer attached to them, and a
    payload that cannot say which machine it came from is useless.
    """
    return {
        "assetId": identity.asset_id,
        "siteId": identity.site_id,
        "batchedAt": iso(batched_at),
        "readings": [reading.to_payload() for reading in readings],
    }


def registration(
    identity: AssetIdentity,
    sensor_ids: Sequence[str],
    agent_version: str,
    declared_at: datetime,
) -> dict[str, Any]:
    """The asset announcing itself and what it will report."""
    return {
        "assetId": identity.asset_id,
        "siteId": identity.site_id,
        "machineType": identity.machine_type,
        "firmwareVersion": identity.firmware_version,
        "agentVersion": agent_version,
        "sensors": list(sensor_ids),
        "declaredAt": iso(declared_at),
    }


def stamp_sent(payload: Mapping[str, Any], sent_at: datetime) -> dict[str, Any]:
    """Adds ``sentAt`` as a payload leaves the agent.

    Applied at publish rather than at enqueue so the value is honest about when the message
    reached the broker, which is what makes buffering delay measurable downstream.
    """
    return {**payload, "sentAt": iso(sent_at)}
