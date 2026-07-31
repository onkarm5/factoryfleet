"""Sensor plugins.

Importing this package registers every built-in sensor, so callers can resolve a
configured sensor type without knowing which module implements it.
"""

from factoryfleet_agent.sensors.base import (
    Reading,
    Sensor,
    SensorCondition,
    SensorError,
    UnknownCommand,
    UnknownSensorType,
    create,
    create_all,
    register,
    registered_types,
)

__all__ = [
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
]
