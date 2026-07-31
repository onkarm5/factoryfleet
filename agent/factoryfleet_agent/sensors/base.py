"""Sensor plugin contract and registry.

A sensor is anything that produces numeric readings about a machine. Adding one means
writing a class that implements :meth:`Sensor.read`, decorating it with :func:`register`,
and dropping a config file next to the others — nothing in the scheduler, the store, or
the transport changes.

The base class, not the subclass, owns error handling and timestamping. A subclass's
``read`` may raise freely: :meth:`Sensor.sample` turns the failure into a reading with
condition ``ERROR``. That keeps one broken sensor from ending a sampling cycle and taking
its healthy neighbours down with it, and means no subclass can forget to stamp a reading.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import RLock
from typing import Any, Callable, Iterable, Mapping

from factoryfleet_agent.config import SensorConfig
from factoryfleet_agent.timeutil import Clock, iso, utc_now

__all__ = [
    "Clock",
    "Reading",
    "Sensor",
    "SensorCondition",
    "SensorError",
    "UnknownCommand",
    "UnknownSensorType",
    "create",
    "create_all",
    "register",
    "registered_types",
    "utc_now",
]


class SensorCondition(str, Enum):
    """What a single reading says about the sensor, judged locally on the machine.

    Deliberately narrower than the asset health states the backend maintains. The agent can
    only see one reading at a time, so it can tell that a value has breached a fixed limit
    but not that it is drifting away from the machine's normal baseline. Drift detection
    needs history across readings, which is the backend's job — that is where ``DEGRADED``
    comes from.
    """

    OK = "OK"
    """Within the sensor's configured limits."""

    CRITICAL = "CRITICAL"
    """A hard limit has been breached. Actionable without any further analysis."""

    ERROR = "ERROR"
    """The sensor could not be read. Says nothing about the machine, only about the sensor."""


class SensorError(Exception):
    """Raised by a sensor that cannot produce a reading."""


class UnknownSensorType(Exception):
    """A configuration file names a sensor type nothing has registered."""


class UnknownCommand(Exception):
    """A command was routed to a sensor that does not accept it."""


@dataclass(frozen=True)
class Reading:
    """One sensor's measurement at one instant."""

    sensor_id: str
    captured_at: datetime
    metrics: Mapping[str, float] = field(default_factory=dict)
    condition: SensorCondition = SensorCondition.OK
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Renders the reading as it travels on the wire."""
        payload: dict[str, Any] = {
            "sensorId": self.sensor_id,
            "capturedAt": iso(self.captured_at),
            "condition": self.condition.value,
            "metrics": dict(self.metrics),
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


class Sensor(ABC):
    """Base class for every sensor.

    Subclasses implement :meth:`read`, and optionally :meth:`evaluate` to judge their own
    readings and :meth:`handle_command` to accept commands.
    """

    #: Command names this sensor accepts, addressed as ``sensor:<sensor_id>`` from the cloud.
    COMMANDS: frozenset[str] = frozenset()

    def __init__(self, config: SensorConfig, clock: Clock = utc_now) -> None:
        self._config = config
        self._clock = clock
        # Sampling and command execution can arrive on different threads while sharing one
        # device handle, so both paths take this lock.
        self._lock = RLock()

    @property
    def sensor_id(self) -> str:
        return self._config.sensor_id

    @property
    def type_name(self) -> str:
        return self._config.type

    @property
    def config(self) -> SensorConfig:
        return self._config

    @abstractmethod
    def read(self) -> Mapping[str, float]:
        """Takes a measurement, returning metric name to numeric value.

        Free to raise: :meth:`sample` converts a failure into an ``ERROR`` reading.
        """

    def evaluate(self, metrics: Mapping[str, float]) -> SensorCondition:
        """Judges a reading against this sensor's own limits. ``OK`` unless overridden."""
        return SensorCondition.OK

    def sample(self) -> Reading:
        """Takes one timestamped, judged reading. Never raises."""
        captured_at = self._clock()
        try:
            with self._lock:
                metrics = _validate_metrics(self.read())
        except Exception as error:  # noqa: BLE001 — a broken sensor must not end the cycle
            return Reading(
                sensor_id=self.sensor_id,
                captured_at=captured_at,
                condition=SensorCondition.ERROR,
                error=_describe(error),
            )

        try:
            condition = self.evaluate(metrics)
        except Exception as error:  # noqa: BLE001 — keep the metrics, flag the judgement
            return Reading(
                sensor_id=self.sensor_id,
                captured_at=captured_at,
                metrics=metrics,
                condition=SensorCondition.ERROR,
                error=f"evaluate failed: {_describe(error)}",
            )

        return Reading(
            sensor_id=self.sensor_id,
            captured_at=captured_at,
            metrics=metrics,
            condition=condition,
        )

    def execute(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Runs a command against this sensor, rejecting anything undeclared."""
        if name not in self.COMMANDS:
            raise UnknownCommand(
                f"sensor '{self.sensor_id}' does not accept command '{name}'"
            )
        with self._lock:
            return self.handle_command(name, args)

    def handle_command(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Handles a command already checked against :attr:`COMMANDS`."""
        raise UnknownCommand(f"sensor '{self.sensor_id}' has no handler for '{name}'")


_REGISTRY: dict[str, type[Sensor]] = {}


def register(type_name: str) -> Callable[[type[Sensor]], type[Sensor]]:
    """Registers a sensor class under the ``type`` its config files will name."""

    def decorator(cls: type[Sensor]) -> type[Sensor]:
        if not isinstance(cls, type) or not issubclass(cls, Sensor):
            raise TypeError(f"@register('{type_name}') requires a Sensor subclass")
        existing = _REGISTRY.get(type_name)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"sensor type '{type_name}' is already registered to {existing.__name__}"
            )
        _REGISTRY[type_name] = cls
        return cls

    return decorator


def registered_types() -> frozenset[str]:
    return frozenset(_REGISTRY)


def create(config: SensorConfig, clock: Clock = utc_now) -> Sensor:
    """Builds the sensor its configuration names."""
    cls = _REGISTRY.get(config.type)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise UnknownSensorType(
            f"{config.source}: no sensor registered for type '{config.type}' "
            f"(registered: {known})"
        )
    return cls(config, clock)


def create_all(configs: Iterable[SensorConfig], clock: Clock = utc_now) -> tuple[Sensor, ...]:
    return tuple(create(config, clock) for config in configs)


def _validate_metrics(metrics: Mapping[str, float]) -> dict[str, float]:
    """Rejects anything that would not survive the trip as JSON numbers."""
    if not isinstance(metrics, Mapping):
        raise SensorError(f"read() must return a mapping, got {type(metrics).__name__}")
    validated: dict[str, float] = {}
    for name, value in metrics.items():
        # bool is an int subclass; a true/false metric is almost certainly a mistake.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SensorError(
                f"metric '{name}' must be a number, got {type(value).__name__}"
            )
        if value != value or value in (float("inf"), float("-inf")):
            raise SensorError(f"metric '{name}' is not a finite number: {value}")
        validated[str(name)] = value
    return validated


def _describe(error: Exception) -> str:
    """A one-line description of a failure, kept short enough for a telemetry field."""
    message = str(error).strip()
    name = type(error).__name__
    return f"{name}: {message}" if message else name
