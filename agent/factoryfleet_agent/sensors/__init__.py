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

# Imported for the registration side effect: each module calls @register at import time, so
# a configured sensor type resolves without anything holding a hard-coded list of modules.
from factoryfleet_agent.sensors import cycle_count, temperature, vibration  # noqa: F401,E402

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
