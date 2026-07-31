"""Configuration loading for the asset agent.

Two levels, mirroring how the agent is operated: ``agent.toml`` carries everything about
the machine and its connection to the cloud, and one file per sensor under ``sensors/``
carries that sensor's tuning. Adding a sensor is therefore a new file, never an edit to a
shared list.

Every problem is reported as a :class:`ConfigError` naming the file and key at fault. An
agent that starts with bad configuration and reports nonsense is worse than one that
refuses to start, so validation happens once here rather than being discovered later by
whichever component first trips over it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# Kept in step with the backend's MachineType enum. Validating here means a typo is caught
# at agent startup rather than as a rejected registration.
MACHINE_TYPES = frozenset(
    {
        "CNC_MILL",
        "HYDRAULIC_PRESS",
        "CONVEYOR",
        "ROBOT_ARM",
        "INJECTION_MOLDER",
    }
)

LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigError(Exception):
    """Configuration is missing, malformed, or internally inconsistent."""


@dataclass(frozen=True)
class AssetIdentity:
    """Who this agent says it is. Also its MQTT topic prefix and registry key."""

    site_id: str
    asset_id: str
    machine_type: str
    firmware_version: str

    @property
    def topic_prefix(self) -> str:
        return f"factoryfleet/{self.site_id}/{self.asset_id}"


@dataclass(frozen=True)
class BrokerConfig:
    host: str
    port: int
    tls: bool
    keepalive_seconds: int
    client_id: str


@dataclass(frozen=True)
class SensorConfig:
    """One sensor's identity and tuning, as loaded from its own file."""

    sensor_id: str
    type: str
    enabled: bool
    options: Mapping[str, Any]
    source: Path

    def option(self, key: str, default: Any = None) -> Any:
        """Reads a tuning value, falling back to ``default`` when unset."""
        return self.options.get(key, default)

    def required_float(self, key: str) -> float:
        """Reads a tuning value that the sensor cannot operate without."""
        if key not in self.options:
            raise ConfigError(f"{self.source}: [options] is missing required key '{key}'")
        return _as_float(self.options[key], key, self.source)


@dataclass(frozen=True)
class AgentConfig:
    asset: AssetIdentity
    broker: BrokerConfig
    sample_interval_seconds: float
    publish_interval_seconds: float
    publish_batch_size: int
    database_path: Path
    log_level: str
    sensors: tuple[SensorConfig, ...]

    @property
    def enabled_sensors(self) -> tuple[SensorConfig, ...]:
        return tuple(sensor for sensor in self.sensors if sensor.enabled)


def load(config_dir: Path) -> AgentConfig:
    """Loads ``agent.toml`` plus every ``sensors/*.toml`` under ``config_dir``.

    Relative paths inside the configuration resolve against ``config_dir``'s parent, which
    is the agent's own directory.
    """
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        raise ConfigError(f"configuration directory not found: {config_dir}")

    agent_file = config_dir / "agent.toml"
    document = _read_toml(agent_file)
    root = config_dir.parent

    asset_table = _table(document, "asset", agent_file)
    asset = AssetIdentity(
        site_id=_require_str(asset_table, "site_id", agent_file, "asset"),
        asset_id=_require_str(asset_table, "asset_id", agent_file, "asset"),
        machine_type=_require_machine_type(asset_table, agent_file),
        firmware_version=_require_str(asset_table, "firmware_version", agent_file, "asset"),
    )

    broker_table = _table(document, "broker", agent_file)
    # An empty client_id means "use the asset id", which is what IoT Core policies are
    # written against. Defaulting it here keeps that coupling in one place.
    client_id = str(broker_table.get("client_id") or "").strip() or asset.asset_id
    broker = BrokerConfig(
        host=_require_str(broker_table, "host", agent_file, "broker"),
        port=_require_positive_int(broker_table, "port", agent_file, "broker"),
        tls=_require_bool(broker_table, "tls", agent_file, "broker"),
        keepalive_seconds=_require_positive_int(
            broker_table, "keepalive_seconds", agent_file, "broker"
        ),
        client_id=client_id,
    )

    sampling = _table(document, "sampling", agent_file)
    publishing = _table(document, "publishing", agent_file)
    storage = _table(document, "storage", agent_file)
    logging_table = _table(document, "logging", agent_file)

    log_level = _require_str(logging_table, "level", agent_file, "logging").upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(
            f"{agent_file}: [logging] level '{log_level}' is not one of "
            f"{', '.join(sorted(LOG_LEVELS))}"
        )

    database_path = Path(_require_str(storage, "database_path", agent_file, "storage"))
    if not database_path.is_absolute():
        database_path = root / database_path

    return AgentConfig(
        asset=asset,
        broker=broker,
        sample_interval_seconds=_require_positive_float(
            sampling, "interval_seconds", agent_file, "sampling"
        ),
        publish_interval_seconds=_require_positive_float(
            publishing, "interval_seconds", agent_file, "publishing"
        ),
        publish_batch_size=_require_positive_int(
            publishing, "batch_size", agent_file, "publishing"
        ),
        database_path=database_path,
        log_level=log_level,
        sensors=_load_sensors(config_dir / "sensors"),
    )


def _load_sensors(sensors_dir: Path) -> tuple[SensorConfig, ...]:
    if not sensors_dir.is_dir():
        raise ConfigError(f"sensor configuration directory not found: {sensors_dir}")

    # Sorted so sensor order is deterministic across machines and runs, which keeps
    # telemetry batches and test expectations stable.
    sensors: list[SensorConfig] = []
    seen: dict[str, Path] = {}
    for path in sorted(sensors_dir.glob("*.toml")):
        document = _read_toml(path)
        sensor_id = _require_str(document, "sensor_id", path)
        if sensor_id in seen:
            raise ConfigError(
                f"{path}: sensor_id '{sensor_id}' is already declared in {seen[sensor_id]}"
            )
        seen[sensor_id] = path

        options = document.get("options", {})
        if not isinstance(options, dict):
            raise ConfigError(f"{path}: [options] must be a table")

        sensors.append(
            SensorConfig(
                sensor_id=sensor_id,
                type=_require_str(document, "type", path),
                enabled=_require_bool(document, "enabled", path),
                options=dict(options),
                source=path,
            )
        )

    if not sensors:
        raise ConfigError(f"no sensor configuration files found in {sensors_dir}")
    if not any(sensor.enabled for sensor in sensors):
        raise ConfigError(
            f"every sensor in {sensors_dir} is disabled; the agent would report nothing"
        )
    return tuple(sensors)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML — {error}") from error


def _table(document: Mapping[str, Any], name: str, path: Path) -> Mapping[str, Any]:
    value = document.get(name)
    if value is None:
        raise ConfigError(f"{path}: missing required [{name}] section")
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [{name}] must be a table")
    return value


def _where(path: Path, section: str | None, key: str) -> str:
    location = f"[{section}] {key}" if section else key
    return f"{path}: {location}"


def _require_str(
    table: Mapping[str, Any], key: str, path: Path, section: str | None = None
) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{_where(path, section, key)} must be a non-empty string")
    return value.strip()


def _require_bool(
    table: Mapping[str, Any], key: str, path: Path, section: str | None = None
) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{_where(path, section, key)} must be true or false")
    return value


def _require_positive_int(
    table: Mapping[str, Any], key: str, path: Path, section: str | None = None
) -> int:
    value = table.get(key)
    # bool is an int subclass in Python; accepting it here would let `port = true` through.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{_where(path, section, key)} must be a positive integer")
    return value


def _require_positive_float(
    table: Mapping[str, Any], key: str, path: Path, section: str | None = None
) -> float:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{_where(path, section, key)} must be a positive number")
    return float(value)


def _require_machine_type(table: Mapping[str, Any], path: Path) -> str:
    value = _require_str(table, "machine_type", path, "asset").upper()
    if value not in MACHINE_TYPES:
        raise ConfigError(
            f"{path}: [asset] machine_type '{value}' is not one of "
            f"{', '.join(sorted(MACHINE_TYPES))}"
        )
    return value


def _as_float(value: Any, key: str, path: Path) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: [options] {key} must be a number")
    return float(value)
