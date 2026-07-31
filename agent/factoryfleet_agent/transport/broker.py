"""The broker connection, and the narrow interface the publisher sees of it.

:class:`BrokerClient` exists so the publisher's logic — ordering, confirmation, what happens
when a send fails — is testable without a broker, and so that milestone 3's switch from
Mosquitto to AWS IoT Core replaces this file and nothing else. Both speak MQTT; only
addressing and authentication differ.

Confirmation is the important part of the contract. :meth:`BrokerClient.publish` returns
``True`` only once the broker has acknowledged the message, never merely because it was
handed to the network. The outbox treats that return value as permission to stop retrying,
so a client that reported success early would turn the store's at-least-once guarantee into
at-most-once, silently.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
from typing import Any, Mapping, Protocol

import paho.mqtt.client as mqtt

from factoryfleet_agent.config import BrokerConfig

logger = logging.getLogger(__name__)

# At-least-once. QoS 0 would drop messages on a flaky plant link with no way to tell, and
# QoS 2 pays for exactly-once the pipeline does not need: readings are idempotent, since
# re-delivering one overwrites the same (asset, sensor, capturedAt) rather than double
# counting anything.
DEFAULT_QOS = 1


class BrokerClient(Protocol):
    """What the publisher needs from a broker connection."""

    def is_connected(self) -> bool: ...

    def publish(self, topic: str, payload: Mapping[str, Any]) -> bool:
        """Sends one message, returning ``True`` only once the broker has confirmed it."""


class MqttBrokerClient:
    """MQTT connection with automatic reconnection, backed by paho.

    Connects asynchronously and never blocks the caller waiting for a broker: an agent that
    refuses to start because the network is down is useless, since the machine still needs
    measuring and the readings still need to queue. Sampling therefore proceeds while this
    reconnects in the background.
    """

    def __init__(
        self,
        config: BrokerConfig,
        *,
        publish_timeout_seconds: float = 10.0,
        qos: int = DEFAULT_QOS,
        reconnect_min_seconds: float = 1.0,
        reconnect_max_seconds: float = 60.0,
    ) -> None:
        self._config = config
        self._publish_timeout = publish_timeout_seconds
        self._qos = qos
        self._started = False
        self._connected = threading.Event()

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.client_id,
            protocol=mqtt.MQTTv5,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        # Backs off rather than hammering a broker that is already struggling, and caps the
        # delay so a machine does not sit disconnected for hours after a brief outage.
        self._client.reconnect_delay_set(
            min_delay=int(reconnect_min_seconds), max_delay=int(reconnect_max_seconds)
        )
        if config.tls:
            # Milestone 3 replaces this with a per-asset X.509 client certificate, which is
            # what IoT Core authenticates against and what scopes an asset to its own topics.
            self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)

    def start(self) -> None:
        """Begins connecting and starts the network loop. Does not wait for a connection."""
        if self._started:
            return
        self._started = True
        logger.info(
            "Connecting to broker %s:%d as '%s'",
            self._config.host,
            self._config.port,
            self._config.client_id,
        )
        # connect_async rather than connect: a broker that is down at startup must be a
        # retry, not a crash.
        self._client.connect_async(
            self._config.host, self._config.port, keepalive=self._config.keepalive_seconds
        )
        self._client.loop_start()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._client.disconnect()
        self._client.loop_stop()
        self._connected.clear()

    def wait_until_connected(self, timeout: float) -> bool:
        """Blocks briefly, only used to make startup logs and demos readable."""
        return self._connected.wait(timeout)

    def is_connected(self) -> bool:
        return self._connected.is_set() and self._client.is_connected()

    def publish(self, topic: str, payload: Mapping[str, Any]) -> bool:
        if not self.is_connected():
            return False

        message = self._client.publish(
            topic,
            json.dumps(payload, separators=(",", ":")),
            qos=self._qos,
        )
        if message.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.warning("Publish to %s refused locally: rc=%s", topic, message.rc)
            return False

        try:
            # Blocks until the broker's acknowledgement. Without this the outbox entry would
            # be deleted on the strength of a local handoff, which is not delivery.
            message.wait_for_publish(self._publish_timeout)
        except (ValueError, RuntimeError) as error:
            logger.warning("Publish to %s failed while awaiting broker: %s", topic, error)
            return False

        if not message.is_published():
            logger.warning(
                "Broker did not confirm publish to %s within %.1fs",
                topic,
                self._publish_timeout,
            )
            return False
        return True

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties=None) -> None:
        if reason_code == 0:
            self._connected.set()
            logger.info("Connected to broker %s:%d", self._config.host, self._config.port)
        else:
            self._connected.clear()
            logger.warning("Broker refused the connection: %s", reason_code)

    def _on_disconnect(
        self, _client, _userdata, _flags=None, reason_code=None, _properties=None
    ) -> None:
        self._connected.clear()
        # Not an error: the link dropping is expected on a plant floor, the outbox absorbs it,
        # and paho is already reconnecting.
        logger.info("Disconnected from broker (%s); telemetry will queue until it returns", reason_code)
