from pathlib import Path

import pytest

from factoryfleet_agent import config

REPO_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

AGENT_TOML = """
[asset]
site_id = "PLANT-A"
asset_id = "PRESS-01"
machine_type = "HYDRAULIC_PRESS"
firmware_version = "2.4.1"

[broker]
host = "localhost"
port = 1883
tls = false
keepalive_seconds = 30
client_id = ""

[sampling]
interval_seconds = 10.0

[publishing]
interval_seconds = 15.0
batch_size = 50

[storage]
database_path = "data/agent.sqlite"

[logging]
level = "INFO"
"""

SENSOR_TOML = """
sensor_id = "vibration"
type = "vibration"
enabled = true

[options]
baseline_rms_mm_s = 4.2
"""


def write_config(root: Path, agent_toml: str = AGENT_TOML, **sensors: str) -> Path:
    """Lays out a config directory under ``root`` and returns it."""
    config_dir = root / "config"
    (config_dir / "sensors").mkdir(parents=True)
    (config_dir / "agent.toml").write_text(agent_toml)
    for name, body in (sensors or {"vibration": SENSOR_TOML}).items():
        (config_dir / "sensors" / f"{name}.toml").write_text(body)
    return config_dir


def test_loads_the_repository_configuration(tmp_path):
    """The checked-in configuration must be valid, or a fresh clone cannot start."""
    loaded = config.load(REPO_CONFIG_DIR)

    assert loaded.asset.asset_id == "PRESS-01"
    assert loaded.asset.machine_type == "HYDRAULIC_PRESS"
    assert {sensor.sensor_id for sensor in loaded.enabled_sensors} == {
        "vibration",
        "temperature",
        "cycle_count",
    }


def test_topic_prefix_addresses_the_machine(tmp_path):
    loaded = config.load(write_config(tmp_path))

    assert loaded.asset.topic_prefix == "factoryfleet/PLANT-A/PRESS-01"


def test_empty_client_id_defaults_to_asset_id(tmp_path):
    """IoT Core policies are written against the client id, so it must not be blank."""
    loaded = config.load(write_config(tmp_path))

    assert loaded.broker.client_id == "PRESS-01"


def test_explicit_client_id_is_kept(tmp_path):
    agent_toml = AGENT_TOML.replace('client_id = ""', 'client_id = "press-01-agent"')

    loaded = config.load(write_config(tmp_path, agent_toml))

    assert loaded.broker.client_id == "press-01-agent"


def test_relative_database_path_resolves_against_the_agent_directory(tmp_path):
    config_dir = write_config(tmp_path)

    loaded = config.load(config_dir)

    assert loaded.database_path == tmp_path / "data" / "agent.sqlite"


def test_absolute_database_path_is_left_alone(tmp_path):
    agent_toml = AGENT_TOML.replace(
        'database_path = "data/agent.sqlite"', 'database_path = "/var/lib/ff/agent.sqlite"'
    )

    loaded = config.load(write_config(tmp_path, agent_toml))

    assert loaded.database_path == Path("/var/lib/ff/agent.sqlite")


def test_sensors_load_in_deterministic_order(tmp_path):
    """Stable ordering keeps telemetry batches comparable between runs."""
    config_dir = write_config(
        tmp_path,
        AGENT_TOML,
        vibration=SENSOR_TOML,
        temperature=SENSOR_TOML.replace("vibration", "temperature"),
        cycle_count=SENSOR_TOML.replace("vibration", "cycle_count"),
    )

    loaded = config.load(config_dir)

    assert [sensor.sensor_id for sensor in loaded.sensors] == [
        "cycle_count",
        "temperature",
        "vibration",
    ]


def test_disabled_sensor_is_loaded_but_excluded_from_enabled(tmp_path):
    config_dir = write_config(
        tmp_path,
        AGENT_TOML,
        vibration=SENSOR_TOML,
        temperature=SENSOR_TOML.replace("vibration", "temperature").replace(
            "enabled = true", "enabled = false"
        ),
    )

    loaded = config.load(config_dir)

    assert len(loaded.sensors) == 2
    assert [sensor.sensor_id for sensor in loaded.enabled_sensors] == ["vibration"]


def test_sensor_options_are_exposed(tmp_path):
    loaded = config.load(write_config(tmp_path))
    sensor = loaded.sensors[0]

    assert sensor.required_float("baseline_rms_mm_s") == 4.2
    assert sensor.option("missing", 1.5) == 1.5


def test_missing_required_option_names_the_file(tmp_path):
    loaded = config.load(write_config(tmp_path))
    sensor = loaded.sensors[0]

    with pytest.raises(config.ConfigError, match="hard_limit_rms_mm_s"):
        sensor.required_float("hard_limit_rms_mm_s")


def test_unknown_machine_type_is_rejected(tmp_path):
    agent_toml = AGENT_TOML.replace("HYDRAULIC_PRESS", "TELEPORTER")

    with pytest.raises(config.ConfigError, match="TELEPORTER"):
        config.load(write_config(tmp_path, agent_toml))


def test_machine_type_is_normalised_to_upper_case(tmp_path):
    agent_toml = AGENT_TOML.replace("HYDRAULIC_PRESS", "hydraulic_press")

    loaded = config.load(write_config(tmp_path, agent_toml))

    assert loaded.asset.machine_type == "HYDRAULIC_PRESS"


def test_blank_asset_id_is_rejected(tmp_path):
    agent_toml = AGENT_TOML.replace('asset_id = "PRESS-01"', 'asset_id = "   "')

    with pytest.raises(config.ConfigError, match="asset_id"):
        config.load(write_config(tmp_path, agent_toml))


def test_missing_section_is_reported(tmp_path):
    agent_toml = AGENT_TOML.replace("[sampling]\ninterval_seconds = 10.0", "")

    with pytest.raises(config.ConfigError, match=r"\[sampling\]"):
        config.load(write_config(tmp_path, agent_toml))


def test_non_positive_interval_is_rejected(tmp_path):
    """A zero interval would spin the scheduler at full speed."""
    agent_toml = AGENT_TOML.replace("interval_seconds = 10.0", "interval_seconds = 0")

    with pytest.raises(config.ConfigError, match="interval_seconds"):
        config.load(write_config(tmp_path, agent_toml))


def test_boolean_is_not_accepted_where_a_port_is_expected(tmp_path):
    """bool subclasses int in Python; the loader must not let that slip through."""
    agent_toml = AGENT_TOML.replace("port = 1883", "port = true")

    with pytest.raises(config.ConfigError, match="port"):
        config.load(write_config(tmp_path, agent_toml))


def test_unknown_log_level_is_rejected(tmp_path):
    agent_toml = AGENT_TOML.replace('level = "INFO"', 'level = "CHATTY"')

    with pytest.raises(config.ConfigError, match="CHATTY"):
        config.load(write_config(tmp_path, agent_toml))


def test_duplicate_sensor_id_across_files_is_rejected(tmp_path):
    """Two files claiming one sensor id would silently shadow each other."""
    config_dir = write_config(
        tmp_path,
        AGENT_TOML,
        vibration=SENSOR_TOML,
        vibration_spare=SENSOR_TOML,
    )

    with pytest.raises(config.ConfigError, match="already declared"):
        config.load(config_dir)


def test_all_sensors_disabled_is_rejected(tmp_path):
    config_dir = write_config(
        tmp_path,
        AGENT_TOML,
        vibration=SENSOR_TOML.replace("enabled = true", "enabled = false"),
    )

    with pytest.raises(config.ConfigError, match="disabled"):
        config.load(config_dir)


def test_missing_config_directory_is_reported(tmp_path):
    with pytest.raises(config.ConfigError, match="configuration directory not found"):
        config.load(tmp_path / "nope")


def test_missing_agent_file_is_reported(tmp_path):
    config_dir = tmp_path / "config"
    (config_dir / "sensors").mkdir(parents=True)

    with pytest.raises(config.ConfigError, match="agent.toml"):
        config.load(config_dir)


def test_malformed_toml_is_reported_with_the_path(tmp_path):
    with pytest.raises(config.ConfigError, match="invalid TOML"):
        config.load(write_config(tmp_path, "[asset\nsite_id = "))
